#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
STEP="starting"

fail() {
  printf 'INSTALL FAILED at %s: %s\n' "$STEP" "$1" >&2
  exit 1
}

new_secret() { openssl rand 32 | openssl base64 -A | tr '+/' '-_'; }

STEP="checking Docker Compose"
docker compose version >/dev/null 2>&1 || fail 'docker compose is unavailable'
command -v openssl >/dev/null 2>&1 || fail 'openssl is required'

mkdir -p "$ROOT/secrets"
chmod 700 "$ROOT/secrets"
if [[ ! -f "$ROOT/.env" ]]; then
  STEP="generating installation secrets"
  waha_api_key="$(new_secret)"
  waha_dashboard_password="$(new_secret)"
  webhook_secret="$(new_secret)"
  panel_data_key="$(new_secret)"
  admin_password="$(new_secret)"
  umask 077
  printf '%s\n' \
    "WAHA_API_KEY=$waha_api_key" \
    'WAHA_IMAGE=devlikeapro/waha:latest-2026.8.2' \
    'WAHA_IMAGE_TAG=latest-2026.8.2' \
    'WAHA_DASHBOARD_USERNAME=admin' \
    "WAHA_DASHBOARD_PASSWORD=$waha_dashboard_password" \
    "WAHA_WEBHOOK_SECRET=$webhook_secret" \
    "PANEL_DATA_ENCRYPTION_KEY=$panel_data_key" \
    'PANEL_IMAGE=docker.io/cangnan88/waha-panel:1.0.0' \
    'PANEL_VERSION=1.0.0' \
    'PANEL_PORT=3003' 'PANEL_BIND_ADDRESS=127.0.0.1' \
    'WAHA_PORT=3002' 'WAHA_BIND_ADDRESS=127.0.0.1' \
    'PORTABLE_VOLUME_PREFIX=waha-release' \
    'PORTABLE_NETWORK_NAME=waha-release-internal' \
    'PANEL_SPONSOR_ENABLED=0' \
    'PANEL_SPONSOR_IMAGE_URL=https://www.6spring.com/wp-content/uploads/2026/09/cangnan.jpg' > "$ROOT/.env"
  printf '%s\n' \
    "WAHA_API_KEY=$waha_api_key" \
    "WAHA_WEBHOOK_SECRET=$webhook_secret" \
    'WAHA_DASHBOARD_USERNAME=admin' \
    "WAHA_DASHBOARD_PASSWORD=$waha_dashboard_password" > "$ROOT/secrets/waha_credentials"
  printf '%s\n' 'PANEL_ADMIN_USERNAME=admin' "PANEL_ADMIN_PASSWORD=$admin_password" > "$ROOT/secrets/panel_admin_bootstrap"
  chmod 600 "$ROOT/.env" "$ROOT/secrets/waha_credentials" "$ROOT/secrets/panel_admin_bootstrap"
else
  STEP="checking existing installation files"
  [[ -f "$ROOT/secrets/waha_credentials" ]] || fail 'existing .env has a missing WAHA credentials file'
  admin_password=""
fi

STEP="pulling release images"
docker compose pull || fail 'docker compose pull failed'
STEP="starting isolated release services"
docker compose up -d || fail 'docker compose up failed'
STEP="checking panel response"
PANEL_READY_TIMEOUT=90
PANEL_READY_INTERVAL=2
deadline=$((SECONDS + PANEL_READY_TIMEOUT))
status="000"
printf 'Waiting for panel to become ready (up to %ss)...\n' "$PANEL_READY_TIMEOUT"
while (( SECONDS < deadline )); do
  status="$(curl -ksS -o /dev/null -w '%{http_code}' --connect-timeout 3 --max-time 5 http://127.0.0.1:3003/ 2>/dev/null || true)"
  if [[ "$status" == '200' || "$status" == '401' ]]; then
    break
  fi
  sleep "$PANEL_READY_INTERVAL"
done
[[ "$status" == '200' || "$status" == '401' ]] || fail "panel did not become ready within ${PANEL_READY_TIMEOUT}s (last HTTP status ${status:-000}); run: docker compose logs --tail=100 waha-panel"

printf 'Portable WAHA + panel release is running.\n'
printf 'Panel: http://127.0.0.1:3003/\nWAHA:  http://127.0.0.1:3002/\n'
if [[ -n "$admin_password" ]]; then
  printf 'Initial panel administrator: admin\n'
  printf 'Initial panel password (shown once): %s\n' "$admin_password"
else
  printf 'Existing administrator credentials were preserved.\n'
fi
