"""单个账号在授权目标上的批量动作。

批量执行本身（遍历账号、限速、汇总结果）在 handlers 和 rate_limit 里，
这里只有「对一个账号做一次动作」的最小单元。这样拆的原因是重试和限速策略
需要看到每次调用的独立结果，而把循环写进来就只能返回聚合值。

target 的两种形态在这一层被区分开：
- 邀请链接（t.me/+hash）走 ImportChatInviteRequest，能加入未加入的私有群；
- 其他（@username / 数字 ID）走 JoinChannelRequest，只能加入公开群或已加入的群。
未加入的私有群无法仅凭数字 ID 加入，这是 Telegram 的限制，不是实现缺陷。

异常策略：Telegram 的已知错误码在这里翻译成中文 ValueError（可以直接展示给用户），
未识别的错误原样抛出，由上层记录完整堆栈。
"""

from __future__ import annotations

from sqlalchemy import select
from telethon import helpers, types
from telethon.errors import (
    ChannelsTooMuchError,
    InviteHashExpiredError,
    InviteHashInvalidError,
    InviteRequestSentError,
    UserAlreadyParticipantError,
)
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import (
    ForwardMessagesRequest,
    GetMessagesViewsRequest,
    ImportChatInviteRequest,
    SendReactionRequest,
)

from app.db.models import TgAccount
from app.services.targets import canonicalize_target_ref, telegram_invite_hash
from app.tg.client_pool import ClientPool


def normalize_target(target: str) -> str | int:
    """把目标字符串规范成 int 或 str。

    纯数字（允许前导负号）转 int，其余原样返回。负数 ID 是超级群的形态。"""
    stripped = target.strip()
    if stripped.lstrip("-").isdigit():
        return int(stripped)
    return stripped


async def get_peer(pool: ClientPool, account_id: int, target: str):
    """取 client 与目标 entity，两步都完成才返回。

    内部会调用 resolve_target_entity，后者自己也调 get_client，因此这里
    拿到的是同一个已连接对象（池内复用）。"""
    client = await pool.get_client(account_id)
    return client, await resolve_target_entity(pool, account_id, target)


async def resolve_target_entity(pool: ClientPool, account_id: int, target: str):
    """把目标字符串解析成 Telethon 的 entity。

    私有链接在这里无法解析，必须先用 subscribe 加入。解析失败时抛
    ValueError 并带上原始 target，方便定位是哪一条授权目标失效了。"""
    client = await pool.get_client(account_id)
    normalized = normalize_target(target)
    try:
        return await client.get_input_entity(normalized)
    except ValueError:
        if not isinstance(normalized, int) or normalized < 0:
            raise
        async with pool.sessionmaker() as session:
            account = await session.scalar(select(TgAccount).where(TgAccount.user_id == normalized))
        if account and account.username:
            return await client.get_input_entity(f"@{account.username}")
        raise


async def send_message(pool: ClientPool, account_id: int, target: str, text: str) -> dict[str, int]:
    """向目标发一条文本消息，返回 message_id。

    会给目标发真实消息，频控由调用方负责。"""
    client = await pool.get_client(account_id)
    entity = await resolve_target_entity(pool, account_id, target)
    msg = await client.send_message(entity, text)
    return {"message_id": msg.id}


async def subscribe(pool: ClientPool, account_id: int, target: str) -> dict[str, str | int]:
    """加入目标群组或频道，返回 {joined, status, chat_id?}。

    邀请链接路径下，已经在群里返回 status=already_member，
    需要审核的群返回 request_sent，这两种都不算失败。
    公开群路径下重复加入不报错，Telegram 视为成功。

    链接过期、链接无效、群组数量超限三种情况翻译成中文 ValueError。"""
    invite_hash = telegram_invite_hash(target)
    if invite_hash:
        client = await pool.get_client(account_id)
        normalized = canonicalize_target_ref(target)
        try:
            result = await client(ImportChatInviteRequest(invite_hash))
        except UserAlreadyParticipantError:
            return {"joined": normalized, "status": "already_member"}
        except InviteRequestSentError:
            return {"joined": normalized, "status": "request_sent"}
        except InviteHashExpiredError as exc:
            raise ValueError("群组邀请链接已过期，请更新授权目标中的链接") from exc
        except InviteHashInvalidError as exc:
            raise ValueError("群组邀请链接无效，请检查链接是否完整") from exc
        except ChannelsTooMuchError as exc:
            raise ValueError("该账号加入的频道或群组数量已达 Telegram 上限") from exc
        payload: dict[str, str | int] = {"joined": normalized, "status": "joined"}
        chats = getattr(result, "chats", None) or []
        if chats and getattr(chats[0], "id", None) is not None:
            payload["chat_id"] = int(chats[0].id)
        return payload
    try:
        client, peer = await get_peer(pool, account_id, target)
    except ValueError as exc:
        if target.strip().lstrip("-").isdigit():
            raise ValueError(
                "未加入的私有群无法仅凭 ID 加入，请改用已授权的 t.me/+ 邀请链接"
            ) from exc
        raise
    await client(JoinChannelRequest(peer))
    return {"joined": canonicalize_target_ref(target), "status": "joined"}


async def react(pool: ClientPool, account_id: int, target: str, message_id: int, emoji: str) -> dict[str, str | int]:
    """给指定消息加表情回应，返回 {message_id, reaction}。

    只能用一个 emoji；多表情回应需要 ReactionCustomEmoji，这里不支持。"""
    client, peer = await get_peer(pool, account_id, target)
    await client(
        SendReactionRequest(
            peer=peer,
            msg_id=message_id,
            reaction=[types.ReactionEmoji(emoticon=emoji)],
        )
    )
    return {"message_id": message_id, "reaction": emoji}


async def unreact(pool: ClientPool, account_id: int, target: str, message_id: int) -> dict[str, int]:
    """撤销对指定消息的回应。没有回应时 Telegram 不报错。"""
    client, peer = await get_peer(pool, account_id, target)
    await client(SendReactionRequest(peer=peer, msg_id=message_id, reaction=[]))
    return {"message_id": message_id}


async def view_post(pool: ClientPool, account_id: int, target: str, message_id: int) -> dict[str, int]:
    """读取一条消息并把浏览量 +1，返回最新浏览量。

    increment=True 才是真正的「增加浏览」，只读不加要去掉这个参数。"""
    client, peer = await get_peer(pool, account_id, target)
    result = await client(GetMessagesViewsRequest(peer=peer, id=[message_id], increment=True))
    views = (result.views[0].views if result.views else 0) or 0
    return {"message_id": message_id, "views": views}


async def forward(
    pool: ClientPool,
    account_id: int,
    source: str,
    message_id: int,
    target: str,
) -> dict[str, int]:
    """把源消息转发到目标，返回新消息的 message_id。

    source 与 target 分别解析，源消息必须对执行账号可见。"""
    client = await pool.get_client(account_id)
    from_peer = await resolve_target_entity(pool, account_id, source)
    to_peer = await resolve_target_entity(pool, account_id, target)
    result = await client(
        ForwardMessagesRequest(
            from_peer=from_peer,
            id=[message_id],
            to_peer=to_peer,
            random_id=[helpers.generate_random_long()],
        )
    )
    updates = getattr(result, "updates", [])
    sent_id = next((getattr(u.message, "id", None) for u in updates if hasattr(u, "message")), None)
    return {"message_id": sent_id or 0}
