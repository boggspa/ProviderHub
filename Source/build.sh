#!/bin/bash
set -euo pipefail
SOURCE_DIR="$(cd "$(dirname "$0")" && pwd)"
PACKAGE_DIR="$(dirname "$SOURCE_DIR")"
APP_DIR="$PACKAGE_DIR/Provider Hub Preview.app"
BUILD_DIR="${MISTRAL_BRIDGE_BUILD_DIR:-$SOURCE_DIR/.build}"
mkdir -p "$APP_DIR/Contents/MacOS" "$BUILD_DIR"
# This directory contains generated app content only. Start fresh so removed
# modules and old vendor files cannot survive into a newly signed release.
rm -rf "$APP_DIR/Contents/Resources/worker"
mkdir -p "$APP_DIR/Contents/Resources/worker"
SWIFT_SOURCES=(
  "$SOURCE_DIR/CatalogueSelection.swift" "$SOURCE_DIR/HubModels.swift"
  "$SOURCE_DIR/HubLayout.swift" "$SOURCE_DIR/ProviderViews.swift"
  "$SOURCE_DIR/DevinAgentsView.swift" "$SOURCE_DIR/CodexHarness.swift"
  "$SOURCE_DIR/HubTheme.swift"
  "$SOURCE_DIR/MistralBridge.swift"
)
xcrun swiftc -swift-version 5 -parse-as-library -O -target arm64-apple-macosx14.0 \
  -module-cache-path "$BUILD_DIR/ModuleCache" \
  -framework AppKit -framework SwiftUI -framework Security \
  "${SWIFT_SOURCES[@]}" \
  -o "$APP_DIR/Contents/MacOS/MistralBridge"
for module in bridge_core protocol gateway model_names fast_models catalogue hub_config providers provider_registry provider_discovery provider_requests devin_agent qwen_provider openrouter_provider gemini_provider branding cerebras_replay catalogue_lifecycle responses_native responses_tools responses_bridge responses_compact codex_catalogue codex_profile codex_token codex_runtime codex_accent effort_map chat_tool_order rate_limit spawn_depth subagent_catalogue ollama_lifecycle cli_session cli_lifecycle codex_session_pool cli_routes cli_auth_probe cli_tool_call cli_host_mcp cli_host_bridge cli_live_session host_tools_mcp cli_structured_reply cli_images cli_image_history claude_cli_agent codex_cli_agent agy_cli_agent agy_context muse_cli_agent grok_cli_agent claude_context; do
  cp "$SOURCE_DIR/$module.py" "$APP_DIR/Contents/Resources/worker/"
done
cp "$SOURCE_DIR/provider_branding.json" "$APP_DIR/Contents/Resources/worker/"
cp -R "$SOURCE_DIR/provider-logos" "$APP_DIR/Contents/Resources/worker/"
cp -R "$SOURCE_DIR/vendor" "$APP_DIR/Contents/Resources/worker/"
# Bytecode caches never ship: the worker writes its own cache into the hub's
# state directory at run time, and a cache inside Resources would be sealed
# into the signature only to go stale.
find "$APP_DIR/Contents/Resources/worker" -type d -name __pycache__ -prune -exec rm -rf {} +
if [ -n "${PROVIDER_HUB_PYTHON_RUNTIME:-}" ]; then
  if [ ! -x "$PROVIDER_HUB_PYTHON_RUNTIME/bin/python3" ]; then
    printf 'The supplied Python runtime is missing bin/python3.\n' >&2
    exit 1
  fi
  # This is generated app content; no user state is stored in the bundle.
  rm -rf "$APP_DIR/Contents/Resources/python"
  ditto "$PROVIDER_HUB_PYTHON_RUNTIME" "$APP_DIR/Contents/Resources/python"
  # Make the copied runtime distributable: prune the development subtrees the
  # worker never imports, rewrite its recorded build prefix (a personal
  # directory) to the neutral value CPython was configured with, then prove the
  # result still runs. This fails the build rather than shipping a stranger's
  # home directory or a runtime that cannot import cryptography.
  python3 "$PACKAGE_DIR/scripts/prepare_runtime.py" \
    "$APP_DIR/Contents/Resources/python" \
    --worker-dir "$APP_DIR/Contents/Resources/worker"
fi
# A bundled CPython arrives with its own bytecode caches, and any copied tree
# can carry .DS_Store droppings. Neither belongs in a signed, notarized
# distribution, so prune the whole bundle rather than only the worker.
find "$APP_DIR" -type d -name __pycache__ -prune -exec rm -rf {} +
find "$APP_DIR" -type f -name .DS_Store -delete
# Apache-2.0 4(a) and 4(d): the licence text and its notices travel with the
# distribution, not only with the source repository.
cp "$PACKAGE_DIR/LICENSE" "$PACKAGE_DIR/NOTICE" "$APP_DIR/Contents/Resources/"
cat > "$APP_DIR/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Provider Hub Preview</string>
  <key>CFBundleDisplayName</key><string>Provider Hub Preview</string>
  <key>CFBundleIdentifier</key><string>com.mistralbridge.providerhub</string>
  <key>CFBundleExecutable</key><string>MistralBridge</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.5.5</string>
  <key>CFBundleVersion</key><string>33</string>
  <key>BridgeStateName</key><string>Provider Hub Preview</string>
  <key>BridgeProfileID</key><string>14c58c94-d7e8-4a15-96b8-81668956e474</string>
  <key>BridgeDefaultPort</key><integer>11438</integer>
  <key>BridgeKeychainService</key><string>com.mistralbridge.providerhub</string>
  <key>BridgeSeedMistralMetadata</key><true/>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>LSUIElement</key><true/>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSAppleEventsUsageDescription</key><string>Open Mistral Vibe in Terminal when you choose Open Vibe.</string>
</dict></plist>
PLIST
if [ -f "$SOURCE_DIR/AppIcon.icns" ]; then cp "$SOURCE_DIR/AppIcon.icns" "$APP_DIR/Contents/Resources/"; fi
python3 "$SOURCE_DIR/build_provenance.py" write --source-dir "$SOURCE_DIR" \
  --app-dir "$APP_DIR" --swift-sources "${SWIFT_SOURCES[@]}"
if [ "${MISTRAL_BRIDGE_SKIP_CODESIGN:-0}" = "1" ]; then
  rm -rf "$APP_DIR/Contents/_CodeSignature"
else
  /usr/bin/codesign --force --sign - "$APP_DIR"
  /usr/bin/codesign --verify --strict "$APP_DIR"
fi
printf 'Built %s\n' "$APP_DIR"
