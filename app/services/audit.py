"""审计日志写入。

只往 session 里 add，不 commit —— 审计记录必须和它描述的业务变更
落在同一个事务里。分两次提交会出现"操作成功但审计丢失"或反过来，
两种都会让事后追查失去意义。调用方 commit 业务变更时一起落库。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Admin, AuditLog


async def audit(
    session: AsyncSession,
    admin: Admin | None,
    action: str,
    entity_type: str | None = None,
    entity_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    """登记一条审计记录。

    admin 可以为 None：定时任务、Mini App 内部动作没有对应的管理员身份，
    这类记录 admin_id 为空，靠 action 和 entity 区分来源。
    payload 会被原样存成 JSON，调用方负责不要塞凭据进去。
    """
    session.add(
        AuditLog(
            admin_id=admin.id if admin else None,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            payload_json=payload,
        )
    )
