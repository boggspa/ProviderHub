"""Give a CLI-backed route the desktop harness's tools as native MCP tools.

The text protocol in cli_tool_call works only while a model follows prompt
instructions over its own tool list. A vendor CLI that also has a native tool
(hosted search, say) lets the model call host tools natively instead, where
the CLI answers "No such tool available" (Sonnet 5 under Codex, 24 Sep 2026).
Serving the harness's tools from an MCP server puts them in the model's real
tool list, so every model calls them the way it was trained to, and the
adapter hands each call to the harness exactly as before.

The server (host_tools_mcp.py) never executes anything. By default it only
lists the tools: the adapter reads the call off the CLI's stream and ends the
turn, and the CLI's fail-closed permission mode refuses to run the call itself.
With a bridge (cli_host_bridge) the CLI may call them, and the server forwards
each call to the hub, which answers it with the harness's result so that the
same CLI process carries on.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

from cli_tool_call import HOST_EXECUTION_NOTE

#: The MCP server name, which the CLIs fold into the model-facing tool names.
SERVER_NAME = "host"
SERVER_SCRIPT = Path(__file__).resolve().with_name("host_tools_mcp.py")
#: The providers' tool-name ceiling (Anthropic and xAI both allow 64).
NAME_LIMIT = 64
#: Inline result ceiling a bridged tool declares (``anthropic/maxResultSizeChars``,
#: claude 2.1.280's cap). Past its default, Claude Code saves a large result to a
#: file and shows the model a preview, and a route without file tools cannot read it.
LIVE_RESULT_CHARS = 500_000
#: For bridged tools only. Claude Code runs the calls of one message
#: concurrently only when every tool is marked read-only (``readOnlyHint``), and
#: a live session needs all of a message's calls waiting at once before the
#: host runs any of them. Nothing ever runs inside the CLI either way.
LIVE_ANNOTATIONS = {"readOnlyHint": True}


def _alias(name: str, budget: int, taken) -> str:
    """A stable name within ``budget`` characters for a host tool that is too long."""
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()
    for size in range(8, len(digest) + 1):
        candidate = name[:budget - size - 1] + "_" + digest[:size]
        if candidate not in taken:
            return candidate
    raise ValueError("host tool names collide after aliasing")


class HostToolset:
    """One request's host tools under the names a CLI's MCP client shows its model.

    ``prefix`` is how the CLI qualifies an MCP tool (Claude Code writes
    ``mcp__host__<tool>``). A host name that would overflow the name ceiling
    once prefixed is served under a short alias whose description names the
    original, so instructions that mention the tool still lead to it.
    """

    def __init__(self, tools, *, prefix: str, limit: int = NAME_LIMIT):
        self.prefix = prefix
        self.tools = [dict(tool) for tool in tools]
        budget = limit - len(prefix)
        if budget < 16:
            raise ValueError("MCP tool prefix leaves too little room for tool names")
        self._served = {}
        taken = {tool["name"] for tool in self.tools if len(tool["name"]) <= budget}
        for tool in self.tools:
            name = tool["name"]
            served = name if len(name) <= budget else _alias(name, budget, taken)
            taken.add(served)
            self._served[name] = served
        self._hosts = {prefix + served: name for name, served in self._served.items()}

    def __bool__(self) -> bool:
        return bool(self.tools)

    def model_name(self, host_name: str) -> str:
        """The name the model sees and calls for one host tool."""
        return self.prefix + self._served[host_name]

    def host_name(self, model_name) -> str | None:
        """The host tool a model-facing name stands for, or None if it is not one."""
        return self._hosts.get(model_name) if isinstance(model_name, str) else None

    @property
    def model_names(self) -> frozenset[str]:
        return frozenset(self._hosts)

    def served_tools(self, *, live: bool = False) -> list[dict]:
        """The tool list the server publishes, in the host's order."""
        served = []
        for tool in self.tools:
            name = self._served[tool["name"]]
            description = tool.get("description") or ""
            if name != tool["name"]:
                description = f"(Host tool `{tool['name']}`.) " + description
            entry = {"name": name, "description": description,
                     "input_schema": tool.get("input_schema") or {"type": "object", "properties": {}}}
            if live:
                entry["annotations"] = dict(LIVE_ANNOTATIONS)
                entry["_meta"] = {"anthropic/maxResultSizeChars": LIVE_RESULT_CHARS}
            served.append(entry)
        return served

    def write(self, directory, *, bridge=None) -> Path:
        """Write the server's tools file into ``directory`` (owner-only) and return its path.

        With ``bridge`` (a cli_host_bridge.HostCallBridge) the file also carries
        its endpoint, and the server forwards calls instead of refusing them.
        """
        path = Path(directory) / "host-tools.json"
        payload = (self.served_tools() if bridge is None
                   else {"tools": self.served_tools(live=True), "bridge": bridge.endpoint})
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        return path


def server_command(tools_path) -> list[str]:
    """argv for the server: -I -B keep the bundled interpreter from writing bytecode
    into the signed app (see agy_cli_agent's hook) and keep the CLI's environment out."""
    return [sys.executable, "-I", "-B", str(SERVER_SCRIPT), str(tools_path)]


def tools_note(toolset: HostToolset, tool_choice=None) -> str:
    """System text telling the model how the attached host tools are named and used."""
    example = toolset.tools[0]["name"]
    lines = [
        HOST_EXECUTION_NOTE,
        f"The host application's tools are attached to this session as native tools named "
        f"`{toolset.prefix}<tool>`. When instructions or earlier turns name a host tool plainly, "
        f"such as `{example}`, call it as `{toolset.model_name(example)}`. Call them directly as "
        "tools, never through a text format, and wait for their results; they are the only way "
        "to act on the host."
    ]
    if isinstance(tool_choice, dict):
        kind = tool_choice.get("type")
        if kind in {"any", "required"}:
            lines.append("You must call at least one host tool in this reply.")
        elif kind == "tool" and isinstance(tool_choice.get("name"), str):
            try:
                lines.append(f"You must call `{toolset.model_name(tool_choice['name'])}` in this reply.")
            except KeyError:
                pass
    return " ".join(lines)
