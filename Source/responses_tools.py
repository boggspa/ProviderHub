"""Flatten Codex tool namespaces for native provider function APIs."""
import copy
import hashlib
import json
import re

from bridge_core import BridgeError


WIRE_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _check_identity(namespace, name):
    if not isinstance(name, str) or not name:
        raise BridgeError("Function tools need a name.")
    if namespace is not None and (not isinstance(namespace, str) or not namespace):
        raise BridgeError("Tool namespaces need a name.")


def qualified_name(namespace, name):
    """The collision-proof flattened name: readable prefix plus an identity hash."""
    identity = json.dumps([namespace, name], separators=(",", ":"))
    prefix = re.sub(r"[^A-Za-z0-9_-]", "_", (namespace or "") + "_" + name)[:42]
    return "ph_" + prefix + "_" + hashlib.sha256(identity.encode()).hexdigest()[:16]


def tool_name(namespace, name):
    """The name a namespaced Codex tool should carry on the provider wire.

    Codex's own prompt names these tools the way it declares them - the
    multi-agent briefing tells the model to "use `spawn_agent`", and the
    apps briefings name their tools bare. A namespace is structure the
    Responses function API cannot carry, so it has to be flattened, but
    flattening is not licence to rename: a model handed
    `ph_collaboration_spawn_agent_8361b6d8076404de` while its instructions
    say `spawn_agent` has no callable tool matching anything it was told
    to call, and simply never delegates. That was the whole of why Ultra
    stopped spawning sub-agents on every projected route.

    So the leaf name travels as-is when the wire accepts it. The namespace
    is not lost - flatten_tools already prepends the namespace description
    to each child - and `mapping` keeps the reverse identity either way, so
    output_names still restores namespace and name on the way back.
    Collisions fall back to qualified_name(), which is unique by
    construction; register() owns that decision because only it can see
    what the rest of this request already claimed.
    """
    _check_identity(namespace, name)
    if WIRE_NAME.fullmatch(name):
        return name
    return qualified_name(namespace, name)


def _candidates(namespace, name):
    """Flattened names to try for one tool, most readable first."""
    preferred = tool_name(namespace, name)
    qualified = qualified_name(namespace, name)
    return (preferred,) if preferred == qualified else (preferred, qualified)


def register(mapping, namespace, name):
    identity = {"namespace": namespace, "name": name}
    for mapped in _candidates(namespace, name):
        existing = mapping.get(mapped)
        if existing is None or existing == identity:
            mapping[mapped] = identity
            return mapped
        # A custom-tool marker for the same provider name wins: the plain
        # function identity describes the same provider-side tool, so a
        # returning custom_tool_call history must not clobber the marker
        # that egress conversion needs.
        if (isinstance(existing, dict) and existing.get("custom")
                and existing.get("namespace") == namespace and existing.get("name") == name):
            return mapped
    raise BridgeError("A provider tool name collides after namespace translation.")


# Codex advertises apply_patch as a Lark-grammar custom tool, which no
# third-party Responses provider implements. The bridge projects it as a
# plain function tool carrying the patch in one string parameter, and
# converts calls back to custom_tool_call on the way out so Codex core
# feeds its TurnDiffTracker (the close-out diff card) from real patches.
APPLY_PATCH_TOOL_NAME = "apply_patch"
APPLY_PATCH_PARAM = "patch"

