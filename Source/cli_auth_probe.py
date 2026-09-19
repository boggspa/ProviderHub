"""Read-only capability probe for the coding-agent CLIs the hub can route to.

This module is READ-ONLY BY CONSTRUCTION. It never reads, opens, stats or
parses a credential file - not ``~/.codex/auth.json``, not
``~/.config/muse/auth.json``, not ``~/.grok/auth.json``, not
``~/.gemini/oauth_creds.json``, not the macOS Keychain. Auth state comes only
from running the CLI's own read-only status subcommand, and is reported
``"unknown"`` when the CLI offers no such thing. Nothing here touches the
filesystem for a credential, so there is no code path that could leak one.

That is the whole point of Route 2 (CLI-as-transport). The hub spawns the
vendor's own binary and lets *it* own and refresh its login, the way the Vibe
CLI route already does for Mistral. A hub that copied a token out of a
credential file would hold a secret it cannot renew, and would break the
moment the vendor rotated its on-disk format.

Equally, a probe never runs a prompt, never starts a session and never
mutates a provider home. Every argv this module can build comes from
:data:`ALLOWED_VERBS` through :func:`_argv`, which refuses anything else, so
the discipline does not depend on a caller remembering it. Probes are bounded
by ``timeout`` and stdin is devnull: ``agy`` reads a prompt from stdin, and a
probe that accidentally looked like one would hang the hub's settings pane.

Nothing here is allowed to raise at the caller either. A CLI that is absent,
slow, wedged or answering with a shape we have not seen degrades into
``installed`` / ``auth_state`` / ``warnings``. Following the repo's own
doctrine about markers, a probe that failed to answer is *absence of
evidence*, never evidence of absence: only a CLI that answers with a non-zero
status is reported as signed out.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from typing import NamedTuple

#: The five CLIs this experiment can front as hub model routes. Order is the
#: display order the settings pane uses, so every list this module returns is
#: stable across calls rather than following dict iteration of a probe result.
SUPPORTED_PROVIDERS = ("codex", "claude", "muse", "grok", "antigravity")

#: One probe's whole budget. Generous enough for a CLI that phones home before
#: it prints (``agy models`` fetches), short enough that five of them in series
#: cannot wedge the settings pane.
DEFAULT_TIMEOUT = 25

#: The only argv tails this module may ever construct. Anything outside it is
#: refused by :func:`_argv`, which is the single place an argv is built - a
#: prompt, a session, a login or a logout is therefore not expressible here.
ALLOWED_VERBS = (
    ("--version",),
    ("--help",),
    ("login", "status"),
    ("auth", "status"),
    ("models",),
)

#: Which auth states mean "this CLI can carry a turn". ``missing`` and
#: ``unsupported`` are excluded: a signed-out or absent CLI has no login for
#: the transport to borrow, and Route 2 never supplies one for it.
USABLE_AUTH_STATES = ("authenticated", "unknown")


class CliProbeError(RuntimeError):
    """An unusable probe request: an unknown provider, or a verb off the allowlist."""


class _Answer(NamedTuple):
    """One bounded subprocess result. ``None`` instead of this means it never ran."""

    rc: int
    stdout: str
    stderr: str


class _Provider(NamedTuple):
    binary: str
    install_hint: str
    no_auto_update: bool = False


#: Install hints are the paths these binaries actually occupy on a working
#: install, quoted as advice for a user whose probe came back empty. They are
#: never stat-ed: naming a path is not probing the filesystem.
_PROVIDERS = {
    "codex": _Provider("codex", "/opt/homebrew/bin/codex"),
    "claude": _Provider("claude", "~/.local/bin/claude"),
    "muse": _Provider("muse", "~/.local/bin/muse"),
    # grok lives inside its own provider home and self-updates on launch, so
    # every probe carries --no-auto-update: a capability check must not be the
    # thing that swaps the binary the hub is about to route through.
    "grok": _Provider("grok", "~/.grok/bin/grok", no_auto_update=True),
    "antigravity": _Provider("agy", "~/.local/bin/agy"),
}

#: The turn transport each CLI is known to offer, named for the settings pane.
#: Deliberately a *label* and never an argv: this module has no business
#: knowing how to start a turn, and holding the shape here would put a prompt
#: one function call away from a read-only probe. Building and running these
#: belongs to the per-provider CLI session modules.
KNOWN_TURN_TRANSPORTS = {
    "codex": "codex app-server",
    "claude": "claude --print",
    "muse": "muse serve",
    "grok": "grok agent (stdio)",
    "antigravity": "agy --print",
}

#: A bare ``x.y`` or ``x.y.z``, which is what every one of these CLIs prints
#: somewhere in its version line. First match wins, and that is what makes
#: ``Muse Code 1.3.0 (1.3.0-R3401.1)`` normalize to ``1.3.0`` rather than to
#: the build id in parentheses.
_VERSION = re.compile(r"\d+\.\d+(?:\.\d+)?")
_LOOSE_VERSION = re.compile(r"\b\d+\b")

#: ``You are logged in with grok.com.`` and friends. Only an affirmative match
#: is believed: the signed-*out* shape of ``grok models`` has not been
#: observed, and guessing at it would report a working login as missing.
_LOGGED_IN = re.compile(r"logged in", re.IGNORECASE)
_GROK_MODEL = re.compile(r"^\s*[*-]\s*(\S+)")
_GROK_DEFAULT = re.compile(r"^Default model:\s*(\S+)", re.IGNORECASE)


def normalize_version(raw) -> str:
    """Reduce a CLI's version line to the bare version it contains.

    Each of these five prints a different sentence around the same fact -
    ``codex-cli 0.153.0``, ``2.1.276 (Claude Code)``, ``grok 1.0.34
    (3736acbc8658) [stable]`` - and the hub wants the fact, with the sentence
    kept separately as ``version_raw`` for a user who needs to recognize it.
    Something with no digits at all returns empty rather than a guess.
    """
    text = _first_line(raw)
    if not text:
        return ""
    found = _VERSION.search(text)
    if found:
        return found.group(0)
    loose = _LOOSE_VERSION.search(text)
    return loose.group(0) if loose else ""


def _first_line(raw) -> str:
    """The first non-blank line of a probe's output, stripped."""
    if not isinstance(raw, str):
        return ""
    for line in raw.splitlines():
        if line.strip():
            return line.strip()
    return ""


