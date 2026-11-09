from __future__ import annotations

from alembic import op
import sqlalchemy as sa

"""login_email_protection_events 增加 attempt_count。

先用 inspector 查列是否存在再决定加不加，而不是直接 add_column ——
这个迁移在早期部署上可能被手工执行过一部分，重复执行应当安全。
"""

revision = "0006_login_email_retry_count"
down_revision = "0005_login_email_protection"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("login_email_protection_events")
    }
    if "attempt_count" not in columns:
        op.add_column(
            "login_email_protection_events",
            sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        )


def downgrade() -> None:
    op.drop_column("login_email_protection_events", "attempt_count")
