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
from app.services import (
    OverrideValidationError,
    cancel_override,
    create_override,
    find_conflicts,
    update_override,
)

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
    upcoming = list(db.execute(
        select(Override)
        .where(
            Override.status.in_([OverrideStatus.SCHEDULED, OverrideStatus.ACTIVE]),
            Override.end_at > now,
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


_NEAREST_DEFAULT = "medium"


def _nearest_speed(value, mapping: dict) -> str:
    """Inverse of the speed maps: pick the label whose numeric value is
    closest to the stored one. Used to pre-fill the edit form."""
    if value is None:
        return _NEAREST_DEFAULT
    return min(mapping, key=lambda k: abs(mapping[k] - value))


def _reverse_speed(effect_name: str, params: dict) -> str:
    if effect_name == "rainbow":
        return _nearest_speed(params.get("speed"), _RAINBOW_SPEED_MAP)
    if effect_name == "crossfade":
        return _nearest_speed(params.get("dwell"), _CROSSFADE_DWELL_MAP)
    if effect_name == "two_color":
        if params.get("chase"):
            return _nearest_speed(params.get("tick"), _TWO_COLOR_TICK_MAP)
        return _nearest_speed(params.get("dwell"), _TWO_COLOR_DWELL_MAP)
    return _NEAREST_DEFAULT


def _override_to_form(ov: Override) -> dict:
    """Reverse-map a stored override into the form dict the template expects."""
    tz = _local_tz()
    fmt = "%Y-%m-%dT%H:%M"
    form = {
        "name": ov.name,
        "start_at": ov.start_at.astimezone(tz).strftime(fmt),
        "end_at": ov.end_at.astimezone(tz).strftime(fmt),
        "mode": "preset",
        "color_hex": "#3b82f6",
        "effect_name": "rainbow",
        "effect_mode": "together",
        "effect_speed": "medium",
        "effect_palette": "rainbow",
        "effect_color_a": "#dc2626",
        "effect_color_b": "#22c55e",
        "notes": ov.notes or "",
    }
    if ov.is_effect:
        p = ov.effect_params
        form["mode"] = "effect"
        form["effect_name"] = ov.effect_name
        form["effect_mode"] = "chase" if p.get("chase") else "together"
        form["effect_speed"] = _reverse_speed(ov.effect_name, p)
        if ov.effect_name == "crossfade":
            form["effect_palette"] = p.get("palette", "rainbow")
        if ov.effect_name == "two_color":
            form["effect_color_a"] = p.get("color_a", "#dc2626")
            form["effect_color_b"] = p.get("color_b", "#22c55e")
    elif ov.color_hex:
        # Land on the Custom tab pre-loaded with the exact hex — the wheel
        # round-trips any colour, preset or not.
        form["mode"] = "custom"
        form["color_hex"] = ov.color_hex
    return form


def _form_from_fields(**fields) -> dict:
    """Assemble the template form dict from raw POST fields, applying the same
    empty-value fallbacks the new-override form uses."""
    return {
        "name": fields["name"],
        "start_at": fields["start_at"], "end_at": fields["end_at"],
        "mode": fields["mode"], "color_hex": fields["color_hex"] or "#3b82f6",
        "effect_name": fields["effect_name"] or "rainbow",
        "effect_mode": fields["effect_mode"], "effect_speed": fields["effect_speed"],
        "effect_palette": fields["effect_palette"],
        "effect_color_a": fields["effect_color_a"], "effect_color_b": fields["effect_color_b"],
        "notes": fields["notes"],
    }


def _target_kwargs(form: dict) -> dict:
    """Translate the chosen mode into create/update kwargs. Raises
    OverrideValidationError for an incomplete effect selection."""
    if form["mode"] == "effect":
        if not form["effect_name"]:
            raise OverrideValidationError("Pick an effect")
        return {
            "effect_name": form["effect_name"],
            "effect_params": _build_effect_params(form["effect_name"], form),
        }
    # both 'preset' and 'custom' produce a single hex.
    return {"color_hex": form["color_hex"]}


def _render_form(request: Request, user: User, form: dict, *,
                 error: str | None = None, conflicts: list | None = None,
                 status: int = 200, form_action: str = "/overrides/new",
                 heading: str = "Add override"):
    return request.app.state.templates.TemplateResponse(
        request, "override_form.html",
        {
            "user": user, "error": error,
            "conflicts": conflicts or [], "form": form,
            "settings": get_settings(), "palettes": PALETTES,
            "form_action": form_action, "heading": heading,
        },
        status_code=status,
    )


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
    form = _form_from_fields(
        name=name, start_at=start_at, end_at=end_at, mode=mode,
        color_hex=color_hex, effect_name=effect_name, effect_mode=effect_mode,
        effect_speed=effect_speed, effect_palette=effect_palette,
        effect_color_a=effect_color_a, effect_color_b=effect_color_b, notes=notes,
    )

    try:
        start_dt = _parse_local_datetime(start_at)
        end_dt = _parse_local_datetime(end_at)
    except ValueError:
        return _render_form(request, user, form, error="Invalid date/time format", status=400)

    conflicts = find_conflicts(db, start_dt, end_dt)
    if conflicts and confirm_overlap != "yes":
        return _render_form(request, user, form, conflicts=conflicts)

    try:
        kwargs = _target_kwargs(form)
        create_override(
            db, creator=user, name=name, start_at=start_dt, end_at=end_dt,
            notes=notes or None, **kwargs,
        )
    except OverrideValidationError as e:
        return _render_form(request, user, form, error=str(e), status=400)

    return RedirectResponse(url="/overrides", status_code=303)


def _editable_or_error(db: Session, override_id: str, user: User) -> Override:
    """Fetch an override and assert the user may edit it. Raises HTTPException."""
    override = db.get(Override, override_id)
    if override is None:
        raise HTTPException(status_code=404, detail="override not found")
    if override.created_by_id != user.id and user.role != Role.ADMIN:
        raise HTTPException(status_code=403, detail="you can only edit your own overrides")
    if override.status != OverrideStatus.SCHEDULED:
        raise HTTPException(
            status_code=400,
            detail=f"only scheduled overrides can be edited (this one is {override.status.value})",
        )
    if override.scene_id is not None:
        raise HTTPException(
            status_code=400,
            detail="legacy scene-based overrides can't be edited here; cancel and recreate",
        )
    return override


@router.get("/overrides/{override_id}/edit")
def edit_override_form(
    override_id: str,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    override = _editable_or_error(db, override_id, user)
    return _render_form(
        request, user, _override_to_form(override),
        form_action=f"/overrides/{override_id}/edit", heading="Edit override",
    )


@router.post("/overrides/{override_id}/edit")
def edit_override_submit(
    override_id: str,
    request: Request,
    name: str = Form(...),
    start_at: str = Form(...),
    end_at: str = Form(...),
    mode: str = Form("preset"),
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
    override = _editable_or_error(db, override_id, user)
    form = _form_from_fields(
        name=name, start_at=start_at, end_at=end_at, mode=mode,
        color_hex=color_hex, effect_name=effect_name, effect_mode=effect_mode,
        effect_speed=effect_speed, effect_palette=effect_palette,
        effect_color_a=effect_color_a, effect_color_b=effect_color_b, notes=notes,
    )
    action = f"/overrides/{override_id}/edit"

    try:
        start_dt = _parse_local_datetime(start_at)
        end_dt = _parse_local_datetime(end_at)
    except ValueError:
        return _render_form(request, user, form, error="Invalid date/time format",
                            status=400, form_action=action, heading="Edit override")

    # Exclude self so an unchanged window doesn't count as a conflict.
    conflicts = find_conflicts(db, start_dt, end_dt, exclude_id=override_id)
    if conflicts and confirm_overlap != "yes":
        return _render_form(request, user, form, conflicts=conflicts,
                            form_action=action, heading="Edit override")

    try:
        kwargs = _target_kwargs(form)
        update_override(
            db, override=override, actor=user, name=name,
            start_at=start_dt, end_at=end_dt, notes=notes or None, **kwargs,
        )
    except OverrideValidationError as e:
        return _render_form(request, user, form, error=str(e), status=400,
                            form_action=action, heading="Edit override")

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
