"""APScheduler integration for firing override start/end triggers.

Architecture:

  Override (DB row)  ----->  two APScheduler jobs
                              start job:  fire scene.trigger_num at start_at
                              end   job:  fire scene.release_trigger_num at end_at

The scheduler persists its job state in the same SQLite file as the app via
SQLAlchemyJobStore, so process restarts don't lose pending jobs.

Boot recovery: on startup, we scan the DB for overrides whose window contains
the current moment and (a) re-fire their start trigger so the lights match
intent and (b) ensure their end job is scheduled. Without this, a Linux box
reboot mid-override leaves the lights stranded.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger
from sqlalchemy import select

from app.config import get_settings
from app.db import session_scope
from app.effects import EffectEngine
from app.models import AuditAction, AuditLog, Override, OverrideStatus
from app.tpc import TPCClient, TPCError

logger = logging.getLogger(__name__)


# Module-level handles set during init() so APScheduler jobs (which are loaded
# by class path) can reach them without serialising into job kwargs.
_tpc_client: TPCClient | None = None
_effect_engine: EffectEngine | None = None
_scheduler: BackgroundScheduler | None = None


def _audit(action: AuditAction, *, target_id: str, detail: str) -> None:
    with session_scope() as s:
        s.add(AuditLog(
            actor_username="scheduler",
            action=action,
            target_type="override",
            target_id=target_id,
            detail=detail,
        ))


# ---------------------------------------------------------------------------
# Job functions (must be importable by class path)
# ---------------------------------------------------------------------------


def run_start_job(override_id: str) -> None:
    """Fire the scene's start trigger and mark the override ACTIVE.

    Called by APScheduler at override.start_at. Robust to transient TPC errors:
    on failure, the override is marked FAILED and the end job will still run
    (it fires a release, which is idempotent and harmless).
    """
    logger.info("run_start_job(%s)", override_id)
    if _tpc_client is None:
        logger.error("TPC client not initialised; aborting start job for %s", override_id)
        return

    with session_scope() as s:
        ov = s.get(Override, override_id)
        if ov is None:
            logger.warning("Override %s not found (cancelled?); nothing to do", override_id)
            return
        if ov.status == OverrideStatus.CANCELLED:
            logger.info("Override %s was cancelled; skipping start", override_id)
            return

        if ov.is_effect:
            # Effect path — kick off the engine. The engine itself handles
            # per-tick pushes; we just record start/end.
            try:
                if _effect_engine is None:
                    raise TPCError("effect engine not initialised")
                _effect_engine.start(ov.effect_name, ov.effect_params)
            except (TPCError, ValueError) as e:
                logger.exception("Failed to start effect %s for %s", ov.effect_name, override_id)
                ov.status = OverrideStatus.FAILED
                ov.fire_error = str(e)
                s.add(AuditLog(
                    actor_username="scheduler",
                    action=AuditAction.OVERRIDE_FAILED,
                    target_type="override", target_id=override_id,
                    detail=f"effect {ov.effect_name} failed to start: {e}",
                ))
                return
            ov.status = OverrideStatus.ACTIVE
            s.add(AuditLog(
                actor_username="scheduler",
                action=AuditAction.OVERRIDE_FIRED,
                target_type="override", target_id=override_id,
                detail=f"started effect {ov.effect_name} params={ov.effect_params_json or '{}'}",
            ))
            return

        if ov.is_custom_colour:
            # Direct-RGB override path
            ch = ov.color_hex.lstrip("#")
            r, g, b = int(ch[0:2], 16), int(ch[2:4], 16), int(ch[4:6], 16)
            try:
                _tpc_client.set_override_color(r, g, b, target="group", num=0, fade_seconds=1.0)
            except TPCError as e:
                logger.exception("Failed to set override colour for %s", override_id)
                ov.status = OverrideStatus.FAILED
                ov.fire_error = str(e)
                s.add(AuditLog(
                    actor_username="scheduler",
                    action=AuditAction.OVERRIDE_FAILED,
                    target_type="override", target_id=override_id,
                    detail=f"set_override_color {ov.color_hex} failed: {e}",
                ))
                return
            ov.status = OverrideStatus.ACTIVE
            s.add(AuditLog(
                actor_username="scheduler",
                action=AuditAction.OVERRIDE_FIRED,
                target_type="override", target_id=override_id,
                detail=f"set custom colour {ov.color_hex}",
            ))
            return

        # Scene-based path (legacy)
        trig = ov.scene.trigger_num
        try:
            _tpc_client.fire_trigger(trig)
        except TPCError as e:
            logger.exception("Failed to fire start trigger %d for override %s", trig, override_id)
            ov.status = OverrideStatus.FAILED
            ov.fire_error = str(e)
            s.add(AuditLog(
                actor_username="scheduler",
                action=AuditAction.OVERRIDE_FAILED,
                target_type="override",
                target_id=override_id,
                detail=f"start trigger {trig} failed: {e}",
            ))
            return

        ov.status = OverrideStatus.ACTIVE
        s.add(AuditLog(
            actor_username="scheduler",
            action=AuditAction.OVERRIDE_FIRED,
            target_type="override",
            target_id=override_id,
            detail=f"fired start trigger {trig} ({ov.scene.display_name!r})",
        ))


def run_end_job(override_id: str) -> None:
    """Fire the scene's release trigger and mark the override COMPLETED.

    Called by APScheduler at override.end_at. Always tries to fire the release
    trigger, even if the start failed; the release is idempotent and ensures
    the daily schedule resumes cleanly.
    """
    logger.info("run_end_job(%s)", override_id)
    if _tpc_client is None:
        logger.error("TPC client not initialised; aborting end job for %s", override_id)
        return

    with session_scope() as s:
        ov = s.get(Override, override_id)
        if ov is None:
            logger.warning("Override %s not found at end time; nothing to do", override_id)
            return
        if ov.status == OverrideStatus.CANCELLED:
            logger.info("Override %s was cancelled; skipping release", override_id)
            return

        if ov.is_effect:
            try:
                if _effect_engine is not None:
                    _effect_engine.stop()  # also clears the per-fixture overrides
                ov.status = OverrideStatus.COMPLETED
                ov.completed_at = datetime.now(timezone.utc)
                s.add(AuditLog(
                    actor_username="scheduler",
                    action=AuditAction.OVERRIDE_RELEASED,
                    target_type="override", target_id=override_id,
                    detail=f"stopped effect {ov.effect_name}",
                ))
            except Exception as e:  # noqa: BLE001
                logger.exception("Failed to stop effect for %s", override_id)
                ov.fire_error = (ov.fire_error or "") + f" | effect stop failed: {e}"
            return

        if ov.is_custom_colour:
            try:
                _tpc_client.clear_overrides(fade_seconds=1.0)
                ov.status = OverrideStatus.COMPLETED
                ov.completed_at = datetime.now(timezone.utc)
                s.add(AuditLog(
                    actor_username="scheduler",
                    action=AuditAction.OVERRIDE_RELEASED,
                    target_type="override", target_id=override_id,
                    detail="cleared custom colour override",
                ))
            except TPCError as e:
                logger.exception("Failed to clear override for %s", override_id)
                ov.fire_error = (ov.fire_error or "") + f" | clear failed: {e}"
                s.add(AuditLog(
                    actor_username="scheduler",
                    action=AuditAction.OVERRIDE_FAILED,
                    target_type="override", target_id=override_id,
                    detail=f"clear_overrides failed: {e}",
                ))
            return

        trig = ov.scene.release_trigger_num
        try:
            _tpc_client.fire_trigger(trig)
            ov.status = OverrideStatus.COMPLETED
            ov.completed_at = datetime.now(timezone.utc)
            s.add(AuditLog(
                actor_username="scheduler",
                action=AuditAction.OVERRIDE_RELEASED,
                target_type="override",
                target_id=override_id,
                detail=f"fired release trigger {trig}",
            ))
        except TPCError as e:
            logger.exception("Failed to fire release trigger %d for override %s", trig, override_id)
            ov.fire_error = (ov.fire_error or "") + f" | release failed: {e}"
            s.add(AuditLog(
                actor_username="scheduler",
                action=AuditAction.OVERRIDE_FAILED,
                target_type="override",
                target_id=override_id,
                detail=f"release trigger {trig} failed: {e}",
            ))


# ---------------------------------------------------------------------------
# Lifecycle + scheduling API
# ---------------------------------------------------------------------------


def init(tpc: TPCClient, effect_engine: EffectEngine) -> BackgroundScheduler:
    """Create the scheduler, attach the SQLite jobstore, run boot recovery."""
    global _tpc_client, _effect_engine, _scheduler
    _tpc_client = tpc
    _effect_engine = effect_engine

    settings = get_settings()
    jobstore = SQLAlchemyJobStore(url=settings.sqlalchemy_url, tablename="apscheduler_jobs")
    scheduler = BackgroundScheduler(
        jobstores={"default": jobstore},
        timezone=settings.app_timezone,
    )
    scheduler.start()
    _scheduler = scheduler
    logger.info("Scheduler started (timezone=%s)", settings.app_timezone)

    _recover_active_overrides()
    return scheduler


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        logger.info("Stopping scheduler")
        _scheduler.shutdown(wait=False)
        _scheduler = None


def _start_job_id(override_id: str) -> str:
    return f"override:{override_id}:start"


def _end_job_id(override_id: str) -> str:
    return f"override:{override_id}:end"


def schedule_override(override: Override) -> None:
    """Add both start and end jobs for a newly-created override."""
    if _scheduler is None:
        raise RuntimeError("scheduler not initialised")

    _scheduler.add_job(
        run_start_job,
        trigger=DateTrigger(run_date=override.start_at),
        args=[override.id],
        id=_start_job_id(override.id),
        replace_existing=True,
        misfire_grace_time=30,
    )
    _scheduler.add_job(
        run_end_job,
        trigger=DateTrigger(run_date=override.end_at),
        args=[override.id],
        id=_end_job_id(override.id),
        replace_existing=True,
        misfire_grace_time=30,
    )
    logger.info(
        "Scheduled override %s: start=%s end=%s",
        override.id, override.start_at, override.end_at,
    )


def unschedule_override(override_id: str) -> None:
    """Remove both jobs for a cancelled override. Safe if jobs don't exist."""
    if _scheduler is None:
        return
    for jid in (_start_job_id(override_id), _end_job_id(override_id)):
        try:
            _scheduler.remove_job(jid)
        except Exception:  # noqa: BLE001 - apscheduler raises JobLookupError on missing
            pass
    logger.info("Unscheduled override %s", override_id)