# Codex's freeform tool carries its patch syntax as a Lark grammar, which a
# JSON function cannot forward. Models that were not trained on Codex's
# format (Mistral Medium 3.5 answered with SEARCH/REPLACE blocks and unified
# diffs, which core rejects) need the rules spelled out, so the projected
# description restates the grammar compactly. Kimi already knew the format.
APPLY_PATCH_FORMAT_GUIDE = (
    "Patch format (Codex apply_patch, not a unified diff): the text starts with '*** Begin Patch' and ends with "
    "'*** End Patch'. Each file edit is a header line '*** Update File: relative/path', '*** Add File: relative/path' "
    "or '*** Delete File: relative/path' (paths relative to the workspace root; add '*** Move to: new/path' on the "
    "line after an Update header to rename). An Update section holds one or more hunks that each begin with '@@' "
    "(optionally followed by a nearby function or class line for context), then the changed region as full lines: "
    "unchanged context lines start with a single space, removed lines with '-', added lines with '+'. Include about "
    "three unchanged lines around each change so it matches exactly once. Put each marker at the start of the line it "
    "applies to ('-' directly before the old text, '+' directly before the new text), never on a line of its own; a "
    "blank context line is a single space. Copy every existing line whole, exactly as it appears in the file, however "
    "long it is. An Add section lists every new line with a leading '+'. Never use '--- a/', '+++ b/', line numbers, "
    "or SEARCH/REPLACE markers. Example:\n"
    "*** Begin Patch\n*** Update File: src/app.py\n@@ def greet():\n     name = \"world\"\n-    return \"hi\"\n"
    "+    return f\"hello {name}\"\n*** End Patch"
)


def register_custom(mapping, namespace, name):
    identity = {"namespace": namespace, "name": name, "custom": APPLY_PATCH_TOOL_NAME}
    for mapped in _candidates(namespace, name):
        existing = mapping.get(mapped)
        # The marker replaces a plain identity for the same tool: egress
        # conversion back to custom_tool_call reads the marker, and both
        # describe one provider-side tool.
        if existing is None or existing == identity or existing == {"namespace": namespace, "name": name}:
            mapping[mapped] = identity
            return mapped
    raise BridgeError("A provider tool name collides after namespace translation.")


def is_custom_tool(mapping, provider_name):
    identity = mapping.get(provider_name)
    return isinstance(identity, dict) and identity.get("custom") == APPLY_PATCH_TOOL_NAME


def apply_patch_parameters():
    return {"type": "object",
            "properties": {APPLY_PATCH_PARAM: {
                "type": "string",
                "description": "The complete apply_patch patch text, from '*** Begin Patch' to '*** End Patch'."}},
            "required": [APPLY_PATCH_PARAM]}


def describe_tool(tool, namespace=None) -> str:
    """Name a tool the adapter cannot take, for the client-facing refusal.

    Only the tool's own identity travels: its declared type, its name and
    the namespace it arrived under. Descriptions, schemas and arguments are
    never included, so a refusal cannot leak what the tool would have been
    asked to do.
    """
    if not isinstance(tool, dict):
        return f"a tool of type {type(tool).__name__}"
    kind = tool.get("type")
    kind = str(kind)[:40] if isinstance(kind, str) and kind else "an unnamed type"
    name = tool.get("name")
    where = f" in namespace '{str(namespace)[:40]}'" if isinstance(namespace, str) and namespace else ""
    if isinstance(name, str) and name:
        return f"tool '{name[:60]}' of type '{kind}'{where}"
    return f"a tool of type '{kind}'{where}"


SEARCH_TOOL_TYPE = "web_search"
SEARCH_CONTEXT_SIZES = ("low", "medium", "high")
GOAL_TOOLS = ("create_goal", "update_goal")
GOAL_BUDGET_FIELD = "token_budget"


