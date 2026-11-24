"""The Gemini protocol surface the Antigravity app talks to.

Antigravity's agent speaks Gemini's ``generateContent``: it POSTs to
``/v1beta/models/<id>:generateContent`` (or ``:streamGenerateContent``) and
reads a Gemini response. The Hub's gateway serves Anthropic Messages and the
OpenAI Responses API, so this module is the adapter between the two.

There are two directions.

* A Gemini request becomes an Anthropic Messages request, which the gateway
  already plans, routes, meters and relays. That request is handed to the
  gateway's own Messages surface over the loopback, so one relay serves
  every protocol: the Antigravity surface cannot drift away from the
  routing, compaction and credential handling the other surfaces share, and
  nothing about the existing relay has to change to carry a third one.
* A Messages response becomes a Gemini response: content blocks become
  candidate parts, the stop reason becomes a finish reason, and the usage
  becomes ``usageMetadata``.

The model id Antigravity sends is a Hub route in a flat spelling - see
``antigravity_catalogue`` - because the id travels as one URL path segment
and a route's slash would split it. The gateway turns it back into a route.
"""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request

import antigravity_effort
from antigravity_catalogue import effort_choices, project_antigravity, route_for_slug


#: The path prefix Antigravity's Gemini client uses.
GEMINI_PREFIX = "/v1beta/models/"

#: The two methods the client calls.
GENERATE = "generateContent"
STREAM = "streamGenerateContent"

#: Gemini's finish reasons, as the client expects them.
FINISH_STOP = "STOP"
FINISH_MAX_TOKENS = "MAX_TOKENS"


def is_gemini_path(path: str) -> bool:
    """Whether a request path addresses the Gemini surface."""
    return isinstance(path, str) and path.startswith(GEMINI_PREFIX)


def parse_path(path: str) -> tuple[str, str] | None:
    """The model id and method a Gemini path addresses."""
    if not is_gemini_path(path):
        return None
    remainder = path[len(GEMINI_PREFIX):]
    model, separator, method = remainder.rpartition(":")
    if not separator or method not in {GENERATE, STREAM} or not model:
        return None
    return model, method


class _ToolCalls:
    """Correlates Gemini's tool calls with the results that follow them.

    Gemini carries no call id, so the ids are minted here: a function call
    and the response that answers it are matched by name, most recent
    unanswered call first.
    """

    def __init__(self):
        self.counter = 0
        self.open: list[tuple[str, str]] = []

    def open_call(self, name: str, identifier: str | None = None) -> str:
        self.counter += 1
        identifier = identifier if isinstance(identifier, str) and identifier else f"call_{self.counter}"
        self.open.append((name, identifier))
        return identifier

    def close_call(self, name: str, identifier: str | None = None) -> str:
        for index in range(len(self.open)):
            if self.open[index][0] == name and (not identifier or self.open[index][1] == identifier):
                return self.open.pop(index)[1]
        raise ValueError(f"A function response for {name!r} has no matching call.")


def _text_of(parts) -> str:
    """The plain text of a Gemini part list."""
    if isinstance(parts, str):
        return parts
    lines = []
    for part in parts or []:
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            lines.append(part["text"])
    return "\n".join(lines)


def _blocks(parts, calls: _ToolCalls) -> list[dict]:
    """Gemini parts as Anthropic content blocks."""
    blocks: list[dict] = []
    for part in parts or []:
        if not isinstance(part, dict):
            continue
        if isinstance(part.get("text"), str):
            if part.get("thought") is True:
                signature = part.get("thoughtSignature")
                if signature:
                    try:
                        decoded = base64.b64decode(signature, validate=True).decode()
                    except (ValueError, UnicodeError):
                        raise ValueError("Invalid thinking signature.") from None
                    blocks.append({"type": "thinking", "thinking": part["text"], "signature": decoded})
            else:
                blocks.append({"type": "text", "text": part["text"]})
        elif isinstance(part.get("inlineData"), dict):
            inline = part["inlineData"]
            data = inline.get("data")
            if isinstance(data, str) and data:
                blocks.append({"type": "image", "source": {
                    "type": "base64",
                    "media_type": inline.get("mimeType") or "image/png",
                    "data": data}})
        elif isinstance(part.get("functionCall"), dict):
            call = part["functionCall"]
            name = call.get("name") or "tool"
            arguments = call.get("args")
            blocks.append({"type": "tool_use", "id": calls.open_call(name, call.get("id")), "name": name,
                           "input": arguments if isinstance(arguments, dict) else {}})
        elif isinstance(part.get("functionResponse"), dict):
            response = part["functionResponse"]
            name = response.get("name") or "tool"
            body = response.get("response")
            blocks.append({"type": "tool_result", "tool_use_id": calls.close_call(name, response.get("id")),
                           "content": body if isinstance(body, str) else json.dumps(body or {}, ensure_ascii=False)})
    return blocks


