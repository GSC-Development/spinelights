"""Dashboard: the at-a-glance 'what are the lights doing right now' screen."""
from __future__ import annotations

from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_user
from app.config import get_settings
from app.db import get_db
from app.models import Scene, User
from app.services import get_dashboard_state

router = APIRouter()


def _scene_lookup(db: Session) -> dict[str, Scene]:
    """Case-insensitive map of every Scene by display_name AND key. Used to
    translate a live scene name reported by the TPC into a curated swatch."""
    rows = list(db.execute(select(Scene)).scalars())
    lookup: dict[str, Scene] = {}
    for s in rows:
        lookup.setdefault(s.display_name.strip().lower(), s)
        lookup.setdefault(s.key.strip().lower(), s)
    return lookup


def _live_state(state, scene_lookup: dict[str, Scene]) -> tuple[str, str, str]:
    """Decide (bg_color, big_label, chip_text) for the live panel.

    Priority: TPC offline → scheduled override → live scene → live timeline → idle.
    """
    if not state.tpc_reachable:
        return ("#374151", "TPC offline", "Unreachable")

    if state.active_override is not None:
        ov = state.active_override
        return (ov.swatch, f"{ov.display_scene_label} — {ov.name}", "Override active")

    # Direct colour override (from /controls/color). The TPC has no GET for
    # active overrides, so we remember it in app state when we set it.
    direct = getattr(state, "direct_color", None)
    if direct:
        return (direct, f"Custom colour {direct.upper()}", "Custom colour")

    # Live scene takes precedence over timelines: scenes are what this project
    # actually drives the lights with day-to-day.
    onstage = [s for s in state.scenes if s.is_live]
    if onstage:
        live = onstage[0]
        match = scene_lookup.get(live.name.strip().lower())
        if match is not None:
            return (match.swatch, match.display_name, "Live scene")
        return ("#6b7280", live.name, "Live scene")

    active_tl = next((t for t in state.timelines if t.is_active), None)
    if active_tl is not None:
        mapped = state.daily_timelines.get(active_tl.num)
        if mapped is not None:
            return (mapped.swatch, mapped.display_name, "Timeline running")
        return ("#6b7280", f"Timeline {active_tl.num} (unmapped)", "Timeline running")

    return ("#000000", "Lights off", "Idle")


def _panel_context(request: Request, db: Session) -> dict:
    """Shared context for the live state panel — used by both initial render and poll."""
    tpc = request.app.state.tpc
    state = get_dashboard_state(db, tpc)
    # Attach the in-memory direct-colour override (set by /controls/color).
    state.direct_color = getattr(request.app.state, "direct_color", None)
    lookup = _scene_lookup(db)
    bg, label, chip = _live_state(state, lookup)
    tz = ZoneInfo(get_settings().app_timezone)
    next_up = [
        ov for ov in state.upcoming
        if state.active_override is None or ov.id != state.active_override.id
    ][:3]
    return {
        "state": state,
        "bg_color": bg,
        "bg_label": label,
        "chip": chip,
        "local_tz": tz,
        "next_up": next_up,
    }


@router.get("/dashboard")
def show_dashboard(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """The controller: live state panel + colour wheel + presets + next-up.

    Scenes are no longer surfaced as buttons — every action goes through
    /controls/color (direct RGB override) for deterministic live state. The
    Scene table is still used for scheduled overrides via /admin and /overrides.
    """
    ctx = _panel_context(request, db)
    ctx["user"] = user
    return request.app.state.templates.TemplateResponse(request, "dashboard.html", ctx)


@router.get("/dashboard/poll")
def dashboard_poll(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """HTMX poll target: returns just the live panel fragment."""
    return request.app.state.templates.TemplateResponse(
        request,
        "_dashboard_panel.html",
        _panel_context(request, db),
    )
