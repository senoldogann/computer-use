#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="$ROOT_DIR/dist/ComputerUse.app"
MACOS_DIR="$APP_DIR/Contents/MacOS"
USER_APPS="$HOME/Applications"

echo "==> Building Rust binaries (release)..."
cargo build --manifest-path "$ROOT_DIR/driver/Cargo.toml" --release --bin actuation-driver --bin actuation-menu

mkdir -p "$MACOS_DIR" "$APP_DIR/Contents/Resources" "$USER_APPS"

echo "==> Packaging as ComputerUse (release)..."
cp "$ROOT_DIR/driver/target/release/actuation-menu" "$MACOS_DIR/ComputerUse"
cp "$ROOT_DIR/driver/target/release/actuation-menu" "$MACOS_DIR/actuation-menu"
cp "$ROOT_DIR/driver/target/release/actuation-driver" "$MACOS_DIR/actuation-driver"
chmod +x "$MACOS_DIR/ComputerUse" "$MACOS_DIR/actuation-menu" "$MACOS_DIR/actuation-driver"

echo "==> Installing to $USER_APPS/ComputerUse.app..."
rm -rf "$USER_APPS/ComputerUse.app"
cp -R "$APP_DIR" "$USER_APPS/ComputerUse.app"

echo "==> Code-signing application bundles..."
codesign --force --deep --sign - "$APP_DIR"
codesign --force --deep --sign - "$USER_APPS/ComputerUse.app"

echo "==> Configuring ~/.computeruse/root..."
mkdir -p "$HOME/.computeruse"
echo "$ROOT_DIR" > "$HOME/.computeruse/root"

echo "==> Done! Installed at: $USER_APPS/ComputerUse.app"
