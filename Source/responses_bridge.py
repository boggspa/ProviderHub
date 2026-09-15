"""Stateless Responses-to-Messages translation for the existing provider adapters."""
from __future__ import annotations

import copy
import base64
import hashlib
import json
import time
import uuid

from bridge_core import BridgeError, private_token


ENVELOPE_PREFIX = "ph_reasoning_v1."


class ReasoningEnvelope:
    def __init__(self, root):
        try:
            from cryptography.fernet import Fernet
        except ImportError as exc:
            raise BridgeError("This Python runtime needs the cryptography dependency. Use the self-contained Provider Hub app or install Source/requirements.txt.") from exc
        key = private_token(root, "responses-encryption-key")
        material = hashlib.sha256(("provider-hub-responses-v1:" + key).encode()).digest()
        self.cipher = Fernet(base64.urlsafe_b64encode(material))

    def seal(self, blocks, scope):
        value = json.dumps({"scope": scope, "blocks": blocks}, separators=(",", ":")).encode()
        return ENVELOPE_PREFIX + self.cipher.encrypt(value).decode()

    def open(self, token, scope):
        if not isinstance(token, str) or not token.startswith(ENVELOPE_PREFIX):
            raise BridgeError("This reasoning history belongs to another connection. Start a new task when changing providers.")
        try:
            value = json.loads(self.cipher.decrypt(token[len(ENVELOPE_PREFIX):].encode()))
        except Exception as exc:
            raise BridgeError("The saved reasoning history could not be authenticated. Start a new task.") from exc
        if value.get("scope") != scope or not isinstance(value.get("blocks"), list):
            raise BridgeError("This reasoning history belongs to another model or account. Start a new task.")
        blocks = value["blocks"]
        if any(not isinstance(block, dict) or block.get("type") not in {"thinking", "redacted_thinking"} for block in blocks):
            raise BridgeError("The saved reasoning history is malformed.")
        return blocks


def content_blocks(content):
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if not isinstance(content, list):
        raise BridgeError("Message content must be text or content blocks.")
    result = []
    for part in content:
        if not isinstance(part, dict):
            raise BridgeError("Message content blocks must be objects.")
        kind = part.get("type")
        # Anthropic-compatible providers reject empty text blocks with
        # "text content is empty" (HTTP 400); drop them instead of failing
        # the whole replayed history.
        if kind in {"input_text", "output_text", "text"} and isinstance(part.get("text"), str):
            if part["text"]:
                result.append({"type": "text", "text": part["text"]})
        elif kind == "input_image":
            url = part.get("image_url")
            if isinstance(url, str) and url.startswith("data:") and ";base64," in url:
                media, data = url[5:].split(";base64,", 1)
                result.append({"type": "image", "source": {"type": "base64", "media_type": media, "data": data}})
            elif isinstance(url, str) and url.startswith("https://"):
                result.append({"type": "image", "source": {"type": "url", "url": url}})
            else:
                raise BridgeError("Images need an embedded data URL or an HTTPS URL.")
        elif kind == "refusal":
            refusal = str(part.get("refusal", ""))
            if refusal:
                result.append({"type": "text", "text": refusal})
        else:
            raise BridgeError("This provider needs text or image inputs. Extract documents to text before using them.")
    return result


