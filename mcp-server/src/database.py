"""
MCP Server 数据库模块
复用后端的数据库连接和模型
"""
import uuid
from typing import List, Optional
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base
from sqlalchemy import Column, String, Boolean, Text, Float, Integer, DateTime, ForeignKey, select
from sqlalchemy.dialects.postgresql import UUID, JSON
from sqlalchemy.sql import func

from .config import settings


# 创建异步引擎
engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10
)

# 会话工厂
async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False
)

# 基础模型
Base = declarative_base()


class ExtractSchema(Base):
    """提取模板"""
    __tablename__ = "extract_schema"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(255), nullable=False)
    category = Column(String(100))
    description = Column(Text)
    fields = Column(JSON, default=list)
    version = Column(Integer, default=1)
    is_preset = Column(Boolean, default=False)
    is_deleted = Column(Boolean, default=False)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


@asynccontextmanager
async def get_db_session():
    """获取数据库会话"""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_all_schemas(
    session: AsyncSession,
    category: Optional[str] = None,
    include_deleted: bool = False
) -> List[ExtractSchema]:
    """获取所有模板"""
    q = select(ExtractSchema)
    
    if not include_deleted:
        q = q.where(ExtractSchema.is_deleted == False)
    
    if category:
        q = q.where(ExtractSchema.category == category)
    
    q = q.order_by(ExtractSchema.created_at)
    
    result = await session.execute(q)
    return result.scalars().all()


async def get_schema_by_id(session: AsyncSession, schema_id: uuid.UUID) -> Optional[ExtractSchema]:
    """根据 ID 获取模板"""
    result = await session.execute(
        select(ExtractSchema).where(
            ExtractSchema.id == schema_id,
            ExtractSchema.is_deleted == False
        )
    )
    return result.scalar_one_or_none()


async def get_schema_by_name(session: AsyncSession, name: str) -> Optional[ExtractSchema]:
    """根据名称获取模板"""
    result = await session.execute(
        select(ExtractSchema).where(
            ExtractSchema.name == name,
            ExtractSchema.is_deleted == False
        )
    )
    return result.scalar_one_or_none()


async def get_categories(session: AsyncSession) -> List[str]:
    """获取所有类别"""
    result = await session.execute(
        select(ExtractSchema.category)
        .where(ExtractSchema.is_deleted == False)
        .where(ExtractSchema.category.isnot(None))
        .distinct()
    )
    return [r[0] for r in result.all() if r[0]]
