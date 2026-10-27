#!/usr/bin/env bash
# Run `mlib pod sync` hourly on the music server, as a systemd *user* timer.
# Run this ON the server, from the repo clone. Safe to re-run: it rewrites the
# units and restarts the timer. mlib reads .env from the repo, which on the
# server should set MLIB_MUSIC_DIR (direct mode) and the MLIB_ND_* login.
# Subscriptions are in the library itself (<music>/.mlib/podcasts.json), shared
# with any laptop that runs `mlib pod add`; the server needs no catalog/ clone.
# A run with no subscriptions, or with a failed show, exits 1 - the unit then
# shows as failed in `systemctl --user status mlib-podcasts.service`.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
NAME=mlib-podcasts

if [ -x "$REPO_DIR/.venv/bin/python" ]; then
  PYTHON="$REPO_DIR/.venv/bin/python"
else
  PYTHON="${PYTHON:-$(command -v python3)}"
fi
(cd "$REPO_DIR" && "$PYTHON" -c "import mlib.podcasts") || {
  echo "!! $PYTHON cannot import mlib - pip install -e $REPO_DIR first" >&2; exit 1; }

if ! grep -q '^MLIB_MUSIC_DIR=' "$REPO_DIR/.env" 2>/dev/null; then
  echo "!! $REPO_DIR/.env has no MLIB_MUSIC_DIR - sync will upload over WebDAV instead" >&2
fi
if ! grep -q '^MLIB_ND_PASS=..*' "$REPO_DIR/.env" 2>/dev/null; then
  echo "!! MLIB_ND_PASS is empty - sync cannot see what was played, so it never rotates" >&2
fi

mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/$NAME.service" <<EOF
[Unit]
Description=mlib: replace heard podcast episodes with the newest unheard
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$REPO_DIR
ExecStart=$PYTHON -m mlib pod sync
EOF

cat > "$UNIT_DIR/$NAME.timer" <<EOF
[Unit]
Description=Hourly mlib podcast sync

[Timer]
OnCalendar=hourly
RandomizedDelaySec=300
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now "$NAME.timer"
systemctl --user restart "$NAME.timer"

# Without lingering, user timers stop when you log out.
if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != "yes" ]; then
  loginctl enable-linger "$USER" 2>/dev/null \
    || echo "!! run 'sudo loginctl enable-linger $USER' so the timer runs while logged out" >&2
fi

systemctl --user list-timers "$NAME.timer" --no-pager
echo "==> logs: journalctl --user -u $NAME.service -n 50"
echo "    run now: systemctl --user start $NAME.service"
