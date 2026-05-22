"""Business logic that sits between routes and the DB / scheduler / TPC.

Keeping route handlers thin and pushing logic here makes the tricky bits
(conflict detection, cancel-while-active) testable without standing up the
whole app.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app import scheduler as scheduler_mod
from app.models import (
    AuditAction,
    AuditLog,
    DailyTimeline,
    Override,
    OverrideStatus,
    Scene,
    User,
)
from app.tpc import SceneState, TPCClient, TPCError, TimelineState

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Override creation / cancellation
# ---------------------------------------------------------------------------


@dataclass
class OverrideValidationError(Exception):
    message: str

    def __str__(self) -> str:  # for FastAPI HTTPException detail
        return self.message


def _to_utc(dt: datetime) -> datetime:
    """Ensure a datetime is timezone-aware in UTC. Naive inputs are rejected."""
    if dt.tzinfo is None:
        raise OverrideValidationError("datetimes must include a timezone")
    return dt.astimezone(timezone.utc)


def find_conflicts(db: Session, start_at: datetime, end_at: datetime, exclude_id: str | None = None) -> list[Override]:
    """Return overrides whose window overlaps the given window."""
    start_at = _to_utc(start_at)
    end_at = _to_utc(end_at)
    stmt = (
        select(Override)
        .where(
            Override.status.in_([OverrideStatus.SCHEDULED, OverrideStatus.ACTIVE]),
            # Two windows overlap if neither ends before the other starts.
            and_(Override.start_at < end_at, Override.end_at > start_at),
        )
        .order_by(Override.start_at)
    )
    if exclude_id is not None:
        stmt = stmt.where(Override.id != exclude_id)
    return list(db.execute(stmt).scalars())


def create_override(
    db: Session,
    *,
    creator: User,
    name: str,
    start_at: datetime,
    end_at: datetime,
    scene_id: str | None = None,
    color_hex: str | None = None,
    notes: str | None = None,
) -> Override:
    """Create an override. Exactly one of scene_id or color_hex must be set."""
    start_at = _to_utc(start_at)
    end_at = _to_utc(end_at)
    now = datetime.now(timezone.utc)

    if not name.strip():
        raise OverrideValidationError("name is required")
    if end_at <= start_at:
        raise OverrideValidationError("end time must be after start time")
    if start_at <= now:
        raise OverrideValidationError("start time must be in the future")

    if bool(scene_id) == bool(color_hex):
        raise OverrideValidationError("pick a scene OR a custom colour, not both/neither")

    scene = None
    color_to_store = None
    detail_target = ""

    if scene_id:
        scene = db.get(Scene, scene_id)
        if scene is None or not scene.enabled:
            raise OverrideValidationError("scene not found or disabled")
        detail_target = f"scene={scene.key}"
    else:
        # Validate hex shape
        try:
            ch = (color_hex or "").strip().lower()
            if not (ch.startswith("#") and len(ch) == 7):
                raise ValueError
            int(ch[1:], 16)
            color_to_store = ch
        except ValueError as e:
            raise OverrideValidationError("colour must be a 7-char hex like #ff8800") from e
        detail_target = f"color={color_to_store}"

    override = Override(
        name=name.strip(),
        start_at=start_at,
        end_at=end_at,
        scene_id=(scene.id if scene is not None else None),
        color_hex=color_to_store,
        status=OverrideStatus.SCHEDULED,
        created_by_id=creator.id,
        notes=(notes or None),
    )
    db.add(override)
    db.flush()

    db.add(AuditLog(
        actor_id=creator.id,
        actor_username=creator.username,
        action=AuditAction.OVERRIDE_CREATED,
        target_type="override",
        target_id=override.id,
        detail=f"{name!r} {start_at.isoformat()} -> {end_at.isoformat()} {detail_target}",
    ))
    db.commit()
    db.refresh(override)

    scheduler_mod.schedule_override(override)
    return override


def cancel_override(
    db: Session,
    *,
    override: Override,
    actor: User,
    tpc: TPCClient,
) -> None:
    """Cancel an override. If currently active, fire its release trigger now."""
    if override.status in {OverrideStatus.COMPLETED, OverrideStatus.CANCELLED}:
        return

    was_active = override.status == OverrideStatus.ACTIVE
    override.status = OverrideStatus.CANCELLED
    override.cancelled_at = datetime.now(timezone.utc)

    db.add(AuditLog(
        actor_id=actor.id,
        actor_username=actor.username,
        action=AuditAction.OVERRIDE_CANCELLED,
        target_type="override",
        target_id=override.id,
        detail=f"cancelled by {actor.username}; was_active={was_active}",
    ))
    db.commit()

    scheduler_mod.unschedule_override(override.id)

    if was_active:
        try:
            if override.is_custom_colour:
                tpc.clear_overrides(fade_seconds=1.0)
            elif override.scene is not None:
                tpc.fire_trigger(override.scene.release_trigger_num)
        except TPCError as e:
            logger.exception("Release failed during cancel of %s: %s", override.id, e)


# ---------------------------------------------------------------------------
# Dashboard state
# ---------------------------------------------------------------------------


@dataclass
class DashboardState:
    tpc_reachable: bool
    active_override: Override | None
    timelines: list[TimelineState]
    scenes: list[SceneState]
    upcoming: list[Override]
    daily_timelines: dict[int, DailyTimeline]  # keyed by timeline num


def get_dashboard_state(db: Session, tpc: TPCClient) -> DashboardState:
    # What does the scheduler think is active right now?
    now = datetime.now(timezone.utc)
    active = db.execute(
        select(Override)
        .where(
            Override.status == OverrideStatus.ACTIVE,
            Override.start_at <= now,
            Override.end_at > now,
        )
        .order_by(Override.start_at.desc())
        .limit(1)
    ).scalar_one_or_none()

    # What does the TPC say? Treat scenes + timelines as a single reachability
    # gate — if one call fails the controller is effectively unreachable.
    try:
        timelines = tpc.list_timelines()
        scenes = tpc.list_scenes()
        reachable = True
    except TPCError:
        timelines = []
        scenes = []
        reachable = False

    upcoming = list(db.execute(
        select(Override)
        .where(
            Override.status.in_([OverrideStatus.SCHEDULED, OverrideStatus.ACTIVE]),
            Override.end_at > now,
        )
        .order_by(Override.start_at)
        .limit(20)
    ).scalars())

    daily_map = {
        d.num: d for d in db.execute(
            select(DailyTimeline).where(DailyTimeline.source == "timeline")
        ).scalars()
    }

    return DashboardState(
        tpc_reachable=reachable,
        active_override=active,
        timelines=timelines,
        scenes=scenes,
        upcoming=upcoming,
        daily_timelines=daily_map,
    )
