from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings

_is_test_database = "test" in settings.database_url.rsplit("/", 1)[-1].split("?", 1)[0]
engine = create_async_engine(
    settings.database_url,
    pool_pre_ping=not _is_test_database,
    poolclass=NullPool if _is_test_database else None,
)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with AsyncSessionLocal() as session:
        yield session
