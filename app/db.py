"""SQLAlchemy engine + session factory.

The same SQLite file backs both the application tables and the APScheduler
jobstore. Single file = single backup target.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker

from app.config import get_settings

Base = declarative_base()


def _build_engine() -> Engine:
    settings = get_settings()
    engine = create_engine(
        settings.sqlalchemy_url,
        connect_args={"check_same_thread": False},
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _enable_sqlite_pragmas(dbapi_conn, _connection_record):  # pragma: no cover
        # WAL = better concurrency between APScheduler worker threads and request handlers.
        # foreign_keys=ON = SQLite enforces FK constraints (off by default, very surprising).
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()

    return engine


engine: Engine = _build_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db() -> Iterator[Session]:
    """FastAPI dependency: yields a session, closes after the request."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Context-manager session for code outside the request cycle (scheduler jobs, CLI)."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_schema() -> None:
    """Create all tables + apply additive column migrations. Safe to call repeatedly."""
    # Import models so they register with Base.metadata
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)

    # Additive column migrations. SQLite `ALTER TABLE ADD COLUMN` is idempotent
    # only via try/except — there's no IF NOT EXISTS for ADD COLUMN.
    _additive_migrations = [
        "ALTER TABLE overrides ADD COLUMN effect_name VARCHAR(32)",
        "ALTER TABLE overrides ADD COLUMN effect_params_json TEXT",
    ]
    with engine.begin() as conn:
        for stmt in _additive_migrations:
            try:
                conn.exec_driver_sql(stmt)
            except Exception:
                pass  # column already exists
