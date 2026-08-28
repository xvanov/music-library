#!/usr/bin/env bash
# Deploy the music server stack to ~/music-server on the local machine.
# Run this ON the server (or via ssh). Requires docker + docker compose.
set -euo pipefail

TARGET="${MUSIC_SERVER_DIR:-$HOME/music-server}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> deploying to $TARGET"
mkdir -p "$TARGET/music" "$TARGET/data" "$TARGET/tagger"

cp "$REPO_DIR/server/docker-compose.yml" "$TARGET/docker-compose.yml"
cp "$REPO_DIR/server/tagger/"*.py "$TARGET/tagger/"

if [ ! -f "$TARGET/.env" ]; then
  cp "$REPO_DIR/server/.env.example" "$TARGET/.env"
  # sensible local defaults
  sed -i "s|^PUID=.*|PUID=$(id -u)|" "$TARGET/.env"
  sed -i "s|^PGID=.*|PGID=$(id -g)|" "$TARGET/.env"
  if command -v tailscale >/dev/null 2>&1; then
    IP="$(tailscale ip -4 2>/dev/null | head -1)"
    [ -n "$IP" ] && sed -i "s|^TAILNET_IP=.*|TAILNET_IP=$IP|" "$TARGET/.env"
  fi
  chmod 600 "$TARGET/.env"
  echo "==> wrote $TARGET/.env — SET UPLOAD_PASS before continuing"
  echo "    then re-run this script"
  exit 1
fi

if grep -q '^UPLOAD_PASS=CHANGEME' "$TARGET/.env"; then
  echo "!! UPLOAD_PASS is still CHANGEME in $TARGET/.env" >&2
  exit 1
fi

cd "$TARGET"
docker compose up -d
sleep 10
docker compose ps

# shellcheck disable=SC1091
. "$TARGET/.env"
echo "==> checks"
curl -fsS -o /dev/null -w "navidrome=%{http_code}\n" "http://${TAILNET_IP}:4533/ping" || true
curl -fsS -u "admin:${UPLOAD_PASS}" -o /dev/null -w "uploader=%{http_code}\n" "http://${TAILNET_IP}:4534/" || true

cat <<NOTE

Next:
  1. open  http://${TAILNET_IP}:4533   and create the admin user
  2. optional HTTPS for the iOS Files app:
       tailscale serve --bg --https=8445 http://${TAILNET_IP}:4534
  3. point Amperfy at http://${TAILNET_IP}:4533 (login type: Subsonic)
NOTE
