#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="$ROOT_DIR/dist/ComputerUse.app"
MACOS_DIR="$APP_DIR/Contents/MacOS"
USER_APPS="$HOME/Applications"

echo "==> Building Rust binaries (release)..."
cargo build --manifest-path "$ROOT_DIR/driver/Cargo.toml" --release --bin actuation-driver --bin actuation-menu

mkdir -p "$MACOS_DIR" "$APP_DIR/Contents/Resources" "$USER_APPS"

# Info.plist is generated, not committed: dist/ is gitignored, so a fresh
# clone had no bundle skeleton and this script silently produced an app macOS
# refused to launch. Generating it here makes packaging reproducible from a
# bare checkout, and keeps the usage strings (which the TCC prompts show the
# user verbatim) versioned with the code that needs those permissions.
cat > "$APP_DIR/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleIdentifier</key>
    <string>com.computeruse.app</string>
    <key>CFBundleName</key>
    <string>ComputerUse</string>
    <key>CFBundleDisplayName</key>
    <string>Computer Use</string>
    <key>CFBundleExecutable</key>
    <string>ComputerUse</string>
    <key>CFBundleVersion</key>
    <string>1.0.0</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0.0</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>LSUIElement</key>
    <string>1</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>NSAccessibilityUsageDescription</key>
    <string>Computer Use requires accessibility permissions to perceive screen elements and simulate natural human cursor and keyboard actuation.</string>
    <key>NSScreenCaptureUsageDescription</key>
    <string>Computer Use requires screen recording permissions to visually perceive desktop state and navigate applications.</string>
</dict>
</plist>
PLIST

echo "==> Packaging as ComputerUse (release)..."
cp "$ROOT_DIR/driver/target/release/actuation-menu" "$MACOS_DIR/ComputerUse"
cp "$ROOT_DIR/driver/target/release/actuation-menu" "$MACOS_DIR/actuation-menu"
cp "$ROOT_DIR/driver/target/release/actuation-driver" "$MACOS_DIR/actuation-driver"
chmod +x "$MACOS_DIR/ComputerUse" "$MACOS_DIR/actuation-menu" "$MACOS_DIR/actuation-driver"

# Stable code-signing identity (see scripts/make_signing_cert.sh for WHY):
# ad-hoc signatures rebind the macOS TCC Accessibility grant on every rebuild
# (cdhash requirement), which is why the user's permission "keeps turning
# off". With the self-signed cert the requirement survives reinstalls.
SIGN_ID="ComputerUse Dev"
if security find-identity -v -p codesigning 2>/dev/null | grep -q "\"$SIGN_ID\""; then
    echo "==> Code-signing with stable identity: $SIGN_ID"
    # Sign each nested binary with a STABLE identifier before bundling; the
    # bundle itself is signed WITHOUT --deep so these nested signatures (and
    # their stable requirements) are preserved instead of being re-signed
    # with fresh cdhash-based identifiers.
    codesign --force -s "$SIGN_ID" --identifier com.computeruse.menu "$MACOS_DIR/ComputerUse"
    codesign --force -s "$SIGN_ID" --identifier com.computeruse.menu "$MACOS_DIR/actuation-menu"
    codesign --force -s "$SIGN_ID" --identifier com.computeruse.driver "$MACOS_DIR/actuation-driver"
    codesign --force -s "$SIGN_ID" "$APP_DIR"
else
    echo "==> WARNING: identity '$SIGN_ID' not found; falling back to ad-hoc" >&2
    echo "    (run scripts/make_signing_cert.sh once — otherwise Accessibility" >&2
    echo "    grants will not survive reinstalls)" >&2
    SIGN_ID="-"
    codesign --force --deep --sign - "$APP_DIR"
fi

echo "==> Installing to $USER_APPS/ComputerUse.app..."
rm -rf "$USER_APPS/ComputerUse.app"
cp -R "$APP_DIR" "$USER_APPS/ComputerUse.app"

# The installed copy is byte-identical to the packaged one; re-signing it with
# the same identity keeps its signature verifiable at its final location.
echo "==> Code-signing installed bundle..."
codesign --force -s "$SIGN_ID" "$USER_APPS/ComputerUse.app"

echo "==> Configuring ~/.computeruse/root..."
mkdir -p "$HOME/.computeruse"
echo "$ROOT_DIR" > "$HOME/.computeruse/root"

echo "==> Done! Installed at: $USER_APPS/ComputerUse.app"