def _recover_active_overrides() -> None:
    """On boot: re-fire start triggers for overrides whose window contains now,
    and ensure end jobs are scheduled for all pending/active overrides."""
    if _scheduler is None or _tpc_client is None:
        return

    now = datetime.now(timezone.utc)

    with session_scope() as s:
        rows = s.execute(
            select(Override).where(
                Override.status.in_([OverrideStatus.SCHEDULED, OverrideStatus.ACTIVE])
            )
        ).scalars().all()

        for ov in rows:
            override_id = ov.id
            start_at = ov.start_at
            end_at = ov.end_at

            def _recover_release() -> None:
                """Stop effect / clear direct override / fire release trigger."""
                if ov.is_effect:
                    if _effect_engine is not None:
                        _effect_engine.stop()
                elif ov.is_custom_colour:
                    _tpc_client.clear_overrides(fade_seconds=1.0)
                elif ov.scene is not None:
                    _tpc_client.fire_trigger(ov.scene.release_trigger_num)

            def _recover_start() -> None:
                if ov.is_effect:
                    if _effect_engine is None:
                        raise TPCError("effect engine not initialised on boot recovery")
                    _effect_engine.start(ov.effect_name, ov.effect_params)
                elif ov.is_custom_colour:
                    ch = ov.color_hex.lstrip("#")
                    r, g, b = int(ch[0:2], 16), int(ch[2:4], 16), int(ch[4:6], 16)
                    _tpc_client.set_override_color(r, g, b, target="group", num=0, fade_seconds=1.0)
                elif ov.scene is not None:
                    _tpc_client.fire_trigger(ov.scene.trigger_num)

            if end_at <= now:
                logger.warning(
                    "Override %s window already passed on boot (end=%s); firing release",
                    override_id, end_at,
                )
                try:
                    _recover_release()
                except TPCError as e:
                    logger.error("Recovery release failed: %s", e)
                ov.status = OverrideStatus.COMPLETED
                ov.completed_at = now
                s.add(AuditLog(
                    actor_username="scheduler",
                    action=AuditAction.OVERRIDE_RELEASED,
                    target_type="override",
                    target_id=override_id,
                    detail=f"recovered after restart; window had already ended at {end_at}",
                ))
                continue

            if start_at <= now < end_at:
                logger.info("Override %s in-window on boot; re-firing start", override_id)
                try:
                    _recover_start()
                    ov.status = OverrideStatus.ACTIVE
                    s.add(AuditLog(
                        actor_username="scheduler",
                        action=AuditAction.OVERRIDE_FIRED,
                        target_type="override",
                        target_id=override_id,
                        detail="recovered after restart; re-fired start",
                    ))
                except TPCError as e:
                    logger.error("Recovery start failed: %s", e)
                    ov.status = OverrideStatus.FAILED
                    ov.fire_error = str(e)

                _scheduler.add_job(
                    run_end_job,
                    trigger=DateTrigger(run_date=end_at),
                    args=[override_id],
                    id=_end_job_id(override_id),
                    replace_existing=True,
                    misfire_grace_time=30,
                )
                continue

            # Override is in the future. Re-schedule both jobs.
            _scheduler.add_job(
                run_start_job,
                trigger=DateTrigger(run_date=start_at),
                args=[override_id],
                id=_start_job_id(override_id),
                replace_existing=True,
                misfire_grace_time=30,
            )
            _scheduler.add_job(
                run_end_job,
                trigger=DateTrigger(run_date=end_at),
                args=[override_id],
                id=_end_job_id(override_id),
                replace_existing=True,
                misfire_grace_time=30,
            )

        logger.info("Boot recovery scanned %d active/scheduled overrides", len(rows))
