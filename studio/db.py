"""Database engine and the app-wide settings holder."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import Settings


class Base(DeclarativeBase):
    pass


class _State:
    settings: Settings | None = None
    engine = None
    SessionLocal: sessionmaker | None = None


state = _State()


def utcnow() -> datetime:
    """Naive UTC timestamp; every datetime in the database is naive UTC."""
    return datetime.now(UTC).replace(tzinfo=None)


def init(settings: Settings) -> None:
    from . import models  # noqa: F401  (registers tables)

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.media_dir.mkdir(parents=True, exist_ok=True)
    engine = create_engine(settings.db_url, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()

    Base.metadata.create_all(engine)
    state.settings = settings
    state.engine = engine
    state.SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def settings() -> Settings:
    assert state.settings is not None, "studio.db.init() has not been called"
    return state.settings


@contextmanager
def session_scope() -> Iterator[Session]:
    db = state.SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    with session_scope() as db:
        yield db
