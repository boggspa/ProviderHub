"""Bounded Team execution policy and measured run accounting.

Checkpoints save progress, not permission. Limits are checked between model
requests and tool actions; already in-flight requests can exceed a token cap.
Provider billing is deliberately not inferred from context or token counts.
"""
from __future__ import annotations

import json
import hashlib
import time


DEFAULTS = {"mode": "task", "contextTokens": 200000, "processes": 4,
            "minutes": None, "tokens": None}


class RunLimitReached(ValueError):
    pass


def guard(service):
    if service is None or service.role != "team": return
    with service.team_parent._mutex:
        team = service.team_parent.chat["team"]
        reason = limit(team)
        if reason:
            team["limitReason"] = reason
            raise RunLimitReached(reason)


def settings(value=None, *, legacy=False):
    result = {**DEFAULTS, **({"mode": "contribution"} if legacy else {})}
    if value is None:
        return result
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise ValueError("Invalid Team execution settings.")
    result.update(value)
    if result["mode"] not in {"task", "contribution"}:
        raise ValueError("Choose Task or One contribution for Team execution.")
    for key, low, high in (("contextTokens", 16000, 2000000), ("processes", 1, 8),
                           ("minutes", 1, 10080), ("tokens", 1, 2000000000)):
        item = result[key]
        if key in {"minutes", "tokens"} and item is None:
            continue
        if type(item) is not int or not low <= item <= high:
            raise ValueError(f"Team {key} must be an integer from {low} to {high}.")
    return result


def policy(team):
    return settings(team.get("execution"), legacy="execution" not in team)


def task(service):
    return service.role == "team" and policy(service.team_parent.chat["team"])["mode"] == "task"


def context(team, choice):
    advertised = choice.get("context")
    return min(advertised, policy(team)["contextTokens"]) if type(advertised) is int and advertised > 0 else min(128000, policy(team)["contextTokens"])


def begin(team):
    team["runUsage"] = {"started": time.time(), "tokens": 0, "requests": 0, "usageComplete": True}
    team.pop("limitReason", None)


def record(service, usage):
    if service.role != "team":
        return
    with service.team_parent._mutex:
        run = service.team_parent.chat["team"].setdefault("runUsage", {})
        keys = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
        values = [usage.get(key, 0) for key in keys]
        measured = all(key in usage for key in ("input_tokens", "output_tokens"))
        valid = all(type(value) is int and value >= 0 for value in values)
        run["tokens"] = run.get("tokens", 0) + (sum(values) if valid else 0)
        run["requests"] = run.get("requests", 0) + 1
        run["usageComplete"] = run.get("usageComplete", True) and valid and measured


def limit(team):
    config, run = policy(team), team.get("runUsage", {})
    if config["minutes"] and time.time() - run.get("started", time.time()) >= config["minutes"] * 60:
        return "Time limit reached"
    if config["tokens"]:
        if run.get("usageComplete") is False:
            return "Token usage unavailable; the token limit cannot be enforced"
        if run.get("tokens", 0) >= config["tokens"]:
            return "Token limit reached"
    return None


def progress(member):
    record = member.get("progress")
    if not record:
        return None
    return {"role": "user", "content": [{"type": "text", "text":
        "[Saved work record, reference only; inspect current files before acting.]\n" + json.dumps(record, ensure_ascii=False)}]}


def evidence(service, name, arguments, result):
    if not task(service) or name == "team_status" or result.get("is_error"):
        return
    # New findings/read results count as progress, not only changed files.
    # Bounded exact evidence catches repeated searches and empty polling while
    # avoiding a second model or guessing whether prose sounds productive.
    digest = hashlib.sha256(json.dumps([name, arguments, result.get("content")], sort_keys=True).encode()).hexdigest()
    with service.team_parent._mutex:
        member = service.team_member
        seen = member.setdefault("seenEvidence", [])
        if digest not in seen:
            member["newEvidence"] = True
            seen.append(digest)
            del seen[:-128]


def checkpoint(service):
    with service.team_parent._mutex:
        member = service.team_member
        member["checkpoints"] = member.get("checkpoints", 0) + 1
        member["idleCheckpoints"] = 0 if member.pop("newEvidence", False) else member.get("idleCheckpoints", 0) + 1
        if member["idleCheckpoints"] >= 3:
            from chat_runtime import entry
            member["waitReason"] = "Paused: no new tool results in three checkpoints"
            service.chat["status"] = "stopped"
            service.add(entry("notice", "No new tool results across three checkpoints. Team paused for review.",
                              service.chat["route"], noticeKind="team_progress"))
        service.save()
        return service.chat["status"] == "working"
