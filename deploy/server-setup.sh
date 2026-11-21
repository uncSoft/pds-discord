#!/usr/bin/env bash
# Runs ON THE SERVER (Ubuntu/Debian) as root. Safe to run again: it updates in place.
set -euo pipefail
APP=/opt/pds-discord

if ! python3 -m venv --help >/dev/null 2>&1; then
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 python3-venv >/dev/null
fi

id pdsbot >/dev/null 2>&1 || useradd --system --home "$APP" --shell /usr/sbin/nologin pdsbot

cd "$APP"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

if [ ! -f .env ]; then
  echo "!! $APP/.env is missing. Copy it up first (deploy/push.sh does this with --first)." >&2
  exit 1
fi
chown -R pdsbot:pdsbot "$APP"
chmod 600 .env

cp deploy/pds-discord.service /etc/systemd/system/pds-discord.service
systemctl daemon-reload
systemctl enable pds-discord >/dev/null
systemctl restart pds-discord
sleep 6
systemctl --no-pager --lines=0 status pds-discord | head -3
echo "--- last log lines ---"
journalctl -u pds-discord --no-pager -n 6 -o cat || true