def _argv(provider_id: str, verb) -> list[str]:
    """The one and only argv builder: binary, optional prefix, allowlisted verb.

    The allowlist is checked here rather than at each call site because a
    read-only guarantee that depends on every caller remembering it is not a
    guarantee. An unlisted verb raises instead of being silently dropped, so a
    future probe that needs a new verb has to widen :data:`ALLOWED_VERBS` in
    the open, in review.
    """
    provider = _PROVIDERS.get(provider_id)
    if provider is None:
        raise CliProbeError(f"Unknown CLI provider '{provider_id}'.")
    tail = tuple(verb)
    if tail not in ALLOWED_VERBS:
        raise CliProbeError(
            f"{' '.join(tail) or '(empty)'} is not a read-only probe verb for "
            f"{provider_id}. Allowed: {'; '.join(' '.join(v) for v in ALLOWED_VERBS)}.")
    prefix = ("--no-auto-update",) if provider.no_auto_update else ()
    return [provider.binary, *prefix, *tail]


def _cli_session():
    """The sibling CLI session module, imported lazily.

    Lazy because the two modules are written in parallel and neither should be
    able to make the other unimportable: a probe that raised ImportError at
    module scope would take the settings pane down with it. Resolution and
    environment are delegated so the probe sees the same PATH and the same
    scrubbed environment the turn transport will, instead of inheriting the
    desktop app's.
    """
    try:
        import cli_session
    except ImportError as exc:
        raise CliProbeError("The CLI session module is unavailable, so no binary can be resolved.") from exc
    return cli_session


