"""Chat-owned optional plugin registry. The host retains approval authority.

Configuration and enablement never launch a process. start() is explicit.
External tools ALWAYS require the host's normal approval gate; upstream
readOnlyHint/trust annotations cannot downgrade that requirement.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import selectors
import subprocess
import threading
import time

from chat_plugin_mcp import MCPError, StdioMCPClient, MAX_MESSAGE, MAX_TOOLS

NAME = re.compile(r"[a-zA-Z][a-zA-Z0-9_]{0,39}\Z")


def peekaboo_config(executable, *, executable_sha256=None):
    """Native binary defaults from the reviewed release lock, with no launch."""
    manifest = json.loads(Path(__file__).with_name("plugin-dependencies.lock.json").read_text())
    pin = manifest["plugins"]["peekaboo"]
    return PluginConfig("peekaboo", executable, tuple(pin["command_args"]), pin["version"],
                        executable_sha256, pin["minimum_macos"])


@dataclass(frozen=True)
class PluginConfig:
    id: str
    executable: str
    args: tuple[str, ...] = ()
    expected_version: str = ""
    executable_sha256: str | None = None
    minimum_macos: str | None = None

    def __post_init__(self):
        if not NAME.fullmatch(self.id) or "__" in self.id:
            raise ValueError("Invalid plugin id")
        if not isinstance(self.executable, str) or not os.path.isabs(self.executable):
            raise ValueError("Configure an exact absolute executable path")
        # Homebrew commonly supplies symlinks. Resolve once at configuration;
        # never follow a subsequently retargeted launcher at dispatch/start.
        object.__setattr__(self, "executable", str(Path(self.executable).resolve()))
        if len(self.args) > 32 or any(not isinstance(x, str) or "\0" in x or len(x) > 4096 for x in self.args):
            raise ValueError("Invalid plugin arguments")
        object.__setattr__(self, "args", tuple(self.args))
        if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?", self.expected_version):
            raise ValueError("An exact expected semantic version is required")
        if self.executable_sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", self.executable_sha256):
            raise ValueError("Invalid executable SHA256")
        if self.minimum_macos is not None and not re.fullmatch(r"\d+\.\d+", self.minimum_macos):
            raise ValueError("Invalid minimum macOS version")


def validate_arguments(value, schema, depth=0):
    """Fail-closed small JSON Schema subset; unsupported assertions reject calls.

    References and complex schemas require an SDK-backed adapter in future.
    They cannot silently bypass argument checking in this lean adapter.
    """
    if depth > 24 or not isinstance(schema, dict):
        raise ValueError("Invalid or excessively nested tool schema")
    supported = {"type", "properties", "required", "additionalProperties", "items", "enum",
                 "const", "minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems",
                 "anyOf", "oneOf", "allOf", "uniqueItems", "description", "title", "default", "examples",
                 "$schema", "$id", "$comment", "deprecated", "readOnly", "writeOnly"}
    if set(schema) - supported:
        raise ValueError("Unsupported tool schema assertion: " + sorted(set(schema) - supported)[0])
    types = {"object": lambda v: isinstance(v, dict), "array": lambda v: isinstance(v, list),
             "string": lambda v: isinstance(v, str), "boolean": lambda v: type(v) is bool,
             "integer": lambda v: type(v) is int, "number": lambda v: type(v) in (int, float) and math.isfinite(v),
             "null": lambda v: v is None}
    expected = schema.get("type")
    if expected is not None:
        choices = expected if isinstance(expected, list) else [expected]
        if not choices or any(t not in types for t in choices) or not any(types[t](value) for t in choices):
            raise ValueError("Tool argument has wrong type")
    for key in ("anyOf", "oneOf", "allOf"):
        if key in schema:
            count = 0
            for child in schema[key]:
                try:
                    validate_arguments(value, child, depth + 1)
                    count += 1
                except ValueError:
                    pass
            if (key == "anyOf" and not count or key == "oneOf" and count != 1 or
                    key == "allOf" and count != len(schema[key])):
                raise ValueError("Tool argument fails schema alternatives")
    if "enum" in schema and not any(type(value) is type(x) and value == x for x in schema["enum"]):
        raise ValueError("Tool argument outside enum")
    if "const" in schema and (type(value) is not type(schema["const"]) or value != schema["const"]):
        raise ValueError("Tool argument differs from const")
    if isinstance(value, dict):
        if any(x not in value for x in schema.get("required", [])):
            raise ValueError("Missing required tool argument")
        properties = schema.get("properties", {})
        for key, item in value.items():
            child = properties.get(key, schema.get("additionalProperties", True))
            if child is False:
                raise ValueError("Unknown tool argument: " + key)
            if isinstance(child, dict):
                validate_arguments(item, child, depth + 1)
    if isinstance(value, list):
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", MAX_MESSAGE):
            raise ValueError("Tool array size outside bounds")
        unique = schema.get("uniqueItems", False)
        if type(unique) is not bool:
            raise ValueError("Invalid uniqueItems assertion")
        if unique:
            def identity(item):
                if isinstance(item, dict):
                    return ("object", tuple((k, identity(v)) for k, v in sorted(item.items())))
                if isinstance(item, list): return ("array", tuple(identity(v) for v in item))
                if type(item) in (int, float): return ("number", item)
                return (type(item).__name__, item)
            if len({identity(item) for item in value}) != len(value):
                raise ValueError("Tool array requires unique items")
        if "items" in schema:
            for item in value:
                validate_arguments(item, schema["items"], depth + 1)
    if isinstance(value, str) and not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", MAX_MESSAGE):
        raise ValueError("Tool string size outside bounds")
    if type(value) in (int, float) and (not math.isfinite(value) or not schema.get("minimum", -math.inf) <= value <= schema.get("maximum", math.inf)):
        raise ValueError("Tool number outside bounds")


class PluginRegistry:
    def __init__(self, storage_dir, *, client_factory=StdioMCPClient, load_saved=True):
        self.path = Path(storage_dir) / "plugins.json"
        self._lock = threading.RLock()
        self._configs, self._enabled, self._clients, self._tools, self._generation = {}, {}, {}, {}, {}
        self._factory = client_factory
        if load_saved and self.path.exists():
            if self.path.is_symlink():
                raise ValueError("Plugin configuration must not be a symbolic link")
            if self.path.stat().st_size > MAX_MESSAGE:
                raise ValueError("Plugin configuration exceeds limit")
            data = json.loads(self.path.read_text())
            if data.get("version") != 1 or not isinstance(data.get("plugins"), list) or len(data["plugins"]) > 32:
                raise ValueError("Invalid plugin configuration")
            for entry in data["plugins"]:
                config = PluginConfig(**entry["config"])
                pin = self._dependency(config.id)
                if pin and (config.expected_version != pin["version"] or config.minimum_macos != pin["minimum_macos"]):
                    raise ValueError("Stored plugin does not match the reviewed dependency lock")
                if config.id in self._configs or type(entry["enabled"]) is not bool:
                    raise ValueError("Duplicate or invalid plugin configuration")
                self._configs[config.id], self._enabled[config.id] = config, entry["enabled"]

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        import tempfile
        fd, name = tempfile.mkstemp(prefix=".plugins-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump({"version": 1, "plugins": [{"config": asdict(c), "enabled": self._enabled[i]}
                           for i, c in self._configs.items()]}, stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def configure(self, config):
        if not isinstance(config, PluginConfig):
            raise ValueError("Expected PluginConfig")
        pin = self._dependency(config.id)
        if pin and (config.expected_version != pin["version"] or config.minimum_macos != pin["minimum_macos"]):
            raise ValueError("Plugin version and runtime must match the reviewed dependency lock")
        with self._lock:
            if config.id not in self._configs and len(self._configs) >= 32:
                raise ValueError("Too many plugins")
            self._retire(config.id)
            self._configs[config.id] = config
            self._enabled[config.id] = False
            self._save()

    def _retire(self, plugin_id):
        self._generation[plugin_id] = self._generation.get(plugin_id, 0) + 1
        client = self._clients.pop(plugin_id, None)
        self._tools.pop(plugin_id, None)
        if client:
            client.close()

    def set_enabled(self, plugin_id, enabled):
        if type(enabled) is not bool:
            raise ValueError("Enabled must be boolean")
        with self._lock:
            if plugin_id not in self._configs:
                raise KeyError(plugin_id)
            if not enabled:
                self._retire(plugin_id)
            self._enabled[plugin_id] = enabled
            self._save()

    def _availability(self, config):
        if str(Path(config.executable).resolve()) != config.executable:
            return "executable_path_changed"
        if config.minimum_macos:
            if platform.system() != "Darwin":
                return "requires_macos"
            current = tuple(int(x) for x in platform.mac_ver()[0].split(".")[:2])
            if current < tuple(map(int, config.minimum_macos.split("."))):
                return "unsupported_macos"
        if not Path(config.executable).is_file() or not os.access(config.executable, os.X_OK):
            return "missing_executable"
        if config.executable_sha256:
            with open(config.executable, "rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if digest != config.executable_sha256:
                return "executable_hash_mismatch"
        return "available"

    def health(self):
        with self._lock:
            return [{"id": i, "enabled": self._enabled[i], "availability": self._availability(c),
                     "running": i in self._clients and self._clients[i].running,
                     "expected_version": c.expected_version, "executable": c.executable,
                     "dependency": self._dependency(i), "executable_sha256": c.executable_sha256}
                    for i, c in self._configs.items()]

    def _dependency(self, plugin_id):
        manifest = json.loads(Path(__file__).with_name("plugin-dependencies.lock.json").read_text())
        return copy.deepcopy(manifest["plugins"].get(plugin_id, {}))

    def start(self, plugin_id, cancel_event=None):
        if cancel_event is not None and cancel_event.is_set():
            raise MCPError("Plugin startup cancelled")
        with self._lock:
            config = self._configs[plugin_id]
            if not self._enabled[plugin_id]:
                raise MCPError("Plugin is disabled")
            if plugin_id in self._clients and self._clients[plugin_id].running:
                return
            availability = self._availability(config)
            if availability != "available":
                raise MCPError(availability)
            self._retire(plugin_id)
            generation = self._generation[plugin_id]
            identity = os.stat(config.executable)
        # Version checking is also explicit; never use PATH, npx or a shell.
        try:
            with selectors.DefaultSelector() as selector:
                process = subprocess.Popen([config.executable, "--version"], stdout=subprocess.PIPE,
                                           stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                           env={"PATH": "/usr/bin:/bin", "HOME": os.path.expanduser("~")},
                                           start_new_session=True)
                deadline = time.monotonic() + 5
                output = bytearray()
                os.set_blocking(process.stdout.fileno(), False)
                selector.register(process.stdout, selectors.EVENT_READ)
                try:
                    while True:
                        if cancel_event is not None and cancel_event.is_set():
                            raise MCPError("Plugin startup cancelled")
                        with self._lock:
                            if self._generation[plugin_id] != generation or not self._enabled[plugin_id]:
                                raise MCPError("Plugin was disabled or reconfigured during startup")
                        if time.monotonic() >= deadline:
                            raise MCPError("Plugin version probe timed out")
                        if selector.select(.05):
                            chunk = os.read(process.stdout.fileno(), 4097 - len(output))
                            output.extend(chunk)
                            if len(output) > 4096:
                                raise MCPError("Plugin version output exceeds limit")
                            if not chunk and process.poll() is not None:
                                break
                finally:
                    import signal
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
                    process.stdout.close()
                version_text = output.decode("utf-8", "replace")
                versions = re.findall(r"(?<![\w.])\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?(?![\w.])", version_text)
                if process.returncode or len(version_text) > 4096 or config.expected_version not in versions:
                    raise MCPError("Plugin version does not match configured pin")
            current = os.stat(config.executable)
            if (identity.st_dev, identity.st_ino, identity.st_size, identity.st_mtime_ns) != (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) or self._availability(config) != "available":
                raise MCPError("Plugin executable changed during startup")
            client = self._factory(config.executable, config.args)
            with self._lock:
                if self._generation[plugin_id] != generation or not self._enabled[plugin_id]:
                    raise MCPError("Plugin was disabled or reconfigured during startup")
                self._clients[plugin_id] = client
            client.start(cancel_event)
            catalog = client.list_tools(cancel_event)
            definitions = []
            names = set()
            excluded = set(self._dependency(plugin_id).get("excluded_tools", []))
            for tool in catalog:
                name = tool.get("name") if isinstance(tool, dict) else None
                if not isinstance(name, str) or not NAME.fullmatch(name) or "__" in name or name in names:
                    raise MCPError("Invalid or duplicate upstream tool name")
                schema = tool.get("inputSchema")
                if not isinstance(schema, dict) or schema.get("type") != "object":
                    raise MCPError("Tool input schema must be an object")
                names.add(name)
                if name in excluded: continue
                if len(f"plugin__{plugin_id}__{name}") > 64:
                    raise MCPError("Namespaced tool name exceeds 64 characters")
                definitions.append({"name": f"plugin__{plugin_id}__{name}",
                                    "description": str(tool.get("description", ""))[:4000],
                                    "input_schema": copy.deepcopy(schema), "requires_approval": True})
            if len(definitions) > MAX_TOOLS:
                raise MCPError("Excessive tool catalog")
            with self._lock:
                if self._generation[plugin_id] != generation or not self._enabled[plugin_id]:
                    raise MCPError("Plugin was disabled or reconfigured during startup")
                self._tools[plugin_id] = definitions
        except Exception:
            with self._lock:
                if self._generation.get(plugin_id) == generation:
                    self._retire(plugin_id)
            raise

    def tool_definitions(self):
        with self._lock:
            return copy.deepcopy([t for i, tools in self._tools.items()
                                  if self._enabled[i] and self._clients[i].running for t in tools])

    def dispatch(self, name, arguments, cancel_event=None):
        if not isinstance(arguments, dict) or len(json.dumps(arguments, allow_nan=False).encode()) > MAX_MESSAGE:
            raise ValueError("Invalid or excessive tool arguments")
        with self._lock:
            parts = name.split("__")
            if len(parts) != 3 or parts[0] != "plugin":
                raise MCPError("Unknown plugin tool")
            plugin_id, upstream = parts[1:]
            if not self._enabled.get(plugin_id) or plugin_id not in self._clients:
                raise MCPError("Plugin is disabled or stopped")
            tool = next((t for t in self._tools.get(plugin_id, []) if t["name"] == name), None)
            if tool is None:
                raise MCPError("Unknown plugin tool")
            validate_arguments(arguments, tool["input_schema"])
            client = self._clients[plugin_id]
        # Disable closes this exact transport, including any already queued call.
        result = client.call_tool(upstream, arguments, cancel_event)
        return {"content": result["content"], "is_error": bool(result.get("isError", False)),
                "summary": f"Plugin {plugin_id}: {upstream}", "changed_files": []}

    def describe(self, name, arguments):
        """Approval hint only: the host applies Manual/Accept Edits/YOLO policy."""
        with self._lock:
            tool = next((t for t in self.tool_definitions() if t["name"] == name), None)
            if tool is None:
                raise MCPError("Plugin tool is disabled or unavailable")
            if not isinstance(arguments, dict) or len(json.dumps(arguments, allow_nan=False).encode()) > MAX_MESSAGE:
                raise ValueError("Invalid or excessive tool arguments")
            validate_arguments(arguments, tool["input_schema"])
        return {"summary": f"Run external plugin tool {name}", "requires_approval": True}

    def close(self):
        with self._lock:
            for plugin_id in list(self._clients):
                self._retire(plugin_id)
