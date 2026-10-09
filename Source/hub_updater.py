"""Stage official notarized releases without changing the running app.

The native app decides when it is safe to quit. Only update_install.sh, started
after graceful shutdown, replaces the bundle after its owning process exits.
No provider credentials, GitHub token, or elevated helper are used.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import plistlib
import re
import shutil
import stat
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import unicodedata
import uuid
import zipfile

REPOSITORY = "boggspa/ProviderHub"
API = f"https://api.github.com/repos/{REPOSITORY}/releases"
BUNDLE_ID = "com.mistralbridge.providerhub"
TEAM_ID = "8CZML8FK2D"
APP_NAME = "Provider Hub.app"
MAX_DOWNLOAD = 256 * 1024 * 1024
MAX_EXPANDED = 1024 * 1024 * 1024
TAG = re.compile(r"v(\d+\.\d+\.\d+)-build(\d+)")
REQUIREMENT = (f'anchor apple generic and identifier "{BUNDLE_ID}" '
               f'and certificate leaf[subject.OU] = "{TEAM_ID}" '
               'and certificate 1[field.1.2.840.113635.100.6.2.6] exists '
               'and certificate leaf[field.1.2.840.113635.100.6.1.13] exists')


def version_key(version, build):
    if not re.fullmatch(r"\d+\.\d+\.\d+", str(version)) or not re.fullmatch(r"\d+", str(build)):
        raise ValueError("Unrecognized release version")
    return (*map(int, version.split(".")), int(build))


def request(url):
    return urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
        "User-Agent": "ProviderHub-Updater", "X-GitHub-Api-Version": "2022-11-28"})


def fetch_release(tag=None):
    if tag is not None and not TAG.fullmatch(tag):
        raise ValueError("Unrecognized release tag")
    url = API + ("/tags/" + urllib.parse.quote(tag, safe="") if tag else "/latest")
    try:
        with urllib.request.urlopen(request(url), timeout=30) as response:
            data = response.read(2 * 1024 * 1024 + 1)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise
    if len(data) > 2 * 1024 * 1024:
        raise ValueError("Release metadata is too large")
    return json.loads(data)


def release_candidate(release, current_version, current_build):
    if not release or release.get("draft") is not False or release.get("prerelease") is not False:
        return None
    match = TAG.fullmatch(release.get("tag_name", ""))
    if not match:
        return None
    version, build = match.groups()
    if version_key(version, build) <= version_key(current_version, current_build):
        return None
    prefix = f"ProviderHub-{version}-build{build}-"
    assets = [a for a in release.get("assets", []) if a.get("state") == "uploaded"
              and re.fullmatch(re.escape(prefix) + r"[0-9a-f]{7,40}-notarized\.zip", a.get("name", ""))]
    if len(assets) != 1:
        return None
    asset = assets[0]
    url = urllib.parse.urlsplit(asset.get("browser_download_url", ""))
    expected_path = f"/{REPOSITORY}/releases/download/{release['tag_name']}/{asset['name']}"
    if (url.scheme != "https" or url.netloc != "github.com" or url.path != expected_path
            or url.query or url.fragment):
        raise ValueError("The release download is not an official Provider Hub asset")
    digest = asset.get("digest", "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("The release has no SHA-256 checksum")
    size = asset.get("size")
    if type(size) is not int or not 0 < size <= MAX_DOWNLOAD:
        raise ValueError("The release download size is invalid")
    return {"tag": release["tag_name"], "version": version, "build": build,
            "url": asset["browser_download_url"], "sha256": digest[7:], "size": size}


def download(candidate, destination):
    digest = hashlib.sha256()
    size = 0
    with urllib.request.urlopen(request(candidate["url"]), timeout=60) as response, destination.open("xb") as output:
        if urllib.parse.urlsplit(response.geturl()).scheme != "https":
            raise ValueError("The release download was redirected to an insecure connection")
        while chunk := response.read(1024 * 1024):
            size += len(chunk)
            if size > candidate["size"] or size > MAX_DOWNLOAD:
                raise ValueError("The release download is larger than expected")
            digest.update(chunk)
            output.write(chunk)
    if size != candidate["size"] or digest.hexdigest() != candidate["sha256"]:
        raise ValueError("The release download failed checksum verification")


def inspect_archive(archive):
    """Validate every destination and symlink before ditto writes any bytes."""
    def key(path):
        # Default macOS volumes fold case and Unicode normalization. Validate
        # aliases as one destination, including symlink targets and ancestors.
        return unicodedata.normalize("NFC", str(path)).casefold()
    with zipfile.ZipFile(archive) as bundle:
        entries = bundle.infolist()
        if len(entries) > 20000 or sum(i.file_size for i in entries) > MAX_EXPANDED:
            raise ValueError("The release archive is too large")
        names, links = set(), set()
        for entry in entries:
            path = PurePosixPath(entry.filename)
            if (not path.parts or path.parts[0] != APP_NAME or path.is_absolute()
                    or ".." in path.parts or "\\" in entry.filename or "\x00" in entry.filename
                    or path.as_posix() != entry.filename.rstrip("/") or key(path) in names
                    or entry.flag_bits & 1):
                raise ValueError("The release archive has an unsafe path")
            names.add(key(path))
            mode = entry.external_attr >> 16
            if stat.S_ISLNK(mode):
                links.add(key(path))
                if entry.file_size > 4096:
                    raise ValueError("The release archive has an invalid symlink")
                target = bundle.read(entry).decode("utf-8")
                if not target or target.startswith("/") or "\\" in target or "\x00" in target:
                    raise ValueError("The release archive has an unsafe symlink")
                # Relative links in the Python runtime must stay inside the app.
                resolved = os.path.normpath(str(path.parent / target))
                if not resolved.startswith(APP_NAME + "/"):
                    raise ValueError("The release archive symlink escapes the app")
            elif stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise ValueError("The release archive contains a special file")
        if any(key(parent) in links for name in names for parent in PurePosixPath(name).parents):
            raise ValueError("The release archive writes through a symlink")
        # A link to another link may be safe in isolation yet escape after
        # resolution. Release runtime links have direct file/directory targets.
        for entry in entries:
            path = PurePosixPath(entry.filename)
            if key(path) in links:
                target = PurePosixPath(os.path.normpath(str(path.parent / bundle.read(entry).decode("utf-8"))))
                if key(target) in links or any(key(parent) in links for parent in target.parents):
                    raise ValueError("The release archive contains chained symlinks")
        if key(APP_NAME + "/Contents/Info.plist") not in names:
            raise ValueError("The release archive has no Provider Hub app")


def run(*arguments):
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=180)
    if result.returncode:
        raise ValueError(result.stderr.strip() or "Update verification failed")
    return result.stdout + result.stderr


def verify_app(app, version, build):
    metadata = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    if (metadata.get("CFBundleIdentifier") != BUNDLE_ID
            or metadata.get("CFBundleExecutable") != "MistralBridge"
            or metadata.get("CFBundleShortVersionString") != version
            or metadata.get("CFBundleVersion") != build):
        raise ValueError("The downloaded app does not match this release")
    minimum = tuple(map(int, metadata.get("LSMinimumSystemVersion", "14.0").split(".")))
    current = tuple(map(int, platform.mac_ver()[0].split(".")))
    if current < minimum:
        raise ValueError("This update requires a newer version of macOS")
    run("/usr/bin/codesign", "--verify", "--deep", "--strict", "--all-architectures", "-R", "=" + REQUIREMENT, str(app))
    assessment = run("/usr/sbin/spctl", "--assess", "--type", "execute", "--verbose=2", str(app))
    if "source=Notarized Developer ID" not in assessment:
        raise ValueError("The downloaded app is not notarized")


def pending(cache, target, current_version, current_build):
    record = cache / "pending.json"
    if not record.exists():
        return None
    value = json.loads(record.read_text())
    # Never accept a pending install aimed at another app or an arbitrary path.
    candidate = Path(value["path"])
    folder = candidate.parent
    if (value["target"] != str(target) or candidate.name != APP_NAME
            or folder.parent != target.parent or not folder.name.startswith(".provider-hub-update-")
            or folder.is_symlink() or candidate.is_symlink()):
        raise ValueError("The staged update location is invalid")
    if version_key(value["version"], value["build"]) <= version_key(current_version, current_build):
        record.unlink()
        return None
    verify_app(candidate, value["version"], value["build"])
    return value


def stage(candidate, cache, target):
    if (target.is_symlink() or not target.is_dir() or "/AppTranslocation/" in str(target)
            or not os.access(target.parent, os.W_OK) or not os.access(target, os.W_OK)):
        raise ValueError("Move Provider Hub to a writable Applications folder before updating")
    metadata = plistlib.loads((target / "Contents/Info.plist").read_bytes())
    if metadata.get("CFBundleIdentifier") != BUNDLE_ID:
        raise ValueError("The installed app is not Provider Hub")
    folder = Path(tempfile.mkdtemp(prefix=".provider-hub-update-", dir=target.parent))
    try:
        archive = folder / "download.zip"
        download(candidate, archive)
        inspect_archive(archive)
        run("/usr/bin/ditto", "-x", "-k", str(archive), str(folder))
        app = folder / APP_NAME
        verify_app(app, candidate["version"], candidate["build"])
        archive.unlink()
        value = {"version": candidate["version"], "build": candidate["build"],
                 "path": str(app), "target": str(target)}
        temporary = cache / ("pending-" + uuid.uuid4().hex + ".json")
        temporary.write_text(json.dumps(value))
        os.replace(temporary, cache / "pending.json")
        return value
    except BaseException:
        shutil.rmtree(folder)
        raise


def prepare(cache, target, version, build):
    value = pending(cache, target, version, build)
    if value is None:
        raise ValueError("The staged update is no longer available")
    installed = plistlib.loads((target / "Contents/Info.plist").read_bytes())
    if (installed.get("CFBundleIdentifier") != BUNDLE_ID
            or installed.get("CFBundleShortVersionString") != version or installed.get("CFBundleVersion") != build):
        raise ValueError("Provider Hub was replaced outside the updater. Quit and reopen it before updating.")
    script = cache / ("install-" + uuid.uuid4().hex + ".sh")
    shutil.copyfile(Path(__file__).with_name("update_install.sh"), script)
    script.chmod(0o600)
    return {**value, "script": str(script), "installedVersion": version, "installedBuild": build}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "stage", "pending", "prepare"))
    parser.add_argument("--version", required=True)
    parser.add_argument("--build", required=True)
    parser.add_argument("--cache", required=True, type=Path)
    parser.add_argument("--target", required=True, type=Path)
    parser.add_argument("--tag")
    args = parser.parse_args()
    try:
        args.cache.mkdir(parents=True, exist_ok=True, mode=0o700)
        if args.action in ("pending", "prepare"):
            result = (prepare if args.action == "prepare" else pending)(args.cache, args.target, args.version, args.build)
        else:
            candidate = release_candidate(fetch_release(args.tag), args.version, args.build)
            if args.action == "stage":
                if not args.tag or candidate is None or candidate["tag"] != args.tag:
                    raise ValueError("This release is no longer available")
                result = stage(candidate, args.cache, args.target)
            else:
                result = candidate
        print(json.dumps({"result": result}))
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile, subprocess.TimeoutExpired) as error:
        print(json.dumps({"error": str(error)}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
