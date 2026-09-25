"""Stateless Responses-to-Messages translation for the existing provider adapters."""
from __future__ import annotations

import copy
import base64
import hashlib
import json
import time
import uuid

from bridge_core import BridgeError, private_token
from responses_tools import APPLY_PATCH_PARAM, APPLY_PATCH_TOOL_NAME, is_custom_tool, repair_apply_patch


ENVELOPE_PREFIX = "ph_reasoning_v1."


def bound_reasoning_blocks(blocks, cap):
    """Bound stored thinking traces to a head prefix of ``cap`` characters.

    Sealed reasoning only re-enters the next request as continuity context;
    the desktop retains every sealed item verbatim until compaction, so an
    uncapped thinking trace from a verbose reasoning model grows the client
    transcript without bound. A positive cap keeps the first ``cap``
    characters and appends a truncation marker so the model sees the
    discontinuity; short traces and ``redacted_thinking`` pass through
    unchanged. A missing or non-positive cap preserves full fidelity.
    """
    if type(cap) is not int or cap <= 0:
        return blocks
    bounded = []
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "thinking":
            thinking = block.get("thinking")
            if isinstance(thinking, str) and len(thinking) > cap:
                head = thinking[:cap].rstrip()
                marker = (f"\n\n[Provider Hub: earlier reasoning truncated - "
                          f"{len(thinking) - cap:,} of {len(thinking):,} characters omitted]")
                block = {**block, "thinking": head + marker}
        bounded.append(block)
    return bounded


class ReasoningEnvelope:
    def __init__(self, root, store_cap=0):
        try:
            from cryptography.fernet import Fernet
        except ImportError as exc:
            raise BridgeError("This Python runtime needs the cryptography dependency. Use the self-contained Provider Hub app or install Source/requirements.txt.") from exc
        key = private_token(root, "responses-encryption-key")
        material = hashlib.sha256(("provider-hub-responses-v1:" + key).encode()).digest()
        self.cipher = Fernet(base64.urlsafe_b64encode(material))
        # Character cap for stored thinking traces; zero keeps full fidelity.
        self.store_cap = store_cap if type(store_cap) is int and store_cap > 0 else 0

    def seal(self, blocks, scope):
        value = json.dumps({"scope": scope, "blocks": bound_reasoning_blocks(blocks, self.store_cap)},
                           separators=(",", ":")).encode()
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

    def replayable(self, token, scope):
        """The blocks to replay for this route, or [] for someone else's reasoning.

        A Codex thread keeps its reasoning items across an in-app model
        switch. Reasoning sealed for another route or account, sealed by
        another install, or produced by a native provider cannot be replayed
        to this model, and the model does not need it: the visible messages
        and tool history carry the conversation. So it is dropped rather than
        failing the turn. A token that authenticates for this very scope but
        is malformed is still an error.
        """
        if not isinstance(token, str) or not token.startswith(ENVELOPE_PREFIX):
            return []
        try:
            value = json.loads(self.cipher.decrypt(token[len(ENVELOPE_PREFIX):].encode()))
        except Exception:
            return []
        if not isinstance(value, dict) or value.get("scope") != scope:
            return []
        return self.open(token, scope)


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


