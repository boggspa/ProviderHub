"""Messages/Chat Completions response translation and legacy Mistral request helpers.

The desktop remains the tool executor. This module never executes tool calls.
Unsupported content/tools fail explicitly instead of silently disappearing.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import secrets

from bridge_core import BridgeError, SLOTS
from model_names import friendly_model_name
from catalogue import status_label


def tool_id(value: str) -> str:
    # Mistral tool call IDs require nine alphanumeric characters. The same ID
    # is reconstructed from the complete conversation on every request.
    return hashlib.sha256(value.encode()).hexdigest()[:9]


def function_name(name: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
        return name
    return re.sub(r"[^A-Za-z0-9_-]", "_", name)[:50] + "_" + hashlib.sha256(name.encode()).hexdigest()[:10]


def blocks(value):
    if isinstance(value, str):
        return [{"type": "text", "text": value}]
    if isinstance(value, list) and all(isinstance(b, dict) for b in value):
        return value
    raise BridgeError("Message content must be text or an array of content blocks.")


def content_part(block):
    kind = block.get("type")
    if kind == "text":
        return {"type": "text", "text": block.get("text", "")}
    if kind == "image":
        source = block.get("source", {})
        if source.get("type") == "base64":
            media = source.get("media_type", "")
            if media not in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
                raise BridgeError("Unsupported image format.")
            return {"type": "image_url", "image_url": "data:" + media + ";base64," + source.get("data", "")}
        if source.get("type") == "url" and str(source.get("url", "")).startswith("https://"):
            return {"type": "image_url", "image_url": source["url"]}
        raise BridgeError("Images must be base64 data or HTTPS URLs.")
    if kind == "document":
        source = block.get("source", {})
        if source.get("type") == "text":
            return {"type": "text", "text": source.get("data", "")}
        if source.get("type") == "content":
            return {"type": "text", "text": "\n".join(b["text"] for b in blocks(source.get("content", [])) if b.get("type") == "text")}
        raise BridgeError("PDF/document uploads are not supported by this prototype. Use extracted text or images.")
    # We do not generate Anthropic thinking/signatures. Replayed externally
    # signed reasoning must not be sent to a different provider.
    if kind in {"thinking", "redacted_thinking"}:
        return None
    raise BridgeError(f"Unsupported content block: {kind or 'missing type'}.")


def compact_content(parts):
    if all(p.get("type") == "text" for p in parts):
        return "\n".join(p.get("text", "") for p in parts)
    return list(parts)


def resolve_model(requested: str, mappings: dict) -> str:
    if requested.endswith("[1m]"):
        requested = requested[:-4]
    if requested in mappings:
        return mappings[requested]
    aliases = {"fable": "claude-fable-5", "opus": "claude-opus-5", "sonnet": "claude-sonnet-5", "haiku": "claude-haiku-4-5"}
    if requested in aliases:
        return mappings[aliases[requested]]
    # Only explicitly configured upstream IDs are callable through this gateway.
    if requested in mappings.values():
        return requested
    raise BridgeError(f"Model {requested!r} is not mapped. Choose it in Provider Hub first.")


def model_catalog(settings: dict):
    rows, seen = [], set()
    for slot, _, family, default in SLOTS:
        identifier = settings["mappings"][slot]
        spec = settings.get("_model_specs", {}).get(identifier)
        if not spec:
            continue
        key = (spec.get("provider_id", "mistral"), spec["id"])
        if key in seen:
            continue
        seen.add(key)
        context = spec.get("context")
        context_text = f"{context:,} token context" if type(context) is int else "Provider-managed context"
        provider = spec.get("presentation", {}).get("displayProvider")
        title = spec["display_name"]
        if provider and provider.casefold() not in title.casefold():
            title += " · " + provider
        row = {"id": slot, "type": "model", "display_name": title,
               "description": f"{spec.get('provider_id', 'mistral')} account · {context_text} · {status_label(spec)}",
               "created_at": "2026-09-12T00:00:00Z",
               "anthropic_family_tier": family, "is_family_default": default}
        if type(context) is int:
            row.update(max_tokens=context, max_input_tokens=context, supports_1m=context >= 1000000)
        rows.append(row)
    return {"data": rows, "first_id": rows[0]["id"] if rows else None,
            "last_id": rows[-1]["id"] if rows else None, "has_more": False}


def model_effort(payload: dict, spec: dict):
    if not spec.get("reasoning"):
        return None
    if (payload.get("thinking") or {}).get("type") == "disabled":
        return "none"
    requested = (payload.get("output_config") or {}).get("effort")
    # Match Vibe's native Mistral backend: it currently uses none/high,
    # with Low mapped to none and Medium/High/Max mapped to high.
    if requested in {"none", "minimal", "low"}:
        return "none"
    if requested in {None, "medium", "high", "xhigh", "max"}:
        return "high"
    raise BridgeError("Unsupported effort value. Use Claude's standard effort control.")


def estimated_tokens(payload: dict) -> int:
    # Local estimate, explicitly labelled in the HTTP response and app docs.
    # Includes system instructions and schemas; not used for usage billing.
    images = 0
    def text_only(value):
        nonlocal images
        if isinstance(value, dict):
            if value.get("type") == "image":
                images += 1
                return {"type": "image"}
            return {key: text_only(item) for key, item in value.items()}
        if isinstance(value, list):
            return [text_only(item) for item in value]
        return value
    subset = text_only({k: payload[k] for k in ("system", "messages", "tools") if k in payload})
    return max(1, math.ceil(len(json.dumps(subset, ensure_ascii=False).encode()) / 3) + images * 4096)


def translate_request(payload: dict, settings: dict):
    requested = payload.get("model", "")
    upstream = resolve_model(requested, settings["mappings"])
    spec = settings.get("_model_specs", {}).get(upstream)
    if not spec or type(spec.get("context")) is not int:
        raise BridgeError("Model limits are not in the current catalogue. Refresh models in Provider Hub first.")
    if requested.endswith("[1m]") and spec["context"] < 1000000:
        raise BridgeError("The selected model does not have a 1M context window.")
    if payload.get("speed") == "fast" or payload.get("service_tier") in {"fast", "priority"}:
        raise BridgeError("Claude Fast mode is not available for this Mistral connection. Use standard speed; the Vibe fast alias is a different model.")
    if not isinstance(payload.get("messages"), list) or not payload["messages"]:
        raise BridgeError("At least one message is required.")
    max_tokens = payload.get("max_tokens", 4096)
    if type(max_tokens) is not int or max_tokens <= 0:
        raise BridgeError("max_tokens must be a positive integer.")
    estimate = estimated_tokens(payload)
    if estimate >= spec["context"]:
        raise BridgeError(f"This conversation is above the model's reported {spec['context']:,}-token context limit. Compact it or start a new session.")
    messages = []
    if payload.get("system"):
        system = [content_part(p) for p in blocks(payload["system"])]
        if any(p is None or p.get("type") != "text" for p in system):
            raise BridgeError("System instructions must contain text.")
        messages.append({"role": "system", "content": compact_content(system)})
    names = {}
    identifiers = {}
    for message in payload["messages"]:
        role = message.get("role")
        if role in {"system", "developer"}:
            parts = [content_part(p) for p in blocks(message.get("content", ""))]
            if any(p is None or p.get("type") != "text" for p in parts):
                raise BridgeError("System/developer messages must contain text.")
            messages.append({"role": "system", "content": compact_content(parts)})
            continue
        if role not in {"user", "assistant"}:
            raise BridgeError(f"Unsupported message role: {str(role)[:30]}.")
        pending = []
        calls = []

        def flush():
            if pending or calls:
                item = {"role": role, "content": compact_content(pending)}
                if calls:
                    item["tool_calls"] = list(calls)
                messages.append(item)
                pending.clear()
                calls.clear()

        for block in blocks(message.get("content", "")):
            kind = block.get("type")
            if kind == "tool_use":
                if role != "assistant":
                    raise BridgeError("Tool calls must be assistant messages.")
                original_id = block.get("id", "")
                name = block.get("name", "")
                if not original_id or not name:
                    raise BridgeError("Tool call IDs and names are required.")
                mapped = tool_id(original_id)
                if mapped in identifiers and identifiers[mapped] != original_id:
                    raise BridgeError("Tool identifier collision; start a new session.")
                identifiers[mapped] = original_id
                names[function_name(name)] = name
                calls.append({"id": mapped, "type": "function", "function": {
                    "name": function_name(name), "arguments": json.dumps(block.get("input", {}), ensure_ascii=False)}})
            elif kind == "tool_result":
                if role != "user":
                    raise BridgeError("Tool results must be user messages.")
                flush()
                original_id = block.get("tool_use_id", "")
                if not original_id:
                    raise BridgeError("Tool results must reference a tool call.")
                parts = [content_part(p) for p in blocks(block.get("content", ""))]
                text_parts = [p for p in parts if p and p["type"] == "text"]
                images = [p for p in parts if p and p["type"] == "image_url"]
                text = compact_content(text_parts)
                if block.get("is_error"):
                    text = "Tool execution error:\n" + text
                if images:
                    text += "\nThe tool's images follow in the next user message."
                messages.append({"role": "tool", "tool_call_id": tool_id(original_id), "content": text})
                if images:
                    pending.extend(images)
            else:
                part = content_part(block)
                if part:
                    pending.append(part)
        flush()
    tools = []
    for tool in payload.get("tools", []):
        if "input_schema" not in tool:
            raise BridgeError(f"Hosted tool {tool.get('name', tool.get('type', 'unknown'))!r} is not supported. Disable that tool in this Claude profile.")
        name = tool.get("name", "")
        if not isinstance(name, str) or not name:
            raise BridgeError("Tools require a name.")
        mapped = function_name(name)
        if mapped in names and names[mapped] != name:
            raise BridgeError("Tool name collision.")
        names[mapped] = name
        tools.append({"type": "function", "function": {"name": mapped, "description": tool.get("description", ""),
                      "parameters": tool["input_schema"]}})
    result = {"model": upstream, "messages": messages, "max_tokens": min(max_tokens, spec["context"] - estimate),
              "stream": bool(payload.get("stream", False))}
    if tools:
        result["tools"] = tools
    choice = payload.get("tool_choice", {})
    if choice:
        if choice.get("type") == "tool":
            result["tool_choice"] = {"type": "function", "function": {"name": function_name(choice.get("name", ""))}}
        elif choice.get("type") in {"any", "auto", "none"}:
            result["tool_choice"] = {"any": "required", "auto": "auto", "none": "none"}[choice["type"]]
        else:
            raise BridgeError("Unsupported tool_choice.")
        if "disable_parallel_tool_use" in choice:
            result["parallel_tool_calls"] = not choice["disable_parallel_tool_use"]
    for key in ("temperature", "top_p"):
        if key in payload:
            result[key] = payload[key]
    if payload.get("stop_sequences"):
        result["stop"] = payload["stop_sequences"]
    effort = model_effort(payload, spec)
    if effort is not None:
        result["reasoning_effort"] = effort
    # Stable cache hint; Mistral decides whether cached prefixes can be reused.
    result["prompt_cache_key"] = hashlib.sha256(json.dumps({"system": payload.get("system"), "tools": payload.get("tools")}, sort_keys=True).encode()).hexdigest()
    return result, names


def visible_text(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if p.get("type") == "text")
    raise BridgeError("The provider returned an unsupported content format.")


def usage_counts(usage):
    return {"input_tokens": max(0, usage.get("prompt_tokens", 0)),
            "output_tokens": max(0, usage.get("completion_tokens", 0))}


def stop_reason(reason, has_tools=False):
    if reason == "length":
        return "max_tokens"
    return "tool_use" if reason == "tool_calls" or has_tools else "end_turn"


# Mistral sometimes answers tool turns by typing the invocation as plain text
# (for example Bash{"command": ...}) instead of using the structured
# tool_calls channel. Recovery converts those predicted calls, but only when
# the shape is unambiguous, so prose about tool calls is never converted.
_IDENTIFIER_RUN = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
# Give up holding an unbalanced candidate tail beyond this size so runaway
# near-JSON prose cannot buffer the whole stream. Genuine arguments from Edit
# and similar tools stay well under this.
_MAX_TOOL_CALL_HOLD = 65536


def _balanced_object_end(text: str, start: int):
    """Index just past the JSON object opening at `start`, string-aware."""
    depth, in_string, escaped = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i + 1
    return None


def segment_predicted_calls(text: str, names: dict) -> list:
    """Split assistant text into ("text", str) and ("tool", name, arguments) segments.

    A tool segment is `Name{...}` (whitespace allowed before the brace) where
    Name is a tool advertised for this request and {...} parses to a JSON
    object. Anything else stays text.
    """
    segments, pos, cursor = [], 0, 0
    while cursor < len(text):
        match = _IDENTIFIER_RUN.search(text, cursor)
        if not match:
            break
        name = match.group(0)
        brace = match.end()
        while brace < len(text) and text[brace] in " \t":
            brace += 1
        end = None
        if name in names and brace < len(text) and text[brace] == "{":
            end = _balanced_object_end(text, brace)
        if end is not None:
            try:
                value = json.loads(text[brace:end])
            except ValueError:
                value = None
            if isinstance(value, dict):
                if match.start() > pos:
                    segments.append(("text", text[pos:match.start()]))
                segments.append(("tool", name, text[brace:end]))
                pos = cursor = end
                continue
        cursor = match.end()
    if pos < len(text):
        segments.append(("text", text[pos:]))
    return segments


def _synthetic_tool_id(occurrence: int, name: str, arguments: str) -> str:
    # Deterministic so tests reproduce, and unique per occurrence so identical
    # repeated calls stay distinguishable. The desktop echoes this ID in its
    # tool_result and the next request re-hashes it for the provider, so the
    # recovered call becomes a structured call in provider history.
    digest = hashlib.sha256(f"{occurrence}:{name}:{arguments}".encode()).hexdigest()
    return "toolu_" + digest[:22]


def translate_response(response, requested, names):
    try:
        choice = response["choices"][0]
        msg = choice["message"]
        content = []
        converted = 0
        text = visible_text(msg.get("content"))
        if text:
            for segment in segment_predicted_calls(text, names):
                if segment[0] == "text":
                    content.append({"type": "text", "text": segment[1]})
                else:
                    _, name, arguments = segment
                    content.append({"type": "tool_use", "id": _synthetic_tool_id(converted, name, arguments),
                                    "name": names.get(name, name), "input": json.loads(arguments)})
                    converted += 1
        for tool in msg.get("tool_calls") or []:
            fn = tool["function"]
            args = fn.get("arguments", "{}")
            content.append({"type": "tool_use", "id": tool.get("id") or "toolu_" + secrets.token_hex(10),
                            "name": names.get(fn["name"], fn["name"]),
                            "input": json.loads(args) if isinstance(args, str) else args})
        return {"id": "msg_" + secrets.token_hex(12), "type": "message", "role": "assistant", "model": requested,
                "content": content,
                "stop_reason": stop_reason(choice.get("finish_reason"), bool(msg.get("tool_calls")) or converted > 0),
                "stop_sequence": None, "usage": usage_counts(response.get("usage", {}))}
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise BridgeError("The provider returned a malformed response or invalid tool arguments.") from exc


class StreamTranslator:
    def __init__(self, requested, names):
        self.requested = requested
        self.names = names
        self.identifier = "msg_" + secrets.token_hex(12)
        self.next_index = 0
        self.text_index = None
        self.text_open = False
        self.open_indices = set()
        self.tools = {}
        # Content text is buffered so predicted tool calls typed as plain text
        # can be converted before their deltas reach the desktop. Only the
        # undecided tail that could still become a candidate is held back.
        self.buffer = ""
        self.converted = []
        self.usage = {"input_tokens": 0, "output_tokens": 0}
        self.finish = None

    def start(self):
        return [{"type": "message_start", "message": {"id": self.identifier, "type": "message", "role": "assistant",
                "model": self.requested, "content": [], "stop_reason": None, "stop_sequence": None, "usage": self.usage.copy()}}]

    def feed(self, chunk):
        events = []
        if chunk.get("usage"):
            self.usage = usage_counts(chunk["usage"])
        for choice in chunk.get("choices", []):
            if choice.get("index", 0) != 0:
                continue
            if choice.get("finish_reason"):
                self.finish = choice["finish_reason"]
            delta = choice.get("delta") or {}
            text = visible_text(delta.get("content"))
            if text:
                self.buffer += text
                self._flush_buffer(events)
            for tool in delta.get("tool_calls") or []:
                key = tool.get("index", 0)
                state = self.tools.setdefault(key, {"id": None, "name": None, "arguments": "", "buffer": "", "index": None})
                fn = tool.get("function", {})
                if tool.get("id"):
                    state["id"] = tool["id"]
                if fn.get("name"):
                    state["name"] = fn["name"]
                arguments = fn.get("arguments", "")
                if not isinstance(arguments, str):
                    arguments = json.dumps(arguments)
                state["arguments"] += arguments
                state["buffer"] += arguments
                if state["index"] is None and state["id"] and state["name"]:
                    state["index"] = self.next_index
                    self.next_index += 1
                    self.open_indices.add(state["index"])
                    events.append({"type": "content_block_start", "index": state["index"], "content_block": {
                        "type": "tool_use", "id": state["id"], "name": self.names.get(state["name"], state["name"]), "input": {}}})
                if state["index"] is not None and state["buffer"]:
                    events.append({"type": "content_block_delta", "index": state["index"], "delta": {
                        "type": "input_json_delta", "partial_json": state["buffer"]}})
                    state["buffer"] = ""
        return events

    def _emit_text(self, events, text):
        if not self.text_open:
            self.text_index = self.next_index
            self.next_index += 1
            self.open_indices.add(self.text_index)
            events.append({"type": "content_block_start", "index": self.text_index,
                           "content_block": {"type": "text", "text": ""}})
            self.text_open = True
        events.append({"type": "content_block_delta", "index": self.text_index,
                       "delta": {"type": "text_delta", "text": text}})

    def _close_text(self, events):
        if self.text_open:
            events.append({"type": "content_block_stop", "index": self.text_index})
            self.open_indices.discard(self.text_index)
            self.text_open = False

    def _decided_prefix(self):
        """Length of the buffer prefix whose interpretation cannot change.

        Complete predicted calls and text before them are decided. A suffix
        that could still become a candidate — a known tool name (or a prefix
        of one) at the buffer end, or an unbalanced '{' — is held back for
        more deltas.
        """
        if not self.names:
            return len(self.buffer)
        limit = len(self.buffer)
        cursor = 0
        while cursor < limit:
            match = _IDENTIFIER_RUN.search(self.buffer, cursor)
            if not match:
                break
            name = match.group(0)
            brace = match.end()
            while brace < limit and self.buffer[brace] in " \t":
                brace += 1
            if name in self.names and brace < limit and self.buffer[brace] == "{":
                end = _balanced_object_end(self.buffer, brace)
                if end is None:
                    if limit - match.start() > _MAX_TOOL_CALL_HOLD:
                        return limit  # stop holding a runaway near-JSON tail
                    return match.start()
                cursor = end  # balanced: appends cannot change this slice
            elif brace >= limit and (name in self.names or any(n.startswith(name) for n in self.names)):
                return match.start()  # appending may complete the name and '{'
            else:
                cursor = match.end()  # followed by ordinary text: never a call
        return limit

    def _flush_buffer(self, events, *, final=False):
        decided = len(self.buffer) if final else self._decided_prefix()
        if not decided:
            return
        head, self.buffer = self.buffer[:decided], self.buffer[decided:]
        for segment in segment_predicted_calls(head, self.names):
            if segment[0] == "text":
                self._emit_text(events, segment[1])
                continue
            _, name, arguments = segment
            self._close_text(events)
            index = self.next_index
            self.next_index += 1
            events.append({"type": "content_block_start", "index": index, "content_block": {
                "type": "tool_use", "id": _synthetic_tool_id(len(self.converted), name, arguments),
                "name": self.names.get(name, name), "input": {}}})
            events.append({"type": "content_block_delta", "index": index, "delta": {
                "type": "input_json_delta", "partial_json": arguments}})
            events.append({"type": "content_block_stop", "index": index})
            self.converted.append((name, arguments))

    def end(self):
        if self.finish is None:
            raise BridgeError("The provider closed the stream before a completion signal.")
        events = []
        self._flush_buffer(events, final=True)
        for tool in self.tools.values():
            if tool["index"] is None:
                raise BridgeError("The provider returned an incomplete tool call.")
            try:
                value = json.loads(tool["arguments"] or "{}")
                if not isinstance(value, dict):
                    raise ValueError()
            except ValueError as exc:
                raise BridgeError("The provider returned invalid tool arguments.") from exc
        result = [{"type": "content_block_stop", "index": i} for i in sorted(self.open_indices)]
        result.append({"type": "message_delta", "delta": {"stop_reason": stop_reason(self.finish, bool(self.tools) or bool(self.converted)), "stop_sequence": None}, "usage": self.usage})
        result.append({"type": "message_stop"})
        return events + result
