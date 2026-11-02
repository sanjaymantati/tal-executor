from pathlib import Path

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

SessionFactory = async_sessionmaker[AsyncSession]

SQLITE_BUSY_TIMEOUT_SEC = 30


def create_engine(database_url: str) -> AsyncEngine:
    ensure_sqlite_directory(database_url)
    engine = create_async_engine(database_url, connect_args=build_connect_args(database_url))
    if is_sqlite(database_url):
        event.listen(engine.sync_engine, "connect", enable_write_ahead_log)
    return engine


def enable_write_ahead_log(dbapi_connection, connection_record) -> None:
    """Lets the dashboard read while the pipeline writes."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()


def is_sqlite(database_url: str) -> bool:
    return make_url(database_url).drivername.startswith("sqlite")


def create_session_factory(engine: AsyncEngine) -> SessionFactory:
    return async_sessionmaker(engine, expire_on_commit=False)


def ensure_sqlite_directory(database_url: str) -> None:
    url = make_url(database_url)
    if not url.drivername.startswith("sqlite"):
        return
    if url.database in (None, "", ":memory:"):
        return
    Path(url.database).parent.mkdir(parents=True, exist_ok=True)


def build_connect_args(database_url: str) -> dict:
    if make_url(database_url).drivername.startswith("sqlite"):
        return {"timeout": SQLITE_BUSY_TIMEOUT_SEC}
    return {}