def gemini_to_messages(payload: dict, route: str, *, effort: str | None = None) -> dict:
    """A Gemini request as an Anthropic Messages request for a Hub route."""
    calls = _ToolCalls()
    messages: list[dict] = []
    for content in payload.get("contents") or []:
        if not isinstance(content, dict):
            continue
        role = "assistant" if content.get("role") == "model" else "user"
        blocks = _blocks(content.get("parts"), calls)
        if not blocks:
            continue
        # Anthropic alternates roles, and Gemini does not: two user turns in
        # a row are one turn here.
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"].extend(blocks)
        else:
            messages.append({"role": role, "content": blocks})

    request: dict = {"model": route, "messages": messages, "max_tokens": 4096}
    system = _text_of((payload.get("systemInstruction") or {}).get("parts")
                      if isinstance(payload.get("systemInstruction"), dict) else payload.get("systemInstruction"))
    if system:
        request["system"] = system

    generation = payload.get("generationConfig") or {}
    maximum = generation.get("maxOutputTokens")
    if type(maximum) is int and maximum > 0:
        request["max_tokens"] = maximum
    for field, target in (("temperature", "temperature"), ("topP", "top_p"), ("topK", "top_k")):
        value = generation.get(field)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            request[target] = value
    stops = generation.get("stopSequences")
    if isinstance(stops, list) and stops:
        request["stop_sequences"] = [stop for stop in stops if isinstance(stop, str)]

    if effort is not None:
        # The picker selection takes precedence over Antigravity's thinking
        # config for discovered models, including its usual zero budget.
        request["output_config"] = {"effort": effort}
        if effort == "none":
            request["thinking"] = {"type": "disabled"}
    else:
        thinking = generation.get("thinkingConfig") or {}
        budget = thinking.get("thinkingBudget")
        if type(budget) is int:
            # Anthropic's smallest enabled budget is 1024 tokens, and zero
            # is the explicit "do not think" spelling.
            request["thinking"] = {"type": "enabled", "budget_tokens": max(1024, budget)} if budget > 0 else {"type": "disabled"}
        level = str(thinking.get("thinkingLevel") or "").lower()
        if level in {"minimal", "low", "medium", "high"}:
            request["output_config"] = {"effort": level}

    tools = []
    for tool in payload.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        for declaration in tool.get("functionDeclarations") or []:
            if not isinstance(declaration, dict) or not declaration.get("name"):
                continue
            schema = declaration.get("parameters") or declaration.get("parametersJsonSchema")
            if not isinstance(schema, dict):
                schema = {"type": "object", "properties": {}}
            tools.append({"name": declaration["name"],
                          "description": declaration.get("description") or "",
                          "input_schema": schema})
    if tools:
        request["tools"] = tools
        config = (payload.get("toolConfig") or {}).get("functionCallingConfig") or {}
        mode = str(config.get("mode") or "AUTO").upper()
        allowed = [name for name in (config.get("allowedFunctionNames") or []) if isinstance(name, str)]
        if mode == "NONE":
            request["tool_choice"] = {"type": "none"}
        elif mode == "ANY" and len(allowed) == 1:
            request["tool_choice"] = {"type": "tool", "name": allowed[0]}
        elif mode == "ANY":
            request["tool_choice"] = {"type": "any"}
        else:
            request["tool_choice"] = {"type": "auto"}
    return request


def _finish(stop_reason) -> str:
    """An Anthropic stop reason as a Gemini finish reason."""
    if stop_reason == "max_tokens":
        return FINISH_MAX_TOKENS
    return FINISH_STOP


