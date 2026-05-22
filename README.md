# Pharos Overrides Scheduler

Web app for scheduling time-bounded lighting overrides on the Glasgow Science Centre Building Spine Pharos TPC, without touching the Designer project.

## What it does

- Authorised staff add overrides ("blue wash, 18:00-22:00 for Smith wedding") via a web UI.
- A background scheduler fires the relevant Pharos triggers at the start time and the project's "Release all" trigger at the end.
- A live dashboard shows what's playing right now, colour-coded.
- The .pd2 project is never touched. Daily schedule remains the source of truth.

## Development (macOS / Linux)

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env
# Edit .env: TPC_PASSWORD, APP_SECRET_KEY

# Verify TPC connectivity
python scripts/smoke_test_tpc.py

# Seed DB with admin + curated scene palette
python -m app.seed --admin-username craig --admin-password ChangeMeFirstLogin

# Run the app
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
# Open http://127.0.0.1:8000/
```

## Layout

```
app/
  config.py        env / .env loader
  db.py            SQLAlchemy engine + session factory
  models.py        ORM: users, scenes, daily_timelines, overrides, audit_log
  security.py      bcrypt password hashing
  tpc.py           Pharos TPC HTTP client (verified against firmware 2.10)
  scheduler.py     APScheduler jobs + boot recovery
  services.py      override creation / cancellation / dashboard state
  auth.py          session-cookie auth + FastAPI dependencies
  main.py          app factory + lifespan
  seed.py          first-run admin + scene seed
  routes/
    auth.py        /login /logout
    dashboard.py   /dashboard + /dashboard/poll (HTMX fragment)
    overrides.py   /overrides list + new + cancel
    admin.py       /admin (users, scenes, daily timelines, audit log)
  templates/       Jinja templates (Tailwind via CDN, HTMX)
scripts/
  smoke_test_tpc.py    end-to-end TPC client verification
deploy/
  pharos-scheduler.service    systemd unit
  Caddyfile                   reverse proxy with auto-TLS
  backup.sh                   nightly SQLite backup
  README.md                   deploy walkthrough
```

## TPC prerequisites

1. **SNTP enabled** on the controller (Settings -> Network Time). Without it, the controller's clock drifts and overrides fire at the wrong time.
2. **Dedicated user** "scheduler" with Control + Status permissions (not Admin). Credentials go in `.env` / `/etc/pharos-scheduler/env`, never in source.
3. **HTTPS enabled** on port 443.

## What's still loose / known follow-ups

- **`/api/scene` not yet queried.** The dashboard shows what `/api/timeline` reports, but this Pharos project drives the lights via scenes, not timelines. Adding `list_scenes()` to the TPC client and surfacing scene state on the dashboard would make "what colour is the building right now" more accurate. Today the dashboard falls back to "Lights off" / "Timeline N (unmapped)" when the daily schedule is running.
- **No edit-override route.** v1 is cancel + recreate. Acceptable.
- **No recurring overrides** (weekly slots). v1.1 candidate.
- **No iCal export / Outlook integration.** v1.1.
- **No panic button.** Wire to trigger 4 ("Release all timelines and scenes in 2s") and a confirm modal.

## Deploying to the venue Linux box

See `deploy/README.md`.
