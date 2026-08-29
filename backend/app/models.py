import uuid
from datetime import datetime, timezone

from sqlalchemy import (BigInteger, Boolean, DateTime, ForeignKey, Integer,
                        Numeric, SmallInteger, String, Text, UniqueConstraint)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.config import settings


class Base(DeclarativeBase):
    pass


def uid() -> uuid.UUID:
    return uuid.uuid4()


def now() -> datetime:
    return datetime.now(timezone.utc)


class ExtractSchema(Base):
    __tablename__ = "extract_schema"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    category: Mapped[str | None] = mapped_column(String(64))
    description: Mapped[str | None] = mapped_column(Text)
    fields: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    version: Mapped[int] = mapped_column(Integer, default=1)
    is_preset: Mapped[bool] = mapped_column(Boolean, default=False)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class UploadedFile(Base):
    __tablename__ = "upload_file"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uid)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    storage_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(128))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(32), default="uploaded")  # uploaded/parsed/failed
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ExtractTask(Base):
    __tablename__ = "extract_task"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uid)
    match_mode: Mapped[str] = mapped_column(String(16), default="assigned")  # assigned/auto
    priority: Mapped[int] = mapped_column(SmallInteger, default=5)  # 0~9，越大越优先
    schema_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("extract_schema.id"))
    llm_config_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("llm_config.id"))
    selected_fields: Mapped[list | None] = mapped_column(JSONB)
    schema_snapshot: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(32), default="pending")  # pending/running/succeeded/partially_succeeded/failed/cancelled
    progress: Mapped[int] = mapped_column(SmallInteger, default=0)
    total_chunks: Mapped[int] = mapped_column(Integer, default=0)
    done_chunks: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    celery_task_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 心跳：worker 每处理完一个文件更新一次，用于识别僵死任务并恢复
    heartbeat: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TaskFile(Base):
    __tablename__ = "task_file"

    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("extract_task.id"), primary_key=True)
    file_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("upload_file.id"), primary_key=True)
    matched_schema_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("extract_schema.id"))
    match_confidence: Mapped[float | None] = mapped_column(Numeric(4, 3))
    match_reason: Mapped[str | None] = mapped_column(Text)
    match_status: Mapped[str] = mapped_column(String(16), default="bound")  # bound/need_confirm/unmatched
    status: Mapped[str] = mapped_column(String(32), default="pending")  # pending/running/succeeded/failed
    error: Mapped[str | None] = mapped_column(Text)


class ExtractResult(Base):
    __tablename__ = "extract_result"
    __table_args__ = (UniqueConstraint("task_id", "file_id", name="uq_result_task_file"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uid)
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("extract_task.id"), index=True)
    file_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("upload_file.id"))
    results: Mapped[dict] = mapped_column(JSONB, nullable=False)  # {field: {value, evidence, chunk, page}}
    coverage: Mapped[float | None] = mapped_column(Numeric(5, 2))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class LlmConfig(Base):
    __tablename__ = "llm_config"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), default="openai-compatible")
    base_url: Mapped[str] = mapped_column(String(512), nullable=False)
    api_key_enc: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    params: Mapped[dict] = mapped_column(JSONB, default=dict)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ResultCorrection(Base):
    """人工修正记录：既用于溯源，也作为同模板后续提取的 few-shot 参考。"""
    __tablename__ = "result_correction"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uid)
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("extract_task.id"))
    file_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("upload_file.id"))
    schema_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("extract_schema.id"))
    field_key: Mapped[str] = mapped_column(String(128), nullable=False)
    field_label: Mapped[str | None] = mapped_column(String(128))
    original_value: Mapped[dict | None] = mapped_column(JSONB)
    corrected_value: Mapped[dict] = mapped_column(JSONB, nullable=False)
    snippet: Mapped[str | None] = mapped_column(Text)  # evidence 片段，作为 few-shot 输入
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
