"""Mini App 鉴权。

Telegram 打开 Mini App 时通过 X-Telegram-Init-Data 头传一段查询串，里面带
user 字段和一个 hash。hash 是 HMAC-SHA256 签名，密钥由 bot_token 派生，
因此伪造不了 —— 这是 Mini App 唯一的身份来源，不接受任何别的凭据。

校验顺序刻意是「先格式、再时效、最后验签」：验签要遍历全部字段做排序拼接，
放在最前面等于允许未签名的请求驱动一次完整的 HMAC 计算。时效检查允许
60 秒的未来偏差（客户端时钟不准），超过 mini_app_auth_max_age_seconds 的
init_data 视为重放。

权限判据和 Bot 侧保持一致：先查 admins 表，表里没有才回落到 ADMIN_IDS。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl

from aiohttp import web
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.db.models import Admin


@dataclass(frozen=True)
class MiniAppUser:
    """init_data 里验签通过的用户身份。只有这几个字段是可信的，其余一律不信。"""

    id: int
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None


def validate_init_data(init_data: str) -> MiniAppUser:
    """校验 init_data 签名与时效，返回身份；任何一步不合法抛 HTTPUnauthorized。

    抛出的都是 web.HTTPUnauthorized，调用方不需要再包装 —— aiohttp 会直接
    转成 401 响应。
    """

    values = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = values.pop("hash", None)
    if not received_hash:
        raise web.HTTPUnauthorized(text="missing hash")

    auth_date_raw = values.get("auth_date")
    if not auth_date_raw:
        raise web.HTTPUnauthorized(text="missing auth_date")
    try:
        auth_date = int(auth_date_raw)
    except ValueError as exc:
        raise web.HTTPUnauthorized(text="invalid auth_date") from exc
    age = time.time() - auth_date
    if age < -60:
        raise web.HTTPUnauthorized(text="auth_date is in the future")
    if age > settings.mini_app_auth_max_age_seconds:
        raise web.HTTPUnauthorized(text="init data expired")

    data_check_string = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret_key = hmac.new(b"WebAppData", settings.bot_token.encode(), hashlib.sha256).digest()
    calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated_hash, received_hash):
        raise web.HTTPUnauthorized(text="invalid init data")

    user_raw = values.get("user")
    if not user_raw:
        raise web.HTTPUnauthorized(text="missing user")
    try:
        user_data = json.loads(user_raw)
        user_id = int(user_data["id"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise web.HTTPUnauthorized(text="invalid user") from exc
    return MiniAppUser(
        id=user_id,
        username=user_data.get("username"),
        first_name=user_data.get("first_name"),
        last_name=user_data.get("last_name"),
    )


async def require_admin(
    request: web.Request,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> MiniAppUser:
    """要求请求方是已激活的管理员，否则抛 HTTPForbidden。

    数据库不可用时这里抛 SQLAlchemy 异常而不是 403 —— 那是服务端故障，
    不该被伪装成权限拒绝。
    """
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    user = validate_init_data(init_data)
    async with sessionmaker() as session:
        admin = await session.scalar(
            select(Admin).where(
                Admin.telegram_user_id == user.id,
            )
        )
    allowed = admin.is_active if admin is not None else user.id in settings.admin_ids
    if not allowed:
        raise web.HTTPForbidden(text="not an admin")
    return user