def _usage(usage: dict) -> dict:
    """An Anthropic usage record as Gemini's ``usageMetadata``."""
    prompt = usage.get("input_tokens")
    completion = usage.get("output_tokens")
    prompt = prompt if type(prompt) is int else 0
    completion = completion if type(completion) is int else 0
    metadata = {"promptTokenCount": prompt, "candidatesTokenCount": completion,
                "totalTokenCount": prompt + completion}
    cached = usage.get("cache_read_input_tokens")
    if type(cached) is int and cached > 0:
        metadata["cachedContentTokenCount"] = cached
    return metadata


def _candidate(parts: list[dict], finish=None, index: int = 0) -> dict:
    """One Gemini candidate."""
    candidate = {"content": {"role": "model", "parts": parts}, "index": index}
    if finish:
        candidate["finishReason"] = finish
    return candidate


def messages_to_gemini(message: dict, model: str | None = None) -> dict:
    """An Anthropic Messages response as a Gemini response."""
    parts: list[dict] = []
    for block in message.get("content") or []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            parts.append({"text": block.get("text") or ""})
        elif kind == "thinking":
            if block.get("signature"):
                parts.append({"text": block.get("thinking") or "", "thought": True,
                              "thoughtSignature": base64.b64encode(block["signature"].encode()).decode()})
        elif kind == "tool_use":
            call = {"name": block.get("name") or "tool",
                    "args": block.get("input") if isinstance(block.get("input"), dict) else {}}
            if block.get("id"):
                call["id"] = block["id"]
            parts.append({"functionCall": call})
        elif kind == "tool_result":
            parts.append({"functionResponse": {"name": block.get("name") or "tool",
                                               "response": {"result": block.get("content")}}})
    response = {"candidates": [_candidate(parts, _finish(message.get("stop_reason")))],
                "usageMetadata": _usage(message.get("usage") or {})}
    version = message.get("model") or model
    if version:
        response["modelVersion"] = version
    return response


class GeminiStream:
    """Turns the relay's Anthropic events into Gemini stream chunks.

    The relay emits Anthropic Messages events whatever the upstream protocol
    was, so this is the one place that knows how a Gemini client reads a
    turn: a text part per delta, a function call once its arguments are
    complete, and a last chunk carrying the finish reason and the usage.
    """

    def __init__(self, model: str | None = None):
        self.model = model
        self.usage: dict = {}
        self.finish = None
        self.blocks: dict = {}
        self.ended = False

    def _chunk(self, parts, finish=None) -> dict:
        chunk = {"candidates": [_candidate(parts, finish)]}
        if self.usage:
            chunk["usageMetadata"] = _usage(self.usage)
        return chunk

    def feed(self, event: dict) -> list[dict]:
        """The Gemini chunks one Anthropic event produces."""
        if not isinstance(event, dict):
            return []
        kind = event.get("type")
        if kind == "message_start":
            message = event.get("message") or {}
            self.usage = message.get("usage") or self.usage
            return []
        if kind == "content_block_start":
            block = event.get("content_block") or {}
            index = event.get("index", 0)
            if block.get("type") == "tool_use":
                self.blocks[index] = {"name": block.get("name") or "tool", "id": block.get("id"), "json": ""}
            elif block.get("type") == "thinking":
                self.blocks[index] = {"thinking": block.get("thinking") or "", "signature": block.get("signature") or ""}
            return []
        if kind == "content_block_delta":
            delta = event.get("delta") or {}
            index = event.get("index", 0)
            if delta.get("type") == "text_delta":
                return [self._chunk([{"text": delta.get("text") or ""}])]
            if delta.get("type") == "thinking_delta":
                state = self.blocks.setdefault(index, {"thinking": "", "signature": ""})
                state["thinking"] += delta.get("thinking") or ""
                return []
            if delta.get("type") == "signature_delta":
                state = self.blocks.setdefault(index, {"thinking": "", "signature": ""})
                state["signature"] += delta.get("signature") or ""
                return []
            if delta.get("type") == "input_json_delta":
                state = self.blocks.setdefault(index, {"name": "tool", "json": ""})
                state["json"] += delta.get("partial_json") or ""
            return []
        if kind == "content_block_stop":
            state = self.blocks.pop(event.get("index", 0), None)
            if state is None:
                return []
            if "thinking" in state:
                if not state.get("signature"):
                    return []
                return [self._chunk([{"text": state["thinking"], "thought": True,
                                     "thoughtSignature": base64.b64encode(state["signature"].encode()).decode()}])]
            arguments = state.get("json") or "{}"
            try:
                parsed = json.loads(arguments)
            except ValueError:
                parsed = {}
            if not isinstance(parsed, dict):
                parsed = {"value": parsed}
            call = {"name": state.get("name") or "tool", "args": parsed}
            if state.get("id"):
                call["id"] = state["id"]
            return [self._chunk([{"functionCall": call}])]
        if kind == "message_delta":
            delta = event.get("delta") or {}
            if delta.get("stop_reason"):
                self.finish = _finish(delta["stop_reason"])
            if event.get("usage"):
                self.usage.update(event["usage"])
            return []
        if kind == "message_stop":
            self.ended = True
            parts = []
            return [self._chunk(parts, self.finish or FINISH_STOP)]
        if kind == "error":
            self.ended = True
            return [{"error": {"code": 502, "status": "INTERNAL",
                               "message": (event.get("error") or {}).get("message", "The provider stream failed.")}}]
        return []

    def end(self) -> list[dict]:
        """The closing chunk, when the stream ended without one."""
        if not self.ended:
            return [{"error": {"code": 502, "status": "INTERNAL", "message": "The provider stream ended unexpectedly."}}]
        return []


