"""Database setup for MailAI trust, audit, and review state."""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


def _normalize_database_url(raw_url: str) -> str:
    if raw_url.startswith("postgres://"):
        return raw_url.replace("postgres://", "postgresql+psycopg://", 1)
    if raw_url.startswith("postgresql://") and "+psycopg" not in raw_url:
        return raw_url.replace("postgresql://", "postgresql+psycopg://", 1)
    return raw_url


def database_url() -> str:
    raw_url = (os.getenv("DATABASE_URL") or "").strip()
    if raw_url:
        return _normalize_database_url(raw_url)

    Path("data").mkdir(exist_ok=True)
    return "sqlite:///data/mailai_trust.db"


def _engine_kwargs(url: str) -> dict:
    kwargs = {"future": True, "pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    return kwargs


ENGINE = create_engine(database_url(), **_engine_kwargs(database_url()))
SessionLocal = sessionmaker(
    bind=ENGINE,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
    future=True,
)


def init_db() -> None:
    """Create missing trust-layer tables.

    This keeps local setup and Railway startup simple. Production deployments can
    later replace this with Alembic while keeping the same SQLAlchemy models.
    """
    from trust import models  # noqa: F401

    Base.metadata.create_all(bind=ENGINE)


@contextmanager
def session_scope() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