def strip_goal_budget(tools):
    """Remove the optional goal token budget from Codex's goal tools.

    A Codex goal carries an optional token budget, and `thread_goals`
    stores it NULL when none is given - an unlimited goal is the
    supported state, not a workaround. Codex already asks for that:
    its own schema calls the field "Positive token budget for the new
    goal. Omit unless explicitly requested."

    That instruction is prose, so it holds only as well as the model
    reading it does, and across this catalogue it does not hold evenly.
    A weaker route fills the optional field because it is there, the
    goal starts budgeted, and the run stops at `budget_limited` with
    the objective unfinished - a cap nobody asked for. Deleting the
    property instead is the same intent expressed where it cannot be
    declined: the model has no field to fill, so every provider
    behaves the way the description asked for.

    Only the property is dropped; the tool, its objective and the rest
    of its schema travel untouched, and a route that should still be
    able to set a budget keeps the field by leaving codex_goal_budget
    on. Returns how many tools were changed.
    """
    if not isinstance(tools, list):
        return 0
    changed = 0
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("name") not in GOAL_TOOLS:
            continue
        parameters = tool.get("parameters")
        if not isinstance(parameters, dict):
            continue
        properties = parameters.get("properties")
        if not isinstance(properties, dict) or GOAL_BUDGET_FIELD not in properties:
            continue
        del properties[GOAL_BUDGET_FIELD]
        # Codex declares the budget optional, so this should never fire.
        # Honour it anyway: a required name with no property is a schema
        # some providers reject outright, which would cost the tool call
        # rather than just the field.
        required = parameters.get("required")
        if isinstance(required, list) and GOAL_BUDGET_FIELD in required:
            parameters["required"] = [name for name in required if name != GOAL_BUDGET_FIELD]
        changed += 1
    return changed


def split_hosted_search(tools):
    """Lift a hosted web-search request out of the tools array.

    Codex asks for search the way the OpenAI Responses API does: a hosted
    `web_search` tool that whoever serves the model is expected to run. No
    third-party provider in this catalogue runs OpenAI's hosted tool, so the
    request cannot travel as it stands, and left in the array it only earns
    the whole turn a refusal from flatten_tools. Pulling it out first lets
    the route decide - translate it into the provider's own search where one
    exists, refuse it by name where none does.

    Returns the tools that still need flattening, and a plain description of
    what was asked for when search was asked for at all. The dated and
    preview spellings name the same hosted tool, so all of them are
    recognised. Only a top-level request is lifted: a search tool nested in
    a namespace is not something Codex sends, and guessing at one would be a
    silent reinterpretation of the turn rather than a translation of it.

    `user_location` is deliberately left behind. No provider here accepts an
    equivalent, and it is the one field in the request that describes the
    person rather than the search.
    """
    if not isinstance(tools, list):
        return tools, None
    kept, request = [], None
    for tool in tools:
        kind = tool.get("type") if isinstance(tool, dict) else None
        if not isinstance(kind, str) or not (kind == SEARCH_TOOL_TYPE or kind.startswith(SEARCH_TOOL_TYPE + "_")):
            kept.append(tool)
            continue
        if request is not None:
            continue  # One search per turn; the first spelling wins.
        size = tool.get("search_context_size")
        filters = tool.get("filters") if isinstance(tool.get("filters"), dict) else {}
        allowed = filters.get("allowed_domains")
        request = {
            "context_size": size if size in SEARCH_CONTEXT_SIZES else None,
            "allowed_domains": [domain for domain in allowed if isinstance(domain, str) and domain][:20]
            if isinstance(allowed, list) else [],
        }
    return kept, request