def _resolve(session, name: str) -> str:
    """Absolute path of ``name``, or a CliProbeError naming what to install."""
    resolved = None
    try:
        resolved = session.resolve_binary(name)
    except (TypeError, AttributeError, OSError, ValueError):
        # A resolver whose signature or failure mode differs from the one this
        # module was written against still must not take the probe down.
        resolved = None
    if resolved:
        return str(resolved)
    found = shutil.which(name)
    if not found:
        raise CliProbeError(f"'{name}' was not found on PATH.")
    return found


def default_capture(argv, *, timeout: int = DEFAULT_TIMEOUT):
    """The production capture: run one allowlisted argv, bounded, no stdin.

    ``stdin=DEVNULL`` is load-bearing rather than tidy - ``agy`` takes a prompt
    from stdin, so an inherited terminal is the difference between a probe and
    a session. ``check=False`` because a non-zero status is an answer worth
    reporting (a signed-out CLI says so by exiting non-zero), not an exception.
    """
    session = _cli_session()
    binary = _resolve(session, str(argv[0]))
    completed = subprocess.run([binary, *[str(part) for part in argv[1:]]],
                               capture_output=True, text=True, timeout=timeout,
                               env=session.minimal_env(), stdin=subprocess.DEVNULL,
                               check=False)
    return completed.returncode, completed.stdout, completed.stderr


class _Runner:
    """One provider's bounded probe channel, accumulating its own warnings.

    Every subprocess goes through here, which is what makes "a probe can never
    hang or crash the caller" a property of the module instead of a hope about
    each call site. A capture that raises - absent binary, timeout, or a fake
    in tests - becomes ``None`` plus a warning.
    """

    def __init__(self, provider_id: str, capture, timeout: int):
        self.provider_id = provider_id
        self.capture = capture
        self.timeout = timeout
        self.warnings: list[str] = []

    def run(self, verb):
        """Run one allowlisted verb, or return None and say why."""
        argv = _argv(self.provider_id, verb)
        label = " ".join(verb)
        try:
            rc, stdout, stderr = self.capture(list(argv), timeout=self.timeout)
        except subprocess.TimeoutExpired:
            self.warnings.append(f"`{label}` did not answer within {self.timeout}s.")
            return None
        except CliProbeError as exc:
            self.warnings.append(f"`{label}` could not be resolved: {exc}")
            return None
        except Exception as exc:  # noqa: BLE001 - a probe must never raise at the hub
            # Deliberately broad: the caller is a settings pane, and an OSError
            # for a missing binary, a ValueError from a malformed fake and a
            # vendor CLI dying on a signal are all the same fact from here.
            self.warnings.append(f"`{label}` could not be run: {exc.__class__.__name__}: {exc}")
            return None
        try:
            code = int(rc)
        except (TypeError, ValueError):
            code = -1
        return _Answer(code, stdout if isinstance(stdout, str) else "",
                       stderr if isinstance(stderr, str) else "")


def _parse_claude_auth(answer: _Answer, warnings: list[str]) -> tuple[str, str]:
    """Read ``claude auth status``, which answers in JSON on stdout.

    Email and org id are in that JSON and are deliberately not carried into the
    result: this dict is rendered in a settings pane, and a capability probe
    has no reason to put an account address on screen. Method and subscription
    tier are the parts that explain what a route will actually cost.
    """
    if answer.rc != 0:
        detail = _first_line(answer.stderr) or _first_line(answer.stdout)
        warnings.append(f"`claude auth status` exited {answer.rc}: {detail or 'no detail'}.")
        return "missing", detail
    try:
        payload = json.loads(answer.stdout)
    except ValueError:
        warnings.append("`claude auth status` did not return JSON; auth state is unknown rather than assumed.")
        return "unknown", ""
    if not isinstance(payload, dict):
        warnings.append("`claude auth status` returned a JSON shape this probe does not understand.")
        return "unknown", ""
    if payload.get("loggedIn") is not True:
        method = str(payload.get("authMethod") or "").strip()
        return "missing", method or "Claude Code reports no active login."
    parts = [str(payload.get(name)).strip() for name in ("authMethod", "subscriptionType")]
    detail = " / ".join(part for part in parts if part and part != "None")
    return "authenticated", detail or "Claude Code reports an active login."


