#!/usr/bin/env bash
# Change the settings of an already-installed RunningTime, then restart it.
#
#   sudo bash deploy/reconfigure.sh                      # walk through every setting (Enter keeps the current value)
#   sudo bash deploy/reconfigure.sh --show               # print current settings (secrets masked) and exit
#   sudo bash deploy/reconfigure.sh COACH_EFFORT=low TZ=America/Chicago     # change specific settings, no prompts
#   sudo bash deploy/reconfigure.sh --unset INVITE_CODE  # remove a setting
#   sudo bash deploy/reconfigure.sh --check              # test the Anthropic key/model the way the service sees them
#   Flags: --no-restart   write the config but don't restart (used for testing)
#          --no-check     don't auto-test the Anthropic key after changing the key or model
#
# Safety: the old config is backed up first; all values are validated before anything is written; the service
# restarts only if something changed; and if the app isn't healthy afterwards the previous config is restored.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-/etc/running-coach.env}"
SERVICE="${SERVICE:-running-coach}"
HEALTH_TRIES="${HEALTH_TRIES:-20}"
. "$APP_DIR/deploy/lib.sh"

SHOW=0; RESTART=1; CHECK=0; AUTOCHECK=1; ARGS=(); UNSETS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --show) SHOW=1 ;;
    --no-restart) RESTART=0 ;;
    --check) CHECK=1 ;;
    --no-check) AUTOCHECK=0 ;;
    --unset) shift; UNSETS+=("${1:?--unset needs a KEY}") ;;
    -h|--help) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *=*) ARGS+=("$1") ;;
    *) echo "Unknown argument: $1 (try --help)" >&2; exit 2 ;;
  esac
  shift
done

die() { echo "Error: $*" >&2; exit 1; }

clean() {  # trim whitespace and one pair of surrounding quotes (common paste mistakes)
  local v="$1"
  v="${v#"${v%%[![:space:]]*}"}"; v="${v%"${v##*[![:space:]]}"}"
  case "$v" in \"*\"|\'*\') v="${v:1:${#v}-2}" ;; esac
  printf '%s' "$v"
}

run_check() {  # test the Anthropic key/model as the service sees them
  if [ -n "${CHECK_CMD:-}" ]; then $CHECK_CMD
  else "$APP_DIR/.venv/bin/python" -m coach.check "$ENV_FILE"; fi
}

[ -f "$ENV_FILE" ] || die "$ENV_FILE not found. Is RunningTime installed? Run deploy/install.sh first."
if [ "$RESTART" = 1 ] && [ "$SHOW" = 0 ] && [ "$CHECK" = 0 ]; then
  [ "$(id -u)" -eq 0 ] || die "Run as root."
  systemctl cat "$SERVICE" >/dev/null 2>&1 || die "Service '$SERVICE' isn't installed. Run deploy/install.sh first."
  # The port (and other process settings) live in the systemd unit, which only install.sh rewrites. An old unit
  # silently ignores COACH_PORT, so refuse to continue rather than "succeed" without effect.
  systemctl cat "$SERVICE" 2>/dev/null | grep -q 'COACH_PORT' ||
    die "The installed service file is out of date (it predates COACH_PORT, so it still listens on a fixed port). Run: bash deploy/install.sh"
fi

KEYS=(ANTHROPIC_API_KEY COACH_MODEL COACH_EFFORT COACH_CHAT_MODEL CHAT_MESSAGES_PER_DAY PLAYLISTS_PER_DAY COACH_PORT PUBLIC_URL TZ ALLOW_SIGNUPS INVITE_CODE
      STRAVA_CLIENT_ID STRAVA_CLIENT_SECRET STRAVA_ATHLETE_LIMIT SYNC_INTERVAL_MIN AUTO_ADJUST FORWARDED_ALLOW_IPS)
SECRETS=" ANTHROPIC_API_KEY STRAVA_CLIENT_SECRET INVITE_CODE "

mask_value() {  # mask_value KEY VALUE
  local k="$1" v="$2"
  [ -n "$v" ] || { echo "(not set)"; return 0; }
  case "$SECRETS" in
    *" $k "*) if [ "${#v}" -gt 8 ]; then echo "${v:0:7}…(${#v} chars)"; else echo "••••"; fi ;;
    *) echo "$v" ;;
  esac
}

show_settings() {
  echo "Current settings in $ENV_FILE:"
  for k in "${KEYS[@]}"; do printf '  %-22s %s\n' "$k" "$(mask_value "$k" "$(get_var "$k")")"; done
}

validate() {  # validate KEY VALUE : prints a message and returns 1 if invalid
  local k="$1" v="$2"
  case "$v" in *$'\n'*|*$'\r'*) echo "$k can't contain line breaks"; return 1 ;; esac
  case "$k" in
    ANTHROPIC_API_KEY)
      case "$v" in *[[:space:]]*) echo "ANTHROPIC_API_KEY contains a space — paste just the key"; return 1 ;; esac
      case "$v" in sk-ant-*) ;; *) echo "  note: Anthropic API keys normally start with sk-ant- (saving anyway; the test below will tell)" >&2 ;; esac ;;
    CHAT_MESSAGES_PER_DAY|PLAYLISTS_PER_DAY) { [[ "$v" =~ ^[0-9]+$ ]] && [ "$v" -ge 1 ]; } || { echo "$k must be a whole number of 1 or more"; return 1; } ;;
    COACH_EFFORT) case "$v" in low|medium|high) ;; *) echo "COACH_EFFORT must be low, medium or high"; return 1 ;; esac ;;
    PUBLIC_URL) case "$v" in http://*|https://*) ;; *) echo "PUBLIC_URL must start with http:// or https://"; return 1 ;; esac ;;
    TZ) [ -f "/usr/share/zoneinfo/$v" ] || { echo "Unknown timezone '$v' (e.g. America/New_York)"; return 1; } ;;
    COACH_PORT) { [[ "$v" =~ ^[0-9]+$ ]] && [ "$v" -ge 1 ] && [ "$v" -le 65535 ]; } || { echo "COACH_PORT must be a port number from 1 to 65535"; return 1; } ;;
    ALLOW_SIGNUPS|AUTO_ADJUST) case "$v" in true|false) ;; *) echo "$k must be true or false"; return 1 ;; esac ;;
    STRAVA_ATHLETE_LIMIT) { [[ "$v" =~ ^[0-9]+$ ]] && [ "$v" -ge 1 ]; } || { echo "STRAVA_ATHLETE_LIMIT must be a whole number of 1 or more"; return 1; } ;;
    SYNC_INTERVAL_MIN) [[ "$v" =~ ^[0-9]+$ ]] || { echo "SYNC_INTERVAL_MIN must be a whole number of minutes (0 disables)"; return 1; } ;;
  esac
  return 0
}

