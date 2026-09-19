#!/bin/bash
set -euo pipefail
SOURCE_DIR="$(cd "$(dirname "$0")" && pwd)"
PACKAGE_DIR="$(dirname "$SOURCE_DIR")"
APP_DIR="$PACKAGE_DIR/Provider Hub Preview.app"
BUILD_DIR="${MISTRAL_BRIDGE_BUILD_DIR:-$SOURCE_DIR/.build}"
mkdir -p "$APP_DIR/Contents/MacOS" "$APP_DIR/Contents/Resources/worker" "$BUILD_DIR"
xcrun swiftc -swift-version 5 -parse-as-library -O -target arm64-apple-macosx14.0 \
  -module-cache-path "$BUILD_DIR/ModuleCache" \
  -framework AppKit -framework SwiftUI -framework Security \
  "$SOURCE_DIR/HubModels.swift" "$SOURCE_DIR/ProviderViews.swift" "$SOURCE_DIR/DevinAgentsView.swift" "$SOURCE_DIR/CodexHarness.swift" "$SOURCE_DIR/MistralBridge.swift" \
  -o "$APP_DIR/Contents/MacOS/MistralBridge"
for module in bridge_core protocol gateway model_names catalogue hub_config providers devin_agent qwen_provider openrouter_provider gemini_provider branding cerebras_replay catalogue_lifecycle responses_native responses_tools responses_bridge codex_catalogue codex_profile codex_token codex_runtime codex_accent effort_map chat_tool_order rate_limit spawn_depth ollama_lifecycle cli_session cli_routes cli_auth_probe claude_cli_agent codex_cli_agent agy_cli_agent muse_cli_agent grok_cli_agent; do
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
fi
cat > "$APP_DIR/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Provider Hub Preview</string>
  <key>CFBundleDisplayName</key><string>Provider Hub Preview</string>
  <key>CFBundleIdentifier</key><string>com.mistralbridge.providerhub</string>
  <key>CFBundleExecutable</key><string>MistralBridge</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.5.3</string>
  <key>CFBundleVersion</key><string>15</string>
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
/usr/bin/codesign --force --sign - "$APP_DIR"
/usr/bin/codesign --verify --strict "$APP_DIR"
printf 'Built %s\n' "$APP_DIR"
