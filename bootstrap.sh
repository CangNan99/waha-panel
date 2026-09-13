#!/usr/bin/env bash
set -Eeuo pipefail

REPO_SLUG="${WAHA_PANEL_REPO:-Cangnan99/waha-panel}"
REF="${WAHA_PANEL_REF:-main}"
INSTALL_DIR="${WAHA_PANEL_INSTALL_DIR:-$PWD/waha-panel}"
STEP="starting"

fail() {
  printf 'BOOTSTRAP FAILED at %s: %s\n' "$STEP" "$1" >&2
  exit 1
}

command -v curl >/dev/null 2>&1 || fail 'curl is required'
command -v tar >/dev/null 2>&1 || fail 'tar is required'
[[ "$REPO_SLUG" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || fail 'WAHA_PANEL_REPO is invalid'
[[ "$REF" =~ ^[A-Za-z0-9._/-]+$ ]] || fail 'WAHA_PANEL_REF is invalid'

if [[ -e "$INSTALL_DIR" ]]; then
  fail "install directory already exists: $INSTALL_DIR; run its install.sh directly"
fi

TEMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/waha-panel-bootstrap.XXXXXX")"
cleanup() { rm -rf "$TEMP_DIR"; }
trap cleanup EXIT

STEP="downloading GitHub release"
ARCHIVE="$TEMP_DIR/release.tar.gz"
URL="https://github.com/${REPO_SLUG}/archive/refs/heads/${REF}.tar.gz"
curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 "$URL" -o "$ARCHIVE" \
  || fail 'GitHub release download failed'

STEP="extracting GitHub release"
mkdir -p "$INSTALL_DIR"
tar -xzf "$ARCHIVE" --strip-components=1 -C "$INSTALL_DIR" \
  || fail 'GitHub release extraction failed'
[[ -f "$INSTALL_DIR/install.sh" && -f "$INSTALL_DIR/docker-compose.yml" ]] \
  || fail 'GitHub release is missing installation files'

STEP="starting local installer"
chmod +x "$INSTALL_DIR/install.sh" "$INSTALL_DIR/update.sh" "$INSTALL_DIR/backup.sh"
exec "$INSTALL_DIR/install.sh"
