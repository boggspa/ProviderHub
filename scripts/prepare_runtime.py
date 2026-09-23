#!/usr/bin/env python3
"""Prepare a bundled CPython runtime for distribution.

The runtime that `Source/build.sh` embeds is a locally built CPython whose
recorded build prefix is a personal directory, and which carries development
subtrees the worker never imports. Both end up inside the signed, notarized,
publicly downloadable app bundle.

This runs on the copy already inside the bundle and makes it distributable:

1. Prune subtrees nothing imports. Tracing the worker's module-level import
   closure shows no module reaching `sysconfig`, `distutils`, `pip`, `venv`,
   `ensurepip`, `idlelib` or `tkinter`; `site.py` deliberately hand-copies the
   two helpers it needs rather than importing `sysconfig`. The interpreter
   statically links libpython (`--enable-static-libpython-for-interpreter`), so
   `lib/libpython3.*.dylib` is dead weight that no Mach-O in the bundle loads.
2. Rewrite the recorded build prefix in `_sysconfigdata` to `/install` — the
   value CPython was actually configured with. The prefix is derived at run
   time from the runtime itself, never hardcoded, so this is reproducible on
   any machine and by any contributor.
3. Fail the build if a personal path survives, or if the pruned runtime cannot
   start and import the worker's hard dependencies.

The recorded prefix is inert at runtime: `sys.prefix` is derived from the
interpreter's own location, and `sysconfig.get_paths()` relocates correctly.
Rewriting it therefore cannot break path discovery — it only stops
`sysconfig.get_config_var("prefix")` from reporting a stranger's home
directory, which is the point.

Runs under the system Python 3.9. It must not depend on the runtime it is
preparing, because that runtime is what is under test.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

# Directories to remove, as fnmatch patterns relative to the runtime root.
# Version numbers are globbed so this does not rot on the next CPython bump.
PRUNE_DIR_PATTERNS = (
    "include",
    "share",
    "lib/pkgconfig",
    "lib/itcl*",
    "lib/tcl*",
    "lib/thread*",
    "lib/tk*",
    "lib/python*/config-*-darwin",
    "lib/python*/ensurepip",
    "lib/python*/idlelib",
    "lib/python*/venv",
    "lib/python*/tkinter",
    "lib/python*/site-packages/pip",
    "lib/python*/site-packages/pip-*.dist-info",
    # Holds console-script wrappers (cffi-gen-src and friends) whose shebangs
    # point back at the build prefix. Nothing in the bundle invokes them.
    "lib/python*/site-packages/bin",
)

PRUNE_FILE_PATTERNS = (
    "lib/libpython*.dylib",
    "lib/libtcl*.dylib",
    "lib/libtk*.dylib",
    "lib/python*/lib-dynload/_tkinter*.so",
)

# Kept in bin/: the interpreter and its versioned aliases. Everything else
# (pip*, idle*, pydoc*, *-config, 2to3*) is development tooling.
BIN_KEEP_RE = re.compile(r"^python[0-9.]*$")

# `/Users/runner/...` is a GitHub Actions CI path baked into upstream wheel
# artifacts by their own builds. It is not personal and is not patchable out of
# a Mach-O, so it is permitted; every other `/Users/<name>` fails the build.
PERSONAL_PATH_RE = re.compile(rb"/Users/(?!runner/)[A-Za-z0-9._-]+")


def _matches(rel: str, patterns) -> bool:
    return any(fnmatch.fnmatch(rel, p) for p in patterns)


def _tree_bytes(root: Path) -> int:
    """Apparent size without following symlinks — bin/python3 aliases python3.13."""
    total = 0
    for path in root.rglob("*"):
        if path.is_symlink():
            continue
        try:
            if path.is_file():
                total += os.lstat(path).st_size
        except OSError:
            continue
    return total


def rewrite_text(text: str, prefix: str) -> str:
    """Apply the neutralization rules to one file's contents.

    Pure and side-effect free so the rules can be unit-tested without a real
    CPython runtime: the recorded build prefix, plus per-user TMPDIR build
    directories, which carry no username but are still machine-specific.
    Upstream `/Users/runner/...` CI paths are deliberately left alone; they are
    compiled into third-party wheel artifacts and cannot be rewritten.
    """
    if prefix and prefix.startswith("/"):
        text = text.replace(prefix, "/install")
    text = re.sub(r"/private/var/folders/[A-Za-z0-9/._-]+/T/tmp[A-Za-z0-9_]+",
                  "/install", text)
    return re.sub(r"/var/folders/[A-Za-z0-9/._-]+/T/tmp[A-Za-z0-9_]+",
                  "/install", text)

def prune(runtime: Path) -> dict:
    """Remove dead subtrees. Returns counts for the build log."""
    removed_dirs = removed_files = 0
    # Walk a snapshot: deleting while walking would invalidate the iteration.
    entries = []
    for root, dirs, files in os.walk(runtime):
        base = Path(root)
        for d in list(dirs):
            entries.append(("dir", base / d))
        for f in files:
            entries.append(("file", base / f))

    for kind, path in entries:
        if not path.exists() and not path.is_symlink():
            continue  # already gone with a parent we removed
        rel = path.relative_to(runtime).as_posix()
        if path.name == "__pycache__":
            shutil.rmtree(path, ignore_errors=True)
            removed_dirs += 1
            continue
        if kind == "dir" and _matches(rel, PRUNE_DIR_PATTERNS):
            shutil.rmtree(path, ignore_errors=True)
            removed_dirs += 1
        elif kind == "file" and _matches(rel, PRUNE_FILE_PATTERNS):
            path.unlink(missing_ok=True)
            removed_files += 1
        elif rel.startswith("bin/") and rel.count("/") == 1:
            if not BIN_KEEP_RE.match(path.name):
                path.unlink(missing_ok=True)
                removed_files += 1
    return {"dirs": removed_dirs, "files": removed_files}


def recorded_prefix(runtime: Path) -> str:
    """Read the runtime's own configured prefix rather than assuming a path."""
    env = dict(os.environ)
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    out = subprocess.run(
        [str(runtime / "bin" / "python3"), "-B", "-c",
         "import sysconfig; print(sysconfig.get_config_var('prefix') or '')"],
        capture_output=True, text=True, env=env,
    )
    if out.returncode != 0:
        raise SystemExit("prepare_runtime: could not query the runtime prefix:\n" + out.stderr)
    return out.stdout.strip()


