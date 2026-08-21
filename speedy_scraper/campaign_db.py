from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from speedy_scraper.campaign_models import Base

_engines: dict[str, Engine] = {}


def database_url() -> str:
    value = os.environ.get("DATABASE_URL", "").strip()
    if value.startswith("postgres://"):
        value = "postgresql+psycopg://" + value.removeprefix("postgres://")
    elif value.startswith("postgresql://") and "+" not in value.split(":", 1)[0]:
        value = "postgresql+psycopg://" + value.removeprefix("postgresql://")
    if value:
        return value
    path = Path(os.environ.get("CAMPAIGN_SQLITE_PATH", "data/email_campaigns.db"))
    path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{path.resolve()}"


def get_engine(url: str | None = None) -> Engine:
    selected = url or database_url()
    if selected in _engines:
        return _engines[selected]
    kwargs: dict[str, object] = {"pool_pre_ping": True}
    if selected.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    engine = create_engine(selected, **kwargs)
    if selected.startswith("sqlite"):
        event.listen(engine, "connect", _enable_sqlite_foreign_keys)
    _engines[selected] = engine
    return engine


def _enable_sqlite_foreign_keys(connection, _record) -> None:
    cursor = connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def init_database(url: str | None = None) -> Engine:
    engine = get_engine(url)
    Base.metadata.create_all(engine)
    return engine


@contextmanager
def session_scope(url: str | None = None) -> Iterator[Session]:
    engine = init_database(url)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_engine_cache() -> None:
    for engine in _engines.values():
        engine.dispose()
    _engines.clear()
