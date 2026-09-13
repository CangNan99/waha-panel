#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
STEP="checking installation"
fail() { printf 'BACKUP FAILED at %s: %s\n' "$STEP" "$1" >&2; exit 1; }
[[ -f .env ]] || fail 'Run install.sh first'
prefix="$(awk -F= '$1 == "PORTABLE_VOLUME_PREFIX" {print $2; exit}' .env)"
prefix="${prefix:-waha-release}"
backup_dir="$ROOT/backups/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$backup_dir"
docker compose version >/dev/null 2>&1 || fail 'docker compose is unavailable'
for name in panel_data sessions media; do
  STEP="backing up $name"
  docker run --rm -v "${prefix}_${name}:/source:ro" -v "$backup_dir:/backup" busybox:1.36 sh -c "tar -czf /backup/${name}.tar.gz -C /source ." || fail "backup failed for $name"
done
printf 'Backup created at %s\n' "$backup_dir"
