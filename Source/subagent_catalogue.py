"""Expose the host's full Codex catalogue on its existing spawn tool.

The desktop advertises only five priority rows in the tool description, but
accepts other catalogue routes in the model argument. This is discovery
metadata, not a new tool, permission, model route, or execution mechanism.

The catalogue is appended after the host's own description, never put in
front of it. The host's text is what identifies the tool ("Spawn a new agent
..."); forty-odd rows of model slugs ahead of it turned the spawn tool's
description into a model listing with the purpose at the bottom, which on a
route where the tool also travels under an alias is what a model reads as
"agent metadata" rather than as the control it was told to use.
"""
from __future__ import annotations

import re

from codex_catalogue import project_codex
from spawn_depth import SPAWN_NAMESPACE, SPAWN_TOOL, is_spawn_tool_reference


_OVERRIDES = re.compile(
    r"Available model overrides[^\n]*\n(?:[ \t]*- [^\n]*(?:\n|$))*"
)
_CATALOGUE_START = "[Provider Hub subagent catalogue]"
_CATALOGUE_END = "[/Provider Hub subagent catalogue]"
_FORK_GUIDANCE = (
    'To use a different model or reasoning effort, set fork_turns="none" or '
    'a positive turn count and supply a self-contained task. Prefer the '
    'inherited parent model and effort for full-history forks. Follow the '
    'host instructions governing when delegation and overrides are appropriate.'
)


def advertise_subagent_models(tools, settings, inventory):
    """Update only an offered host spawn tool; never restore a stripped one.

    Call before tool flattening so an unrelated tool with the same leaf name
    is distinguishable. Prefer the collaboration namespace over a bare name.
    An explicit model enum, if a future host provides one, remains binding.
    """
    if not isinstance(tools, list) or not isinstance(inventory, dict):
        return 0
    exact, bare = [], []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") == "namespace" and tool.get("name") == SPAWN_NAMESPACE:
            exact.extend(child for child in tool.get("tools", [])
                         if isinstance(child, dict) and child.get("type") == "function"
                         and child.get("name") == SPAWN_TOOL)
        elif tool.get("type") == "function" and is_spawn_tool_reference(None, tool.get("name")):
            bare.append(tool)
    targets = exact or bare
    if not targets:
        return 0
    models = project_codex(settings, inventory)["models"]
    models.sort(key=lambda row: (row["priority"], row["slug"]))
    changed = 0
    for tool in targets:
        properties = (tool.get("parameters") or {}).get("properties", {})
        model_property = properties.get("model")
        if not isinstance(model_property, dict):
            continue
        allowed = model_property.get("enum")
        rows = [row for row in models if allowed is None or row["slug"] in allowed]
        if not rows:
            continue
        lines = [_CATALOGUE_START,
                 "Available model overrides from this host's Codex catalogue (optional; inherit the parent by default):"]
        for row in rows:
            efforts = [level["effort"] + (" (default)" if level["effort"] == row["default_reasoning_level"] else "")
                       for level in row["supported_reasoning_levels"]]
            line = f"- `{row['slug']}`: {row['display_name']}."
            if efforts:
                line += " Reasoning efforts: " + ", ".join(efforts) + "."
            lines.append(line)
        lines.extend([_FORK_GUIDANCE, _CATALOGUE_END])
        description = tool.get("description") or ""
        # Idempotence matters on replayed tool catalogues. Preserve the host's
        # task, concurrency and permission guidance, and keep it first, after
        # replacing its list.
        description = re.sub(re.escape(_CATALOGUE_START) + r".*?" + re.escape(_CATALOGUE_END),
                             "", description, flags=re.DOTALL)
        description = _OVERRIDES.sub("", description).strip()
        tool["description"] = (description + "\n\n" if description else "") + "\n".join(lines)
        changed += 1
    return changed