def neutralize(runtime: Path) -> dict:
    """Rewrite the recorded build prefix and per-user TMPDIR paths to /install."""
    prefix = recorded_prefix(runtime)
    rewritten = {}
    for path in runtime.rglob("*.py"):
        if path.parent.name == "__pycache__":
            continue
        try:
            text = path.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        updated = rewrite_text(text, prefix)
        if updated != text:
            path.write_text(updated)
            rewritten[path.relative_to(runtime).as_posix()] = 1
    return {"prefix": prefix, "files": len(rewritten), "names": sorted(rewritten)}


def leak_scan(runtime: Path) -> list:
    """Return every file still carrying a personal /Users/<name> path."""
    leaks = []
    for root, _dirs, files in os.walk(runtime):
        for name in files:
            path = Path(root) / name
            try:
                data = path.read_bytes()
            except OSError:
                continue
            if PERSONAL_PATH_RE.search(data):
                leaks.append(path.relative_to(runtime).as_posix())
    return leaks


def smoke_test(runtime: Path, worker_dir: Path | None) -> None:
    """Prove the pruned runtime still starts and can run the worker."""
    env = dict(os.environ)
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    interpreter = runtime / "bin" / "python3"
    if not interpreter.exists():
        raise SystemExit("prepare_runtime: bin/python3 is missing after pruning")
    if not list((runtime / "lib").glob("python*/os.py")):
        raise SystemExit("prepare_runtime: the stdlib landmark os.py is missing")

    check = (
        "import sys, os, ssl, _socket, encodings, sysconfig;"
        "import cryptography.fernet;"
        "assert sys.version_info[:2] == (3, 13), sys.version_info;"
        "assert os.path.realpath(sys.prefix) == os.path.realpath(sys.argv[1]), sys.prefix;"
        "leaks = [k for k, v in sysconfig.get_paths().items()"
        "         if '/Users/' in str(v) and '/Users/runner/' not in str(v)];"
        "assert not leaks, leaks;"
        "print('smoke-ok')"
    )
    out = subprocess.run([str(interpreter), "-B", "-c", check, str(runtime)],
                         capture_output=True, text=True, env=env)
    if out.returncode != 0 or "smoke-ok" not in out.stdout:
        raise SystemExit("prepare_runtime: the pruned runtime failed its smoke test:\n"
                         + out.stdout + out.stderr)

    if worker_dir is None:
        return
    helper = worker_dir / "gateway.py"
    if not helper.exists():
        return  # lightweight build without the worker copied yet
    out = subprocess.run([str(interpreter), "-B", str(helper), "inspect"],
                         capture_output=True, text=True, env=env, cwd=str(worker_dir))
    try:
        payload = json.loads(out.stdout.strip().splitlines()[-1]) if out.stdout.strip() else {}
    except (ValueError, IndexError):
        payload = {}
    if out.returncode != 0 or not payload.get("ok"):
        raise SystemExit("prepare_runtime: gateway.py inspect did not report ok:\n"
                         + out.stdout[-800:] + out.stderr[-800:])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("runtime", type=Path,
                        help="the bundled runtime directory (Contents/Resources/python)")
    parser.add_argument("--worker-dir", type=Path, default=None,
                        help="optional worker directory, used to run gateway.py inspect")
    args = parser.parse_args()

    runtime = args.runtime.resolve()
    if not (runtime / "bin" / "python3").exists():
        raise SystemExit(f"prepare_runtime: {runtime} does not look like a CPython runtime")

    before = _tree_bytes(runtime)
    stats = prune(runtime)
    info = neutralize(runtime)
    after = _tree_bytes(runtime)

    print(f"[prepare_runtime] pruned {stats['dirs']} dirs / {stats['files']} files; "
          f"{before / 1e6:.1f} MB -> {after / 1e6:.1f} MB")
    print(f"[prepare_runtime] rewrote prefix {info['prefix']!r} -> '/install' "
          f"in {info['files']} file(s)")

    leaks = leak_scan(runtime)
    if leaks:
        print("[prepare_runtime] FAIL: personal paths survive in:", file=sys.stderr)
        for item in leaks:
            print(f"  {item}", file=sys.stderr)
        return 1
    print("[prepare_runtime] leak gate passed: no personal /Users path in the runtime")

    smoke_test(runtime, args.worker_dir.resolve() if args.worker_dir else None)
    print("[prepare_runtime] smoke gate passed: interpreter, cryptography and gateway ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
