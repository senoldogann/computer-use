#!/bin/sh
# computeruse Desktop .app paketleyicisi (Aşama 1).
# SwiftPM çıktısını gerçek bir macOS uygulama paketine sarar; aksi halde
# çıplak binary BackgroundOnly olarak açılır ve pencere çizemez.
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BIN="$ROOT/.build/arm64-apple-macosx/debug/ComputerUseDesktop"
APP="$ROOT/ComputerUseDesktop.app"

swift build --package-path "$ROOT"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN" "$APP/Contents/MacOS/ComputerUseDesktop"
cp "$ROOT/packaging/Info.plist" "$APP/Contents/Info.plist"
printf 'APPL????' > "$APP/Contents/PkgInfo"
chmod +x "$APP/Contents/MacOS/ComputerUseDesktop"

# Stable code signature ("ComputerUse Dev" self-signed cert, see
# scripts/make_signing_cert.sh). TCC binds the Screen Recording grant to the
# signing identity: an unsigned (or ad-hoc signed) binary gets a new identity
# on every rebuild and silently loses the grant. Sign with the stable cert so
# a grant issued once survives all future rebuilds.
if security find-identity -v -p codesigning 2>/dev/null | grep -q '"ComputerUse Dev"'; then
    codesign --force --sign "ComputerUse Dev" "$APP/Contents/MacOS/ComputerUseDesktop"
    echo "signed: ComputerUse Dev"
else
    echo "warning: 'ComputerUse Dev' identity missing (run scripts/make_signing_cert.sh); Screen Recording grant will not survive rebuilds" >&2
fi
echo "packaged: $APP"
