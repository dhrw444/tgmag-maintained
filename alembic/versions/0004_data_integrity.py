from __future__ import annotations

from alembic import op
import sqlalchemy as sa

"""补三条数据完整性约束。

- tg_accounts.user_id 部分唯一索引：user_id 为 NULL 的行不受约束，因为
  未同步资料的账号允许并存；一旦同步出 user_id 就不允许重复。
- tg_sessions 每个账号只能有一条 is_active：靠部分唯一索引在数据库层兜住，
  应用层「先置旧行为 False」如果被绕过也不会出现双活 session。
- rate_limits 的三个 CHECK：max_actions / per_seconds 必须为正，
  jitter_min <= jitter_max。纯防御，正常代码路径不会违反。
"""

revision = "0004_data_integrity"
down_revision = "0003_service_source_unique"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "uq_tg_accounts_user_id_not_null",
        "tg_accounts",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("user_id IS NOT NULL"),
    )
    op.create_index(
        "uq_tg_sessions_one_active_per_account",
        "tg_sessions",
        ["account_id"],
        unique=True,
        postgresql_where=sa.text("is_active IS TRUE"),
    )
    op.create_check_constraint("ck_rate_max_actions_positive", "rate_limits", "max_actions > 0")
    op.create_check_constraint("ck_rate_per_seconds_positive", "rate_limits", "per_seconds > 0")
    op.create_check_constraint(
        "ck_rate_jitter_valid",
        "rate_limits",
        "jitter_min >= 0 AND jitter_max >= jitter_min",
    )


def downgrade() -> None:
    op.drop_constraint("ck_rate_jitter_valid", "rate_limits", type_="check")
    op.drop_constraint("ck_rate_per_seconds_positive", "rate_limits", type_="check")
    op.drop_constraint("ck_rate_max_actions_positive", "rate_limits", type_="check")
    op.drop_index("uq_tg_sessions_one_active_per_account", table_name="tg_sessions")
    op.drop_index("uq_tg_accounts_user_id_not_null", table_name="tg_accounts")
