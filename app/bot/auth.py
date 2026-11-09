"""Bot 侧鉴权中间件。

两道判据，先看数据库再看配置：

1. admins 表里存在且 is_active 的 telegram_user_id 一律放行 —— 这是正常路径，
   管理员通过运维脚本或首次启动的 bootstrap 写入。
2. 表里没有记录时回落到 ADMIN_IDS 环境变量。这个回落是给首次部署用的，
   否则第一次启动时没人能进 Bot，也就没人能去配置管理员。

非 Message / CallbackQuery 的事件（内联查询等）没有 from_user，直接放行到
handler —— 那些 handler 自己不读账号数据，真正的鉴权在业务层还会再做一次。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject
from sqlalchemy import select

from app.config import settings
from app.db.models import Admin


class AdminOnlyMiddleware(BaseMiddleware):
    """把通过鉴权的 Admin 对象放进 data["admin"]，未通过则直接应答并终止。

    handler 里 data["admin"] 可能是 None —— 那是「靠 ADMIN_IDS 放行但库里
    没有对应行」的情况，写审计时按匿名管理员处理。
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Message):
            user = event.from_user
        elif isinstance(event, CallbackQuery):
            user = event.from_user
        else:
            user = None
        if user is None:
            return await handler(event, data)
        sessionmaker = data["sessionmaker"]
        async with sessionmaker() as session:
            admin = await session.scalar(
                select(Admin).where(
                    Admin.telegram_user_id == user.id,
                )
            )
            allowed = admin.is_active if admin is not None else user.id in settings.admin_ids
            if not allowed:
                if isinstance(event, CallbackQuery):
                    await event.answer("未授权。", show_alert=True)
                else:
                    await event.answer("未授权。")
                return None
            data["admin"] = admin if admin and admin.is_active else None
        return await handler(event, data)
