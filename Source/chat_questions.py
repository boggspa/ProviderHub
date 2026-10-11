"""Host-owned user questions. Cards are records; answers are never approvals."""
from __future__ import annotations

import copy
import json
import threading
import time
import uuid

from chat_tools import _tool

OPTION = {"type": "object", "properties": {"label": {"type": "string"}, "description": {"type": "string"}},
          "required": ["label"], "additionalProperties": False}
QUESTION = {"type": "object", "properties": {
    "id": {"type": "string"}, "header": {"type": "string"}, "question": {"type": "string"},
    "options": {"type": "array", "maxItems": 6, "items": OPTION}, "multiSelect": {"type": "boolean"}},
    "required": ["id", "question"], "additionalProperties": False}
PROPERTIES = {"questions": {"type": "array", "minItems": 1, "maxItems": 4, "items": QUESTION}}
TOOL_DEFINITIONS = [
    _tool("request_user_input", "Ask the user 1–4 short questions in transcript cards and wait for their answers. Only this agent waits; other work can continue. Options are optional and free text is always available. Dismissal is not consent. Never use a question to grant tool permissions.", PROPERTIES, ["questions"]),
    _tool("request_user_input_async", "Show question cards and return a request_id immediately. Continue independent work. Answers arrive at a model boundary; use wait_for_user_input when dependent work must wait. Do not infer an answer from elapsed time.", PROPERTIES, ["questions"]),
    _tool("wait_for_user_input", "Wait for an earlier question from this agent. A timeout returns pending and leaves the card open. Stop cancels the wait. Repeated reads return the recorded answer.", {
        "request_id": {"type": "string"}, "timeout_ms": {"type": "integer", "minimum": 0, "maximum": 60000}}, ["request_id"]),
]
TOOLS = {item["name"] for item in TOOL_DEFINITIONS}


def _text(value, limit, label, *, empty=False):
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        raise ValueError(f"{label} must be {'at most' if empty else '1–'}{limit} characters.")
    return value.strip()


def validate(name, args):
    if not isinstance(args, dict): raise ValueError("Question arguments must be an object.")
    if name == "wait_for_user_input":
        if set(args) - {"request_id", "timeout_ms"}: raise ValueError("Unknown wait argument.")
        identifier = _text(args.get("request_id"), 128, "request_id")
        timeout = args.get("timeout_ms", 30000)
        if type(timeout) is not int or not 0 <= timeout <= 60000: raise ValueError("timeout_ms must be 0–60000.")
        return identifier, timeout
    if name not in TOOLS or set(args) != {"questions"}: raise ValueError("Use questions only.")
    questions = args["questions"]
    if not isinstance(questions, list) or not 1 <= len(questions) <= 4: raise ValueError("Ask 1–4 questions.")
    result, ids = [], set()
    for question in questions:
        if not isinstance(question, dict) or set(question) - set(QUESTION["properties"]): raise ValueError("Invalid question fields.")
        identifier = _text(question.get("id"), 64, "Question id")
        if identifier in ids: raise ValueError("Question ids must be unique.")
        ids.add(identifier)
        options = question.get("options", [])
        if not isinstance(options, list) or len(options) > 6: raise ValueError("Use at most six options.")
        choices, labels = [], set()
        for option in options:
            if not isinstance(option, dict) or set(option) - {"label", "description"}: raise ValueError("Invalid option.")
            label = _text(option.get("label"), 120, "Option label")
            if label in labels: raise ValueError("Option labels must be unique.")
            labels.add(label)
            choices.append({"label": label, "description": _text(option.get("description", ""), 500, "Option description", empty=True)})
        multiple = question.get("multiSelect", False)
        if type(multiple) is not bool: raise ValueError("multiSelect must be a boolean.")
        result.append({"id": identifier, "header": _text(question.get("header", ""), 40, "Header", empty=True),
                       "question": _text(question.get("question"), 2000, "Question"), "options": choices, "multiSelect": multiple})
    if len(json.dumps(result, ensure_ascii=False).encode("utf8")) > 20000: raise ValueError("Keep the questions and options shorter.")
    return result


