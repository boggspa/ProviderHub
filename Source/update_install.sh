#!/bin/sh
# Paths arrive as arguments, never interpolated shell code. This independent
# helper survives Provider Hub and replaces it only after its process exits.
set -eu
owner_pid=$1
target=$2
candidate=$3
installed_version=$4
installed_build=$5
case "$owner_pid" in ''|*[!0-9]*) exit 1;; esac
stage_dir=$(/usr/bin/dirname "$candidate")
backup="$stage_dir/previous.app"
while /bin/kill -0 "$owner_pid" 2>/dev/null; do /bin/sleep 1; done
[ -d "$target" ] && [ ! -L "$target" ] && [ -d "$candidate" ] && [ ! -L "$candidate" ]
[ ! -e "$backup" ]
# Recheck after the wait: an external install must never be overwritten, and
# candidate resources must still have the same developer signature.
[ "$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$target/Contents/Info.plist")" = "$installed_version" ]
[ "$(/usr/libexec/PlistBuddy -c 'Print :CFBundleVersion' "$target/Contents/Info.plist")" = "$installed_build" ]
/usr/bin/codesign --verify --deep --strict --all-architectures -R '=anchor apple generic and identifier "com.mistralbridge.providerhub" and certificate leaf[subject.OU] = "8CZML8FK2D" and certificate 1[field.1.2.840.113635.100.6.2.6] exists and certificate leaf[field.1.2.840.113635.100.6.1.13] exists' "$candidate"
/usr/sbin/spctl --assess --type execute --verbose=2 "$candidate"
# The original bundle is retained as a rollback copy. A failed install or
# relaunch puts it back; an interrupted swap can also be recovered manually.
/bin/mv "$target" "$backup"
if ! /bin/mv "$candidate" "$target"; then
    /bin/mv "$backup" "$target"
    /usr/bin/open -n "$target"
    exit 1
fi
if ! /usr/bin/open -n "$target"; then
    /bin/mv "$target" "$candidate"
    /bin/mv "$backup" "$target"
    /usr/bin/open -n "$target"
    exit 1
fi
