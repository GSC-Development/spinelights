"""Immediate (non-scheduled) controller actions: fire scene, release all.

These are the dashboard's quick-action buttons. No DB override row is created
- the action is logged to audit_log so we can answer "what happened at 21:43?"
but the lighting state is otherwise stateless from the app's perspective.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy.orm import Session

from app.auth import require_user
from app.db import get_db
from app.models import AuditAction, AuditLog, Scene, User
from app.tpc import TPCError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/controls")


# The release trigger number is project-specific. We default to 4 (matching
# the existing GSC project's "Release all timelines and scenes in 2s") but it
# is configurable via env if a re-uploaded project ever renumbers it.
RELEASE_TRIGGER_NUM = 4


def _status_response(request: Request, ok: bool, message: str, swatch: str | None = None):
    """Render a small status fragment for HTMX swap into #last-action."""
    return request.app.state.templates.TemplateResponse(
        request,
        "_last_action.html",
        {
            "ok": ok,
            "message": message,
            "swatch": swatch,
            "at": datetime.now(timezone.utc),
        },
        status_code=200 if ok else 502,
    )


@router.post("/scene/{scene_id}")
def fire_scene(
    scene_id: str,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    scene = db.get(Scene, scene_id)
    if scene is None or not scene.enabled:
        raise HTTPException(status_code=404, detail="scene not found")

    tpc = request.app.state.tpc
    try:
        tpc.fire_trigger(scene.trigger_num)
    except TPCError as e:
        logger.exception("control fire_scene %s failed", scene.key)
        db.add(AuditLog(
            actor_id=user.id, actor_username=user.username,
            action=AuditAction.CONTROL_SCENE_FIRED,
            target_type="scene", target_id=scene.id,
            detail=f"FAILED to fire scene {scene.key} (trigger {scene.trigger_num}): {e}",
        ))
        db.commit()
        return _status_response(request, ok=False, message=f"TPC error: {e}")

    db.add(AuditLog(
        actor_id=user.id, actor_username=user.username,
        action=AuditAction.CONTROL_SCENE_FIRED,
        target_type="scene", target_id=scene.id,
        detail=f"fired scene {scene.key} (trigger {scene.trigger_num})",
    ))
    db.commit()
    request.app.state.direct_color = None
    return _status_response(
        request, ok=True,
        message=f"{scene.display_name}",
        swatch=scene.swatch,
    )


@router.post("/release")
def fire_release(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """All Off: clear any direct colour override AND fire the release trigger.

    Doing both means the building returns to the daily schedule whether it was
    coloured by a direct picker call or by a scene trigger."""
    tpc = request.app.state.tpc
    errors: list[str] = []

    try:
        tpc.clear_overrides(fade_seconds=1.0)
    except TPCError as e:
        errors.append(f"clear overrides failed: {e}")

    try:
        tpc.fire_trigger(RELEASE_TRIGGER_NUM)
    except TPCError as e:
        errors.append(f"release trigger failed: {e}")

    if errors:
        logger.error("control release had errors: %s", errors)
        db.add(AuditLog(
            actor_id=user.id, actor_username=user.username,
            action=AuditAction.CONTROL_RELEASE_FIRED,
            detail="FAILED: " + "; ".join(errors),
        ))
        db.commit()
        return _status_response(request, ok=False, message=" / ".join(errors))

    db.add(AuditLog(
        actor_id=user.id, actor_username=user.username,
        action=AuditAction.CONTROL_RELEASE_FIRED,
        detail=f"cleared overrides + fired release (trigger {RELEASE_TRIGGER_NUM})",
    ))
    db.commit()
    request.app.state.direct_color = None
    return _status_response(request, ok=True, message="All off / released", swatch="#000000")


def _hex_to_rgb(hex_str: str) -> tuple[int, int, int]:
    h = hex_str.lstrip("#")
    if len(h) != 6:
        raise ValueError("hex colour must be #RRGGBB")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


@router.post("/color")
def set_color(
    request: Request,
    color: str = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Set the building to an arbitrary RGB colour. Bypasses the scene system
    entirely - uses PUT /api/override against group 0 (All Fixtures)."""
    try:
        r, g, b = _hex_to_rgb(color)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    tpc = request.app.state.tpc
    try:
        tpc.set_override_color(r, g, b, target="group", num=0, fade_seconds=1.0)
    except TPCError as e:
        logger.exception("control set_color %s failed", color)
        db.add(AuditLog(
            actor_id=user.id, actor_username=user.username,
            action=AuditAction.CONTROL_COLOR_SET,
            detail=f"FAILED to set {color}: {e}",
        ))
        db.commit()
        return _status_response(request, ok=False, message=f"TPC error: {e}")

    db.add(AuditLog(
        actor_id=user.id, actor_username=user.username,
        action=AuditAction.CONTROL_COLOR_SET,
        detail=f"set color {color} ({r},{g},{b}) on group 0",
    ))
    db.commit()

    # Remember the active direct override so the live panel can show it.
    # Cleared by /controls/release and overridden by any new scene fire.
    request.app.state.direct_color = color.lower()

    return _status_response(
        request, ok=True,
        message=f"Custom colour {color.upper()}",
        swatch=color,
    )
