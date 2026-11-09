"""把 .env 里的管理员和默认限流写进数据库。

只在启动时跑一次，幂等：已存在的管理员和 batch 限流不会重复插入。
库里的值是运行期的真实来源 —— 管理员在 Bot 里改限流会写库，不会回写 .env。
所以这里只在"库里没有"时用配置值做初值，不覆盖已有记录。
"""

from __future__ import annotations

from sqlalchemy import select

from app.config import settings
from app.db.models import Admin, RateLimit
from app.db.session import sessionmaker


async def bootstrap_defaults() -> None:
    """确保 ADMIN_IDS 里的每个 ID 都有管理员记录，且 batch 限流存在。

    role 固定 owner：新增管理员请走 Bot 命令，那里会做权限校验；
    这里只处理部署者自己（写在 .env 里的人）。
    """
    async with sessionmaker() as session:
        for admin_id in settings.admin_ids:
            exists = await session.scalar(select(Admin).where(Admin.telegram_user_id == admin_id))
            if not exists:
                session.add(Admin(telegram_user_id=admin_id, role="owner", is_active=True))

        default_rate = await session.scalar(select(RateLimit).where(RateLimit.scope == "batch"))
        if not default_rate:
            session.add(
                RateLimit(
                    scope="batch",
                    max_actions=settings.default_rate_max_actions,
                    per_seconds=settings.default_rate_per_seconds,
                    jitter_min=settings.default_jitter_min,
                    jitter_max=settings.default_jitter_max,
                )
            )
        await session.commit()
