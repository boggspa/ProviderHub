"""Flatten Codex tool namespaces for native provider function APIs."""
import copy
import hashlib
import json
import re

from bridge_core import BridgeError


def tool_name(namespace, name):
    if not isinstance(name, str) or not name:
        raise BridgeError("Function tools need a name.")
    if namespace is not None and (not isinstance(namespace, str) or not namespace):
        raise BridgeError("Tool namespaces need a name.")
    if namespace is None and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
        return name
    identity = json.dumps([namespace, name], separators=(",", ":"))
    prefix = re.sub(r"[^A-Za-z0-9_-]", "_", (namespace or "") + "_" + name)[:42]
    return "ph_" + prefix + "_" + hashlib.sha256(identity.encode()).hexdigest()[:16]


def register(mapping, namespace, name):
    mapped = tool_name(namespace, name)
    identity = {"namespace": namespace, "name": name}
    existing = mapping.get(mapped)
    if existing is not None and existing != identity:
        # A custom-tool marker for the same provider name wins: the plain
        # function identity describes the same provider-side tool, so a
        # returning custom_tool_call history must not clobber the marker
        # that egress conversion needs.
        if (isinstance(existing, dict) and existing.get("custom")
                and existing.get("namespace") == namespace and existing.get("name") == name):
            return mapped
        raise BridgeError("A provider tool name collides after namespace translation.")
    mapping[mapped] = identity
    return mapped


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
    "three unchanged lines around each change so it matches exactly once. An Add section lists every new line with a "
    "leading '+'. Never use '--- a/', '+++ b/', line numbers, or SEARCH/REPLACE markers. Example:\n"
    "*** Begin Patch\n*** Update File: src/app.py\n@@ def greet():\n     name = \"world\"\n-    return \"hi\"\n"
    "+    return f\"hello {name}\"\n*** End Patch"
)


def register_custom(mapping, namespace, name):
    mapped = tool_name(namespace, name)
    identity = {"namespace": namespace, "name": name, "custom": APPLY_PATCH_TOOL_NAME}
    if mapped in mapping and mapping[mapped] != identity:
        raise BridgeError("A provider tool name collides after namespace translation.")
    mapping[mapped] = identity
    return mapped


def is_custom_tool(mapping, provider_name):
    identity = mapping.get(provider_name)
    return isinstance(identity, dict) and identity.get("custom") == APPLY_PATCH_TOOL_NAME


def apply_patch_parameters():
    return {"type": "object",
            "properties": {APPLY_PATCH_PARAM: {
                "type": "string",
                "description": "The complete apply_patch patch text, from '*** Begin Patch' to '*** End Patch'."}},
            "required": [APPLY_PATCH_PARAM]}


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
                raise BridgeError("This Responses route only adapts the apply_patch custom tool. Other free-form tools need a separate adapter.")
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
            raise BridgeError("This Responses route accepts function tools and function namespaces. Hosted and free-form tools need a separate adapter.")
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


def extract_patch(arguments):
    """Unwrap the provider-side patch argument back to raw patch text."""
    if not isinstance(arguments, str):
        return "" if arguments is None else str(arguments)
    try:
        value = json.loads(arguments)
    except ValueError:
        # Lenient: a model that sent raw patch text instead of JSON still
        # yields a usable patch for Codex core to parse or reject itself.
        return arguments
    if isinstance(value, dict) and isinstance(value.get(APPLY_PATCH_PARAM), str):
        return value[APPLY_PATCH_PARAM]
    if isinstance(value, str):
        return value
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
