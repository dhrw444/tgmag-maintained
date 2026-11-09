"""操作目标的白名单。

任何批量/单条操作（发消息、加群、拉人等）都必须先过 require_allowed_target。
白名单存的是"归一化后的目标引用"，所以用户填 @Name、t.me/name、t.me/+hash
还是数字 ID，都要先经过 canonicalize_target_ref 变成同一个规范形式再比对，
否则同一个人用不同写法就能绕过检查。

归一化的输出只有三种形态：
  - 纯数字 ID（去掉前导零和多余的负号）
  - @username（小写）
  - https://t.me/+hash（私群邀请链接）
认不出来的原样返回，此时白名单里必须字面匹配才通过。
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AllowedTarget

TELEGRAM_LINK_HOSTS = {"t.me", "www.t.me", "telegram.me", "www.telegram.me"}
# 邀请 hash 的字符集与长度。Telegram 的 hash 长度不固定，
# 这里按官方链接实际可能出现的最宽范围取，超出范围的当作非法输入丢弃。
INVITE_HASH_PATTERN = re.compile(r"[A-Za-z0-9_-]{5,128}")


def telegram_invite_hash(target_ref: str) -> str | None:
    """从私群邀请链接里取出 hash，取不到返回 None。

    支持四种写法：`+hash`、`t.me/+hash`、`t.me/joinchat/hash`、
    `tg://join?invite=hash`。都不匹配就返回 None —— 调用方据此判断
    "这不是私群邀请"，而不是抛异常，因为公开目标也会走这个函数。
    """
    value = target_ref.strip()
    candidate = ""
    if value.startswith("+"):
        candidate = value[1:]
    else:
        parsed = urlparse(value if "://" in value else "")
        if parsed.scheme in {"http", "https"} and parsed.hostname in TELEGRAM_LINK_HOSTS:
            path = parsed.path.strip("/")
            if path.startswith("+"):
                candidate = path[1:]
            elif path.lower().startswith("joinchat/"):
                candidate = path.split("/", 1)[1]
        elif parsed.scheme == "tg" and parsed.netloc.lower() == "join":
            candidate = parse_qs(parsed.query).get("invite", [""])[0]
    if candidate and INVITE_HASH_PATTERN.fullmatch(candidate):
        return candidate
    return None


def canonicalize_target_ref(target_ref: str) -> str:
    """把各种写法归一成白名单里存的规范形式。

    空串抛 ValueError。识别不出的写法原样返回（只做 strip），
    这样私有部署里用自定义标识也能工作。
    """
    value = target_ref.strip()
    if not value:
        raise ValueError("目标不能为空")
    # 数字 ID 允许带负号（群和频道的 ID 是负的），转成 int 再去掉
    # 前导零和 "+0" 之类的写法，保证 "-100123" 和 "-0100123" 归一一致。
    if value.lstrip("-").isdigit():
        return str(int(value))
    if value.startswith("@"):
        return "@" + value[1:].lower()
    invite_hash = telegram_invite_hash(value)
    if invite_hash:
        return f"https://t.me/+{invite_hash}"
    parsed = urlparse(value if "://" in value else "")
    if parsed.hostname in TELEGRAM_LINK_HOSTS:
        path = parsed.path.strip("/")
        # 只把"单段路径"当用户名；多段路径是帖子/评论链接，不是目标本身。
        if path and "/" not in path and not path.startswith("+"):
            return "@" + path.lower()
    return value


async def is_allowed_target(session: AsyncSession, target_ref: str) -> bool:
    """判断目标是否在白名单里。

    @username 走大小写不敏感比较（库里可能存的是历史大小写写法），
    其余形态要求完全相等 —— 数字 ID 和邀请链接本身没有大小写概念。
    """
    normalized = canonicalize_target_ref(target_ref)
    if normalized.startswith("@"):
        condition = func.lower(AllowedTarget.target_ref) == normalized.lower()
    else:
        condition = AllowedTarget.target_ref == normalized
    target = await session.scalar(select(AllowedTarget).where(condition))
    return target is not None


async def require_allowed_target(session: AsyncSession, target_ref: str) -> None:
    """不在白名单时抛 ValueError。所有操作入口都应该先调它。"""
    if not await is_allowed_target(session, target_ref):
        raise ValueError("目标不在授权白名单中，请先使用 /target_allowlist add 添加。")
