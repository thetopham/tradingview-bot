# Pi: Cloudflare Tunnel (cloudflared) — Public (GitHub-safe) Rebuild Guide

Sanitized from local Pi snapshot doc (do NOT include tunnel creds/cert in GitHub).

This guide documents how to expose services running on a Raspberry Pi through **Cloudflare Tunnel** using `cloudflared`
and **systemd**, in a way that is safe to publish publicly.

✅ Includes: systemd unit pattern, config structure, rebuild steps, verification commands  
🚫 Excludes: tunnel UUIDs, credential file contents, origin certs, personal hostnames/domains, private paths

---

## Architecture (what this does)

Cloudflare Tunnel maps public hostnames to local services without opening inbound ports on your router.

Example mappings (placeholders):
- `<HOSTNAME_N8N>` → `http://localhost:5678` (n8n)
- `<HOSTNAME_ALERTS>` → `http://localhost:5000` (trading bot / Flask)
- everything else → 404

---

## 1) Install cloudflared

Install method depends on distro. Verify after install:

```bash
cloudflared --version
which cloudflared
```

If you want consistent installs across rebuilds, document your method (apt repo, package manager, or direct binary)
in your project’s main README.

---

## 2) Create / authenticate a tunnel (interactive login)

This step creates an origin cert under your user’s `~/.cloudflared/` directory.

```bash
cloudflared tunnel login
```

Then create a tunnel:

```bash
cloudflared tunnel create <TUNNEL_NAME>
```

List tunnels:

```bash
cloudflared tunnel list
```

> ⚠️ Do NOT commit `~/.cloudflared/*.json` or `~/.cloudflared/cert.pem` to GitHub.

---

## 3) Route DNS hostnames to the tunnel

Create DNS routes (example):

```bash
cloudflared tunnel route dns <TUNNEL_NAME> <HOSTNAME_N8N>
cloudflared tunnel route dns <TUNNEL_NAME> <HOSTNAME_ALERTS>
```

---

## 4) Configure ingress rules

Create the config directory:

```bash
sudo mkdir -p /etc/cloudflared
```

Create `/etc/cloudflared/config.yml`:

```yaml
tunnel: <TUNNEL_NAME>
credentials-file: /home/<USER>/.cloudflared/<TUNNEL_UUID>.json

ingress:
  - hostname: <HOSTNAME_N8N>
    service: http://localhost:5678

  - hostname: <HOSTNAME_ALERTS>
    service: http://localhost:5000

  - service: http_status:404

origincert: /home/<USER>/.cloudflared/cert.pem
```

Permissions (safe defaults):

```bash
sudo chown -R root:root /etc/cloudflared
sudo chmod 755 /etc/cloudflared
sudo chmod 644 /etc/cloudflared/config.yml

sudo chmod 700 /home/<USER>/.cloudflared
sudo chmod 600 /home/<USER>/.cloudflared/*.json /home/<USER>/.cloudflared/cert.pem
```

---

## 5) systemd service (runs cloudflared on boot)

Create `/etc/systemd/system/cloudflared.service`:

```ini
[Unit]
Description=cloudflared
After=network-online.target
Wants=network-online.target

[Service]
TimeoutStartSec=15
Type=notify
ExecStart=/usr/bin/cloudflared --no-autoupdate --config /etc/cloudflared/config.yml tunnel run
Restart=on-failure
RestartSec=5s
StartLimitIntervalSec=0

[Install]
WantedBy=multi-user.target
```

Enable + start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable cloudflared.service
sudo systemctl restart cloudflared.service
systemctl status cloudflared.service --no-pager
```

---

## 6) Verify locally (before blaming Cloudflare)

Confirm your local services are listening:

```bash
sudo ss -ltnp | egrep ':5678|:5000' || true
```

Curl local endpoints:

```bash
curl -I http://localhost:5678 || true
curl -I http://localhost:5000/ || true
```

Tail tunnel logs:

```bash
sudo journalctl -u cloudflared.service -f
```

---

## 7) Common troubleshooting

### “context canceled” / “stream canceled”
Often benign (client disconnect, edge retry, websocket cancellation). If persistent:
- confirm the local service is healthy on localhost (curl)
- verify DNS routes still exist for the hostname(s)
- check any Cloudflare Zero Trust / Access policies
- verify the credentials file path in `config.yml` is correct

Useful commands:

```bash
systemctl status cloudflared.service --no-pager
sudo journalctl -u cloudflared.service -n 200 --no-pager
cloudflared tunnel list
cloudflared tunnel info <TUNNEL_NAME_OR_UUID>
```

---

## 8) What to back up (PRIVATE ONLY)

Back up these **privately** (not GitHub):
- `/etc/cloudflared/config.yml` (may include hostnames & paths)
- `/home/<USER>/.cloudflared/<TUNNEL_UUID>.json` (credentials)
- `/home/<USER>/.cloudflared/cert.pem` (origin cert)

A safe pattern:
- Put **templates** in GitHub (this doc)
- Keep **real configs + creds** in an encrypted backup or a private repo

---

## 9) GitHub hygiene (highly recommended)

Add these to `.gitignore` (adjust to your repo):

```gitignore
.env
.env.*
*.pem
*.json
.cloudflared/
**/secrets*
**/*token*
**/*session*
```

Scan for secrets before pushing:

```bash
git grep -n -i "session\|token\|apikey\|secret\|supabase\|cloudflare\|bearer" || true
```

If you already pushed secrets, rotate them and scrub git history (BFG or git-filter-repo).
