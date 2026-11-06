# Deploying on Proxmox (LXC)

No Docker needed: a small LXC container, Python venv, and a systemd service.

## 1. Create the container
In Proxmox: download a **Debian 12** (or Ubuntu 22.04+) template, then *Create CT*:
- Unprivileged container, 1 vCPU, 512 MB–1 GB RAM, 4–8 GB disk
- Network: a static IP or DHCP reservation (you'll point the proxy at it)
- Start at boot: on

## 2. Install
```bash
pct enter <CTID>                       # or SSH in
apt update && apt install -y git
git clone <your repo URL> /opt/running-coach
cd /opt/running-coach && git checkout claude/running-coach   # or main, once merged
bash deploy/install.sh
nano /etc/running-coach.env            # fill in the values below
systemctl restart running-coach
curl http://127.0.0.1:8000/healthz     # -> ok
```
Values to set in `/etc/running-coach.env`:

| Variable | Set to |
|---|---|
| `ANTHROPIC_API_KEY` | your key (billed for every user's plans/reviews) |
| `PUBLIC_URL` | the address users type, e.g. `https://coach.example.com` (`https://` makes cookies Secure) |
| `TZ` | your timezone, e.g. `America/New_York` |
| `INVITE_CODE` / `ALLOW_SIGNUPS` | restrict who can sign up |
| `STRAVA_CLIENT_ID/SECRET` | optional; callback domain in Strava = host of `PUBLIC_URL` |

> **Testing over plain HTTP first?** If you open `http://<container-ip>:8000` before HTTPS is set up, set
> `PUBLIC_URL=http://<container-ip>:8000` for now. With an `https://` `PUBLIC_URL` the login cookie is marked
> Secure and browsers will silently drop it over HTTP, so sign-in appears to do nothing. Switch it back once HTTPS is in place.

Logs: `journalctl -u running-coach -f`

## 3. HTTPS access (pick one)
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