def flatten_tools(tools):
    if not isinstance(tools, list):
        raise BridgeError("tools must be an array.")
    result, mapping = [], {}

    def add(tool, namespace=None, description=""):
        if not isinstance(tool, dict):
            raise BridgeError("Tools must be objects.")
        if tool.get("type") == "namespace" and namespace is None:
            if not isinstance(tool.get("name"), str) or not tool["name"]:
                raise BridgeError("Tool namespaces need a name.")
            children = tool.get("tools")
            if not isinstance(children, list):
                raise BridgeError("A tool namespace must contain tools.")
            for child in children:
                add(child, tool.get("name"), str(tool.get("description", "")))
            return
        if tool.get("type") == "custom":
            if tool.get("name") != APPLY_PATCH_TOOL_NAME:
                raise BridgeError("This Responses route only adapts the apply_patch custom tool. Other free-form tools "
                                  f"need a separate adapter. Rejected {describe_tool(tool, namespace)}.")
            # The incoming Lark-grammar description tells the model not to
            # wrap the patch in JSON; the provider side needs the opposite
            # instruction, so the description is replaced, not forwarded,
            # and the grammar it drops is restated as APPLY_PATCH_FORMAT_GUIDE.
            item = {"type": "function", "name": register_custom(mapping, namespace, tool.get("name")),
                    "description": ((description + "\n") if description else "")
                    + "Edit workspace files with an apply_patch patch. Pass the complete patch text in the 'patch' "
                    + "string parameter. Prefer this tool over shell rewrites for edits: it records the change for review.\n"
                    + APPLY_PATCH_FORMAT_GUIDE,
                    "parameters": apply_patch_parameters()}
            result.append(item)
            return
        if tool.get("type") != "function":
            raise BridgeError("This Responses route accepts function tools and function namespaces. Hosted and free-form "
                              f"tools need a separate adapter. Rejected {describe_tool(tool, namespace)}.")
        item = copy.deepcopy(tool)
        item["name"] = register(mapping, namespace, tool.get("name"))
        if description:
            item["description"] = description + "\n" + str(item.get("description", ""))
        result.append(item)
    for tool in tools:
        add(tool)
    return result, mapping


def input_names(items, mapping):
    if not isinstance(items, list):
        return items
    for item in items:
        if isinstance(item, dict) and item.get("type") == "function_call":
            item["name"] = register(mapping, item.pop("namespace", None), item.get("name"))
    return items


def output_names(item, mapping):
    if not isinstance(item, dict) or item.get("type") != "function_call" or item.get("namespace"):
        return item
    identity = mapping.get(item.get("name"))
    if identity:
        item["name"] = identity["name"]
        if identity["namespace"] is not None:
            item["namespace"] = identity["namespace"]
    return item


def normalize_custom_calls(items, mapping):
    """Rewrite custom_tool_call history to provider-side function items.

    Runs at ingress before the history whitelist, so providers only ever
    see function_call/function_call_output. The namespace is left for
    input_names so namespaced tools keep their reversible mapping.
    """
    if not isinstance(items, list):
        return items
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "custom_tool_call":
            if item.get("name") != APPLY_PATCH_TOOL_NAME:
                raise BridgeError("This Responses route only adapts the apply_patch custom tool. Other free-form tools need a separate adapter.")
            mapped = register_custom(mapping, item.get("namespace"), item.get("name"))
            patch = item.pop("input", "")
            if not isinstance(patch, str):
                patch = json.dumps(patch, ensure_ascii=False)
            item["type"] = "function_call"
            item["name"] = mapped
            item["arguments"] = json.dumps({APPLY_PATCH_PARAM: patch}, ensure_ascii=False)
        elif kind == "custom_tool_call_output":
            output = item.get("output", "")
            if not isinstance(output, str):
                item["output"] = json.dumps(output, ensure_ascii=False)
            item["type"] = "function_call_output"
    return items


_PATCH_BEGIN = "*** Begin Patch"
_PATCH_END = "*** End Patch"
_PATCH_EOF = "*** End of File"
_UPDATE_FILE = "*** Update File: "
_ADD_FILE = "*** Add File: "
_DELETE_FILE = "*** Delete File: "
_REPLACE_SIMILARITY = 0.6


def _unified_diff_header(line, path):
    """True for a '--- a/path' or '+++ b/path' line that names the section's file."""
    for prefix in ("--- ", "+++ "):
        if not line.startswith(prefix):
            continue
        target = line[len(prefix):].strip()
        if target == "/dev/null":
            return True
        candidates = {target}
        if target[:2] in ("a/", "b/"):
            candidates.add(target[2:])
        return bool(path) and any(
            candidate == path or path.endswith("/" + candidate) or candidate.endswith("/" + path)
            for candidate in candidates if candidate)
    return False