if [ "$SHOW" = 1 ]; then show_settings; exit 0; fi
if [ "$CHECK" = 1 ]; then run_check; exit $?; fi

# Normalise pasted values (trim, drop wrapping quotes).
for i in "${!ARGS[@]}"; do ARGS[$i]="${ARGS[$i]%%=*}=$(clean "${ARGS[$i]#*=}")"; done

# Validate everything up front: all-or-nothing.
for kv in "${ARGS[@]+"${ARGS[@]}"}"; do
  err="$(validate "${kv%%=*}" "${kv#*=}")" || die "$err"
done

BEFORE="$(cat "$ENV_FILE")"
BACKUP="$ENV_FILE.bak.$(date +%Y%m%d-%H%M%S)"
cp -p "$ENV_FILE" "$BACKUP"
ls -1t "$ENV_FILE".bak.* 2>/dev/null | tail -n +6 | xargs -r rm -f   # keep the 5 newest backups

ask() {  # ask KEY "label" [secret] : current value is the default; '-' clears; Enter keeps; re-asks if invalid
  local k="$1" label="$2" secret="${3:-}" cur ans err
  cur="$(get_var "$k")"
  while true; do
    if [ -n "$secret" ]; then
      read -rsp "$label [$(mask_value "$k" "$cur")] (Enter keeps, - clears): " ans || ans=""; echo
    else
      read -rp "$label [${cur:-not set}] (Enter keeps, - clears): " ans || ans=""
    fi
    ans="$(clean "$ans")"
    if [ -z "$ans" ]; then return 0; fi
    if [ "$ans" = "-" ]; then set_var "$k" ""; return 0; fi
    if err="$(validate "$k" "$ans")"; then set_var "$k" "$ans"; return 0; fi
    echo "  $err" >&2
    if [ ! -t 0 ]; then rm -f "$BACKUP"; die "$err"; fi   # piped input can't retry
  done
}

if [ ${#ARGS[@]} -gt 0 ] || [ ${#UNSETS[@]} -gt 0 ]; then
  for kv in "${ARGS[@]+"${ARGS[@]}"}"; do set_var "${kv%%=*}" "${kv#*=}"; done
  for k in "${UNSETS[@]+"${UNSETS[@]}"}"; do set_var "$k" ""; done
else
  show_settings; echo
  echo "Change a setting by typing a new value; press Enter to keep it."
  ask ANTHROPIC_API_KEY "Anthropic API key" secret
  ask COACH_MODEL "Model (e.g. claude-sonnet-5-5, claude-haiku-5-5)"
  ask COACH_EFFORT "Effort: low | medium | high (lower = cheaper)"
  ask COACH_CHAT_MODEL "Chat model (cheapest is claude-haiku-5-5)"
  ask CHAT_MESSAGES_PER_DAY "Chat messages per user per day (bounds cost)"
  ask COACH_PORT "Port the app listens on (80 = no :port needed in URLs)"
  ip="$(detect_ip)"
  ask PUBLIC_URL "Public URL (this container is ${ip:-unknown}; e.g. $(url_with_port "${ip:-HOST}" "$(env_port)") or your https:// address)"
  ask TZ "Timezone"
  ask ALLOW_SIGNUPS "Allow signups: true | false"
  cur="$(get_var INVITE_CODE)"
  read -rp "Invite code [$(mask_value INVITE_CODE "$cur")] (Enter keeps, - none, 'new' generates, or type one): " ans || ans=""
  case "$ans" in
    "") ;;
    -) set_var INVITE_CODE "" ;;
    new) set_var INVITE_CODE "$(random_code)"; echo "  New invite code: $(get_var INVITE_CODE)" ;;
    *) err="$(validate INVITE_CODE "$ans")" || { rm -f "$BACKUP"; die "$err"; }; set_var INVITE_CODE "$ans" ;;
  esac
  ask STRAVA_CLIENT_ID "Strava app client ID (the one app every user connects through)"
  ask STRAVA_CLIENT_SECRET "Strava app client secret" secret
  ask STRAVA_ATHLETE_LIMIT "Strava athlete limit (your app's capacity; new Strava apps start at 1)"
  ask SYNC_INTERVAL_MIN "Strava sync interval in minutes (0 disables)"
  ask AUTO_ADJUST "Automatic weekly plan review: true | false"