def _authorised(handler, token: str) -> bool:
    """Whether the caller carries the gateway's credential.

    Antigravity's Gemini client sends its key the way the Gemini API does -
    as ``x-goog-api-key`` or a ``key`` query parameter - so those are read
    as well as the bearer form the other surfaces use.
    """
    headers = handler.headers
    candidates = []
    authorisation = headers.get("Authorization") or ""
    if authorisation.startswith("Bearer "):
        candidates.append(authorisation[7:])
    candidates.append(headers.get("x-api-key") or "")
    candidates.append(headers.get("x-goog-api-key") or "")
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(handler.path).query)
    candidates.extend(query.get("key") or [])
    import hmac
    return any(hmac.compare_digest(candidate, token) for candidate in candidates if candidate)


def serve_models(handler) -> None:
    """Expose the prepared catalogue in the Gemini model-list format."""
    host = handler.headers.get("Host", "").split(":")[0]
    if host not in {"127.0.0.1", "localhost"}:
        handler.error(403, "This gateway accepts local desktop requests only.")
        return
    if not _authorised(handler, handler.runtime.token):
        handler.error(401, "The local gateway credential is missing or incorrect.")
        return
    rows = project_antigravity(handler.runtime.settings, handler.runtime.catalogue)["models"]
    handler.json_response(200, {"models": [{
        "name": "models/" + row["model_name"], "displayName": row["display_name"],
        "description": row["description"], "inputTokenLimit": row["context"],
        "outputTokenLimit": row["max_output_tokens"],
        "supportedGenerationMethods": ["generateContent"],
    } for row in rows]})


def _loopback(handler, request: dict, stream: bool) -> tuple[int, bytes, dict]:
    """The gateway's own Messages surface, asked for this one request."""
    runtime = handler.runtime
    port = handler.server.server_address[1]
    body = json.dumps(request).encode()
    relay = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/messages", data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + runtime.token,
                 "Content-Length": str(len(body))})
    try:
        with urllib.request.urlopen(relay, timeout=600) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as error:
        return error.code, error.read(), dict(error.headers)


def _stream_loopback(handler, request: dict):
    """The gateway's Messages surface, asked for a streamed turn."""
    runtime = handler.runtime
    port = handler.server.server_address[1]
    body = json.dumps(request).encode()
    relay = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/messages", data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + runtime.token,
                 "Content-Length": str(len(body))})
    return urllib.request.urlopen(relay, timeout=600)