def to_messages(body, route, spec, envelope, scope):
    if body.get("store") or body.get("previous_response_id"):
        raise BridgeError("This translated Responses connection requires full input history and store:false.")
    if body.get("truncation") not in (None, "disabled"):
        raise BridgeError("Use Codex compaction for this connection; server-side truncation is not supported.")
    text = body.get("text") or {}
    if not isinstance(text, dict) or (text.get("format") or {}).get("type", "text") != "text":
        raise BridgeError("Structured final-output schemas are not yet supported by this Responses translation.")
    inputs = body["input"]
    if isinstance(inputs, str):
        inputs = [{"role": "user", "content": inputs}]
    messages, pending, role = [], [], None

    def flush():
        nonlocal pending, role
        if pending:
            messages.append({"role": role, "content": pending})
        pending, role = [], None

    def add(target, blocks):
        nonlocal role
        if role != target:
            flush()
        role = target
        pending.extend(blocks)

    for item in inputs:
        kind = item.get("type", "message")
        if kind == "message":
            target = item.get("role")
            if target not in {"user", "assistant", "system", "developer"}:
                raise BridgeError("Unsupported message role.")
            blocks = content_blocks(item.get("content", ""))
            if blocks:
                add(target, blocks)
        elif kind == "function_call":
            try:
                arguments = json.loads(item.get("arguments", "{}"))
            except (TypeError, ValueError) as exc:
                raise BridgeError("A function call contains invalid JSON arguments.") from exc
            if not isinstance(arguments, dict):
                raise BridgeError("Function arguments must be an object.")
            add("assistant", [{"type": "tool_use", "id": item.get("call_id"), "name": item.get("name"), "input": arguments}])
        elif kind == "function_call_output":
            blocks = content_blocks(item.get("output", ""))
            add("user", [{"type": "tool_result", "tool_use_id": item.get("call_id"),
                          # An empty string is valid tool_result content; an empty
                          # text block is not (see content_blocks above).
                          "content": blocks if blocks else ""}])
        elif kind == "reasoning":
            token = item.get("encrypted_content")
            if token:
                add("assistant", envelope.open(token, scope))
            elif item.get("summary"):
                raise BridgeError("Reasoning summaries cannot replace authenticated provider reasoning. Start a new task.")
        else:
            raise BridgeError("Unsupported Responses history item.")
    flush()
    if not messages:
        raise BridgeError("The conversation history has no content to send.")
    result = {"model": route, "messages": messages, "stream": body["stream"],
              "max_tokens": body.get("max_output_tokens") or min(spec.get("max_output") or 16384, 16384)}
    if body.get("instructions"):
        result["system"] = body["instructions"]
    tools = body.get("tools", [])
    if tools:
        result["tools"] = [{"name": tool["name"], "description": tool.get("description", ""),
                            "input_schema": copy.deepcopy(tool.get("parameters", {"type": "object"}))} for tool in tools]
        choice = body.get("tool_choice", "auto")
        if isinstance(choice, str) and choice in {"auto", "required", "none"}:
            result["tool_choice"] = {"type": {"required": "any"}.get(choice, choice)}
        elif isinstance(choice, dict) and choice.get("type") == "function":
            result["tool_choice"] = {"type": "tool", "name": choice.get("name")}
        else:
            raise BridgeError("Unsupported function tool choice.")
        if "parallel_tool_calls" in body:
            result["tool_choice"]["disable_parallel_tool_use"] = not body["parallel_tool_calls"]
    reasoning = body.get("reasoning") or {}
    if not isinstance(reasoning, dict):
        raise BridgeError("reasoning must be an object.")
    effort = reasoning.get("effort")
    if effort is not None:
        result["output_config"] = {"effort": effort}
        if effort == "none":
            result["thinking"] = {"type": "disabled"}
    for key in ("temperature", "top_p", "service_tier"):
        if body.get(key) is not None:
            result[key] = body[key]
    return result


def response_usage(value):
    def integer(key):
        number = value.get(key, 0)
        return number if type(number) is int and number >= 0 else 0
    cached = integer("cache_read_input_tokens")
    input_tokens = integer("input_tokens") + integer("cache_creation_input_tokens") + cached
    output = integer("output_tokens")
    return {"input_tokens": input_tokens, "output_tokens": output, "total_tokens": input_tokens + output,
            "input_tokens_details": {"cached_tokens": cached}}


