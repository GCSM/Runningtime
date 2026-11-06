#!/usr/bin/env bash
# Install / update Running Coach inside a Debian or Ubuntu LXC container. Run as root from the repo checkout:
#   git clone <repo> /opt/running-coach && cd /opt/running-coach && sudo bash deploy/install.sh
# Re-running is safe: it updates dependencies and the service file and restarts the service.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE=/etc/running-coach.env
DATA_DIR=/var/lib/running-coach

[ "$(id -u)" -eq 0 ] || { echo "Run as root." >&2; exit 1; }

apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip sqlite3 >/dev/null

id coach >/dev/null 2>&1 || useradd --system --home "$DATA_DIR" --shell /usr/sbin/nologin coach
install -d -o coach -g coach -m 750 "$DATA_DIR"

python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

if [ ! -f "$ENV_FILE" ]; then
  cp "$APP_DIR/.env.example" "$ENV_FILE"
  sed -i "s#^COACH_DB=.*#COACH_DB=$DATA_DIR/coach.db#" "$ENV_FILE"
  chown root:coach "$ENV_FILE"; chmod 640 "$ENV_FILE"   # holds your API keys: not world-readable
  NEW_ENV=1
fi

sed "s#@APP_DIR@#$APP_DIR#g" "$APP_DIR/deploy/running-coach.service" > /etc/systemd/system/running-coach.service
chown -R coach:coach "$DATA_DIR"
systemctl daemon-reload
systemctl enable running-coach >/dev/null

if [ "${NEW_ENV:-0}" = 1 ]; then
  echo
  echo "Created $ENV_FILE. Edit it (ANTHROPIC_API_KEY, PUBLIC_URL, TZ, signup settings), then run:"
  echo "  systemctl restart running-coach"
else
  systemctl restart running-coach
  sleep 2
  curl -fsS http://127.0.0.1:8000/healthz && echo " <- healthy"
fi
