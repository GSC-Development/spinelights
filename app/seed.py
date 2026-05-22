"""Initial DB seed: create schema, seed an admin user, seed Scenes from
the trigger list observed on the live TPC.

Run from the project root:

    python -m app.seed --admin-username craig --admin-password CHANGE_ME

The Scene seed reflects the triggers observed on the GSC TPC project on
21 May 2026. Trigger numbers will change if the .pd2 is re-uploaded; an
admin can edit them in the UI.

Release trigger 4 is the project's "Release all timelines and scenes in 2s"
which restores the daily schedule cleanly. All scenes use it as their
release target.
"""
from __future__ import annotations

import argparse
import logging
import sys

from sqlalchemy import select

from app.db import init_schema, session_scope
from app.models import AuditAction, AuditLog, Role, Scene, User
from app.security import hash_password

logger = logging.getLogger(__name__)


# (key, display_name, swatch, trigger_num)
# Release trigger num is fixed at 4 (project's "Release all" Real Time trigger).
SEED_SCENES: list[tuple[str, str, str, int]] = [
    ("blue",            "Blue",                 "#1d4ed8", 14),   # trigger 14 "DAY time BLUE" - Start Blue
    ("white",           "White",                "#f5f5f5", 13),   # trigger 13 "CLEANING LIGHTS ON" - Start WHITE
    ("red",             "Red",                  "#dc2626",  5),   # trigger 5 (touch btn) - Toggle RED
    ("christmas",       "Christmas",            "#16a34a",  6),   # trigger 6 (touch btn) - Toggle Christmas
    ("yellow",          "Yellow",               "#facc15", 18),   # trigger 18 - Start Yellow
    ("purple",          "Purple",               "#7e22ce", 23),   # trigger 23 - Start purple
    ("pink_yellow",     "Pink and yellow",      "#ec4899", 21),   # trigger 21 - Start pink and yellow
    ("evening_blue",    "Evening blue",         "#1e3a8a", 17),   # trigger 17 - Start Blue (Evening)
]

DEFAULT_RELEASE_TRIGGER = 4  # "Release all timelines and scenes in 2s", 18:00 daily

# The shared "scheduler" user is used by APP_AUTO_LOGIN_AS for zero-friction
# access on the LAN. A long random password is set so it can't be used to log
# in by any other means.
SHARED_USERNAME = "scheduler"


def seed(admin_username: str, admin_password: str) -> None:
    import secrets

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    logger.info("Creating schema if missing")
    init_schema()

    with session_scope() as s:
        # Seed admin user
        existing = s.execute(select(User).where(User.username == admin_username)).scalar_one_or_none()
        if existing is None:
            admin = User(
                username=admin_username,
                password_hash=hash_password(admin_password),
                role=Role.ADMIN,
                display_name="Initial admin",
            )
            s.add(admin)
            s.flush()
            s.add(AuditLog(
                actor_username="system",
                action=AuditAction.USER_CREATED,
                target_type="user",
                target_id=admin.id,
                detail=f"Initial admin seeded: {admin_username}",
            ))
            logger.info("Created admin user %r", admin_username)
        else:
            logger.info("Admin user %r already exists, leaving alone", admin_username)

        # Seed shared "scheduler" user for auto-login
        shared = s.execute(select(User).where(User.username == SHARED_USERNAME)).scalar_one_or_none()
        if shared is None:
            shared_user = User(
                username=SHARED_USERNAME,
                password_hash=hash_password(secrets.token_urlsafe(32)[:60]),
                role=Role.ADMIN,
                display_name="Duty manager (shared)",
            )
            s.add(shared_user)
            s.flush()
            s.add(AuditLog(
                actor_username="system",
                action=AuditAction.USER_CREATED,
                target_type="user",
                target_id=shared_user.id,
                detail=f"Shared auto-login user seeded: {SHARED_USERNAME}",
            ))
            logger.info("Created shared user %r (random password; auto-login only)", SHARED_USERNAME)
        else:
            logger.info("Shared user %r already exists, leaving alone", SHARED_USERNAME)

        # Seed scenes
        for sort_order, (key, name, swatch, trig) in enumerate(SEED_SCENES):
            existing_scene = s.execute(select(Scene).where(Scene.key == key)).scalar_one_or_none()
            if existing_scene is not None:
                logger.info("Scene %r already exists, skipping", key)
                continue
            scene = Scene(
                key=key,
                display_name=name,
                swatch=swatch,
                trigger_num=trig,
                release_trigger_num=DEFAULT_RELEASE_TRIGGER,
                enabled=True,
                sort_order=sort_order,
            )
            s.add(scene)
            s.flush()
            s.add(AuditLog(
                actor_username="system",
                action=AuditAction.SCENE_CREATED,
                target_type="scene",
                target_id=scene.id,
                detail=f"Seeded scene {key} -> trigger {trig}",
            ))
            logger.info("Created scene %r (trigger %d)", key, trig)

    logger.info("Seed complete.")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--admin-username", required=True)
    p.add_argument("--admin-password", required=True)
    args = p.parse_args()
    seed(args.admin_username, args.admin_password)
    return 0


if __name__ == "__main__":
    sys.exit(main())
