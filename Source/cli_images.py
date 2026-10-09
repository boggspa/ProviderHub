"""Lossless screenshot inputs for CLI transports; no remote downloads or resizing."""
from __future__ import annotations

import base64
import binascii
from pathlib import Path

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGES = 20
MAX_TOTAL_BYTES = 32 * 1024 * 1024
_EXTENSIONS = {"image/png": ".png", "image/jpeg": ".jpg",
               "image/webp": ".webp", "image/gif": ".gif"}

IMAGE_COORDINATE_NOTE = (
    "Attached images are labelled with their original pixel dimensions. Follow "
    "the coordinate units required by each host tool. For a tool requesting "
    "pixel coordinates, use the original image width and height, not a 0-1000 "
    "coordinate scale. Convert a normalized estimate using x * width / 1000 "
    "and y * height / 1000 before calling a pixel-coordinate tool. Inspect the "
    "returned screenshot to verify each action and correct a missed target."
)


class CliImageError(ValueError):
    """The image cannot be delivered faithfully to the selected CLI."""


def image_bytes(image):
    source = image.get("source") or {}
    if source.get("type") != "base64":
        raise CliImageError("CLI screenshots require embedded image data; image URLs are not downloaded")
    media, data = source.get("media_type"), source.get("data")
    if media not in _EXTENSIONS or not isinstance(data, str):
        raise CliImageError("CLI images must be PNG, JPEG, WebP, or GIF base64 data")
    if len(data) > ((MAX_IMAGE_BYTES + 2) // 3) * 4:
        raise CliImageError("CLI image exceeds the 8 MiB limit")
    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise CliImageError("CLI image contains invalid base64 data") from exc
    signatures = {
        "image/png": raw.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/jpeg": raw.startswith(b"\xff\xd8\xff"),
        "image/gif": raw.startswith((b"GIF87a", b"GIF89a")),
        "image/webp": raw.startswith(b"RIFF") and raw[8:12] == b"WEBP",
    }
    if not signatures[media] or len(raw) > MAX_IMAGE_BYTES:
        raise CliImageError("CLI image data does not match its declared format or size limit")
    return raw


def normalize_image(block):
    if not isinstance(block, dict):
        raise CliImageError("CLI image blocks must be objects")
    if block.get("type") == "input_image":
        url = block.get("image_url")
        if not isinstance(url, str) or not url.startswith("data:") or ";base64," not in url:
            raise CliImageError("CLI screenshots require embedded image data")
        media, data = url[5:].split(";base64,", 1)
        source = {"type": "base64", "media_type": media, "data": data}
    else:
        source = block.get("source") or {}
    if not isinstance(source, dict):
        raise CliImageError("CLI image source must be an object")
    image = {"type": "image", "source": dict(source)}
    detail = block.get("detail")
    if detail in {"auto", "low", "high", "original"}:
        image["detail"] = detail
    image_bytes(image)
    return image


def normalize_images(images):
    if not isinstance(images, (list, tuple)):
        raise CliImageError("CLI images must be a list")
    if len(images) > MAX_IMAGES:
        raise CliImageError("CLI image history exceeds 20 images; compact the conversation")
    result = [normalize_image(image) for image in images]
    if sum(len(image_bytes(image)) for image in result) > MAX_TOTAL_BYTES:
        raise CliImageError("CLI image history exceeds 32 MiB; compact the conversation")
    return result


def image_dimensions(image):
    """Read common screenshot dimensions without decoding or resizing pixels."""
    data = image_bytes(image)
    media = image["source"]["media_type"]
    if media == "image/png" and len(data) >= 24:
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if media == "image/gif" and len(data) >= 10:
        return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
    if media == "image/jpeg":
        offset = 2
        while offset + 4 <= len(data) and data[offset] == 0xff:
            while offset < len(data) and data[offset] == 0xff:
                offset += 1
            if offset >= len(data):
                break
            marker = data[offset]
            offset += 1
            if marker in {0xda, 0xd9}:
                break
            length = int.from_bytes(data[offset:offset + 2], "big")
            if length < 2 or offset + length > len(data):
                break
            if marker in {0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf} and length >= 7:
                return (int.from_bytes(data[offset + 5:offset + 7], "big"),
                        int.from_bytes(data[offset + 3:offset + 5], "big"))
            offset += length
    return None


def image_label(image, index):
    size = image_dimensions(image)
    geometry = f"; width {size[0]}, height {size[1]} pixels" if size else ""
    return f"Image {index} attached; original screenshot pixels{geometry}"


def recover_tool_content(content):
    """Keep usable tool output and explain only the blocks we cannot relay.

    This is for results already produced by the host, not user attachments.
    Never fetch URLs, resize pixels, or claim to have seen omitted content.
    The caller retains tool identity and the host's own success/error flag.
    """
    if isinstance(content, str):
        return content
    result = []
    for block in content if isinstance(content, list) else [content]:
        kind = block.get("type") if isinstance(block, dict) else None
        kind = kind if isinstance(kind, str) else None
        try:
            if kind in {"text", "input_text", "output_text"} and isinstance(block.get("text"), str):
                result.append({"type": "text", "text": block["text"]})
            elif kind == "refusal" and isinstance(block.get("refusal"), str):
                result.append({"type": "text", "text": block["refusal"]})
            elif kind in {"image", "input_image"}:
                result.append(normalize_image(block))
            else:
                raise CliImageError("This content format cannot be passed to the CLI model")
        except (CliImageError, TypeError, AttributeError) as exc:
            label = "image" if kind in {"image", "input_image"} else "content"
            reason = str(exc) if isinstance(exc, CliImageError) else "Malformed content block"
            result.append({"type": "text", "text":
                f"[Provider Hub could not relay this tool-result {label}: {reason}. "
                "The original result remains in the host transcript. Use a host tool to retrieve "
                "a supported image or text if needed; its contents have not been inspected here.]"})
    return result


def responses_content(content):
    """Project canonical Messages content into native Responses tool output."""
    if isinstance(content, str):
        return [{"type": "input_text", "text": content}] if content else []
    result = []
    for block in content or []:
        if block.get("type") in {"text", "input_text", "output_text"}:
            if block.get("text"):
                result.append({"type": "input_text", "text": block["text"]})
        elif block.get("type") in {"image", "input_image"}:
            image = normalize_image(block)
            source = image["source"]
            result.append({"type": "input_image",
                           "image_url": f"data:{source['media_type']};base64,{source['data']}",
                           **({"detail": image["detail"]} if "detail" in image else {})})
        else:
            raise CliImageError("Unsupported content in CLI tool result")
    return result


def prompt_content(prompt, images, *, acp=False):
    """Attach images in numbered transcript order to a single CLI prompt."""
    content = [{"type": "text", "text": prompt}]
    for index, image in enumerate(normalize_images(images), 1):
        content.append({"type": "text", "text": image_label(image, index) + "."})
        if acp:
            source = image["source"]
            content.append({"type": "image", "mimeType": source["media_type"], "data": source["data"]})
        else:
            content.append({"type": "image", "source": image["source"]})
    return content


def write_images(images, directory):
    """Materialize private, turn-scoped image files without changing pixels."""
    paths = []
    for index, image in enumerate(normalize_images(images), 1):
        path = Path(directory) / f"image-{index}{_EXTENSIONS[image['source']['media_type']]}"
        with path.open("xb") as handle:
            path.chmod(0o600)
            handle.write(image_bytes(image))
        paths.append(str(path))
    return paths
