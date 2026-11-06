#!/usr/bin/env bash
# Install / update Running Coach inside a Debian or Ubuntu LXC container, and configure it automatically.
#
#   git clone <repo> /opt/running-coach && cd /opt/running-coach && sudo bash deploy/install.sh
#
# First run: detects this container's IP and timezone, asks for your Anthropic API key, writes
# /etc/running-coach.env, starts the service and prints the URL. Re-running updates the app and restarts it
# without touching your config. Signups are open (intended for a trusted home LAN); see REQUIRE_INVITE below.
#
# Non-interactive / overrides (all optional), e.g.:
#   ANTHROPIC_API_KEY=sk-ant-... PUBLIC_URL=http://coach.lan TZ=America/Chicago \
#     STRAVA_CLIENT_ID=... STRAVA_CLIENT_SECRET=... bash deploy/install.sh
#   Require an invite code to sign up:  REQUIRE_INVITE=1 (generates one)  or  INVITE_CODE=yourword
#
# --configure-only   write the config file and stop (no apt / systemd); used for testing.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-/etc/running-coach.env}"
DATA_DIR="${DATA_DIR:-/var/lib/running-coach}"
CONFIGURE_ONLY=0
[ "${1:-}" = "--configure-only" ] && CONFIGURE_ONLY=1

. "$APP_DIR/deploy/lib.sh"

configure() {
  cp "$APP_DIR/.env.example" "$ENV_FILE"
  local ip url tz key invite port
  ip="$(detect_ip)"; ip="${ip:-127.0.0.1}"
  port="${COACH_PORT:-80}"
  url="${PUBLIC_URL:-$(url_with_port "$ip" "$port")}"
  tz="${TZ:-$(detect_tz)}"

  key="${ANTHROPIC_API_KEY:-}"
  if [ -z "$key" ] && [ -t 0 ]; then
    read -rsp "Anthropic API key (input hidden, Enter to skip): " key; echo
  fi

  invite="${INVITE_CODE:-}"
  if [ -z "$invite" ] && [ "${REQUIRE_INVITE:-0}" = 1 ]; then
    invite="$(random_code)"
  fi

  set_var ANTHROPIC_API_KEY "$key"
  set_var PUBLIC_URL "$url"
  set_var COACH_PORT "$port"
  set_var TZ "$tz"
  set_var COACH_DB "$DATA_DIR/coach.db"
  set_var ALLOW_SIGNUPS "true"
  set_var INVITE_CODE "$invite"
  [ -n "${STRAVA_CLIENT_ID:-}" ] && set_var STRAVA_CLIENT_ID "$STRAVA_CLIENT_ID"
  [ -n "${STRAVA_CLIENT_SECRET:-}" ] && set_var STRAVA_CLIENT_SECRET "$STRAVA_CLIENT_SECRET"

  if id coach >/dev/null 2>&1; then chown root:coach "$ENV_FILE"; fi
  chmod 640 "$ENV_FILE"   # holds your API keys: not world-readable

  echo
  echo "Configured $ENV_FILE"
  echo "  URL:          $url"
  echo "  Timezone:     $tz"
  if [ -n "$invite" ]; then echo "  Invite code:  $invite   (needed to create an account; share it only with people you trust)"
  else echo "  Signups:      open to anyone who can reach the app (fine on a trusted LAN; use INVITE_CODE if that changes)"; fi
  [ -n "$key" ] || echo "  ! No API key set: add ANTHROPIC_API_KEY to $ENV_FILE and run 'systemctl restart running-coach'."
  case "$url" in http://*) echo "  Note: plain HTTP. Passwords cross your network unencrypted; fine on a trusted home LAN only." ;; esac
}

if [ "$CONFIGURE_ONLY" = 1 ]; then
  [ -f "$ENV_FILE" ] && { echo "$ENV_FILE already exists; leaving it alone."; exit 0; }
  configure; exit 0
fi

[ "$(id -u)" -eq 0 ] || { echo "Run as root." >&2; exit 1; }

echo "==> Installing system packages"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip sqlite3 curl >/dev/null

id coach >/dev/null 2>&1 || useradd --system --home "$DATA_DIR" --shell /usr/sbin/nologin coach
install -d -o coach -g coach -m 750 "$DATA_DIR"

echo "==> Installing Python dependencies"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

NEW_ENV=0
if [ ! -f "$ENV_FILE" ]; then configure; NEW_ENV=1; else echo "==> Keeping existing config $ENV_FILE"; migrate_port; fi
PORT="$(env_port)"

sed "s#@APP_DIR@#$APP_DIR#g" "$APP_DIR/deploy/running-coach.service" > /etc/systemd/system/running-coach.service
chown -R coach:coach "$DATA_DIR"
systemctl daemon-reload
systemctl enable running-coach >/dev/null
systemctl restart running-coach

echo "==> Waiting for the service"
for _ in $(seq 1 20); do
  if curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then
    echo "Running Coach is up."
    [ "$NEW_ENV" = 1 ] && echo "Open $(grep '^PUBLIC_URL=' "$ENV_FILE" | cut -d= -f2-) and create your account."
    exit 0
  fi
  sleep 1
done
echo "Service did not become healthy. Check: journalctl -u running-coach -n 50" >&2
exit 1
