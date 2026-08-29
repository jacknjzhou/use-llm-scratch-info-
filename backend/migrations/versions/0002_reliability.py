"""可靠性增量：heartbeat 心跳列、extract_result 唯一约束（去重）、常用查询索引。

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-29

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1) 任务心跳：用于识别僵死任务
    op.add_column("extract_task",
                  sa.Column("heartbeat", sa.DateTime(timezone=True)))
    # 2) 结果去重：同一 (task_id, file_id) 只保留最新一条，然后加唯一约束防止重跑出现重复行
    op.execute("""
        DELETE FROM extract_result a
        USING extract_result b
        WHERE a.task_id = b.task_id AND a.file_id = b.file_id
          AND (a.created_at, a.id) < (b.created_at, b.id)
    """)
    op.create_unique_constraint("uq_result_task_file", "extract_result",
                                ["task_id", "file_id"])
    # 3) 任务列表/恢复扫描常用索引
    op.create_index("ix_extract_task_status", "extract_task", ["status"])
    op.create_index("ix_extract_task_created_at", "extract_task", ["created_at"])
    op.create_index("ix_result_correction_schema_id",
                    "result_correction", ["schema_id"])


def downgrade() -> None:
    op.drop_index("ix_result_correction_schema_id", table_name="result_correction")
    op.drop_index("ix_extract_task_created_at", table_name="extract_task")
    op.drop_index("ix_extract_task_status", table_name="extract_task")
    op.drop_constraint("uq_result_task_file", "extract_result", type_="unique")
    op.drop_column("extract_task", "heartbeat")
