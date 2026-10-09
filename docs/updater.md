# Updating Provider Hub

The compact Hub and lean Chat mastheads show a small **Update** pill when a
newer stable release is available on GitHub. Provider Hub checks at launch,
after waking, and every six hours. Offline checks stay quiet. Only public
release metadata is requested; no account credentials are sent.

Click **Update** to download and verify the notarized app. If the app is idle,
it shuts down its workers and gateway, replaces its bundle and reopens. If
Codex / ChatGPT or Claude is open, or a lean Chat turn, Side Chat, delegated
task or gateway operation is active, the download stays staged and the pill
becomes **Restart**. Closing those apps or finishing the work does not trigger
a restart: click **Restart** when ready. Unsaved settings and chat drafts also
defer restart.

The pending download survives quitting and reopening Provider Hub. The running
app's resources stay intact until shutdown finishes. Installation requires a
writable app and parent folder; moving Provider Hub into a writable Applications
folder also avoids macOS app translocation. No privileged helper is installed.

## Verification and recovery

The updater accepts one official `ProviderHub-<version>-build<build>-<commit>-notarized.zip`
asset from a stable `v<version>-build<build>` GitHub release. It compares both
the marketing version and numeric build, verifies GitHub's SHA-256 digest,
checks archive paths and symlinks before extraction, then requires the expected
bundle ID, release version, Developer ID team signature and Gatekeeper's
**Notarized Developer ID** assessment. It verifies the staged app again before
restart and before installation.

The installer waits for Provider Hub's process to exit, then moves the existing
bundle to `previous.app` in the private `.provider-hub-update-*` folder beside
the installation. If replacement or relaunch fails, it restores that bundle.
The rollback copy is retained after success. An external replacement with a
different version/build is preserved. The pending record and installer log
live in the Hub state folder's `updates` directory.

This updater first ships in the release containing this change. Older copies
need one manual download before they can show the Update pill. The classic Hub
layout continues to use its existing header; the pill appears in compact Hub
and lean Chat, where the app logo and name share the traffic-light row.
