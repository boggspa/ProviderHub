"""Portable context for an idle model switch, never provider reasoning replay."""
from __future__ import annotations

import base64
from pathlib import Path

from cli_images import normalize_image, MAX_IMAGE_BYTES


def portable_history(entries, *, vision=True):
    """Project only visible dialogue, file contents and recorded tool results.

No thinking/signature/encrypted block, live call id, or pending native RPC is
read from the old Messages history. That history stays intact in an archive.
"""
    messages = []
    def add(role, content):
        if not content:
            return
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"].extend(content)
        else:
            messages.append({"role": role, "content": content})
    add("user", [{"type": "text", "text": "The user switched models in this chat. The following is portable prior conversation. Earlier tool results are records only; do not execute those actions again. Prior provider reasoning was intentionally excluded."}])
    for item in entries:
        kind = item.get("kind")
        content = []
        text = item.get("text", "")
        if kind == "user":
            if text:
                content.append({"type": "text", "text": text})
            for attachment in item.get("attachments") or []:
                if attachment.get("kind") == "image":
                    name = attachment["name"]
                    if vision is False:
                        content.append({"type": "text", "text": f"[Earlier image: {name}. Pixels excluded for this text-only model; the original is kept in the transcript.]"})
                    else:
                        path = Path(attachment["path"])
                        with path.open("rb") as stream:
                            raw = stream.read(MAX_IMAGE_BYTES + 1)
                        media = ("image/png" if raw.startswith(b"\x89PNG") else "image/jpeg" if raw.startswith(b"\xff\xd8")
                                 else "image/gif" if raw.startswith(b"GIF") else "image/webp")
                        image = normalize_image({"type": "image", "source": {"type": "base64", "media_type": media, "data": base64.b64encode(raw).decode()}})
                        content.extend([{"type": "text", "text": "Earlier image attachment: " + name}, image])
                elif attachment.get("contextText"):
                    content.append({"type": "text", "text": attachment["contextText"]})
                else:
                    content.append({"type": "text", "text": "[Earlier attachment: " + attachment["name"] + ". Original available in the saved transcript.]"})
            add("user", content)
        elif kind == "assistant" and text:
            add("assistant", [{"type": "text", "text": text}])
        elif kind == "tool":
            record = "Earlier recorded tool result — do not execute again:\n" + str(item.get("summary") or item.get("tool") or "Tool")
            record += "\n" + (item.get("detail") or text or "No result was recorded; inspect the workspace before retrying.")
            add("user", [{"type": "text", "text": record}])
        elif kind == "error" and text:
            add("user", [{"type": "text", "text": "Earlier harness error: " + text}])
    return messages
