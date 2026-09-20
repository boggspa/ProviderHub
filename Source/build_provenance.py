"""Record the exact source inputs and packaged worker used by a macOS build.

Exported releases can set PROVIDER_HUB_SOURCE_REVISION and
PROVIDER_HUB_SOURCE_REPOSITORY to verify their files against an existing Git
commit. Repository paths are used locally and never written to the manifest.
The signed executable is covered by Apple's code signature, since signing
changes its bytes after this manifest is created.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess


MANIFEST_NAME = "build-manifest.json"


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _worker_hashes(app_dir):
    worker = Path(app_dir) / "Contents/Resources/worker"
    if not worker.is_dir():
        raise ValueError("The app has no packaged worker directory")
    hashes = {}
    for path in sorted(worker.rglob("*")):
        if path.is_symlink():
            raise ValueError("Packaged worker files must not be symbolic links")
        if path.is_file():
            hashes[path.relative_to(worker).as_posix()] = _sha256(path)
    if not hashes:
        raise ValueError("The packaged worker directory is empty")
    return hashes


def _git(repository, *args):
    return subprocess.check_output(
        ["git", "-C", str(repository), *args], stderr=subprocess.DEVNULL)


def _source_identity(source_dir, source_hashes, revision, repository):
    if repository is None:
        try:
            repository = Path(_git(source_dir, "rev-parse", "--show-toplevel").decode().strip())
        except (OSError, subprocess.CalledProcessError):
            repository = None
    declared = revision is not None
    if revision is not None and not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", revision):
        raise ValueError("PROVIDER_HUB_SOURCE_REVISION must be a full Git commit hash")
    if repository is None:
        if revision is None:
            raise ValueError("An exported build requires PROVIDER_HUB_SOURCE_REVISION")
        return {"revision": revision.lower(), "verification": "unverified-export",
                "dirty": None, "changed_inputs": None}
    revision = _git(repository, "rev-parse", "--verify", (revision or "HEAD") + "^{commit}").decode().strip()
    # Entire copied resource directories count, including deletions. The
    # explicit file list excludes unrelated tests, documents and local work.
    paths = sorted(set(source_hashes) | {"Source/vendor", "Source/provider-logos"})
    baseline = _git(repository, "ls-tree", "-r", "--name-only", "-z", revision, "--", *paths)
    baseline_paths = {p.decode() for p in baseline.split(b"\0") if p}
    baseline_paths = {p for p in baseline_paths if "__pycache__" not in Path(p).parts
                      and not p.endswith((".pyc", ".pyo"))}
    changed = set(source_hashes) - baseline_paths
    for path in baseline_paths:
        expected = hashlib.sha256(_git(repository, "show", revision + ":" + path)).hexdigest()
        if source_hashes.get(path) != expected:
            changed.add(path)
    return {"revision": revision, "verification": "git-tree" if declared else "git-head",
            "dirty": bool(changed), "changed_inputs": sorted(changed)}


def create_manifest(source_dir, app_dir, swift_sources, *, revision=None, repository=None):
    source_dir, app_dir = Path(source_dir), Path(app_dir)
    worker_hashes = _worker_hashes(app_dir)
    source_hashes = {}
    for relative, digest in worker_hashes.items():
        source = source_dir / relative
        if not source.is_file() or _sha256(source) != digest:
            raise ValueError("Packaged worker does not match source: " + relative)
        source_hashes["Source/" + relative] = digest
    for path in [*map(Path, swift_sources), source_dir / "build.sh", source_dir / "build_provenance.py"]:
        relative = path.resolve().relative_to(source_dir.resolve()).as_posix()
        source_hashes["Source/" + relative] = _sha256(path)
    if (source_dir / "AppIcon.icns").is_file():
        source_hashes["Source/AppIcon.icns"] = _sha256(source_dir / "AppIcon.icns")
    identity = _source_identity(source_dir, source_hashes, revision, repository)
    metadata = plistlib.loads((app_dir / "Contents/Info.plist").read_bytes())
    manifest = {
        "schema_version": 1,
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "application": {"version": metadata["CFBundleShortVersionString"],
                        "build": metadata["CFBundleVersion"]},
        "source": {**identity, "sha256": dict(sorted(source_hashes.items()))},
        "worker": {"sha256": worker_hashes},
    }
    destination = app_dir / "Contents/Resources" / MANIFEST_NAME
    destination.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def verify_manifest(app_dir, *, revision=None, require_clean=False):
    app_dir = Path(app_dir)
    manifest = json.loads((app_dir / "Contents/Resources" / MANIFEST_NAME).read_text())
    if revision is not None and manifest["source"]["revision"] != revision:
        raise ValueError("The app was built from a different source revision")
    if require_clean and (manifest["source"].get("dirty") is not False
                          or manifest["source"].get("verification") not in {"git-head", "git-tree"}):
        raise ValueError("The app requires clean source verified against a Git tree")
    if _worker_hashes(app_dir) != manifest["worker"]["sha256"]:
        raise ValueError("Packaged worker files do not match the build manifest")
    metadata = plistlib.loads((app_dir / "Contents/Info.plist").read_bytes())
    if manifest["application"] != {"version": metadata["CFBundleShortVersionString"],
                                   "build": metadata["CFBundleVersion"]}:
        raise ValueError("App version does not match the build manifest")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("write", "verify"))
    parser.add_argument("--app-dir", required=True, type=Path)
    parser.add_argument("--source-dir", type=Path)
    parser.add_argument("--swift-sources", nargs="+", type=Path)
    parser.add_argument("--revision", default=os.environ.get("PROVIDER_HUB_SOURCE_REVISION"))
    parser.add_argument("--repository", default=os.environ.get("PROVIDER_HUB_SOURCE_REPOSITORY"))
    parser.add_argument("--require-clean", action="store_true",
                        help="Reject dirty or unverified source when verifying a release")
    args = parser.parse_args()
    if args.action == "verify":
        manifest = verify_manifest(args.app_dir, revision=args.revision, require_clean=args.require_clean)
    else:
        if args.require_clean:
            parser.error("--require-clean is only supported with verify")
        if args.source_dir is None or not args.swift_sources:
            parser.error("write requires --source-dir and --swift-sources")
        manifest = create_manifest(args.source_dir, args.app_dir, args.swift_sources,
                                   revision=args.revision, repository=args.repository)
    print(json.dumps({"revision": manifest["source"]["revision"],
                      "dirty": manifest["source"]["dirty"], **manifest["application"]}))


if __name__ == "__main__":
    main()
