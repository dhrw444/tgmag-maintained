from __future__ import annotations

from alembic import op
import sqlalchemy as sa

"""service_messages 增加 text 列。

原表只有 text_preview（截断的预览），完整正文丢失，识别换绑提醒时关键词
可能落在截断点之后而漏判。这里补 full text 列，并把历史预览回填进去 ——
历史数据拿不回完整正文，用预览顶上比留 NULL 更有用。
"""

revision = "0002_service_message_full_text"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("service_messages", sa.Column("text", sa.Text(), nullable=True))
    op.execute("UPDATE service_messages SET text = text_preview WHERE text IS NULL")


def downgrade() -> None:
    op.drop_column("service_messages", "text")
