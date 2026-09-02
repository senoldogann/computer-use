#!/usr/bin/env bash
# Create the stable code-signing identity used by package_app.sh.
#
# WHY (root cause): ad-hoc signatures (`codesign --sign -`) get a designated
# requirement derived from the executable's cdhash, which changes on every
# rebuild. macOS TCC binds the Accessibility grant to that requirement, so
# every reinstall silently orphans the user's permission — the grant "keeps
# turning off". A self-signed certificate makes the requirement
# (certificate-leaf based) stable across rebuilds, so the permission granted
# once survives every future reinstall. No Apple Developer account needed.
#
# Idempotent: re-running after the identity exists is a no-op.
set -euo pipefail

CERT_NAME="ComputerUse Dev"
LOGIN_KC="$HOME/Library/Keychains/login.keychain-db"
WORK_DIR="${TMPDIR:-/tmp}/cu-signing-cert"

if security find-identity -v -p codesigning 2>/dev/null | grep -q "\"$CERT_NAME\""; then
    echo "==> Signing identity '$CERT_NAME' already present; nothing to do."
    exit 0
fi

echo "==> Creating self-signed code-signing identity '$CERT_NAME' (10 years)..."
mkdir -p "$WORK_DIR"
cd "$WORK_DIR"

# -addext extendedKeyUsage=codeSigning is REQUIRED: without it the cert never
# appears in `security find-identity -p codesigning`.
openssl req -x509 -newkey rsa:2048 -sha256 \
    -keyout cu-key.pem -out cu-cert.pem -days 3650 -nodes \
    -subj "/CN=$CERT_NAME" \
    -addext "extendedKeyUsage=codeSigning" \
    -addext "keyUsage=digitalSignature" 2>/dev/null

# Import cert + private key into the login keychain, allowing /usr/bin/codesign
# to use the key without a GUI prompt.
security import cu-cert.pem -k "$LOGIN_KC" -T /usr/bin/codesign
security import cu-key.pem -k "$LOGIN_KC" -T /usr/bin/codesign

# A self-signed cert is untrusted by default, so find-identity filters it out.
# Trust it as a root in the USER domain for code-signing policy only (no
# admin/sudo required for the login keychain).
security add-trusted-cert -d -r trustRoot -p codeSign -k "$LOGIN_KC" cu-cert.pem

if security find-identity -v -p codesigning 2>/dev/null | grep -q "\"$CERT_NAME\""; then
    echo "==> Done. '$CERT_NAME' is ready; package_app.sh will use it."
else
    echo "ERROR: identity still not listed; re-run and check the keychain." >&2
    exit 1
fi