def tool_output_blocks(item):
    """Keep standalone tool notifications out of paired tool-result history."""
    blocks = content_blocks(item.get("output", ""))
    call_id = item.get("call_id")
    if call_id:
        return [{"type": "tool_result", "tool_use_id": call_id,
                 # Empty content is valid; an empty text block is not.
                 "content": blocks if blocks else ""}]
    name = item.get("name")
    namespace = item.get("namespace")
    if not isinstance(name, str) or not name.strip():
        raise BridgeError("A standalone tool output needs a tool name when call_id is absent.")
    if namespace is not None and (not isinstance(namespace, str) or not namespace.strip()):
        raise BridgeError("A standalone tool output namespace must be a non-empty string.")
    # Cross-task messages arrive as named outputs with no preceding call.
    # Messages APIs require a call for tool_result, and Codex rejects the
    # nameless output that replaying tool_use_id=None would produce. Keep
    # the notification as attributed tool data, including any image blocks,
    # without inventing a tool invocation or elevating it to system text.
    identity = f"{namespace}.{name}" if namespace else name
    return [{"type": "text", "text": f"[Standalone tool output from {identity}]"}, *blocks]


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
            if route.startswith("codex/") and target == "assistant" and item.get("phase") in {"commentary", "final_answer"}:
                for block in blocks:
                    if block.get("type") == "text":
                        block["phase"] = item["phase"]
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
            add("user", tool_output_blocks(item))
        elif kind == "reasoning":
            # Only this route's own sealed reasoning is replayed; anything
            # else (another model's after a switch, or a bare summary) is
            # dropped, never promoted to reasoning (see replayable).
            token = item.get("encrypted_content")
            blocks = envelope.replayable(token, scope) if token and envelope is not None else []
            if blocks:
                add("assistant", blocks)
        elif kind == "custom_tool_call":
            # The gateway normalizes these before delegating; direct callers
            # get the same projection so every layer accepts Codex history.
            if item.get("name") != APPLY_PATCH_TOOL_NAME:
                raise BridgeError("This Responses route only adapts the apply_patch custom tool. Other free-form tools need a separate adapter.")
            patch = item.get("input", "")
            if not isinstance(patch, str):
                patch = json.dumps(patch, ensure_ascii=False)
            add("assistant", [{"type": "tool_use", "id": item.get("call_id"), "name": item.get("name"),
                               "input": {APPLY_PATCH_PARAM: patch}}])
        elif kind == "custom_tool_call_output":
            add("user", tool_output_blocks(item))
        elif kind == "web_search_call":
            # A search the provider already ran. What it found lives in the
            # assistant text that followed, and the item carries no results
            # to replay, so there is nothing here for a provider to take back.
            continue
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


#: Responses ``web_search_call`` action variants and the fields each carries.
SEARCH_ACTION_FIELDS = {"search": ("query", "queries"), "open_page": ("url",),
                        "find_in_page": ("url", "pattern"), "other": ()}


def search_action(value):
    """A web_search server tool's input as the Responses action Codex reads.

    A CLI relay passes the runtime's own action; a provider's native server
    tool carries only its query.
    """
    value = value if isinstance(value, dict) else {}
    action = value.get("action")
    if not (isinstance(action, dict) and action.get("type") in SEARCH_ACTION_FIELDS):
        query = value.get("query")
        action = {"type": "search", "query": query} if isinstance(query, str) and query else {"type": "other"}
    return {"type": action["type"], **{field: copy.deepcopy(action[field])
                                       for field in SEARCH_ACTION_FIELDS[action["type"]] if field in action}}


