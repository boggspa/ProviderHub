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
    if mapped in mapping and mapping[mapped] != identity:
        raise BridgeError("A provider tool name collides after namespace translation.")
    mapping[mapped] = identity
    return mapped


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