def _similar(left, right):
    import difflib
    return left[:24] == right[:24] or difflib.SequenceMatcher(None, left, right).ratio() >= _REPLACE_SIMILARITY


def repair_apply_patch(text):
    """Mend the syntax slips chat models make in Codex's apply_patch format.

    Codex core verifies every patch against the workspace before applying
    it, so a repair can only turn a certain rejection into a candidate; lines
    that already parse are never changed. Inside an Update section a bare
    line becomes a context line; a '-' on a line of its own after a bare line
    marks that line as removed; a lone '-' between a context line and a
    similar '+' line marks the context line as the removed one (a replace
    written with the marker detached, Mistral Medium 3.5's habit); and stray
    '--- a/' / '+++ b/' headers for the section's file are dropped. Inside an
    Add section bare lines get their '+'. Everything else passes through.
    """
    if not isinstance(text, str) or _PATCH_BEGIN not in text:
        return text
    lines = text.split("\n")
    out, bare = [], []
    section, path = None, None
    for index, line in enumerate(lines):
        header = line.rstrip()
        if header.startswith("*** "):
            if header.startswith(_UPDATE_FILE):
                section, path = "update", header[len(_UPDATE_FILE):].strip()
            elif header.startswith(_ADD_FILE):
                section, path = "add", header[len(_ADD_FILE):].strip()
            elif header.startswith(_DELETE_FILE):
                section, path = "delete", None
            elif header in (_PATCH_END, _PATCH_EOF):
                section = None if header == _PATCH_END else section
            out.append(line); bare.append(False)
            continue
        if section == "update":
            if header == "@@" or header.startswith("@@ "):
                out.append(line); bare.append(False)
                continue
            if _unified_diff_header(header, path):
                continue
            if header == "-" and out:
                previous = out[-1]
                following = lines[index + 1] if index + 1 < len(lines) else ""
                if bare[-1]:
                    out[-1] = "-" + previous[1:]
                    bare[-1] = False
                    continue
                if previous.startswith(" ") and following.startswith("+") and _similar(previous[1:], following[1:]):
                    out[-1] = "-" + previous[1:]
                    continue
            if line == "" or line[:1] in (" ", "+", "-"):
                out.append(line); bare.append(False)
                continue
            out.append(" " + line); bare.append(True)
            continue
        if section == "add":
            if line.startswith("+"):
                out.append(line)
            else:
                out.append("+" + line)
            bare.append(False)
            continue
        out.append(line); bare.append(False)
    return "\n".join(out)


def extract_patch(arguments):
    """Unwrap the provider-side patch argument back to raw patch text."""
    if not isinstance(arguments, str):
        return "" if arguments is None else str(arguments)
    try:
        value = json.loads(arguments)
    except ValueError:
        # Lenient: a model that sent raw patch text instead of JSON still
        # yields a usable patch for Codex core to parse or reject itself.
        return repair_apply_patch(arguments)
    if isinstance(value, dict) and isinstance(value.get(APPLY_PATCH_PARAM), str):
        return repair_apply_patch(value[APPLY_PATCH_PARAM])
    if isinstance(value, str):
        return repair_apply_patch(value)
    return arguments


def restore_custom_call(item, mapping):
    """Convert a mapped provider function_call back to custom_tool_call.

    Returns True when the item was converted. Call before output_names:
    the provider-side name is the mapping key, and output_names skips
    anything that is no longer a function_call.
    """
    if not isinstance(item, dict) or item.get("type") != "function_call":
        return False
    provider_name = item.get("name")
    if not is_custom_tool(mapping, provider_name):
        return False
    item["type"] = "custom_tool_call"
    item["name"] = mapping[provider_name]["name"]
    item.pop("namespace", None)
    item["input"] = extract_patch(item.pop("arguments", ""))
    return True
