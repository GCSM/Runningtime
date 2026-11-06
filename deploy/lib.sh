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
