# Deploying on Proxmox (LXC)

No Docker needed: a small LXC container, Python venv, and a systemd service.

## 1. Create the container
In Proxmox: download a **Debian 12** (or Ubuntu 22.04+) template, then *Create CT*:
- Unprivileged container, 1 vCPU, 512 MB–1 GB RAM, 4–8 GB disk
- Network: a static IP or DHCP reservation (you'll point the proxy at it)
- Start at boot: on

## 2. Install (auto-configures itself)
```bash
pct enter <CTID>                       # or SSH in
apt update && apt install -y git
git clone <your repo URL> /opt/running-coach
cd /opt/running-coach && git checkout claude/running-coach   # or main, once merged
bash deploy/install.sh
```
On the first run the script:
- installs Python and the dependencies, creates a locked-down `coach` user and the systemd service
- detects the container's IP and timezone and writes `/etc/running-coach.env` (readable only by root and the service)
- asks for your Anthropic API key (input hidden; press Enter to skip and add it later)
- leaves signups open (no invite code), which suits a trusted home LAN
- starts the service and prints the URL, e.g. `http://192.168.1.50` (the app listens on port 80, so no `:port` is needed)

Open that URL and create your account. Re-running the script later updates the app and restarts it
without touching your config.

Skip the prompts by passing values as environment variables (all optional):
```bash
ANTHROPIC_API_KEY=sk-ant-... PUBLIC_URL=http://coach.lan TZ=America/Chicago \
STRAVA_CLIENT_ID=... STRAVA_CLIENT_SECRET=... STRAVA_ATHLETE_LIMIT=10 bash deploy/install.sh
```
If the app is ever reachable beyond your LAN, require a code to sign up: `REQUIRE_INVITE=1` generates one, or
`INVITE_CODE=yourword` picks your own (or edit `INVITE_CODE` in the env file later). Remember every account's plans and
reviews are billed to your Anthropic key.

To change settings later, use `deploy/reconfigure.sh` (see below).

| Variable | What it does |
|---|---|
| `ANTHROPIC_API_KEY` | your key (billed for every user's plans/reviews) |
| `PUBLIC_URL` | your address, e.g. `http://coach.lan` or `https://…`. Strava callbacks and redirects use the address you actually browse with, so a stale value here can't break them; it's the fallback and marks cookies Secure when it starts with `https://` |
| `COACH_PORT` | port the app listens on (default `80`, so URLs need no `:port`); change with `reconfigure.sh COACH_PORT=8080` |
| `TZ` | decides what counts as "today" and when the weekly review runs |
| `INVITE_CODE` / `ALLOW_SIGNUPS` | restrict who can sign up |
| `STRAVA_CLIENT_ID/SECRET` | your one Strava app; every user connects through it (callback domain in Strava = the host you browse to) |
| `STRAVA_ATHLETE_LIMIT` | the athlete capacity of that Strava app (default 10). The app refuses further connections politely beyond it |

> **Plain HTTP vs HTTPS:** keep `PUBLIC_URL` matching how you actually reach the app. With an `https://` `PUBLIC_URL` the
> login cookie is marked Secure and browsers silently drop it over HTTP, so sign-in appears to do nothing. The installer
> defaults to `http://<container-ip>`, which is fine on a trusted home network (passwords are not encrypted in transit).

Logs: `journalctl -u running-coach -f`

## Changing settings later (`reconfigure.sh`)
For an install that's already running. It shows your current settings (secrets masked), lets you change any of them,
backs up the old config, restarts the service, and **rolls back automatically** if the app isn't healthy afterwards.
```bash
cd /opt/running-coach
bash deploy/reconfigure.sh                          # walk through every setting; Enter keeps the current value
bash deploy/reconfigure.sh --show                   # just print the current settings
bash deploy/reconfigure.sh COACH_EFFORT=low COACH_MODEL=claude-haiku-5-5   # change specific settings, no prompts
bash deploy/reconfigure.sh --unset INVITE_CODE      # remove a setting
```
Values are validated first (effort, URL scheme, timezone, true/false, numbers), and a bad value changes nothing. In the
walkthrough, type `-` to clear a value, and `new` at the invite-code prompt to generate a code. The 5 most recent
backups are kept as `/etc/running-coach.env.bak.<timestamp>`.

## Troubleshooting: still on the old port or old address after updating
The port comes from the systemd unit and the address from the config file; `git pull` changes neither. Check what is
really installed:
```bash
cd /opt/running-coach && git log -1 --oneline                  # should mention port 80 / COACH_PORT
systemctl cat running-coach | grep -E "ExecStart|COACH_PORT|Ambient"   # must show --port ${COACH_PORT}
grep -E "COACH_PORT|PUBLIC_URL" /etc/running-coach.env          # COACH_PORT=80, PUBLIC_URL=your new address
ss -ltnp | grep -E ":(80|8000) "                                # what is actually listening
```
If the unit still says `--port 8000`, run `bash deploy/install.sh` (it rewrites the unit, reloads systemd and restarts).
`reconfigure.sh` now refuses to run against an outdated unit and tells you this.

## Troubleshooting: service won't start on port 80
Port 80 is below 1024, so the unit grants the `coach` user just that one capability (`CAP_NET_BIND_SERVICE`). If
`journalctl -u running-coach -n 30` shows `Permission denied` / `error while attempting to bind on address ('0.0.0.0', 80)`
in your container, either something else already uses port 80 (`ss -ltnp | grep :80`), or the container doesn't allow the
capability. The simple fix is a higher port: `bash deploy/reconfigure.sh COACH_PORT=8000 PUBLIC_URL=http://<host>:8000`
(the port is never part of Strava's callback domain).

## Troubleshooting: "API key error" when building a plan
The app reads its config only when it starts, so **after changing the key, restart it**. `reconfigure.sh` does that for
you and then tests the key; if you edited the file by hand, run `systemctl restart running-coach`.

Test what the service will actually see:
```bash
cd /opt/running-coach
bash deploy/reconfigure.sh --check        # or: .venv/bin/python -m coach.check /etc/running-coach.env
```
It prints the key (masked) and model, flags paste mistakes (quotes, spaces, wrong prefix), makes one tiny API call, and
says what failed. Common causes:

| Message | Fix |
|---|---|
| rejected the API key (401) | key is wrong, revoked, or the wrong kind of credential (API keys start with `sk-ant-` and come from console.anthropic.com → API Keys; a Claude subscription login does not work) |
| wrapped in quotes / whitespace | re-enter with `bash deploy/reconfigure.sh` (it also strips these for you) |
| out of credit | add credit under Billing in the Anthropic Console (API credit is separate from a Claude subscription) |
| couldn't find the model | fix `COACH_MODEL` (e.g. `claude-sonnet-5-5`) |
| couldn't reach Anthropic | the container needs internet access and working DNS |

## Troubleshooting: Strava says `redirect_uri` is invalid
Strava compares the host in the callback URL the app sends with the **Authorization Callback Domain** on the user's
Strava app. They must match exactly. The domain is the bare host only: no `http://`, no port, no trailing slash.
The Me tab shows the exact value to use (it's the host of `PUBLIC_URL`) and the full callback URL being sent.
- The port is not the usual cause; it never belongs in the domain field.
- If the server is addressed by an IP (`http://192.168.1.50`), Strava may refuse an IP as the callback domain
  (not confirmed either way). Give the server a hostname instead — a local DNS name such as `coach.lan`, or a
  `nip.io` name like `coach.192.168.1.50.nip.io`, which resolves to that IP — then run `bash deploy/reconfigure.sh`,
  set `PUBLIC_URL` to `http://<that hostname>`, and use that hostname as the Strava callback domain.
- The callback URL is built from the address you're browsing with (shown on the Me tab), not from `PUBLIC_URL`. If you move to a new hostname, update the Strava app's callback domain to that host.

## 3. HTTPS access (optional; pick one)
- **Reverse proxy** (Caddy / Nginx Proxy Manager / Traefik) in another container: see `deploy/Caddyfile.example`.
  Set `FORWARDED_ALLOW_IPS` to the proxy's IP in the env file.
- **Tailscale** for private use: install it in the container. Containers need `/dev/net/tun` — add to
  `/etc/pve/lxc/<CTID>.conf` on the Proxmox host:
  ```
  lxc.cgroup2.devices.allow: c 10:200 rwm
  lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file
  ```
  then `tailscale up` and `tailscale serve --bg 80` for an HTTPS tailnet URL (set `PUBLIC_URL` to it).
- Don't expose the app's port to the internet directly.

## 4. Backups
Everything is one SQLite file, `/var/lib/running-coach/coach.db` (also holds Strava tokens: treat it as secret).
A Proxmox backup job of the container covers it. For a consistent copy at any time:
```bash
sqlite3 /var/lib/running-coach/coach.db ".backup '/root/coach-$(date +%F).db'"
```

## 5. Updating
> **Upgrading an older install:** earlier versions listened on port 8000. On the first `install.sh` run after upgrading, a plain-HTTP
> install moves to port 80 and `PUBLIC_URL` loses its `:8000`; an `https://` install keeps 8000 so a reverse proxy keeps
> working (switch later with `reconfigure.sh COACH_PORT=80`). The script prints what it changed.

```bash
cd /opt/running-coach && git pull && bash deploy/install.sh
```
`install.sh` updates the code and dependencies and keeps your config; `reconfigure.sh` changes the config only.
