#!/bin/bash
# Read-only diagnostics for a ChatGPT Desktop composer that refuses to send,
# notably "Unable to send message / Select a project to continue" (the
# composer's `missing-workspace` block). See docs/codex-frictions.md.
#
# That block fires when a chat's workspace roots resolve to "/" and Desktop
# does not classify the chat as projectless. The usual cause is a project
# whose primary folder is the disk root: its chats start in "/". Desktop
# classifies a chat as projectless when its id is in `projectless-thread-ids`
# in ~/.codex/.codex-global-state.json, or when its cwd matches
# ~/Documents/Codex/<yyyy-mm-dd>/<slug>. This script prints exactly those
# inputs, the projects rooted at "/", and the most recent chats.
#
# It writes nothing. It prints no titles, messages, tokens or keys, and shows
# $HOME as ~; project names and folder paths do appear. Run it while the block
# is showing, before starting a new chat or relaunching:
#   bash diagnose_codex_composer.sh [thread-id ...] > composer-diag.txt
# With no ids it reports the 12 newest chats and the turn timeline of the 3
# newest; pass the stuck chat's id to report that one. It needs no install:
# it uses Provider Hub's bundled Python when present.
PY="/Applications/Provider Hub.app/Contents/Resources/python/bin/python3"
[ -x "$PY" ] || PY=/usr/bin/python3
exec "$PY" -I - "$@" <<'PYEOF'
import datetime, glob, json, os, platform, plistlib, re, shutil, sqlite3, subprocess, sys

HOME = os.path.expanduser("~")
CODEX = os.path.join(HOME, ".codex")
PROJECTLESS = re.compile(r"^(.*(?:^|[\\/])Documents[\\/]+Codex)[\\/]+(?:\d{4}-\d{2}-\d{2}-[a-z0-9][a-z0-9-]*|\d{4}-\d{2}-\d{2}[\\/]+[a-z0-9][a-z0-9-]*)[\\/]*$")

def tilde(value):
    if not isinstance(value, str):
        return value
    return "~" + value[len(HOME):] if value == HOME or value.startswith(HOME + "/") else value

def bundle(app):
    try:
        with open(f"/Applications/{app}.app/Contents/Info.plist", "rb") as stream:
            info = plistlib.load(stream)
        return f"{info.get('CFBundleShortVersionString')} ({info.get('CFBundleVersion')})"
    except OSError:
        return "not installed"

print("== versions")
print("ChatGPT:", bundle("ChatGPT"), "| Provider Hub:", bundle("Provider Hub"), "| macOS:", platform.mac_ver()[0])
total, _, free = shutil.disk_usage(HOME)
print(f"disk free: {free / 2**30:.1f} GiB of {total / 2**30:.0f} GiB")

print("\n== processes (is Desktop running under the hub's DevTools pipe?)")
listing = subprocess.run(["/bin/ps", "-axo", "pid=,ppid=,etime=,command="], capture_output=True, text=True).stdout
desktop, routes = [], 0
for line in listing.splitlines():
    if 'model_provider="openai"' in line and "/codex" in line:
        routes += 1  # the hub's own Codex route turns; ephemeral, scratch cwd
    elif "/ChatGPT.app/Contents/MacOS/ChatGPT" in line or "codex-accent" in line or " app-server" in line:
        print("  " + tilde(line.strip())[:160])
        desktop.append(line.split()[0])
print(f"  (+{routes} Provider Hub Codex-route turn processes)")
if desktop:
    cwds = subprocess.run(["/usr/sbin/lsof", "-a", "-d", "cwd", "-Fn", "-p", ",".join(desktop)], capture_output=True, text=True).stdout
    pid = None
    for field in cwds.splitlines():
        if field.startswith("p"):
            pid = field[1:]
        elif field.startswith("n"):
            print(f"  process {pid} cwd: {tilde(field[1:])}")

print("\n== Provider Hub Codex switches")
for name in ("Provider Hub", "Provider Hub Preview", "Mistral Bridge"):
    path = os.path.join(HOME, "Library/Application Support", name, "settings.json")
    try:
        with open(path) as stream:
            settings = json.load(stream)
    except (OSError, ValueError):
        continue
    print(f"  {name}:", {key: value for key, value in settings.items() if key.startswith("codex") and not isinstance(value, (dict, list))})

