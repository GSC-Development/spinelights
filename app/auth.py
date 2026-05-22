"""Session-cookie auth helpers + FastAPI dependencies.

We use Starlette's SessionMiddleware (itsdangerous-signed cookies). The
session payload is small: {"user_id": "<uuid>"}. The actual User row is
loaded fresh from the DB on every request via the get_current_user dependency.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import AuditAction, AuditLog, Role, User
from app.security import verify_password


def authenticate_user(db: Session, username: str, password: str) -> User | None:
    user = db.execute(select(User).where(User.username == username)).scalar_one_or_none()
    if user is None or not user.enabled:
        return None
    if not verify_password(password, user.password_hash):
        return None
    user.last_login_at = datetime.now(timezone.utc)
    db.add(AuditLog(
        actor_id=user.id,
        actor_username=user.username,
        action=AuditAction.USER_LOGIN,
        target_type="user",
        target_id=user.id,
    ))
    db.commit()
    return user


def login_session(request: Request, user: User) -> None:
    request.session["user_id"] = user.id


def logout_session(request: Request) -> None:
    request.session.pop("user_id", None)


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    """Returns the current user, or None if not logged in. Use this for routes
    that have an unauthenticated fallback. For routes that REQUIRE login, use
    require_user instead.

    Honours APP_AUTO_LOGIN_AS: if set, looks up that username and returns it
    without needing a session cookie. This is for LAN-only deployments where
    Caddy enforces network-layer access control."""
    user_id = request.session.get("user_id")
    if user_id:
        user = db.get(User, user_id)
        if user is not None and user.enabled:
            return user
        request.session.pop("user_id", None)

    auto = get_settings().app_auto_login_as.strip()
    if auto:
        from sqlalchemy import select
        user = db.execute(select(User).where(User.username == auto)).scalar_one_or_none()
        if user is not None and user.enabled:
            # Cache in session so audit log + cross-request lookups are cheap
            request.session["user_id"] = user.id
            return user
    return None


def require_user(user: User | None = Depends(get_current_user)) -> User:
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="login required")
    return user


def require_admin(user: User = Depends(require_user)) -> User:
    if user.role != Role.ADMIN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin only")
    return user
