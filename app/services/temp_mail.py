"""临时邮箱收件箱。

Cloudflare TempMail 通过 webhook 把邮件推到这里，登录流程再从库里读验证码。
设计约束：

- 收件域名白名单来自 settings.login_email_alias_domains。不在白名单的收件人
  会被静默丢弃（parse_allowed_recipients 不抛错），因为 webhook 是公开端点，
  对陌生人回 4xx 等于把「哪些域名在用」暴露给探测者。
- 唯一键是 (id, to)，不是 id。同一封邮件可能同时投给多个本地收件人，
  每个收件人各存一行，重复投递用 ON CONFLICT DO NOTHING 吃掉。
- 这里写的是原始 payload 的文本化结果，不做 MIME 解析 —— 解析在上游完成，
  这一层只负责落库和按收件人查询。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from email.utils import getaddresses
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import TempMailMessage

ALLOWED_TEMP_MAIL_DOMAINS = frozenset(settings.login_email_alias_domains)


def normalize_recipient(
    address: str,
    allowed_domains: frozenset[str] | set[str] | tuple[str, ...] | None = None,
) -> tuple[str, str]:
    """校验并规范化收件地址，返回 (完整地址, 域名)。

    拒绝超过 320 字符、不含恰好一个 @、本地部分为空或超过 64 字符、
    以及域名不在白名单的地址，统一抛 ValueError。
    """
    normalized = address.strip().lower()
    if len(normalized) > 320 or normalized.count("@") != 1:
        raise ValueError("invalid recipient address")
    local_part, domain = normalized.rsplit("@", 1)
    allowed = ALLOWED_TEMP_MAIL_DOMAINS if allowed_domains is None else frozenset(allowed_domains)
    if not local_part or len(local_part) > 64 or domain not in allowed:
        raise ValueError("recipient domain is not allowed")
    return normalized, domain


def parse_allowed_recipients(
    value: object,
    allowed_domains: frozenset[str] | set[str] | tuple[str, ...] | None = None,
) -> tuple[tuple[str, str], ...]:
    """从 payload 的 to 字段解析出所有合法收件人。

    容忍字符串或字符串列表两种形态。getaddresses 会解析出显示名，
    只取地址部分；非法的、域名不在白名单的条目直接跳过而不是整批失败 ——
    一封邮件投给多个地址时，只要有一个合法就应该入库。
    返回去重后的 (地址, 域名) 元组，顺序保持首次出现。
    """
    if isinstance(value, str):
        headers = [value]
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        headers = value
    else:
        raise ValueError("to must be an email address or a list of email addresses")
    recipients: list[tuple[str, str]] = []
    seen: set[str] = set()
    for _, address in getaddresses(headers):
        try:
            recipient, domain = normalize_recipient(address, allowed_domains)
        except ValueError:
            continue
        if recipient not in seen:
            recipients.append((recipient, domain))
            seen.add(recipient)
    return tuple(recipients)


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


async def store_temp_mail_payload(
    session: AsyncSession,
    payload: Mapping[str, Any],
    *,
    allowed_domains: frozenset[str] | set[str] | tuple[str, ...] | None = None,
) -> int:
    """把一个 webhook payload 落库，返回实际插入的行数。

    没有任何合法收件人时返回 0（不抛错）。重复投递靠 (id, to) 上的
    ON CONFLICT DO NOTHING 忽略，返回值因此可能小于收件人数量。
    payload 的 id 缺失或超过 255 字符抛 ValueError —— 这个字段是唯一键的一部分，
    不能容忍脏值。
    """
    message_id = str(payload.get("id") or "").strip()
    if not message_id or len(message_id) > 255:
        raise ValueError("id must be a non-empty string no longer than 255 characters")
    recipients = parse_allowed_recipients(payload.get("to"), allowed_domains)
    if not recipients:
        return 0
    sender = _optional_text(payload.get("from")) or ""
    rows = [
        {
            "id": message_id,
            "to": recipient,
            "from": sender,
            "domain": domain,
            "subject": _optional_text(payload.get("subject")),
            "raw": _optional_text(payload.get("raw")),
            "parsedText": _optional_text(payload.get("parsedText")),
            "parsedHtml": _optional_text(payload.get("parsedHtml")),
            "url": _optional_text(payload.get("url")),
            "aiExtractType": _optional_text(payload.get("aiExtractType")),
            "aiExtractResult": payload.get("aiExtractResult"),
            "aiExtractResultText": _optional_text(payload.get("aiExtractResultText")),
        }
        for recipient, domain in recipients
    ]
    statement = insert(TempMailMessage).values(rows)
    statement = statement.on_conflict_do_nothing(index_elements=["id", "to"])
    result = await session.execute(statement)
    await session.commit()
    return int(result.rowcount or 0)


async def list_messages_for_recipient(
    session: AsyncSession,
    recipient: str,
    *,
    limit: int = 100,
) -> list[TempMailMessage]:
    """按收件人取最近的邮件，按 received_at 倒序。

    recipient 会先过 normalize_recipient，因此传入非白名单域名会抛 ValueError。
    limit 必须在 1..500 之间。
    """
    normalized, _ = normalize_recipient(recipient)
    if not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    result = await session.scalars(
        select(TempMailMessage)
        .where(TempMailMessage.recipient == normalized)
        .order_by(desc(TempMailMessage.received_at))
        .limit(limit)
    )
    return list(result.all())


async def latest_message_for_recipient(
    session: AsyncSession,
    recipient: str,
) -> TempMailMessage | None:
    """取该收件人最新一条临时邮件，没有则返回 None。

    换绑流程只关心「最新的那封」，旧邮件可能是上一次登录留下的验证码。
    """
    messages = await list_messages_for_recipient(session, recipient, limit=1)
    return messages[0] if messages else None
