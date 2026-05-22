# Deploying pharos-scheduler

Target: a single Linux box on the same VLAN as the TPC. Tested on Ubuntu 24.04 LTS but anything systemd-based will work.

## Prerequisites on the TPC

Before deploying the app, on the TPC itself (admin UI):

1. **SNTP enabled** -> Settings -> Network Time -> point at a working time source. Without this scheduled overrides fire at the wrong time.
2. **Dedicated user** "scheduler" with Control + Status (not Admin).
3. **HTTPS enabled** on port 443.

## One-time setup on the Linux box

```bash
# 1. System user + directories
sudo useradd --system --no-create-home --shell /usr/sbin/nologin pharos
sudo mkdir -p /opt/pharos-scheduler /var/lib/pharos-scheduler /etc/pharos-scheduler
sudo chown -R pharos:pharos /var/lib/pharos-scheduler

# 2. Code + venv
sudo cp -r ./{app,scripts,pyproject.toml,README.md} /opt/pharos-scheduler/
sudo chown -R pharos:pharos /opt/pharos-scheduler
sudo -u pharos python3.11 -m venv /opt/pharos-scheduler/.venv
sudo -u pharos /opt/pharos-scheduler/.venv/bin/pip install -e /opt/pharos-scheduler

# 3. Environment file (chmod 600, contains TPC password + secret key)
sudo install -m 600 -o pharos -g pharos .env /etc/pharos-scheduler/env
# Edit /etc/pharos-scheduler/env:
#   DB_PATH=/var/lib/pharos-scheduler/pharos.sqlite
#   APP_SECRET_KEY=<long random>
#   TPC_PASSWORD=<set>

# 4. Seed the DB with the first admin
sudo -u pharos env $(grep -v '^#' /etc/pharos-scheduler/env | xargs) \
  /opt/pharos-scheduler/.venv/bin/python -m app.seed \
  --admin-username craig --admin-password ChooseAStrongPassword

# 5. systemd unit
sudo cp deploy/pharos-scheduler.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pharos-scheduler
sudo systemctl status pharos-scheduler

# 6. Reverse proxy with auto-TLS
sudo apt install caddy
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile
# Edit Caddyfile to set your venue hostname and LAN subnet
sudo systemctl restart caddy

# 7. Nightly backup
sudo cp deploy/backup.sh /etc/cron.daily/pharos-scheduler-backup
sudo chmod +x /etc/cron.daily/pharos-scheduler-backup
```

## Verifying

```bash
# Service status + logs
systemctl status pharos-scheduler
journalctl -u pharos-scheduler -f

# Direct hit (bypass Caddy)
curl -i http://127.0.0.1:8000/login

# Via Caddy
curl -ki https://spine.gsc.org.uk/login
```

## Updating

```bash
sudo systemctl stop pharos-scheduler
sudo cp -r ./{app,pyproject.toml} /opt/pharos-scheduler/
sudo -u pharos /opt/pharos-scheduler/.venv/bin/pip install -e /opt/pharos-scheduler
sudo systemctl start pharos-scheduler
```

The DB schema is auto-created/upgraded on boot via SQLAlchemy create_all. For destructive changes (column renames, etc.) introduce Alembic migrations.

## Troubleshooting

| Symptom                          | Look at                                                   |
|----------------------------------|-----------------------------------------------------------|
| Overrides not firing             | `journalctl -u pharos-scheduler -f` while crossing time   |
| "TPC offline" banner             | `curl -k https://192.168.54.227/api/system` from the box  |
| Login redirects loop             | `APP_SECRET_KEY` mismatch between restarts (check env)    |
| HTTPS cert untrusted             | Distribute Caddy's internal CA root cert to client devices |
| Time drift                       | Check SNTP on both TPC and Linux box (`timedatectl`)      |

## Off-network requirement

**Do not** expose this app to the public internet. Caddy's `@not_lan` block enforces LAN-only at the proxy layer; the app's session cookies are not designed for hostile network exposure. For remote access, use Tailscale or your venue VPN.
