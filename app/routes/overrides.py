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


@router.get("/overrides")
def list_overrides(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
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
    return request.app.state.templates.TemplateResponse(
        request,
        "overrides_list.html",
        {"user": user, "upcoming": upcoming, "past": past, "local_tz": _local_tz()},
    )


@router.get("/overrides/new")
def new_override_form(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    scenes = list(db.execute(
        select(Scene).where(Scene.enabled.is_(True)).order_by(Scene.sort_order)
    ).scalars())
    default_start, default_end = _default_form_times()
    return request.app.state.templates.TemplateResponse(
        request,
        "override_form.html",
        {
            "user": user,
            "scenes": scenes,
            "error": None,
            "conflicts": [],
            "form": {
                "name": "",
                "start_at": default_start,
                "end_at": default_end,
                "mode": "scene",
                "scene_id": "",
                "color_hex": "#3b82f6",
                "notes": "",
            },
        },
    )


@router.post("/overrides/new")
def create_override_submit(
    request: Request,
    name: str = Form(...),
    start_at: str = Form(...),
    end_at: str = Form(...),
    mode: str = Form("scene"),           # "scene" or "color"
    scene_id: str = Form(""),
    color_hex: str = Form(""),
    notes: str = Form(""),
    confirm_overlap: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    scenes = list(db.execute(
        select(Scene).where(Scene.enabled.is_(True)).order_by(Scene.sort_order)
    ).scalars())
    form = {
        "name": name, "start_at": start_at, "end_at": end_at,
        "mode": mode, "scene_id": scene_id, "color_hex": color_hex or "#3b82f6",
        "notes": notes,
    }
    try:
        start_dt = _parse_local_datetime(start_at)
        end_dt = _parse_local_datetime(end_at)
    except ValueError:
        return request.app.state.templates.TemplateResponse(
            request,
            "override_form.html",
            {"user": user, "scenes": scenes, "error": "Invalid date/time format", "conflicts": [], "form": form},
            status_code=400,
        )

    conflicts = find_conflicts(db, start_dt, end_dt)
    if conflicts and confirm_overlap != "yes":
        return request.app.state.templates.TemplateResponse(
            request,
            "override_form.html",
            {"user": user, "scenes": scenes, "error": None, "conflicts": conflicts, "form": form},
            status_code=200,
        )

    try:
        kwargs: dict = {
            "creator": user, "name": name,
            "start_at": start_dt, "end_at": end_dt,
            "notes": notes or None,
        }
        if mode == "color":
            kwargs["color_hex"] = color_hex
        else:
            kwargs["scene_id"] = scene_id
        create_override(db, **kwargs)
    except OverrideValidationError as e:
        return request.app.state.templates.TemplateResponse(
            request,
            "override_form.html",
            {"user": user, "scenes": scenes, "error": str(e), "conflicts": [], "form": form},
            status_code=400,
        )

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
    cancel_override(db, override=override, actor=user, tpc=request.app.state.tpc)
    return RedirectResponse(url="/overrides", status_code=303)


# Defensive import to silence unused-import linters; require_admin is exported
# for future admin-only override actions.
_ = require_admin
