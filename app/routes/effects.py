"""Effect engine routes: /effects page + start / stop endpoints."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session

from app.auth import require_user
from app.db import get_db
from app.effects import PALETTES
from app.models import AuditAction, AuditLog, User

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/effects")


def _status_fragment(request: Request, message: str, ok: bool = True):
    return request.app.state.templates.TemplateResponse(
        request,
        "_effect_status.html",
        {
            "current": request.app.state.effect_engine.current,
            "message": message,
            "ok": ok,
        },
    )


@router.get("")
def show_effects(request: Request, user: User = Depends(require_user)):
    return request.app.state.templates.TemplateResponse(
        request,
        "effects.html",
        {
            "user": user,
            "current": request.app.state.effect_engine.current,
            "palettes": PALETTES,
        },
    )


# Speed dropdown → numeric mapping. Different per effect because "slow / medium
# / fast" means different things for hue rotation vs palette dwell vs chase tick.
_RAINBOW_SPEED = {"slow": 1.0, "medium": 2.5, "fast": 12.0}
_CROSSFADE_DWELL = {"slow": 8.0, "medium": 4.0, "fast": 2.0}
_CROSSFADE_FADE = {"slow": 3.0, "medium": 1.8, "fast": 0.8}
_TWO_COLOR_DWELL = {"slow": 6.0, "medium": 3.0, "fast": 1.5}     # together mode
_TWO_COLOR_TICK = {"slow": 0.9, "medium": 0.5, "fast": 0.25}     # chase mode


def _is_chase(mode: str) -> bool:
    return mode.strip().lower() == "chase"


@router.post("/rainbow/start")
def start_rainbow(
    request: Request,
    speed: str = Form("medium"),
    mode: str = Form("together"),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    chase = _is_chase(mode)
    engine = request.app.state.effect_engine
    engine.start("rainbow", {
        "speed": _RAINBOW_SPEED.get(speed, 2.5),
        "chase": chase,
    })
    request.app.state.direct_color = None
    db.add(AuditLog(
        actor_id=user.id, actor_username=user.username,
        action=AuditAction.CONTROL_COLOR_SET,
        detail=f"effect: rainbow speed={speed} mode={mode}",
    ))
    db.commit()
    return _status_fragment(request, f"Rainbow{' chase' if chase else ''} started")


@router.post("/crossfade/start")
def start_crossfade(
    request: Request,
    palette: str = Form("rainbow"),
    speed: str = Form("medium"),
    mode: str = Form("together"),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    if palette not in PALETTES:
        palette = "rainbow"
    engine = request.app.state.effect_engine
    engine.start("crossfade", {
        "palette": palette,
        "dwell": _CROSSFADE_DWELL.get(speed, 4.0),
        "fade": _CROSSFADE_FADE.get(speed, 1.8),
        "chase": _is_chase(mode),
    })
    request.app.state.direct_color = None
    db.add(AuditLog(
        actor_id=user.id, actor_username=user.username,
        action=AuditAction.CONTROL_COLOR_SET,
        detail=f"effect: crossfade palette={palette} speed={speed} mode={mode}",
    ))
    db.commit()
    return _status_fragment(request, f"Crossfading — {palette}")


@router.post("/two_color/start")
def start_two_color(
    request: Request,
    color_a: str = Form("#dc2626"),
    color_b: str = Form("#22c55e"),
    speed: str = Form("medium"),
    mode: str = Form("together"),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    chase = _is_chase(mode)
    params: dict = {
        "color_a": color_a.lower(),
        "color_b": color_b.lower(),
        "chase": chase,
    }
    if chase:
        params["tick"] = _TWO_COLOR_TICK.get(speed, 0.5)
    else:
        params["dwell"] = _TWO_COLOR_DWELL.get(speed, 3.0)
        params["fade"] = min(params["dwell"] * 0.4, 2.0)

    engine = request.app.state.effect_engine
    engine.start("two_color", params)
    request.app.state.direct_color = None
    db.add(AuditLog(
        actor_id=user.id, actor_username=user.username,
        action=AuditAction.CONTROL_COLOR_SET,
        detail=f"effect: two_color a={color_a} b={color_b} speed={speed} mode={mode}",
    ))
    db.commit()
    return _status_fragment(request, f"Two-colour{' chase' if chase else ''} — {color_a.upper()} / {color_b.upper()}")


@router.post("/stop")
def stop_effect(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    engine = request.app.state.effect_engine
    was = engine.current
    engine.stop()
    if was is not None:
        db.add(AuditLog(
            actor_id=user.id, actor_username=user.username,
            action=AuditAction.CONTROL_RELEASE_FIRED,
            detail=f"effect stopped: {was.name}",
        ))
        db.commit()
    return _status_fragment(request, "Stopped" if was else "Nothing running")
