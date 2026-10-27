"""Lean native shared WKWebView browser for Chat.

This is a thin Python tool surface around a host-owned WKWebView. The Python
worker never talks to WebKit; it describes and validates actions, then hands
them to an injected ``native_call`` bridge that returns a normalised
JSON-compatible tool result. The worker thread blocks while the Swift side
runs the action asynchronously on the UI, so the host always stays in charge
of cancellation, approval semantics and image persistence.

Each chat has a set of browser tabs. The Browser pane displays the selected
tab; tools can address another tab without changing the user's selection.
Tools never get to inject arbitrary JavaScript: DOM observation runs in a
fixed isolated-world script bundled with the host, and each call returns a
snapshot id plus refs that become stale on navigation or belong to another tab.

Wire protocol (the parent ``native_call`` implements):

    request  = {"id": <opaque str>, "chat": <chat_id>, "tool": <name>,
                "arguments": {...}}    # sent on the JSONL worker pipe
    response = {"id": <opaque str>, "chat": <chat_id>,
                "result": {"content": [...], "is_error": bool,
                           "summary": str, "changed_files": [...]}}
    cancel   = {"id": <opaque str>, "chat": <chat_id>}   # separate event

The parent ``native_call`` is expected to accept ``(chat_id, tool, arguments,
cancel_event, request_id)`` so it can wrap the call into the
``native_action`` wire record. ``request_id`` is the opaque id the host
wrote on the wire (``native_action.id``); cancellation targets that exact
task in the Swift ``ChatBrowserStore``. The Python side does not depend on
``request_id`` for its own correctness; it forwards the value so the
bridge layer can plumb it without changing the call signature.

Only ``http`` and ``https`` URLs are accepted and the URL must round-trip
through :func:`urllib.parse.urlsplit` with a non-empty hostname. ``file://``,
``javascript:``, ``data:`` and ``app:`` schemes are refused here, before the
bridge is called. Unknown keys and unknown action kinds are hard errors, never
silently normalised.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit


# --- Tool definitions -------------------------------------------------------

MAX_URL = 4096
MAX_DEPTH = 8
MAX_OBSERVATION_BYTES = 200_000
MAX_REF_LEN = 128
MAX_SELECTOR_LEN = 512
MAX_ACTION_VALUE_LEN = 4096
MAX_KEY_LEN = 64
MAX_ACTIONS = 32
MAX_SCREENSHOT_EDGE = 4096
MIN_SCREENSHOT_EDGE = 64
DEFAULT_SCREENSHOT_WIDTH = 1280
DEFAULT_SCREENSHOT_HEIGHT = 1280
# Snapshot ids are opaque strings the host attaches to every observation. They
# must be safe to embed in a JSON object and short enough to round-trip freely.
MAX_SNAPSHOT_ID_LEN = 64
_SNAPSHOT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,%d}\Z" % MAX_SNAPSHOT_ID_LEN)
MAX_TAB_ID_LEN = 64
_TAB_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,%d}\Z" % MAX_TAB_ID_LEN)
TAB_ID_SCHEMA = {"type": "string", "minLength": 1, "maxLength": MAX_TAB_ID_LEN}

ALLOWED_SCHEMES = ("http", "https")

_ACTION_KINDS = (
    "click", "type", "select", "focus", "hover",
    "scroll", "press", "wait_for_text", "wait_for_selector",
)
_ACTIONS_NEED_REF = frozenset({"click", "type", "select", "focus", "hover"})
_ACTIONS_NEED_SELECTOR = frozenset({"wait_for_selector"})
_ACTION_KIND_SET = frozenset(_ACTION_KINDS)
_REF_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,%d}\Z" % MAX_REF_LEN)


def _tool(name, description, properties, required):
    return {
        "name": name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


TOOL_DEFINITIONS = [
    _tool(
        "browser_open",
        "Open a URL in a new browser tab. Existing tabs stay open and the "
        "visible selection is preserved. Use the returned tab_id for later "
        "observe, action and screenshot calls. Supply tab_id to reuse that "
        "tab, or set new_tab to false to reuse your last tab. Only http and https "
        "URLs are accepted.",
        {
            "url": {"type": "string", "minLength": 1, "maxLength": MAX_URL},
            "tab_id": TAB_ID_SCHEMA,
            "new_tab": {"type": "boolean"},
        },
        ["url"],
    ),
    _tool(
        "browser_navigate",
        "Navigate the selected or identified browser tab to a new URL. Returns the "
        "new snapshot id, the URL that actually loaded after redirects, and "
        "a stale flag if any prior observe snapshot is no longer current.",
        {
            "url": {"type": "string", "minLength": 1, "maxLength": MAX_URL},
            "tab_id": TAB_ID_SCHEMA,
        },
        ["url"],
    ),
    _tool(
        "browser_list",
        "List the chat's browser tabs and the selected tab, including each "
        "tab's id, title, URL and snapshot id. No action runs.",
        {},
        [],
    ),
    _tool(
        "browser_new",
        "Create an empty browser tab without changing the visible selection. "
        "Use the returned tab_id to navigate it.",
        {},
        [],
    ),
    _tool(
        "browser_select",
        "Select an existing browser tab for the visible Browser pane.",
        {"tab_id": TAB_ID_SCHEMA},
        ["tab_id"],
    ),
    _tool(
        "browser_observe",
        "Capture a JSON DOM snapshot of the current page through a fixed "
        "isolated-world script bundled with the host. The snapshot is the "
        "only handle other browser_* calls may reference; refs are tagged "
        "with the snapshot id and become stale when the page navigates. The "
        "page never sees any user JavaScript.",
        {
            "depth": {"type": "integer", "minimum": 0, "maximum": MAX_DEPTH},
            "max_bytes": {"type": "integer", "minimum": 1024, "maximum": MAX_OBSERVATION_BYTES},
            "snapshot_id": {"type": "string"},
            "tab_id": TAB_ID_SCHEMA,
        },
        [],
    ),
    _tool(
        "browser_act",
        "Run qualified DOM actions against the current page using refs "
        "returned from a fresh browser_observe call. Stale refs are "
        "rejected. Tools never execute user JavaScript. Supported kinds: "
        "click, type, select, focus, hover, scroll, press (keyboard key), "
        "wait_for_text, wait_for_selector.",
        {
            "snapshot_id": {"type": "string"},
            "tab_id": TAB_ID_SCHEMA,
            "actions": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_ACTIONS,
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "enum": list(_ACTION_KINDS)},
                        "ref": {"type": "string"},
                        "selector": {"type": "string"},
                        "value": {"type": "string"},
                        "key": {"type": "string"},
                        "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
                        "timeout_ms": {"type": "integer", "minimum": 0, "maximum": 30000},
                    },
                    "required": ["kind"],
                    "additionalProperties": False,
                },
            },
        },
        ["snapshot_id", "actions"],
    ),
    _tool(
        "browser_screenshot",
        "Return a PNG screenshot of the current page (bounded dimensions) "
        "captured from the same WKWebView used by browser_observe.",
        {
            "max_width": {"type": "integer", "minimum": MIN_SCREENSHOT_EDGE, "maximum": MAX_SCREENSHOT_EDGE},
            "max_height": {"type": "integer", "minimum": MIN_SCREENSHOT_EDGE, "maximum": MAX_SCREENSHOT_EDGE},
            "tab_id": TAB_ID_SCHEMA,
        },
        [],
    ),
    _tool(
        "browser_close",
        "Close the selected or identified browser tab and drop its snapshot "
        "state. Other tabs remain open. Requires approval because it discards "
        "the user's browsing state for that tab.",
        {"tab_id": TAB_ID_SCHEMA},
        [],
    ),
]

TOOL_NAMES = frozenset(t["name"] for t in TOOL_DEFINITIONS)


# --- Validation helpers -----------------------------------------------------

def _validate_url(value, field):
    if not isinstance(value, str) or not value:
        raise ValueError("%s must be a nonempty string" % field)
    if len(value) > MAX_URL:
        raise ValueError("%s exceeds %d characters" % (field, MAX_URL))
    # Validate the URL with urlsplit so scheme/host/port are real, not just
    # the textual prefix. Reject any scheme other than http(s), embedded
    # credentials, and hostnames with whitespace/control bytes.
    try:
        parts = urlsplit(value)
    except ValueError:
        raise ValueError("%s is not a valid URL" % field)
    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise ValueError("Only http and https URLs are accepted")
    if not parts.hostname:
        raise ValueError("%s is missing a hostname" % field)
    if any((ord(c) < 0x21 or ord(c) == 0x7f) for c in parts.hostname):
        raise ValueError("%s contains invalid characters in hostname" % field)
    if parts.username or parts.password:
        raise ValueError("%s must not embed credentials" % field)
    return value


def _integer(args, key, *, minimum, maximum, default=None):
    value = args.get(key, default)
    if value is None:
        if default is None:
            raise ValueError("%s is required" % key)
        return default
    if type(value) is not int or isinstance(value, bool):
        raise ValueError("%s must be an integer" % key)
    if value < minimum or value > maximum:
        raise ValueError("%s must be in [%d, %d]" % (key, minimum, maximum))
    return value


def _validate_snapshot_id(value, field):
    if not isinstance(value, str) or not _SNAPSHOT_ID_PATTERN.match(value):
        raise ValueError("%s must match an opaque snapshot id" % field)
    return value


def _validate_actions(actions):
    if not isinstance(actions, list) or not actions:
        raise ValueError("actions must be a nonempty array")
    if len(actions) > MAX_ACTIONS:
        raise ValueError("actions exceeds %d entries" % MAX_ACTIONS)
    allowed_keys = {"kind", "ref", "selector", "value", "key", "direction", "timeout_ms"}
    for index, action in enumerate(actions):
        if not isinstance(action, dict):
            raise ValueError("actions[%d] must be an object" % index)
        kind = action.get("kind")
        if not isinstance(kind, str) or kind not in _ACTION_KIND_SET:
            raise ValueError("actions[%d].kind is not supported" % index)
        extras = set(action) - allowed_keys
        if extras:
            raise ValueError("actions[%d] has unsupported keys: %s" % (index, sorted(extras)))
        if kind in _ACTIONS_NEED_REF:
            ref = action.get("ref")
            if not isinstance(ref, str) or not _REF_PATTERN.match(ref):
                raise ValueError("actions[%d].ref must match a fresh observation ref" % index)
        if kind in _ACTIONS_NEED_SELECTOR:
            selector = action.get("selector")
            if not isinstance(selector, str) or not selector or len(selector) > MAX_SELECTOR_LEN:
                raise ValueError(
                    "actions[%d].selector must be a nonempty string of at most %d characters"
                    % (index, MAX_SELECTOR_LEN)
                )
        if kind == "type":
            value = action.get("value")
            if not isinstance(value, str) or not value or len(value) > MAX_ACTION_VALUE_LEN:
                raise ValueError(
                    "actions[%d].value must be a nonempty string of at most %d characters"
                    % (index, MAX_ACTION_VALUE_LEN)
                )
            ref = action.get("ref")
            if not isinstance(ref, str) or not _REF_PATTERN.match(ref):
                raise ValueError("actions[%d].ref must be a fresh observation ref" % index)
        if kind == "select":
            value = action.get("value")
            if not isinstance(value, str) or not value or len(value) > MAX_ACTION_VALUE_LEN:
                raise ValueError(
                    "actions[%d].value must be a nonempty string of at most %d characters"
                    % (index, MAX_ACTION_VALUE_LEN)
                )
            ref = action.get("ref")
            if not isinstance(ref, str) or not _REF_PATTERN.match(ref):
                raise ValueError("actions[%d].ref must be a fresh observation ref" % index)
        if kind == "press":
            key = action.get("key")
            if not isinstance(key, str) or not key or len(key) > MAX_KEY_LEN:
                raise ValueError(
                    "actions[%d].key must be a nonempty string of at most %d characters"
                    % (index, MAX_KEY_LEN)
                )
        if kind == "scroll":
            direction = action.get("direction")
            if direction not in ("up", "down", "left", "right"):
                raise ValueError("actions[%d].direction must be one of up/down/left/right" % index)
            target = action.get("ref")
            if target is not None and not (isinstance(target, str) and _REF_PATTERN.match(target)):
                raise ValueError("actions[%d].ref must be a fresh observation ref when provided" % index)
        if "timeout_ms" in action:
            timeout = action["timeout_ms"]
            if type(timeout) is not int or isinstance(timeout, bool) or timeout < 0 or timeout > 30000:
                raise ValueError("actions[%d].timeout_ms must be an integer in [0, 30000]" % index)


def _validate_args(name, args):
    """Strict per-tool validation. Unknown keys raise ValueError, never get
    silently dropped, so the chat_runtime can surface them as user errors."""
    if not isinstance(args, dict):
        raise ValueError("Browser tool arguments must be an object")
    if "tab_id" in args and not (isinstance(args["tab_id"], str) and _TAB_ID_PATTERN.fullmatch(args["tab_id"])):
        raise ValueError("tab_id must be a valid browser tab identifier")
    if name == "browser_open":
        if "url" not in args or set(args) - {"url", "tab_id", "new_tab"}:
            raise ValueError("browser_open requires 'url' and accepts only 'tab_id' and 'new_tab'")
        if "new_tab" in args and type(args["new_tab"]) is not bool:
            raise ValueError("new_tab must be a boolean")
        if args.get("new_tab") is True and "tab_id" in args:
            raise ValueError("new_tab cannot be combined with tab_id")
        _validate_url(args["url"], "url")
        return
    if name == "browser_navigate":
        if "url" not in args or set(args) - {"url", "tab_id"}:
            raise ValueError("browser_navigate requires 'url' and accepts 'tab_id'")
        _validate_url(args["url"], "url")
        return
    if name in {"browser_list", "browser_new"}:
        if args:
            raise ValueError("%s takes no arguments" % name)
        return
    if name == "browser_select":
        if set(args) != {"tab_id"}:
            raise ValueError("browser_select requires 'tab_id'")
        return
    if name == "browser_observe":
        unknown = set(args) - {"depth", "max_bytes", "snapshot_id", "tab_id"}
        if unknown:
            raise ValueError("browser_observe has unsupported keys: %s" % sorted(unknown))
        _integer(args, "depth", minimum=0, maximum=MAX_DEPTH, default=3)
        _integer(args, "max_bytes", minimum=1024, maximum=MAX_OBSERVATION_BYTES,
                 default=MAX_OBSERVATION_BYTES)
        if "snapshot_id" in args:
            _validate_snapshot_id(args["snapshot_id"], "snapshot_id")
        return
    if name == "browser_act":
        unknown = set(args) - {"snapshot_id", "actions", "tab_id"}
        if unknown:
            raise ValueError("browser_act has unsupported keys: %s" % sorted(unknown))
        if "snapshot_id" not in args:
            raise ValueError("browser_act requires snapshot_id from a fresh observe call")
        _validate_snapshot_id(args["snapshot_id"], "snapshot_id")
        if "actions" not in args:
            raise ValueError("browser_act requires 'actions'")
        _validate_actions(args["actions"])
        return
    if name == "browser_screenshot":
        unknown = set(args) - {"max_width", "max_height", "tab_id"}
        if unknown:
            raise ValueError("browser_screenshot has unsupported keys: %s" % sorted(unknown))
        _integer(args, "max_width", minimum=MIN_SCREENSHOT_EDGE,
                 maximum=MAX_SCREENSHOT_EDGE, default=DEFAULT_SCREENSHOT_WIDTH)
        _integer(args, "max_height", minimum=MIN_SCREENSHOT_EDGE,
                 maximum=MAX_SCREENSHOT_EDGE, default=DEFAULT_SCREENSHOT_HEIGHT)
        return
    if name == "browser_close":
        if set(args) - {"tab_id"}:
            raise ValueError("browser_close only accepts 'tab_id'")
        return
    raise ValueError("Unknown browser tool")


# --- Public describe / execute ---------------------------------------------

def describe(name, args):
    """Validate arguments and return a normalised summary for the host.

    Returns the same shape as ``chat_tools.ChatToolRunner.describe`` so the
    chat_runtime can route approval without special-casing browser tools.
    """
    if not isinstance(name, str) or name not in TOOL_NAMES:
        raise ValueError("Unknown browser tool")
    _validate_args(name, args)
    if name == "browser_open":
        return {"summary": "Open browser: %s" % args["url"],
                "requires_approval": True}
    if name == "browser_navigate":
        return {"summary": "Navigate browser: %s" % args["url"],
                "requires_approval": True}
    if name == "browser_list":
        return {"summary": "List browser sessions",
                "requires_approval": False}
    if name == "browser_new":
        return {"summary": "New browser tab", "requires_approval": False}
    if name == "browser_select":
        return {"summary": "Select browser tab", "requires_approval": False}
    if name == "browser_observe":
        depth = args.get("depth", 3)
        return {"summary": "Observe browser (depth %d)" % depth,
                "requires_approval": False}
    if name == "browser_act":
        n = len(args["actions"])
        return {"summary": "Act on browser: %d action%s" % (
            n, "s" if n != 1 else ""),
            "requires_approval": True}
    if name == "browser_screenshot":
        return {"summary": "Screenshot browser",
                "requires_approval": False}
    if name == "browser_close":
        return {"summary": "Close browser",
                "requires_approval": True}
    raise ValueError("Unknown browser tool")


class ChatBrowserTools:
    """Stateless tool surface that hands actions to a host-injected bridge.

    The host decides whether the bridge is in-process, an XPC call, or a
    JSONL round trip. This class only validates arguments and shapes the
    result; it never holds a WKWebView.
    """

    def __init__(self, native_call):
        if not callable(native_call):
            raise ValueError("native_call must be a callable")
        self.native_call = native_call

    def describe(self, name, args):
        description = describe(name, args)
        description["name"] = name
        return description

    def execute(self, name, args, chat_id=None, cancel_event=None,
                request_id=None):
        description = describe(name, args)
        if cancel_event is not None and cancel_event.is_set():
            return {
                "content": [{"type": "text", "text": "Browser action cancelled before dispatch."}],
                "is_error": True,
                "summary": description["summary"],
                "changed_files": [],
            }
        arguments = dict(args)
        # ``request_id`` is the opaque id the host wrote on the wire
        # (``native_action.id``). It lets the Swift store target cancellation
        # at the exact in-flight task. The Python side does not need it for
        # correctness, but forwards it so the bridge layer can plumb it
        # through without changing its own native_call signature.
        return self.native_call(chat_id, name, arguments, cancel_event,
                                request_id)
