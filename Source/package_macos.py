#!/usr/bin/env python3
"""Sign a generated Apple Silicon app and optionally submit it to Apple."""
import argparse
import json
from pathlib import Path
import re
import struct
import subprocess


def run(arguments):
    result = subprocess.run(arguments, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "Packaging command failed.")
    return result.stdout


def loadable_macho(path):
    if path.is_symlink() or not path.is_file():
        return False
    with path.open("rb") as stream:
        header = stream.read(16)
    if len(header) < 16:
        return False
    if header[:4] in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe"):
        return struct.unpack("<I", header[12:16])[0] in {2, 6, 8}
    if header[:4] in (b"\xfe\xed\xfa\xcf", b"\xfe\xed\xfa\xce"):
        return struct.unpack(">I", header[12:16])[0] in {2, 6, 8}
    return header[:4] in (b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf") and struct.unpack(">I", header[4:8])[0] <= 16


def resolve_identity(identity):
    """Return a unique signing identity (SHA-1 hash preferred).

    codesign rejects a human-readable name when two certificates share it
    ("ambiguous" error). Resolve names against the keychain and pick the
    unique match; if still ambiguous, fail with the exact hashes so the
    user can pass one directly.
    """
    if re.fullmatch(r"[0-9A-Fa-f]{40}", identity):
        return identity
    output = run(["security", "find-identity", "-v", "-p", "codesigning"])
    matches = []
    for line in output.splitlines():
        match = re.search(r"\b([0-9A-F]{40})\s+\"([^\"]+)\"", line)
        if match and match.group(2) == identity:
            matches.append(match.group(1))
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise RuntimeError(
            f"Signing identity {identity!r} is ambiguous; matches:\n"
            + "\n".join(f"  {sha}" for sha in matches)
            + "\nPass one of the SHA-1 hashes above as --identity.")
    raise RuntimeError(f"Signing identity {identity!r} not found in keychain.")


def archive(app, output):
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()
    run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(app), str(output)])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--identity", required=True, help="Developer ID Application signing identity or certificate SHA-1")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--submit", action="store_true")
    parser.add_argument("--notary-profile", help="Existing notarytool Keychain profile; never a password")
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()
    app = args.app.resolve()
    if app.suffix != ".app" or not (app / "Contents/Info.plist").is_file():
        parser.error("--app must point to a generated macOS app bundle")
    if args.submit and not args.notary_profile:
        parser.error("--submit requires --notary-profile")
    identity = resolve_identity(args.identity)
    components = sorted((path for path in (app / "Contents").rglob("*")
                         if loadable_macho(path) and "MacOS" not in path.relative_to(app / "Contents").parts[:1]),
                        key=lambda path: len(path.parts), reverse=True)
    print(f"Signing {len(components)} embedded native components…", flush=True)
    for index, path in enumerate(components, 1):
        run(["codesign", "--force", "--sign", identity, "--options", "runtime", "--timestamp", str(path)])
        if index % 15 == 0:
            print(f"Signed {index}/{len(components)} components", flush=True)
    run(["codesign", "--force", "--sign", identity, "--options", "runtime", "--timestamp", str(app)])
    run(["codesign", "--verify", "--deep", "--strict", str(app)])
    archive(app, args.output)
    print("Signed archive ready: " + str(args.output), flush=True)
    if args.submit:
        result = json.loads(run(["xcrun", "notarytool", "submit", str(args.output),
                                 "--keychain-profile", args.notary_profile, "--output-format", "json"]))
        if args.receipt:
            args.receipt.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps({"submission_id": result.get("id"), "status": result.get("status")}), flush=True)


if __name__ == "__main__":
    main()