print("\n== Desktop global state")
state_path = os.path.join(CODEX, ".codex-global-state.json")
state = {}
try:
    print(f"  size {os.path.getsize(state_path):,} bytes, modified {datetime.datetime.fromtimestamp(os.path.getmtime(state_path)):%Y-%m-%d %H:%M:%S}")
    with open(state_path) as stream:
        state = json.load(stream)
    print("  parses: yes")
except OSError as error:
    print("  unreadable:", error)
except ValueError as error:
    print("  DOES NOT PARSE:", error)
leftovers = glob.glob(os.path.join(CODEX, "..codex-global-state.json.*tmp-*"))
print(f"  leftover temp writes: {len(leftovers)}")
projectless = set(state.get("projectless-thread-ids") or [])
outputs = state.get("thread-projectless-output-directories") or {}
hints = state.get("thread-workspace-root-hints") or {}
assignments = state.get("thread-project-assignments") or {}
print(f"  projectless-thread-ids: {len(projectless)}, output-directory hints: {len(outputs)}, root hints: {len(hints)}")
print("  active-workspace-roots:", [tilde(root) for root in state.get("active-workspace-roots") or []])
selected = state.get("selected-project")
print("  selected-project:", {key: tilde(value) for key, value in selected.items()} if isinstance(selected, dict) else selected)
atoms = state.get("electron-persisted-atom-state") or {}
for key in ("home-composer-mode-v1", "agent-mode-by-host-id", "permission-selection-by-host-id:local", "projectless-sidebar-chats-first-v1"):
    print(f"  {key}:", atoms.get(key))
for host, migration in (state.get("app-server-projects-migration-by-host") or {}).items():
    if isinstance(migration, dict):
        print(f"  projects migration {tilde(host)}:", {key: (len(value) if isinstance(value, list) else value) for key, value in migration.items()})
membership = state.get("thread-project-membership-host-ids") or {}
local_projects = state.get("local-projects") or {}
# A pending workspace move's cwd outranks the chat's own cwd in the composer.
transitions = {key.split(":", 1)[1]: value for source in (state, atoms) for key, value in source.items()
               if key.startswith("thread-workspace-state-v1:")}
print(f"  thread workspace states: {len(transitions)}")

# Only the newest schema is live; sort by number, since state_12 < state_5 as text.
databases = sorted((int(match.group(1)), path) for path in glob.glob(os.path.join(CODEX, "state_*.sqlite"))
                   if (match := re.search(r"state_(\d+)\.sqlite$", path)))
database = sqlite3.connect(f"file:{databases[-1][1]}?mode=ro", uri=True) if databases else None

print("\n== projects whose primary folder is '/' (their chats lock after one message)")
rooted = 0
for entry in local_projects.values():
    roots = entry.get("rootPaths") if isinstance(entry, dict) else None
    if isinstance(roots, list) and roots and isinstance(roots[0], str) and roots[0] and not roots[0].strip("/"):
        rooted += 1
        print(f"  global state: {entry.get('id')} {entry.get('name')!r} folders={[tilde(root) for root in roots]}")
if database is not None:
    try:
        for identifier, name in database.execute(
                "select p.id, p.name from projects p join project_roots r on r.project_id = p.id "
                "where r.position = (select min(position) from project_roots where project_id = p.id) "
                "and r.path <> '' and trim(r.path, '/') = ''"):
            rooted += 1
            print(f"  app-server: {identifier} {name!r}")
    except sqlite3.Error as error:
        print("  app-server projects unreadable:", error)
print(f"  ({rooted} records; {len(local_projects)} projects in global state)")

print("\n== recent chats (newest first)")
if database is None:
    print("  no state database found")
