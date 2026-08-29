"""基线 schema：与 create_all 生成的原始表结构一致（不含后续增量）。

Revision ID: 0001
Revises:
Create Date: 2026-08-29

"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "extract_schema",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("category", sa.String(64)),
        sa.Column("description", sa.Text()),
        sa.Column("fields", JSONB(), nullable=False),
        sa.Column("version", sa.Integer()),
        sa.Column("is_preset", sa.Boolean()),
        sa.Column("is_deleted", sa.Boolean()),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "upload_file",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("filename", sa.String(512), nullable=False),
        sa.Column("storage_path", sa.String(1024), nullable=False),
        sa.Column("mime_type", sa.String(128)),
        sa.Column("size_bytes", sa.BigInteger()),
        sa.Column("status", sa.String(32)),
        sa.Column("created_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "llm_config",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("provider", sa.String(64)),
        sa.Column("base_url", sa.String(512), nullable=False),
        sa.Column("api_key_enc", sa.Text(), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("params", JSONB()),
        sa.Column("is_default", sa.Boolean()),
        sa.Column("enabled", sa.Boolean()),
        sa.Column("created_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "extract_task",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("match_mode", sa.String(16)),
        sa.Column("priority", sa.SmallInteger()),
        sa.Column("schema_id", UUID(as_uuid=True),
                  sa.ForeignKey("extract_schema.id")),
        sa.Column("llm_config_id", UUID(as_uuid=True),
                  sa.ForeignKey("llm_config.id")),
        sa.Column("selected_fields", JSONB()),
        sa.Column("schema_snapshot", JSONB()),
        sa.Column("status", sa.String(32)),
        sa.Column("progress", sa.SmallInteger()),
        sa.Column("total_chunks", sa.Integer()),
        sa.Column("done_chunks", sa.Integer()),
        sa.Column("error", sa.Text()),
        sa.Column("celery_task_id", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "task_file",
        sa.Column("task_id", UUID(as_uuid=True),
                  sa.ForeignKey("extract_task.id"), primary_key=True),
        sa.Column("file_id", UUID(as_uuid=True),
                  sa.ForeignKey("upload_file.id"), primary_key=True),
        sa.Column("matched_schema_id", UUID(as_uuid=True),
                  sa.ForeignKey("extract_schema.id")),
        sa.Column("match_confidence", sa.Numeric(4, 3)),
        sa.Column("match_reason", sa.Text()),
        sa.Column("match_status", sa.String(16)),
        sa.Column("status", sa.String(32)),
        sa.Column("error", sa.Text()),
    )
    op.create_table(
        "extract_result",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("task_id", UUID(as_uuid=True),
                  sa.ForeignKey("extract_task.id"), nullable=False),
        sa.Column("file_id", UUID(as_uuid=True),
                  sa.ForeignKey("upload_file.id"), nullable=False),
        sa.Column("results", JSONB(), nullable=False),
        sa.Column("coverage", sa.Numeric(5, 2)),
        sa.Column("created_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_extract_result_task_id", "extract_result", ["task_id"])
    op.create_table(
        "result_correction",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("task_id", UUID(as_uuid=True),
                  sa.ForeignKey("extract_task.id")),
        sa.Column("file_id", UUID(as_uuid=True),
                  sa.ForeignKey("upload_file.id")),
        sa.Column("schema_id", UUID(as_uuid=True),
                  sa.ForeignKey("extract_schema.id")),
        sa.Column("field_key", sa.String(128), nullable=False),
        sa.Column("field_label", sa.String(128)),
        sa.Column("original_value", JSONB()),
        sa.Column("corrected_value", JSONB(), nullable=False),
        sa.Column("snippet", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True)),
    )


def downgrade() -> None:
    op.drop_table("result_correction")
    op.drop_index("ix_extract_result_task_id", table_name="extract_result")
    op.drop_table("extract_result")
    op.drop_table("task_file")
    op.drop_table("extract_task")
    op.drop_table("llm_config")
    op.drop_table("upload_file")
    op.drop_table("extract_schema")
