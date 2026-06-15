"""Override CRUD routes."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_admin, require_user
from app.config import get_settings
from app.db import get_db
from app.effects import PALETTES
from app.models import Override, OverrideStatus, Role, Scene, User
from app.services import OverrideValidationError, cancel_override, create_override, find_conflicts

router = APIRouter()


def _local_tz() -> ZoneInfo:
    return ZoneInfo(get_settings().app_timezone)


def _parse_local_datetime(raw: str) -> datetime:
    """Parse an HTML datetime-local input (YYYY-MM-DDTHH:MM) as Europe/London."""
    naive = datetime.fromisoformat(raw)
    return naive.replace(tzinfo=_local_tz())


def _default_form_times() -> tuple[str, str]:
    """Suggest sensible defaults for a new override:
    start = the next upcoming 18:01 (today if not yet 18:00, else tomorrow)
    end   = same day at 23:00

    The 18:01 default is because the project's daily "Release all" trigger
    fires at 18:00; scheduling earlier would be wiped out.
    """
    settings = get_settings()
    tz = _local_tz()
    now = datetime.now(tz)
    release_cutoff = now.replace(
        hour=settings.daily_release_hour, minute=settings.daily_release_minute,
        second=0, microsecond=0,
    )
    start = release_cutoff + timedelta(minutes=1)
    if now >= release_cutoff:
        start = start + timedelta(days=1)
    end = start.replace(hour=23, minute=0)
    fmt = "%Y-%m-%dT%H:%M"
    return start.strftime(fmt), end.strftime(fmt)


def _list_context(db: Session, user: User) -> dict:
    """Shared context for the overrides list — used by both the full page and
    the HTMX poll fragment."""
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=14)
    upcoming = list(db.execute(
        select(Override)
        .where(
            Override.status.in_([OverrideStatus.SCHEDULED, OverrideStatus.ACTIVE]),
            Override.end_at > now,
            Override.start_at < cutoff,
        )
        .order_by(Override.start_at)
    ).scalars())
    past = list(db.execute(
        select(Override)
        .where(Override.end_at <= now)
        .order_by(Override.end_at.desc())
        .limit(20)
    ).scalars())
    return {
        "user": user, "upcoming": upcoming, "past": past,
        "local_tz": _local_tz(),
        "settings": get_settings(),
    }


@router.get("/overrides")
def list_overrides(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    return request.app.state.templates.TemplateResponse(
        request,
        "overrides_list.html",
        _list_context(db, user),
    )


@router.get("/overrides/poll")
def list_overrides_poll(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """HTMX poll target: returns just the upcoming/past panel fragment, so
    overrides programmed by other people show up without a manual reload."""
    return request.app.state.templates.TemplateResponse(
        request,
        "_overrides_panel.html",
        _list_context(db, user),
    )


@router.get("/overrides/new")
def new_override_form(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    default_start, default_end = _default_form_times()
    return request.app.state.templates.TemplateResponse(
        request,
        "override_form.html",
        {
            "user": user,
            "error": None,
            "conflicts": [],
            "settings": get_settings(),
            "palettes": PALETTES,
            "form": {
                "name": "",
                "start_at": default_start,
                "end_at": default_end,
                "mode": "preset",
                "color_hex": "#3b82f6",
                "effect_name": "rainbow",
                "effect_mode": "together",
                "effect_speed": "medium",
                "effect_palette": "rainbow",
                "effect_color_a": "#dc2626",
                "effect_color_b": "#22c55e",
                "notes": "",
            },
        },
    )


_RAINBOW_SPEED_MAP = {"slow": 1.0, "medium": 2.5, "fast": 12.0}
_CROSSFADE_DWELL_MAP = {"slow": 8.0, "medium": 4.0, "fast": 2.0}
_CROSSFADE_FADE_MAP = {"slow": 3.0, "medium": 1.8, "fast": 0.8}
_TWO_COLOR_DWELL_MAP = {"slow": 6.0, "medium": 3.0, "fast": 1.5}
_TWO_COLOR_TICK_MAP = {"slow": 0.9, "medium": 0.5, "fast": 0.25}


def _build_effect_params(effect_name: str, sub: dict) -> dict:
    """Turn form fields into the dict expected by the effect engine."""
    chase = (sub.get("effect_mode") or "together").strip().lower() == "chase"
    speed = (sub.get("effect_speed") or "medium").strip().lower()
    if effect_name == "rainbow":
        return {"speed": _RAINBOW_SPEED_MAP.get(speed, 2.5), "chase": chase}
    if effect_name == "crossfade":
        from app.effects import PALETTES
        palette = (sub.get("effect_palette") or "rainbow").strip().lower()
        if palette not in PALETTES:
            palette = "rainbow"
        return {
            "palette": palette,
            "dwell": _CROSSFADE_DWELL_MAP.get(speed, 4.0),
            "fade": _CROSSFADE_FADE_MAP.get(speed, 1.8),
            "chase": chase,
        }
    if effect_name == "two_color":
        params: dict = {
            "color_a": (sub.get("effect_color_a") or "#dc2626").lower(),
            "color_b": (sub.get("effect_color_b") or "#22c55e").lower(),
            "chase": chase,
        }
        if chase:
            params["tick"] = _TWO_COLOR_TICK_MAP.get(speed, 0.5)
        else:
            params["dwell"] = _TWO_COLOR_DWELL_MAP.get(speed, 3.0)
            params["fade"] = min(params["dwell"] * 0.4, 2.0)
        return params
    return {}


@router.post("/overrides/new")
def create_override_submit(
    request: Request,
    name: str = Form(...),
    start_at: str = Form(...),
    end_at: str = Form(...),
    mode: str = Form("preset"),          # "preset" | "effect" | "custom"
    color_hex: str = Form(""),
    effect_name: str = Form(""),
    effect_mode: str = Form("together"),
    effect_speed: str = Form("medium"),
    effect_palette: str = Form("rainbow"),
    effect_color_a: str = Form("#dc2626"),
    effect_color_b: str = Form("#22c55e"),
    notes: str = Form(""),
    confirm_overlap: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    form = {
        "name": name, "start_at": start_at, "end_at": end_at,
        "mode": mode, "color_hex": color_hex or "#3b82f6",
        "effect_name": effect_name or "rainbow",
        "effect_mode": effect_mode, "effect_speed": effect_speed,
        "effect_palette": effect_palette,
        "effect_color_a": effect_color_a, "effect_color_b": effect_color_b,
        "notes": notes,
    }

    def _render(error: str | None = None, conflicts: list | None = None, status: int = 200):
        return request.app.state.templates.TemplateResponse(
            request, "override_form.html",
            {
                "user": user, "error": error,
                "conflicts": conflicts or [], "form": form,
                "settings": get_settings(),
                "palettes": PALETTES,
            },
            status_code=status,
        )

    try:
        start_dt = _parse_local_datetime(start_at)
        end_dt = _parse_local_datetime(end_at)
    except ValueError:
        return _render(error="Invalid date/time format", status=400)

    conflicts = find_conflicts(db, start_dt, end_dt)
    if conflicts and confirm_overlap != "yes":
        return _render(conflicts=conflicts)

    kwargs: dict = {
        "creator": user, "name": name,
        "start_at": start_dt, "end_at": end_dt,
        "notes": notes or None,
    }
    if mode == "effect":
        if not effect_name:
            return _render(error="Pick an effect", status=400)
        kwargs["effect_name"] = effect_name
        kwargs["effect_params"] = _build_effect_params(effect_name, form)
    else:
        # both 'preset' and 'custom' produce a single hex.
        kwargs["color_hex"] = color_hex

    try:
        create_override(db, **kwargs)
    except OverrideValidationError as e:
        return _render(error=str(e), status=400)

    return RedirectResponse(url="/overrides", status_code=303)


@router.post("/overrides/{override_id}/cancel")
def cancel(
    override_id: str,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    override = db.get(Override, override_id)
    if override is None:
        raise HTTPException(status_code=404, detail="override not found")
    if override.created_by_id != user.id and user.role != Role.ADMIN:
        raise HTTPException(status_code=403, detail="you can only cancel your own overrides")
    cancel_override(
        db, override=override, actor=user,
        tpc=request.app.state.tpc,
        effect_engine=request.app.state.effect_engine,
    )
    return RedirectResponse(url="/overrides", status_code=303)


# Defensive import to silence unused-import linters; require_admin is exported
# for future admin-only override actions.
_ = require_admin