else:
    columns = {row[1] for row in database.execute("pragma table_info(threads)")}
    wanted = [name for name in ("id", "cwd", "project_id", "source", "thread_source", "model_provider", "originator", "archived", "updated_at_ms") if name in columns]
    requested = sys.argv[1:]
    if requested:
        rows = database.execute(f"select {','.join(wanted)} from threads where id in ({','.join('?' * len(requested))})", requested).fetchall()
    else:
        rows = database.execute(f"select {','.join(wanted)} from threads where archived = 0 order by updated_at_ms desc limit 12").fetchall()
    timelines = []
    for row in rows:
        thread = dict(zip(wanted, row))
        cwd = thread.get("cwd") or ""
        identifier = thread["id"]
        if requested or len(timelines) < 3:
            timelines.append(identifier)
        when = datetime.datetime.fromtimestamp((thread.get("updated_at_ms") or 0) / 1000)
        print(f"  {identifier}  {when:%m-%d %H:%M}")
        print(f"     cwd={tilde(cwd)!r} exists={os.path.isdir(cwd)} projectless-shaped={bool(PROJECTLESS.match(cwd.strip()))}")
        print(f"     in projectless-thread-ids={identifier in projectless} output-hint={tilde(outputs.get(identifier))!r} root-hint={tilde(hints.get(identifier))!r}")
        print(f"     project_id={thread.get('project_id')!r} assignment={assignments.get(identifier)!r} membership-host={membership.get(identifier)!r}")
        transition = transitions.get(identifier)
        if isinstance(transition, dict):
            pick = lambda side: tilde((transition.get(side) or {}).get("cwd")) if isinstance(transition.get(side), dict) else transition.get(side)
            print(f"     workspace state: pending cwd={pick('pending')!r} applied cwd={pick('applied')!r} project={transition.get('project')!r}")
        source = str(thread.get("source"))
        print(f"     source={source[:80]!r} thread_source={thread.get('thread_source')!r} provider={thread.get('model_provider')!r} originator={thread.get('originator')!r}")
    counts = database.execute("select sum(cwd = '/'), sum(cwd is null or cwd = ''), count(*) from threads where archived = 0").fetchone()
    print(f"  unarchived chats with cwd '/': {counts[0]}, with no cwd: {counts[1]}, total: {counts[2]}")

    print("\n== turn timeline (only turns whose cwd or roots changed; no message content)")
    for identifier in timelines:
        found = database.execute("select rollout_path from threads where id = ?", (identifier,)).fetchone() if "rollout_path" in columns else None
        path = found[0] if found else None
        print(f"  {identifier}: {tilde(path) if path else 'no rollout path'}")
        if not path or not os.path.isfile(path):
            continue
        previous, turns, model = None, 0, None
        with open(path, errors="replace") as stream:
            for line in stream:
                if '"session_meta"' not in line[:120] and '"turn_context"' not in line[:120]:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                payload = record.get("payload") or {}
                roots = [tilde(root) for root in payload.get("runtime_workspace_roots") or payload.get("workspace_roots") or []]
                if record.get("type") == "session_meta":
                    print(f"     start  cwd={tilde(payload.get('cwd'))!r} roots={roots} originator={payload.get('originator')!r} cli={payload.get('cli_version')!r}")
                    continue
                turns += 1
                model = payload.get("model")
                current = (payload.get("cwd"), roots)
                if current != previous:
                    sandbox = (payload.get("sandbox_policy") or {}).get("type")
                    print(f"     turn {turns} at {record.get('timestamp')} cwd={tilde(payload.get('cwd'))!r} roots={roots} model={model!r} sandbox={sandbox!r}")
                    previous = current
        print(f"     {turns} turn records, last model {model!r}")

print("\n== Desktop log lines that matter (last 3 days)")
cutoff = datetime.datetime.now() - datetime.timedelta(days=3)
logs = [path for path in glob.glob(os.path.join(HOME, "Library/Logs/com.openai.codex/*/*/*/*.log"))
        if datetime.datetime.fromtimestamp(os.path.getmtime(path)) > cutoff]
pattern = re.compile(r"projectless|global.state|ENOSPC|EACCES|EPERM|workspace.?root|missing-workspace|Failed to (load|read|write|save)", re.I)
noise = re.compile(r"source=collab_hydration|browser-sidebar|IAB_LIFECYCLE|Computer Use")
hits = []
for path in sorted(logs, key=os.path.getmtime):
    try:
        with open(path, errors="replace") as stream:
            hits.extend(line.rstrip() for line in stream if pattern.search(line) and " info " not in line[:40] and not noise.search(line))
    except OSError:
        pass
for line in hits[-25:]:
    print("  " + tilde(line)[:300])
print(f"  ({len(hits)} matching warning/error lines in {len(logs)} log files)")
PYEOF
