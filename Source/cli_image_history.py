"""Keep recent screenshots within CLI budgets without dropping user/tool text."""
from __future__ import annotations

from cli_images import MAX_IMAGES, MAX_TOTAL_BYTES, normalize_image


def compact_image_history(messages):
    """Return copied Messages history and budget metadata, newest images first.

    The byte limit applies only to image data. Tool identifiers, tool results,
    user corrections, and text attachments stay in their original positions.
    Each omitted image becomes an explicit text placeholder, never a fabricated
    description of pixels the current model has not seen.
    """
    images = []

    def copy_content(content):
        if not isinstance(content, list):
            return content
        result = []
        for block in content:
            if not isinstance(block, dict):
                result.append(block)
                continue
            copied = dict(block)
            if block.get("type") in {"image", "input_image"}:
                image = normalize_image(block)
                data = image["source"]["data"]
                size = len(data) // 4 * 3 - (len(data) - len(data.rstrip("=")))
                images.append((result, len(result), size))
            elif block.get("type") == "tool_result":
                copied["content"] = copy_content(block.get("content"))
            result.append(copied)
        return result

    copied = [{**message, "content": copy_content(message.get("content"))}
              if isinstance(message, dict) else message for message in messages or []]
    count = total = removed = 0
    exhausted = False
    for container, index, size in reversed(images):
        if exhausted or count >= MAX_IMAGES or total + size > MAX_TOTAL_BYTES:
            exhausted = True
            container[index] = {"type": "text", "text":
                "[Provider Hub omitted this earlier image to keep screenshot history within the CLI "
                "budget. Its accompanying text and tool result remain. Retrieve the image again "
                "with a host tool if its pixels are needed.]"}
            removed += 1
        else:
            count += 1
            total += size
    return copied, {"removed": removed, "kept": count, "bytes_kept": total}