def _parse_codex_auth(answer: _Answer, warnings: list[str]) -> tuple[str, str]:
    """Read ``codex login status``, which answers in prose on stdout.

    Codex signals a signed-out state by exiting non-zero, so the status code is
    the evidence here and the line is only the explanation.
    """
    detail = _first_line(answer.stdout) or _first_line(answer.stderr)
    if answer.rc != 0:
        warnings.append(f"`codex login status` exited {answer.rc}: {detail or 'no detail'}.")
        return "missing", detail
    if not detail:
        warnings.append("`codex login status` exited 0 without saying whether it is logged in.")
        return "unknown", ""
    return "authenticated", detail


def _parse_grok_models(answer: _Answer, warnings: list[str]) -> tuple[str, str, list[dict]]:
    """Read auth *and* models from one ``grok models`` call.

    ``grok models`` is documented as "list available models and exit", and it
    opens by naming the account it is logged in with - so one read-only call
    answers both questions and no separate status subcommand is needed (grok
    has none). Anything other than a clean affirmative leaves auth ``unknown``
    rather than ``missing``: the signed-out shape of this output has not been
    observed, and this module does not guess at evidence it lacks.
    """
    text = answer.stdout or answer.stderr
    if answer.rc != 0:
        warnings.append(f"`grok models` exited {answer.rc}; auth state left unknown.")
        return "unknown", "", []
    lines = text.splitlines()
    auth_state, auth_detail = "unknown", ""
    for line in lines:
        if _LOGGED_IN.search(line):
            auth_state, auth_detail = "authenticated", line.strip()
            break
    if auth_state == "unknown":
        warnings.append("`grok models` did not confirm a login; the CLI owns its own credentials.")
    models: list[dict] = []
    seen: set[str] = set()
    listing = False
    default_model = ""
    for line in lines:
        stripped = line.strip()
        if not default_model:
            found_default = _GROK_DEFAULT.match(stripped)
            if found_default:
                default_model = found_default.group(1)
        if not listing:
            listing = bool(re.match(r"^Available models:", stripped, re.IGNORECASE))
            continue
        found = _GROK_MODEL.match(line)
        if not found:
            continue
        # The listing marks the default in place - ``grok-4.6 (default)`` - and
        # the bullet regex stops at the first space, so the marker never leaks
        # into an id.
        model_id = found.group(1).strip()
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        models.append({"id": model_id, "display_name": model_id})
    if not models and default_model:
        # A build that names only its default still advertised one model, and
        # one real id beats an empty list for deciding route usability.
        models.append({"id": default_model, "display_name": default_model})
    return auth_state, auth_detail, models


def _parse_agy_models(answer: _Answer, warnings: list[str]) -> list[dict]:
    """Read ``agy models``: tab-separated ``id<TAB>Display Name`` lines.

    The command prints a ``Fetching available models...`` banner first (on
    stderr, though a future build may fold it into stdout), so a line is
    believed only when it carries a tab. That single rule skips the banner,
    blank lines and any prose the CLI adds around the table, without this
    module having to know the banner's wording.
    """
    if answer.rc != 0:
        warnings.append(f"`agy models` exited {answer.rc}; no model list is advertised.")
        return []
    models: list[dict] = []
    seen: set[str] = set()
    for line in (answer.stdout or "").splitlines():
        if "\t" not in line:
            continue
        model_id, _, display = line.partition("\t")
        model_id, display = model_id.strip(), display.strip()
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        models.append({"id": model_id, "display_name": display or model_id})
    return models


