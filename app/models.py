"""ORM models for the scheduler.

Five tables:
  users           - app login accounts (Operator / Admin)
  scenes          - admin-curated palette mapping a display name to TPC trigger nums
  daily_timelines - admin-managed map of "what the project does normally" for the dashboard
  overrides       - the heart of the system: time-bounded lighting takeovers
  audit_log       - append-only record of every meaningful action

Times are stored as timezone-aware UTC datetimes. Display conversion to
Europe/London happens at the edge (templates).
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return str(uuid.uuid4())


class Role(str, enum.Enum):
    OPERATOR = "operator"
    ADMIN = "admin"


class OverrideStatus(str, enum.Enum):
    SCHEDULED = "scheduled"   # in the future, jobs queued in APScheduler
    ACTIVE = "active"         # currently firing (between start_at and end_at)
    COMPLETED = "completed"   # ran to its end_at
    CANCELLED = "cancelled"   # cancelled by a user before completing
    FAILED = "failed"         # start trigger could not be fired (TPC unreachable, etc.)


class AuditAction(str, enum.Enum):
    USER_CREATED = "user.created"
    USER_DISABLED = "user.disabled"
    USER_LOGIN = "user.login"
    SCENE_CREATED = "scene.created"
    SCENE_UPDATED = "scene.updated"
    SCENE_DISABLED = "scene.disabled"
    DAILY_TIMELINE_UPSERTED = "daily_timeline.upserted"
    OVERRIDE_CREATED = "override.created"
    OVERRIDE_UPDATED = "override.updated"
    OVERRIDE_CANCELLED = "override.cancelled"
    OVERRIDE_FIRED = "override.fired"
    OVERRIDE_RELEASED = "override.released"
    OVERRIDE_FAILED = "override.failed"
    CONTROL_SCENE_FIRED = "control.scene_fired"
    CONTROL_RELEASE_FIRED = "control.release_fired"
    CONTROL_COLOR_SET = "control.color_set"
    CONTROL_COLOR_CLEARED = "control.color_cleared"


# ---------------------------------------------------------------------------


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[Role] = mapped_column(Enum(Role), nullable=False, default=Role.OPERATOR)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    display_name: Mapped[Optional[str]] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    overrides: Mapped[list["Override"]] = relationship(back_populates="creator")


class Scene(Base):
    """Curated palette entry. Maps friendly name -> TPC trigger numbers.

    `trigger_num` fires the scene at override start.
    `release_trigger_num` fires at override end (typically the project's
    "Release all" trigger so the daily schedule resumes naturally).
    """

    __tablename__ = "scenes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    swatch: Mapped[str] = mapped_column(String(7), nullable=False)  # "#RRGGBB"
    trigger_num: Mapped[int] = mapped_column(Integer, nullable=False)
    release_trigger_num: Mapped[int] = mapped_column(Integer, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class DailyTimeline(Base):
    """Admin-managed map of TPC timeline OR scene num -> display name + swatch.

    Used by the dashboard to render the current colour when the daily schedule
    is running (no override active). Populated once per project version.
    """

    __tablename__ = "daily_timelines"
    __table_args__ = (UniqueConstraint("source", "num", name="uq_daily_source_num"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    source: Mapped[str] = mapped_column(String(16), nullable=False)  # "timeline" or "scene"
    num: Mapped[int] = mapped_column(Integer, nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    swatch: Mapped[str] = mapped_column(String(7), nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(Text)


class Override(Base):
    """A time-bounded lighting takeover.

    Exactly ONE of these is set on a given override:
      - color_hex      -> static RGB on group 0 (any solid colour)
      - effect_name    -> kick off the named effect via EffectEngine, stop at end
      - scene_id       -> LEGACY: fires the scene's start trigger at start_at,
                          release trigger at end_at. Kept for back-compat; new
                          overrides should use color_hex or effect_name.
    """

    __tablename__ = "overrides"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    scene_id: Mapped[Optional[str]] = mapped_column(ForeignKey("scenes.id"), nullable=True)
    color_hex: Mapped[Optional[str]] = mapped_column(String(7), nullable=True)
    effect_name: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    effect_params_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[OverrideStatus] = mapped_column(
        Enum(OverrideStatus), nullable=False, default=OverrideStatus.SCHEDULED, index=True
    )
    created_by_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    fire_error: Mapped[Optional[str]] = mapped_column(Text)
    notes: Mapped[Optional[str]] = mapped_column(Text)

    scene: Mapped[Optional["Scene"]] = relationship(lazy="joined")
    creator: Mapped["User"] = relationship(back_populates="overrides", lazy="joined")

    @property
    def is_active_at(self) -> bool:
        now = _utcnow()
        return (
            self.status in {OverrideStatus.SCHEDULED, OverrideStatus.ACTIVE}
            and self.start_at <= now < self.end_at
        )

    @property
    def effect_params(self) -> dict:
        """Decoded effect_params_json. Empty dict if unset/malformed."""
        import json
        if not self.effect_params_json:
            return {}
        try:
            return json.loads(self.effect_params_json)
        except Exception:
            return {}

    @property
    def swatch(self) -> str:
        """Representative hex colour for the row indicator / live panel."""
        if self.effect_name:
            from app.effects import describe_effect
            sw, _ = describe_effect(self.effect_name, self.effect_params)
            return sw
        if self.color_hex:
            return self.color_hex
        if self.scene is not None:
            return self.scene.swatch
        return "#6b7280"

    @property
    def display_scene_label(self) -> str:
        if self.effect_name:
            from app.effects import describe_effect
            _, lbl = describe_effect(self.effect_name, self.effect_params)
            return lbl
        if self.scene is not None:
            return self.scene.display_name
        if self.color_hex:
            return f"Custom colour {self.color_hex.upper()}"
        return "(unset)"

    @property
    def is_custom_colour(self) -> bool:
        return self.color_hex is not None and self.scene_id is None and self.effect_name is None

    @property
    def is_effect(self) -> bool:
        return self.effect_name is not None


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    actor_id: Mapped[Optional[str]] = mapped_column(ForeignKey("users.id"))
    actor_username: Mapped[Optional[str]] = mapped_column(String(64))  # snapshot, survives deletion
    action: Mapped[AuditAction] = mapped_column(Enum(AuditAction), nullable=False, index=True)
    target_type: Mapped[Optional[str]] = mapped_column(String(32))
    target_id: Mapped[Optional[str]] = mapped_column(String(36))
    detail: Mapped[Optional[str]] = mapped_column(Text)
