from __future__ import annotations

from alembic import op

"""把 service_messages 的唯一键从 (account_id, message_id) 扩成
(account_id, source_user_id, message_id)。

message_id 只在同一个对话内唯一。补拉多个来源（777000 服务号、SpamBot 等）
之后，不同来源的同号消息会撞唯一键，原约束会误吞数据。
"""

revision = "0003_service_source_unique"
down_revision = "0002_service_message_full_text"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("uq_service_account_message", "service_messages", type_="unique")
    op.create_unique_constraint(
        "uq_service_account_source_message",
        "service_messages",
        ["account_id", "source_user_id", "message_id"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_service_account_source_message", "service_messages", type_="unique")
    op.create_unique_constraint("uq_service_account_message", "service_messages", ["account_id", "message_id"])