#: What each CLI can be asked, and how its answer is read. Kept as a table so
#: the read-only surface of the whole module is legible in one place: five
#: providers, and between them four verbs from ALLOWED_VERBS.
def _probe_codex(runner: _Runner) -> tuple[str, str, list[dict]]:
    answer = runner.run(("login", "status"))
    if answer is None:
        return "unknown", "", []
    state, detail = _parse_codex_auth(answer, runner.warnings)
    # codex keeps its catalogue behind app-server's model/list, which means
    # starting a server - out of bounds for a probe. codex_runtime already
    # qualifies that catalogue properly, in a disposable CODEX_HOME.
    runner.warnings.append("codex advertises no read-only model list; the hub projects its own "
                           "catalogue (see codex_runtime.qualify_runtime).")
    return state, detail, []


def _probe_claude(runner: _Runner) -> tuple[str, str, list[dict]]:
    answer = runner.run(("auth", "status"))
    if answer is None:
        return "unknown", "", []
    state, detail = _parse_claude_auth(answer, runner.warnings)
    runner.warnings.append("claude advertises no read-only model list; routes use the hub's own "
                           "Claude model table.")
    return state, detail, []


def _probe_muse(runner: _Runner) -> tuple[str, str, list[dict]]:
    # muse has no non-interactive auth status: `muse auth` stores credentials
    # and `muse login` starts a browser flow, so both are off the allowlist by
    # construction. --help is the strongest read-only claim available - the CLI
    # is present and answering - and the login it holds stays its own business.
    answer = runner.run(("--help",))
    detail = ("muse keeps its own login (browser flow, refreshed by the CLI); this probe does not "
              "and cannot read it.")
    if answer is None:
        return "unknown", detail, []
    if answer.rc != 0:
        runner.warnings.append(f"`muse --help` exited {answer.rc}.")
    runner.warnings.append("muse advertises no read-only model list; routes use the hub's own "
                           "model table.")
    return "unknown", detail, []


def _probe_grok(runner: _Runner) -> tuple[str, str, list[dict]]:
    answer = runner.run(("models",))
    if answer is None:
        return "unknown", "", []
    return _parse_grok_models(answer, runner.warnings)


def _probe_antigravity(runner: _Runner) -> tuple[str, str, list[dict]]:
    # agy has no auth subcommand at all - `agy auth status` exits 2 on
    # `unexpected argument "auth"` - so the probe asks anyway and reports what
    # it learns. Asking is cheap and it means a future agy that grows one is
    # picked up without a change here.
    answer = runner.run(("auth", "status"))
    if answer is None:
        state, detail = "unknown", ""
    elif answer.rc == 0:
        state, detail = "authenticated", _first_line(answer.stdout)
    else:
        state = "unknown"
        detail = "agy has no read-only auth status; the CLI owns its own login."
    models_answer = runner.run(("models",))
    models = [] if models_answer is None else _parse_agy_models(models_answer, runner.warnings)
    if not models:
        runner.warnings.append("agy advertised no models on this run.")
    return state, detail, models


_PROBES = {
    "codex": _probe_codex,
    "claude": _probe_claude,
    "muse": _probe_muse,
    "grok": _probe_grok,
    "antigravity": _probe_antigravity,
}


def _binary_label(provider_id: str, injected: bool) -> str:
    """What to report in ``binary``: a resolved path, or the bare name.

    Resolution is skipped when a capture is injected. An injected capture means
    the caller has taken responsibility for running things - in practice a test
    - and a probe that then went and walked the real PATH would report the
    host's binaries instead of the facts it was handed.
    """
    name = _PROVIDERS[provider_id].binary
    if injected:
        return name
    try:
        return _resolve(_cli_session(), name)
    except (CliProbeError, OSError, ValueError):
        return name


