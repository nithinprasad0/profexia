from collections.abc import AsyncGenerator
from urllib.parse import urlsplit

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase  # noqa: F401  (re-exported for compatibility)

from app.core.config import settings


def engine_options(url: str) -> dict:
    """Engine keyword arguments that work for local Postgres and managed providers.

    - pool_pre_ping / pool_recycle: managed databases (Neon scales to zero)
      close idle connections; recycle them before the server does.
    - PgBouncer in transaction mode (Neon "-pooler" hosts, Supabase pooler,
      etc.) does not support asyncpg's prepared-statement cache, so disable it
      when such a host is detected.
    """
    options: dict = {"pool_pre_ping": True, "pool_recycle": 280}
    host = urlsplit(url).hostname or ""
    if "-pooler." in host or "pgbouncer" in host:
        options["connect_args"] = {
            "statement_cache_size": 0,
            "prepared_statement_cache_size": 0,
        }
    return options


engine = create_async_engine(settings.database_url, **engine_options(settings.database_url))
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session
