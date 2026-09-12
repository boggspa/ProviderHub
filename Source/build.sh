#!/bin/bash
set -euo pipefail
SOURCE_DIR="$(cd "$(dirname "$0")" && pwd)"
PACKAGE_DIR="$(dirname "$SOURCE_DIR")"
APP_DIR="$PACKAGE_DIR/Mistral Bridge.app"
BUILD_DIR="${MISTRAL_BRIDGE_BUILD_DIR:-$SOURCE_DIR/.build}"
mkdir -p "$APP_DIR/Contents/MacOS" "$APP_DIR/Contents/Resources/worker" "$BUILD_DIR"
xcrun swiftc -swift-version 5 -parse-as-library -O -target arm64-apple-macosx14.0 \
  -module-cache-path "$BUILD_DIR/ModuleCache" \
  -framework AppKit -framework SwiftUI -framework Security \
  "$SOURCE_DIR/MistralBridge.swift" -o "$APP_DIR/Contents/MacOS/MistralBridge"
cp "$SOURCE_DIR/bridge_core.py" "$SOURCE_DIR/protocol.py" "$SOURCE_DIR/gateway.py" "$SOURCE_DIR/model_names.py" "$SOURCE_DIR/catalogue.py" "$APP_DIR/Contents/Resources/worker/"
cat > "$APP_DIR/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Mistral Bridge</string>
  <key>CFBundleDisplayName</key><string>Mistral Bridge</string>
  <key>CFBundleIdentifier</key><string>com.mistralbridge.local</string>
  <key>CFBundleExecutable</key><string>MistralBridge</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.2.0</string>
  <key>CFBundleVersion</key><string>3</string>
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