def probe_provider(provider_id: str, *, capture=None, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """One CLI's install, version, auth and model facts, read-only and bounded.

    Returns a dict rather than raising for everything except an unknown
    ``provider_id``: the caller is a settings pane drawing five rows, and one
    wedged CLI should cost that row its detail, not the whole pane.
    """
    if not isinstance(provider_id, str) or provider_id not in _PROVIDERS:
        raise CliProbeError(f"Unknown CLI provider '{provider_id}'. Expected one of "
                            f"{', '.join(SUPPORTED_PROVIDERS)}.")
    injected = capture is not None
    try:
        budget = int(timeout)
    except (TypeError, ValueError):
        budget = DEFAULT_TIMEOUT
    runner = _Runner(provider_id, default_capture if capture is None else capture, budget)
    spec = _PROVIDERS[provider_id]
    probe = {"provider": provider_id, "installed": False, "binary": _binary_label(provider_id, injected),
             "version": "", "version_raw": "", "auth_state": "unsupported", "auth_detail": "",
             "models": [], "warnings": []}

    # The version probe doubles as the existence proof: if it ran at all, the
    # binary is there, whatever it answered. A non-zero status is still an
    # answer, so it does not downgrade `installed`.
    version = runner.run(("--version",))
    if version is None:
        runner.warnings.append(f"{spec.binary} was not run; expected at {spec.install_hint}.")
        probe["warnings"] = runner.warnings
        return probe
    probe["installed"] = True
    probe["version_raw"] = _first_line(version.stdout) or _first_line(version.stderr)
    probe["version"] = normalize_version(probe["version_raw"])
    if version.rc != 0:
        runner.warnings.append(f"`--version` exited {version.rc}.")
    elif not probe["version"]:
        runner.warnings.append(f"Could not read a version out of {probe['version_raw']!r}.")

    state, detail, models = _PROBES[provider_id](runner)
    probe["auth_state"] = state
    probe["auth_detail"] = detail
    probe["models"] = models
    probe["warnings"] = runner.warnings
    return probe


def probe_all(*, capture=None, timeout: int = DEFAULT_TIMEOUT) -> dict[str, dict]:
    """Every supported CLI's probe, always all five entries.

    The fixed shape is the contract the settings pane relies on: it can render
    a row per provider without first working out which providers answered.
    """
    return {provider_id: probe_provider(provider_id, capture=capture, timeout=timeout)
            for provider_id in SUPPORTED_PROVIDERS}


def _usable_for_routes(probe: dict) -> bool:
    """Installed, holding or plausibly holding a login, and able to carry a turn.

    ``unknown`` counts as usable because it means the CLI keeps its own login
    where this probe cannot see it - which is Route 2 working as designed, not
    a gap. ``missing`` and ``unsupported`` do not: there is no login for the
    transport to borrow, and this hub will never supply one.
    """
    if not probe.get("installed") or probe.get("auth_state") not in USABLE_AUTH_STATES:
        return False
    return bool(probe.get("models")) or probe.get("provider") in KNOWN_TURN_TRANSPORTS


def summarize(probes: dict[str, dict]) -> dict:
    """The one-line-per-concern rollup the experiment toggle renders.

    ``missing`` covers both reasons a provider cannot be used yet - no binary
    and no login - because both are the same thing to the user: a row that
    needs an action before it can carry a route. Entries for unsupported
    provider ids and non-dict values are ignored rather than raised on, so a
    partially built probe result still summarizes.
    """
    rows = [(provider_id, probes[provider_id]) for provider_id in SUPPORTED_PROVIDERS
            if isinstance(probes.get(provider_id), dict)]
    return {
        "installed": [name for name, row in rows if row.get("installed")],
        "authenticated": [name for name, row in rows if row.get("auth_state") == "authenticated"],
        "missing": [name for name, row in rows
                    if not row.get("installed") or row.get("auth_state") == "missing"],
        "model_count": sum(len(row.get("models") or []) for _, row in rows),
        "usable_for_routes": [name for name, row in rows
                              if _usable_for_routes({**row, "provider": name})],
    }