def owner(service):
    while getattr(service, "team_parent", None) or getattr(service, "helper_parent", None):
        service = getattr(service, "team_parent", None) or service.helper_parent
    return service


def requester(service):
    member = getattr(service, "team_member", None)
    return "member:" + member["id"] if member else service.chat["id"]


def registry(service):
    root = owner(service)
    with root._mutex:
        if not hasattr(root, "questions"): root.questions = Registry(root)
        return root.questions


def result(record):
    value = {key: record[key] for key in ("id", "status", "answers")}
    value["request_id"] = value.pop("id")
    value["questions"] = [{"id": item["id"], "question": item["question"]} for item in record["questions"]]
    return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
            "is_error": False, "summary": "User question · " + record["status"], "changed_files": []}


class Registry:
    def __init__(self, root):
        self.root, self.requests = root, {}

    def ask(self, service, name, args):
        questions = validate(name, args)
        from chat_runtime import entry
        with self.root._mutex:
            if service.cancel.is_set(): raise InterruptedError("Stopped")
            if sum(row["entry"]["question"]["status"] == "pending" for row in self.requests.values()) >= 16:
                raise ValueError("This chat already has 16 open questions. Wait for an answer first.")
            identifier = uuid.uuid4().hex
            member = getattr(service, "team_member", None)
            asker = member["name"] if member else service.chat.get("label") or service.choice().get("label", "Assistant")
            record = {"id": identifier, "status": "pending", "mode": "async" if name.endswith("_async") else "wait",
                      "questions": questions, "answers": {}, "asker": asker}
            item = entry("question", "\n".join(q["question"] for q in questions), service.chat["route"],
                         question=record, recorded=True)
            if member: item.update(memberID=member["id"], memberName=member["name"])
            row = {"entry": item, "source": service, "requester": requester(service), "event": threading.Event(), "waiting": 0}
            self.root.chat["entries"].append(item)
            try: self.root.save()
            except Exception:
                self.root.chat["entries"].remove(item)
                raise
            self.requests[identifier] = row
            self.root.emit({"event": "entry", "chat": self.root.chat["id"], "entry": copy.deepcopy(item)})
            # Completed records remain in the transcript; bound live references.
            for old_id, old in list(self.requests.items()):
                if len(self.requests) <= 80: break
                if old["entry"]["question"]["status"] != "pending" and not old["waiting"]: self.requests.pop(old_id)
        return self.wait(service, identifier, None) if record["mode"] == "wait" else result(record)

    def wait(self, service, identifier, timeout):
        with self.root._mutex:
            row = self.requests.get(identifier)
            if not row or row["requester"] != requester(service): raise ValueError("That question does not belong to this agent.")
            row["waiting"] += 1
        end = None if timeout is None else time.monotonic() + timeout / 1000
        try:
            while not row["event"].is_set():
                if service.cancel.is_set():
                    self.cancel(service)
                    raise InterruptedError("Stopped while waiting for user input")
                if end is not None and time.monotonic() >= end: break
                row["event"].wait(min(.1, max(0, end - time.monotonic())) if end is not None else .1)
            with self.root._mutex: return result(copy.deepcopy(row["entry"]["question"]))
        finally:
            with self.root._mutex: row["waiting"] -= 1

    def _settle(self, row, status, answers):
        previous = row["entry"]
        item = copy.deepcopy(previous)
        item["question"].update(status=status, answers=answers)
        index = next(i for i, value in enumerate(self.root.chat["entries"]) if value["id"] == item["id"])
        self.root.chat["entries"][index] = item
        try: self.root.save()
        except Exception:
            self.root.chat["entries"][index] = previous
            raise
        row["entry"] = item
        row["event"].set()
        self.root.emit({"event": "entry", "chat": self.root.chat["id"], "entry": copy.deepcopy(item)})

    def answer(self, command):
        with self.root._mutex:
            if command.get("chat") != self.root.chat["id"]: raise ValueError("Question belongs to another chat.")
            row = self.requests.get(command.get("question_id"))
            if row is None: raise ValueError("This question is no longer active.")
            record = row["entry"]["question"]
            if record["status"] != "pending": return  # First durable answer wins.
            dismissed = command.get("dismiss") is True
            answers = {}
            if not dismissed:
                supplied = command.get("answers")
                if not isinstance(supplied, dict) or set(supplied) != {q["id"] for q in record["questions"]}:
                    raise ValueError("Answer each question, or dismiss the card.")
                for question in record["questions"]:
                    value = supplied[question["id"]]
                    if not isinstance(value, list) or not 1 <= len(value) <= (6 if question["multiSelect"] else 1):
                        raise ValueError("Invalid answer count.")
                    answers[question["id"]] = list(dict.fromkeys(_text(v, 8000, "Answer") for v in value))
                if len(json.dumps(answers, ensure_ascii=False).encode("utf8")) > 16000: raise ValueError("Keep the combined answers below 16 KB.")
            self._settle(row, "dismissed" if dismissed else "answered", answers)
            deliver = record["mode"] == "async" and not row["waiting"]
        if deliver:
            try: self.deliver(row)
            except (ValueError, OSError) as exc:
                self.root.emit({"event": "notice", "chat": self.root.chat["id"],
                                "message": "Your answer is saved. Send a follow-up to resume this chat: " + str(exc)})

    def deliver(self, row):
        record, source = row["entry"]["question"], row["source"]
        text = "Answer to " + record["asker"] + "’s question:\n" + "\n".join(
            q["question"] + "\n" + ("; ".join(record["answers"].get(q["id"], [])) if record["status"] == "answered" else "Dismissed — no answer supplied.")
            for q in record["questions"])
        with self.root._mutex:
            member = getattr(source, "team_member", None)
            if member and any(m["id"] == member["id"] for m in (self.root.chat.get("team") or {}).get("members", [])):
                prefix = "@" + member["name"]
                command = {"command": "steer", "id": self.root.chat["id"], "text": prefix + " " + text,
                           "mentions": [{"id": member["id"], "name": member["name"], "route": member["route"],
                                         "start": 0, "length": len(prefix.encode("utf-16-le")) // 2}]}
                self.root.handle(command)
            elif source.busy and not source.cancel.is_set():
                from chat_runtime import entry
                self.root.add(entry("user", text, source.chat["route"], questionAnswerID=record["id"]))
                source.question_inbox.append(text)
                self.root.save()
            elif not self.root.cancel.is_set() and not self.root.closing:
                if self.root.busy:
                    from chat_runtime import entry
                    self.root.add(entry("user", text, source.chat["route"], questionAnswerID=record["id"]))
                    self.root.question_inbox.append(text); self.root.save()
                else: self.root.handle({"command": "send", "id": self.root.chat["id"], "text": text})

    def cancel(self, service):
        with self.root._mutex:
            for row in self.requests.values():
                if row["entry"]["question"]["status"] == "pending" and (service is self.root or row["source"] is service):
                    self._settle(row, "cancelled", {})


def execute(service, name, args):
    if name == "wait_for_user_input": return registry(service).wait(service, *validate(name, args))
    return registry(service).ask(service, name, args)


def consume(service):
    with owner(service)._mutex:
        values = service.question_inbox[:]
        service.question_inbox.clear()
    for value in values:
        service.chat["messages"].append({"role": "user", "content": [{"type": "text", "text": value}]})
    return bool(values)


def finish(service):
    root = owner(service)
    with root._mutex:
        if not service.question_inbox or service.cancel.is_set() or service.closing: return
        if service is not root:
            root.question_inbox.extend(service.question_inbox); service.question_inbox.clear()
            return
        service._working = True
        service.chat["status"] = "working"
        service.thread = threading.Thread(target=service.run, name="question-followup", daemon=True)
        service.thread.start()


def recover(chat):
    changed = False
    for item in chat.get("entries", []):
        question = item.get("question")
        if isinstance(question, dict) and question.get("status") == "pending":
            question.update(status="cancelled", answers={}); changed = True
    return changed
