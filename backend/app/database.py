from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings

# 连接池大小可配：worker（prefork 多进程）建议 DB_POOL_SIZE=2，避免多 worker 扩容后打满 PG 连接
engine = create_async_engine(settings.database_url, pool_pre_ping=True,
                             pool_size=settings.db_pool_size,
                             max_overflow=settings.db_max_overflow)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_db() -> AsyncGenerator:
    async with SessionLocal() as session:
        yield session
