# Shared helpers for deploy/install.sh and deploy/reconfigure.sh. Source this file; don't execute it.
# Expects ENV_FILE to be set by the caller.

set_var() {  # set_var KEY VALUE : replace or append KEY=VALUE, preserving the file's owner and mode
  local k="$1" v="$2" tmp
  # Already set to this value: leave the file untouched (so callers can tell nothing changed).
  if grep -q "^${k}=" "$ENV_FILE" 2>/dev/null && [ "$(get_var "$k")" = "$v" ]; then return 0; fi
  tmp="$(mktemp)"
  { grep -v "^${k}=" "$ENV_FILE" 2>/dev/null || true; printf '%s=%s\n' "$k" "$v"; } > "$tmp"
  cat "$tmp" > "$ENV_FILE"; rm -f "$tmp"
}

get_var() {  # get_var KEY : current value (empty if unset)
  grep "^${1}=" "$ENV_FILE" 2>/dev/null | tail -n1 | cut -d= -f2- || true
}

detect_ip() { hostname -I 2>/dev/null | awk '{print $1}'; }

detect_tz() {
  local tz=""
  tz="$(timedatectl show -p Timezone --value 2>/dev/null || true)"
  [ -n "$tz" ] || tz="$(cat /etc/timezone 2>/dev/null || true)"
  [ -n "$tz" ] || tz="$(readlink /etc/localtime 2>/dev/null | sed 's#.*/zoneinfo/##' || true)"
  echo "${tz:-UTC}"
}

random_code() { head -c 32 /dev/urandom | base64 | tr -dc 'a-z0-9' | head -c 8; }

env_port() { local p; p="$(get_var COACH_PORT)"; echo "${p:-80}"; }

url_with_port() {  # url_with_port HOST PORT : http://HOST (no :80 suffix) or http://HOST:PORT
  if [ "$2" = 80 ]; then echo "http://$1"; else echo "http://$1:$2"; fi
}

migrate_port() {
  # Installs from before COACH_PORT existed listened on 8000. Move plain-HTTP LAN installs to port 80 (and fix
  # PUBLIC_URL to match), but keep 8000 for HTTPS installs because a reverse proxy probably points at it.
  [ -z "$(get_var COACH_PORT)" ] || return 0
  local url; url="$(get_var PUBLIC_URL)"
  case "$url" in
    https://*)
      set_var COACH_PORT 8000
      echo "==> Keeping port 8000 (your PUBLIC_URL is https, so a reverse proxy likely points there)."
      echo "    To move to port 80: bash deploy/reconfigure.sh COACH_PORT=80   (and update the proxy)" ;;
    *)
      set_var COACH_PORT 80
      case "$url" in
        http://*:8000) set_var PUBLIC_URL "${url%:8000}"; echo "==> Now listening on port 80; PUBLIC_URL changed to ${url%:8000}" ;;
        *) echo "==> Now listening on port 80." ;;
      esac
      echo "    The port isn't part of Strava's callback domain, so nothing to change there unless the host changed." ;;
  esac
}
