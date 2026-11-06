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
- generates an **invite code** (needed to create an account) and prints it
- starts the service and prints the URL, e.g. `http://192.168.1.50:8000`

Open that URL and create your account with the invite code. Re-running the script later updates the app and restarts it
without touching your config.

Skip the prompts by passing values as environment variables (all optional):
```bash
ANTHROPIC_API_KEY=sk-ant-... PUBLIC_URL=http://192.168.1.50:8000 TZ=America/Chicago \
STRAVA_CLIENT_ID=... STRAVA_CLIENT_SECRET=... OPEN_SIGNUPS=1 bash deploy/install.sh
```
`OPEN_SIGNUPS=1` skips the invite code; `INVITE_CODE=yourword` picks your own.

To change anything afterwards: `nano /etc/running-coach.env && systemctl restart running-coach`.

| Variable | What it does |
|---|---|
| `ANTHROPIC_API_KEY` | your key (billed for every user's plans/reviews) |
| `PUBLIC_URL` | address users type; `http://IP:8000` for plain LAN use, `https://…` once you add HTTPS |
| `TZ` | decides what counts as "today" and when the weekly review runs |
| `INVITE_CODE` / `ALLOW_SIGNUPS` | restrict who can sign up |
| `STRAVA_CLIENT_ID/SECRET` | optional; callback domain in Strava = host of `PUBLIC_URL` |

> **Plain HTTP vs HTTPS:** keep `PUBLIC_URL` matching how you actually reach the app. With an `https://` `PUBLIC_URL` the
> login cookie is marked Secure and browsers silently drop it over HTTP, so sign-in appears to do nothing. The installer
> defaults to `http://<container-ip>:8000`, which is fine on a trusted home network (passwords are not encrypted in transit).

Logs: `journalctl -u running-coach -f`

## 3. HTTPS access (optional; pick one)
- **Reverse proxy** (Caddy / Nginx Proxy Manager / Traefik) in another container: see `deploy/Caddyfile.example`.
  Set `FORWARDED_ALLOW_IPS` to the proxy's IP in the env file.
- **Tailscale** for private use: install it in the container. Containers need `/dev/net/tun` — add to
  `/etc/pve/lxc/<CTID>.conf` on the Proxmox host:
  ```
  lxc.cgroup2.devices.allow: c 10:200 rwm
  lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file
  ```
  then `tailscale up` and `tailscale serve --bg 8000` for an HTTPS tailnet URL (set `PUBLIC_URL` to it).
- Don't expose port 8000 to the internet directly.

## 4. Backups
Everything is one SQLite file, `/var/lib/running-coach/coach.db` (also holds Strava tokens: treat it as secret).
A Proxmox backup job of the container covers it. For a consistent copy at any time:
```bash
sqlite3 /var/lib/running-coach/coach.db ".backup '/root/coach-$(date +%F).db'"
```

## 5. Updating
```bash
cd /opt/running-coach && git pull && bash deploy/install.sh
```
