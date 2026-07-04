"""Engine/session factory and the unit-of-work context manager.

Transaction discipline (architecture risk #1): `uow()` is the ONLY sanctioned
way to open a write transaction, and orchestration owns its call sites. Never
hold a uow open across network I/O — adapters run outside transactions.
"""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker


def make_engine(database_url: str) -> Engine:
    return create_engine(database_url, pool_pre_ping=True, future=True)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def uow(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """One atomic unit of work: commit on success, rollback on any error."""
    session = session_factory()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()
