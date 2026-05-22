"""Admin routes: users, scenes, daily timelines, audit log."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_admin
from app.db import get_db
from app.models import (
    AuditAction,
    AuditLog,
    DailyTimeline,
    Role,
    Scene,
    User,
)
from app.security import PasswordTooLongError, hash_password

router = APIRouter(prefix="/admin")


@router.get("")
def admin_home(
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    users = list(db.execute(select(User).order_by(User.username)).scalars())
    scenes = list(db.execute(select(Scene).order_by(Scene.sort_order)).scalars())
    dailies = list(db.execute(
        select(DailyTimeline).order_by(DailyTimeline.source, DailyTimeline.num)
    ).scalars())
    recent_audit = list(db.execute(
        select(AuditLog).order_by(AuditLog.at.desc()).limit(100)
    ).scalars())
    return request.app.state.templates.TemplateResponse(
        request,
        "admin.html",
        {
            "user": user,
            "users": users,
            "scenes": scenes,
            "dailies": dailies,
            "audit": recent_audit,
        },
    )


# --- users -----------------------------------------------------------------


@router.post("/users/new")
def create_user(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form("operator"),
    display_name: str = Form(""),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if not username.strip() or not password:
        raise HTTPException(status_code=400, detail="username and password required")
    if db.execute(select(User).where(User.username == username)).scalar_one_or_none():
        raise HTTPException(status_code=400, detail="username already in use")
    try:
        new = User(
            username=username.strip(),
            password_hash=hash_password(password),
            role=Role(role),
            display_name=display_name.strip() or None,
        )
    except PasswordTooLongError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"invalid role: {role}") from e
    db.add(new)
    db.flush()
    db.add(AuditLog(
        actor_id=user.id,
        actor_username=user.username,
        action=AuditAction.USER_CREATED,
        target_type="user",
        target_id=new.id,
        detail=f"created {new.username} role={new.role.value}",
    ))
    db.commit()
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/users/{user_id}/disable")
def disable_user(
    user_id: str,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="user not found")
    if target.id == user.id:
        raise HTTPException(status_code=400, detail="cannot disable yourself")
    target.enabled = False
    db.add(AuditLog(
        actor_id=user.id,
        actor_username=user.username,
        action=AuditAction.USER_DISABLED,
        target_type="user",
        target_id=target.id,
        detail=f"disabled {target.username}",
    ))
    db.commit()
    return RedirectResponse(url="/admin", status_code=303)


# --- scenes ----------------------------------------------------------------


@router.post("/scenes/new")
def create_scene(
    request: Request,
    key: str = Form(...),
    display_name: str = Form(...),
    swatch: str = Form(...),
    trigger_num: int = Form(...),
    release_trigger_num: int = Form(...),
    notes: str = Form(""),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if db.execute(select(Scene).where(Scene.key == key)).scalar_one_or_none():
        raise HTTPException(status_code=400, detail="scene key already in use")
    sort_order = (db.execute(select(Scene)).scalars().all().__len__())
    s = Scene(
        key=key.strip(),
        display_name=display_name.strip(),
        swatch=swatch,
        trigger_num=trigger_num,
        release_trigger_num=release_trigger_num,
        notes=notes or None,
        sort_order=sort_order,
    )
    db.add(s)
    db.flush()
    db.add(AuditLog(
        actor_id=user.id,
        actor_username=user.username,
        action=AuditAction.SCENE_CREATED,
        target_type="scene",
        target_id=s.id,
        detail=f"created scene {s.key} -> trigger {trigger_num}",
    ))
    db.commit()
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/scenes/{scene_id}/disable")
def disable_scene(
    scene_id: str,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    s = db.get(Scene, scene_id)
    if s is None:
        raise HTTPException(status_code=404, detail="scene not found")
    s.enabled = False
    db.add(AuditLog(
        actor_id=user.id,
        actor_username=user.username,
        action=AuditAction.SCENE_DISABLED,
        target_type="scene",
        target_id=s.id,
        detail=f"disabled scene {s.key}",
    ))
    db.commit()
    return RedirectResponse(url="/admin", status_code=303)


# --- daily timelines -------------------------------------------------------


@router.post("/dailies/upsert")
def upsert_daily(
    source: str = Form(...),
    num: int = Form(...),
    display_name: str = Form(...),
    swatch: str = Form(...),
    notes: str = Form(""),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if source not in {"timeline", "scene"}:
        raise HTTPException(status_code=400, detail="source must be 'timeline' or 'scene'")
    existing = db.execute(
        select(DailyTimeline).where(DailyTimeline.source == source, DailyTimeline.num == num)
    ).scalar_one_or_none()
    if existing is None:
        d = DailyTimeline(
            source=source, num=num, display_name=display_name.strip(),
            swatch=swatch, notes=notes or None,
        )
        db.add(d)
        d_id = "<new>"
    else:
        existing.display_name = display_name.strip()
        existing.swatch = swatch
        existing.notes = notes or None
        d_id = existing.id
    db.add(AuditLog(
        actor_id=user.id,
        actor_username=user.username,
        action=AuditAction.DAILY_TIMELINE_UPSERTED,
        target_type="daily_timeline",
        target_id=d_id,
        detail=f"{source}#{num} -> {display_name} {swatch}",
    ))
    db.commit()
    return RedirectResponse(url="/admin", status_code=303)