class MessagesResponsesAdapter:
    def __init__(self, requested, envelope, scope):
        self.requested, self.envelope, self.scope = requested, envelope, scope
        self.identifier = "resp_" + uuid.uuid4().hex
        self.created = int(time.time())
        self.output = []
        self.blocks = {}
        self.usage = {}
        self.stop_reason = None
        self.sequence = 0

    def event(self, kind, **fields):
        value = {"type": kind, "sequence_number": self.sequence, **fields}
        self.sequence += 1
        return value

    def response(self, status="completed", error=None):
        return {"id": self.identifier, "object": "response", "created_at": self.created,
                "model": self.requested, "status": status, "output": copy.deepcopy(self.output),
                "error": error, "incomplete_details": {"reason": "max_output_tokens"} if status == "incomplete" else None,
                "usage": response_usage(self.usage), "store": False}

    def item(self, block):
        kind = block.get("type")
        if kind == "text":
            return {"id": "msg_" + uuid.uuid4().hex, "type": "message", "role": "assistant", "status": "completed",
                    "content": [{"type": "output_text", "text": block.get("text", ""), "annotations": []}]}
        if kind == "tool_use":
            if not isinstance(block.get("input"), dict):
                raise BridgeError("The provider returned invalid function arguments.")
            return {"id": "fc_" + uuid.uuid4().hex, "type": "function_call", "call_id": block["id"],
                    "name": block["name"], "arguments": json.dumps(block["input"], separators=(",", ":")), "status": "completed"}
        if kind in {"thinking", "redacted_thinking"}:
            return {"id": "rs_" + uuid.uuid4().hex, "type": "reasoning", "summary": [],
                    "encrypted_content": self.envelope.seal([block], self.scope)}
        raise BridgeError("The provider returned a content type that this Responses adapter cannot represent.")

    def from_message(self, value):
        if value.get("type") != "message" or not isinstance(value.get("content"), list):
            raise BridgeError("The Messages adapter returned an invalid response.")
        self.output = [self.item(block) for block in value["content"]]
        self.usage = value.get("usage") or {}
        return self.response("incomplete" if value.get("stop_reason") == "max_tokens" else "completed")

    def feed(self, value):
        kind = value.get("type")
        if kind == "ping":
            return []
        if kind == "error":
            error = value.get("error") or {}
            return [self.event("response.failed", response=self.response("failed", {
                "code": "provider_error", "message": error.get("message", "Provider generation failed.")}))]
        if kind == "message_start":
            self.usage.update(value.get("message", {}).get("usage", {}))
            return [self.event("response.created", response=self.response("in_progress"))]
        if kind == "content_block_start":
            block = copy.deepcopy(value["content_block"])
            index = len(self.output)
            # Allocate a stable output index now, including hidden reasoning.
            if block.get("type") in {"thinking", "redacted_thinking"}:
                item = {"id": "rs_" + uuid.uuid4().hex, "type": "reasoning", "summary": [], "encrypted_content": None}
            else:
                item = self.item(block)
                item["status"] = "in_progress"
                if item["type"] == "function_call":
                    item["arguments"] = ""
            self.output.append(item)
            self.blocks[value["index"]] = {"block": block, "output_index": index, "partial": "", "closed": False}
            events = [self.event("response.output_item.added", output_index=index, item=copy.deepcopy(item))]
            if item["type"] == "message":
                events.append(self.event("response.content_part.added", item_id=item["id"], output_index=index,
                                         content_index=0, part=copy.deepcopy(item["content"][0])))
            return events
        if kind == "content_block_delta":
            state = self.blocks.get(value.get("index"))
            if state is None or state["closed"]:
                raise BridgeError("The provider streamed an invalid content-block sequence.")
            block, index = state["block"], state["output_index"]
            item, delta = self.output[index], value["delta"]
            dtype = delta.get("type")
            if dtype == "text_delta":
                block["text"] = block.get("text", "") + delta["text"]
                item["content"][0]["text"] = block["text"]
                return [self.event("response.output_text.delta", item_id=item["id"], output_index=index,
                                   content_index=0, delta=delta["text"])]
            if dtype == "input_json_delta":
                state["partial"] += delta["partial_json"]
                return [self.event("response.function_call_arguments.delta", item_id=item["id"], output_index=index,
                                   delta=delta["partial_json"])]
            if dtype == "thinking_delta":
                block["thinking"] = block.get("thinking", "") + delta["thinking"]
            elif dtype == "signature_delta":
                block["signature"] = block.get("signature", "") + delta["signature"]
            else:
                raise BridgeError("The provider streamed an unsupported content delta.")
            return []
        if kind == "content_block_stop":
            state = self.blocks.get(value.get("index"))
            if state is None or state["closed"]:
                raise BridgeError("The provider stopped an unknown content block.")
            state["closed"] = True
            block, index = state["block"], state["output_index"]
            if block["type"] == "tool_use" and state["partial"]:
                try:
                    block["input"] = json.loads(state["partial"])
                except ValueError as exc:
                    raise BridgeError("The provider returned incomplete function arguments.") from exc
            final = self.item(block)
            final["id"] = self.output[index]["id"]
            self.output[index] = final
            events = []
            if final["type"] == "function_call":
                events.append(self.event("response.function_call_arguments.done", item_id=final["id"], output_index=index, arguments=final["arguments"]))
            elif final["type"] == "message":
                events.append(self.event("response.output_text.done", item_id=final["id"], output_index=index, content_index=0, text=final["content"][0]["text"]))
                events.append(self.event("response.content_part.done", item_id=final["id"], output_index=index, content_index=0, part=copy.deepcopy(final["content"][0])))
            events.append(self.event("response.output_item.done", output_index=index, item=copy.deepcopy(final)))
            return events
        if kind == "message_delta":
            self.stop_reason = value.get("delta", {}).get("stop_reason")
            self.usage.update(value.get("usage", {}))
            return []
        if kind == "message_stop":
            if any(not state["closed"] for state in self.blocks.values()) or self.stop_reason is None:
                raise BridgeError("The Messages stream ended before its content was complete.")
            status = "incomplete" if self.stop_reason == "max_tokens" else "completed"
            return [self.event("response." + status, response=self.response(status))]
        raise BridgeError("The Messages adapter returned an unsupported stream event.")