class MessagesResponsesAdapter:
    def __init__(self, requested, envelope, scope, tool_map=None, *,
                 expose_reasoning_summaries=False):
        self.requested, self.envelope, self.scope = requested, envelope, scope
        self.tool_map = tool_map or {}
        # Enabled only by the Codex CLI planner after requesting the native
        # published-summary lane. Other providers' thinking is not a summary.
        self.expose_reasoning_summaries = expose_reasoning_summaries is True
        self.identifier = "resp_" + uuid.uuid4().hex
        self.created = int(time.time())
        self.output = []
        self.blocks = {}
        self.usage = {}
        self.stop_reason = None
        self.sequence = 0

    def has_published_summary(self, block):
        return self.expose_reasoning_summaries and block.get("type") == "thinking"

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
            if is_custom_tool(self.tool_map, block["name"]):
                patch = block["input"].get(APPLY_PATCH_PARAM)
                if not isinstance(patch, str):
                    patch = json.dumps(block["input"], ensure_ascii=False)
                else:
                    patch = repair_apply_patch(patch)
                return {"id": "ct_" + uuid.uuid4().hex, "type": "custom_tool_call", "call_id": block["id"],
                        "name": self.tool_map[block["name"]]["name"], "input": patch, "status": "completed"}
            return {"id": "fc_" + uuid.uuid4().hex, "type": "function_call", "call_id": block["id"],
                    "name": block["name"], "arguments": json.dumps(block["input"], separators=(",", ":")), "status": "completed"}
        if kind == "server_tool_use" and block.get("name") == "web_search":
            # A search the provider ran itself; Codex renders it and sends it
            # back as history, never as a call for the client to execute.
            return {"id": "ws_" + uuid.uuid4().hex, "type": "web_search_call", "status": "completed",
                    "action": search_action(block.get("input"))}
        if kind in {"thinking", "redacted_thinking"}:
            summary = [{"type": "summary_text", "text": block.get("thinking", "")}] \
                if self.has_published_summary(block) else []
            return {"id": "rs_" + uuid.uuid4().hex, "type": "reasoning", "summary": summary,
                    "encrypted_content": self.envelope.seal([block], self.scope)}
        raise BridgeError("The provider returned a content type that this Responses adapter cannot represent.")

    def from_message(self, value):
        if value.get("type") != "message" or not isinstance(value.get("content"), list):
            raise BridgeError("The Messages adapter returned an invalid response.")
        self.output = [self.item(block) for block in value["content"]
                       if not isinstance(block, dict) or block.get("type") != "web_search_tool_result"]
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
            if block.get("type") == "web_search_tool_result":
                # Results of the search call already emitted; web_search_call
                # has no field for them, so they allocate no output item.
                self.blocks[value["index"]] = {"block": block, "output_index": None, "partial": "",
                                               "closed": False, "hidden": True}
                return []
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
            elif item["type"] == "web_search_call":
                for phase in ("in_progress", "searching"):
                    events.append(self.event("response.web_search_call." + phase, item_id=item["id"],
                                             output_index=index))
            elif self.has_published_summary(block):
                part = {"type": "summary_text", "text": ""}
                events.append(self.event("response.reasoning_summary_part.added", item_id=item["id"],
                                         output_index=index, summary_index=0, part=copy.deepcopy(part)))
                part["text"] = block.get("thinking", "")
                item["summary"] = [part]
                if part["text"]:
                    events.append(self.event("response.reasoning_summary_text.delta", item_id=item["id"],
                                             output_index=index, summary_index=0, delta=part["text"]))
            return events
        if kind == "content_block_delta":
            state = self.blocks.get(value.get("index"))
            if state is None or state["closed"]:
                raise BridgeError("The provider streamed an invalid content-block sequence.")
            if state.get("hidden"):
                return []
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
                if item["type"] in {"custom_tool_call", "web_search_call"}:
                    # The provider streams JSON-wrapped arguments for the
                    # projected apply_patch function; Codex core builds the
                    # call from output_item.done, so the JSON bytes are
                    # dropped instead of entering the patch input buffer.
                    # A search's query likewise arrives with its done item.
                    return []
                return [self.event("response.function_call_arguments.delta", item_id=item["id"], output_index=index,
                                   delta=delta["partial_json"])]
            if dtype == "thinking_delta":
                block["thinking"] = block.get("thinking", "") + delta["thinking"]
                if self.has_published_summary(block):
                    item["summary"][0]["text"] = block["thinking"]
                    return [self.event("response.reasoning_summary_text.delta", item_id=item["id"],
                                       output_index=index, summary_index=0, delta=delta["thinking"])]
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
            if state.get("hidden"):
                return []
            block, index = state["block"], state["output_index"]
            if block["type"] in {"tool_use", "server_tool_use"} and state["partial"]:
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
            elif final["type"] == "web_search_call":
                events.append(self.event("response.web_search_call.completed", item_id=final["id"], output_index=index))
            elif self.has_published_summary(block):
                part = final["summary"][0]
                events.append(self.event("response.reasoning_summary_text.done", item_id=final["id"],
                                         output_index=index, summary_index=0, text=part["text"]))
                events.append(self.event("response.reasoning_summary_part.done", item_id=final["id"],
                                         output_index=index, summary_index=0, part=copy.deepcopy(part)))
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
