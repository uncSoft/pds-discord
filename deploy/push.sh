#!/usr/bin/env bash
# Runs ON YOUR MAC. Copies the bot to a server and (re)starts it there. Render deploys on push
# instead; this is for running it on a plain VPS.
#
#   First time (also copies .env and stops the copy running on this Mac):
#     deploy/push.sh root@YOUR_SERVER_IP --first
#   After code changes:
#     deploy/push.sh root@YOUR_SERVER_IP
set -euo pipefail
HOST="${1:?usage: deploy/push.sh root@SERVER_IP [--first]}"
FIRST="${2:-}"
cd "$(dirname "$0")/.."

ssh "$HOST" 'mkdir -p /opt/pds-discord'
rsync -az --delete \
  --exclude .venv --exclude .env --exclude __pycache__ --exclude .pytest_cache --exclude .git \
  --exclude .DS_Store \
  ./ "$HOST:/opt/pds-discord/"

if [ "$FIRST" = "--first" ]; then
  # Two copies of one bot answer every question twice.
  if pkill -f "python.* -m pds_discord"; then echo "Stopped the copy running on this Mac."; fi
  sleep 1
  rsync -az .env "$HOST:/opt/pds-discord/.env"
fi

ssh "$HOST" 'bash /opt/pds-discord/deploy/server-setup.sh'
echo
echo "Done. Live logs:  ssh $HOST journalctl -u pds-discord -f"
