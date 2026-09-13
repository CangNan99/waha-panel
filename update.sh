#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
COMPONENT="${1:-panel}"
[[ "$COMPONENT" == 'panel' || "$COMPONENT" == 'waha' ]] || { printf 'Usage: %s panel|waha\n' "$0" >&2; exit 2; }
STEP="starting"
fail() { printf 'UPDATE FAILED at %s: %s\n' "$STEP" "$1" >&2; exit 1; }
get_env() { awk -F= -v key="$1" '$1 == key {sub(/^[^=]*=/, ""); print; exit}' .env; }
set_env() {
  key="$1" value="$2" tmp=".env.tmp.$$"
  awk -v key="$key" -v value="$value" 'BEGIN { done=0 } $0 ~ "^" key "=" { print key "=" value; done=1; next } { print } END { if (!done) print key "=" value }' .env > "$tmp"
  mv "$tmp" .env
}

STEP="checking installation"
[[ -f .env && -f secrets/waha_credentials ]] || fail 'Run install.sh first'
docker compose version >/dev/null 2>&1 || fail 'docker compose is unavailable'
command -v curl >/dev/null 2>&1 || fail 'curl is required'
command -v python3 >/dev/null 2>&1 || fail 'python3 is required to parse Docker Hub metadata'

if [[ "$COMPONENT" == 'panel' ]]; then
  repo='cangnan88/waha-panel'; current_key='PANEL_VERSION'; image_key='PANEL_IMAGE'; service='waha-panel'
else
  repo='devlikeapro/waha'; current_key='WAHA_IMAGE_TAG'; image_key='WAHA_IMAGE'; service='waha'
fi
STEP="reading public release tags"
tags_json="$(curl -fsSL --max-time 15 "https://hub.docker.com/v2/repositories/$repo/tags?page_size=100&ordering=last_updated")" || fail 'Docker Hub metadata request failed'
latest="$(printf '%s' "$tags_json" | python3 -c 'import json,re,sys; d=json.load(sys.stdin); c=[]; w=sys.argv[1];
for x in d.get("results",[]):
 t=str(x.get("name","")); m=re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)",t) if w=="panel" else re.fullmatch(r"latest-(\d{4})\.(\d{1,2})\.(\d{1,2})",t)
 if m: c.append((tuple(map(int,m.groups())),t))
print(max(c)[1] if c else "")' "$COMPONENT")"
[[ -n "$latest" ]] || fail 'No stable release tag was found'
current="$(get_env "$current_key" || true)"
printf 'Component: %s\nCurrent:   %s\nLatest:    %s\n' "$COMPONENT" "$current" "$latest"
[[ "$current" == "$latest" ]] && { printf 'Already current.\n'; exit 0; }

backup=".env.before-update-$(date +%Y%m%d-%H%M%S)"
cp -p .env "$backup"
if [[ "$COMPONENT" == 'panel' ]]; then image="docker.io/cangnan88/waha-panel:$latest"; else image="devlikeapro/waha:$latest"; fi
STEP="updating release selection"; set_env "$image_key" "$image"; set_env "$current_key" "$latest"
if ! { STEP="pulling selected image"; docker compose pull "$service" && STEP="recreating selected service"; docker compose up -d --no-deps "$service"; }; then
  cp -p "$backup" .env
  fail 'service update failed; .env was restored'
fi
printf 'Updated %s to %s. Other service and persistent volumes were left unchanged.\n' "$COMPONENT" "$latest"
