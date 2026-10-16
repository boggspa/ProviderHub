#!/bin/bash
set -euo pipefail

app_dir="$(cd "$(dirname "$0")/.." && pwd)"
repo_root="$(cd "$app_dir/../.." && pwd)"
release_dir="$repo_root/.local-only/companion-release"
: "${ASC_API_KEY_ID:?Set ASC_API_KEY_ID}"
: "${ASC_API_ISSUER_ID:?Set ASC_API_ISSUER_ID}"
: "${ASC_API_KEY_PATH:?Set ASC_API_KEY_PATH}"
test -f "$ASC_API_KEY_PATH"
mkdir -p "$release_dir"
xcodegen generate --spec "$app_dir/project.yml"

auth_args=(-allowProvisioningUpdates -authenticationKeyPath "$ASC_API_KEY_PATH"
    -authenticationKeyID "$ASC_API_KEY_ID" -authenticationKeyIssuerID "$ASC_API_ISSUER_ID")
xcodebuild -project "$app_dir/ProviderHubCompanion.xcodeproj" \
    -scheme ProviderHubCompanion -configuration Release -destination 'generic/platform=iOS' \
    -archivePath "$release_dir/ProviderHubCompanion.xcarchive" \
    -derivedDataPath "$release_dir/DerivedData" \
    "${auth_args[@]}" archive

cat > "$release_dir/ExportOptions.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>method</key><string>app-store-connect</string>
<key>destination</key><string>export</string>
<key>signingStyle</key><string>automatic</string>
<key>teamID</key><string>8CZML8FK2D</string>
<key>manageAppVersionAndBuildNumber</key><false/>
<key>uploadSymbols</key><true/>
</dict></plist>
PLIST
xcodebuild -exportArchive -archivePath "$release_dir/ProviderHubCompanion.xcarchive" \
    -exportPath "$release_dir/export" -exportOptionsPlist "$release_dir/ExportOptions.plist" \
    "${auth_args[@]}"

inspection_dir="$(mktemp -d "$release_dir/inspect.XXXXXX")"
trap 'rm -rf "$inspection_dir"' EXIT
ditto -x -k "$release_dir/export/ProviderHubCompanion.ipa" "$inspection_dir"
exported_app="$inspection_dir/Payload/ProviderHubCompanion.app"
codesign --verify --deep --strict "$exported_app"
security cms -D -i "$exported_app/embedded.mobileprovision" > "$inspection_dir/profile.plist"
/usr/libexec/PlistBuddy -c 'Print :Entitlements:application-identifier' "$inspection_dir/profile.plist" | \
    /usr/bin/grep -qx '8CZML8FK2D.dev.providerhub.companion'
test "$(/usr/libexec/PlistBuddy -c 'Print :Entitlements:get-task-allow' "$inspection_dir/profile.plist")" = false
test -f "$exported_app/PrivacyInfo.xcprivacy"

echo "IPA: $release_dir/export/ProviderHubCompanion.ipa"
echo "Upload separately after reviewing the archive and provisioning."