def serve(handler) -> None:
    """Answer one Gemini request from the gateway's own routing."""
    parsed = parse_path(urllib.parse.urlsplit(handler.path).path)
    if parsed is None:
        handler.error(404, "Unknown Gemini endpoint.")
        return
    model, method = parsed
    host = handler.headers.get("Host", "").split(":")[0]
    if host not in {"127.0.0.1", "localhost"}:
        handler.error(403, "This gateway accepts local desktop requests only.")
        return
    if not _authorised(handler, handler.runtime.token):
        handler.error(401, "The local gateway credential is missing or incorrect.")
        return
    if handler.headers.get("Transfer-Encoding"):
        handler.error(400, "Use a Content-Length request body.")
        return
    try:
        length = int(handler.headers.get("Content-Length") or 0)
    except ValueError:
        handler.error(400, "Invalid Content-Length.")
        return
    if length <= 0:
        handler.error(400, "Request body is empty.")
        return
    if length > 128 * 1024 * 1024:
        handler.error(413, "Request body exceeds the gateway transport limit.")
        return
    try:
        payload = json.loads(handler.rfile.read(length))
    except ValueError:
        handler.error(400, "The request body is not valid JSON.")
        return
    if not isinstance(payload, dict):
        handler.error(400, "The request must be a JSON object.")
        return
    # One row per model; a ``.<effort>`` suffix is the alias earlier builds
    # listed per level, still honoured for conversations that saved one.
    base, separator, suffix = model.partition(".")
    route = route_for_slug(base)
    if route is None:
        handler.error(404, f"Model {model!r} is not in the Provider Hub catalogue.")
        return
    rows = project_antigravity(handler.runtime.settings, handler.runtime.catalogue)["models"]
    row = next((row for row in rows if row["model_name"] == base), None)
    effort = None
    if separator:
        if row is None or suffix not in {choice["effort"] for choice in effort_choices(row)}:
            handler.error(404, f"Model {model!r} is not in the Provider Hub catalogue.")
            return
        effort = suffix
    elif row is not None:
        labels = payload.get("labels")
        cascade = labels.get("cascade_id") if isinstance(labels, dict) else None
        effort = antigravity_effort.resolve(handler.runtime.root, cascade, row)
    try:
        request = gemini_to_messages(payload, route, effort=effort)
    except (TypeError, ValueError, AttributeError) as error:
        handler.error(400, str(error))
        return
    if method == STREAM:
        request["stream"] = True
        _serve_stream(handler, request, model)
        return
    status, body, _ = _loopback(handler, request, False)
    if status != 200:
        handler.error(status, _detail(body))
        return
    try:
        message = json.loads(body)
    except ValueError:
        handler.error(502, "The provider returned a response the gateway could not read.")
        return
    handler.json_response(200, messages_to_gemini(message, model))


def _detail(body: bytes) -> str:
    """The relay's own error text, when it sent one."""
    try:
        decoded = json.loads(body)
    except ValueError:
        return "The gateway rejected the request."
    error = decoded.get("error") if isinstance(decoded, dict) else None
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        return error["message"]
    return "The gateway rejected the request."


def _serve_stream(handler, request: dict, model: str) -> None:
    """Relay one streamed turn in Gemini's stream shape."""
    try:
        response = _stream_loopback(handler, request)
    except urllib.error.HTTPError as error:
        handler.error(error.code, _detail(error.read()))
        return
    except OSError:
        handler.error(502, "The gateway could not reach its own relay.")
        return
    stream = GeminiStream(model)
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Transfer-Encoding", "chunked")
    handler.send_header("Connection", "close")
    handler.end_headers()
    try:
        for event in _anthropic_events(response):
            for chunk in stream.feed(event):
                _write(handler, ("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode())
        for chunk in stream.end():
            _write(handler, ("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode())
        handler.wfile.write(b"0\r\n\r\n")
        handler.wfile.flush()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        response.close()
        handler.close_connection = True


def _write(handler, data: bytes) -> None:
    """One chunk of a chunked response."""
    handler.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
    handler.wfile.flush()


def _anthropic_events(response):
    """The Anthropic events in a Messages stream response."""
    lines: list[bytes] = []
    for line in response:
        line = line.rstrip(b"\r\n")
        if not line:
            data = b"\n".join(lines)
            lines = []
            if not data or data == b"[DONE]":
                continue
            try:
                event = json.loads(data)
            except ValueError:
                continue
            if isinstance(event, dict):
                yield event
        elif line.startswith(b"data:"):
            lines.append(line[5:].strip())