fi

AFTER="$(cat "$ENV_FILE")"
if [ "$BEFORE" = "$AFTER" ]; then
  rm -f "$BACKUP"
  echo "No changes."
  exit 0
fi

echo; echo "Changes:"
for k in "${KEYS[@]}"; do
  b="$(printf '%s\n' "$BEFORE" | grep "^${k}=" | tail -n1 | cut -d= -f2- || true)"; a="$(get_var "$k")"
  if [ "$b" != "$a" ]; then printf '  %-22s %s  ->  %s\n' "$k" "$(mask_value "$k" "$b")" "$(mask_value "$k" "$a")"; fi
done
case "$(get_var PUBLIC_URL)" in
  https://*) echo "  note: https:// URL -> login cookies are Secure; open the app over https only." ;;
  http://*)  echo "  note: http:// URL -> fine on a trusted LAN; passwords aren't encrypted in transit." ;;
esac
old_url="$(printf '%s\n' "$BEFORE" | grep '^PUBLIC_URL=' | tail -n1 | cut -d= -f2- || true)"
if [ "$old_url" != "$(get_var PUBLIC_URL)" ]; then
  echo "  note: if you use Strava, its callback domain must match the host of the new PUBLIC_URL."
fi

if [ "$RESTART" = 0 ]; then echo "Saved (not restarted). Backup: $BACKUP"; exit 0; fi

PORT="$(env_port)"   # may have just changed
old_port="$(printf '%s\n' "$BEFORE" | grep '^COACH_PORT=' | tail -n1 | cut -d= -f2- || true)"
if [ "$old_port" != "$(get_var COACH_PORT)" ]; then
  echo "  note: the app will now listen on port $PORT. Make sure PUBLIC_URL (and any reverse proxy) uses it."
fi

echo; echo "Restarting $SERVICE…"
systemctl restart "$SERVICE"
healthy() {
  local _
  for _ in $(seq 1 "$HEALTH_TRIES"); do
    if curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then return 0; fi
    sleep 1
  done
  return 1
}
if healthy; then
  echo "RunningTime is up with the new settings. (Previous config kept at $BACKUP)"
  new_key="$(get_var ANTHROPIC_API_KEY)"; new_model="$(get_var COACH_MODEL)"
  old_key="$(printf '%s\n' "$BEFORE" | grep '^ANTHROPIC_API_KEY=' | tail -n1 | cut -d= -f2- || true)"
  old_model="$(printf '%s\n' "$BEFORE" | grep '^COACH_MODEL=' | tail -n1 | cut -d= -f2- || true)"
  if [ "$AUTOCHECK" = 1 ] && { [ "$new_key" != "$old_key" ] || [ "$new_model" != "$old_model" ]; }; then
    echo; echo "Testing the Anthropic key and model…"
    run_check || echo "  (The app is running, but plan generation will fail until this is fixed. Re-run: bash deploy/reconfigure.sh)" >&2
  fi
else
  echo "The service did not become healthy with the new settings. Restoring the previous config…" >&2
  cat "$BACKUP" > "$ENV_FILE"
  systemctl restart "$SERVICE"
  if healthy; then echo "Rolled back: the previous settings are running again. Check: journalctl -u $SERVICE -n 50" >&2
  else echo "Still unhealthy after rollback. Check: journalctl -u $SERVICE -n 50" >&2; fi
  exit 1
fi
