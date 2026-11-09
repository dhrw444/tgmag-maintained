"""账号生命周期与账号级 Telegram 操作。

覆盖四类操作：登录/导入、资料修改、2FA、以及各类状态查询。所有函数都直接
操作 Telegram API，因此都不做重试 —— 重试策略在调用方（handlers / batch_ops），
这一层只负责「一次调用是否成功」，失败就把原始异常抛出去。

写库约定：
- 账号凭证（phone / session / 2FA 密码 / 隐私提示语）一律 encrypt_text 后入库，
  明文只在函数局部存在。
- save_logged_in_account 是唯一允许新建 TgAccount 的入口，它开头的
  pg_advisory_xact_lock 把「检查容量 + 插入」变成原子操作，避免并发登录
  同时越过系统账号上限。
- 每次换 session 都把旧的置 is_active=False 而不是删掉，保留可追溯历史。

`STATUS_*` 常量是 tg_accounts.status 的取值集合。数据库层没有 CHECK 约束，
新增状态时只要在这里加常量即可，不需要迁移。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from telethon import TelegramClient, functions, types
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession
from telethon.tl.custom.qrlogin import QRLogin

from app.config import settings
from app.db.models import (
    AccountSecurity,
    PrivacySettings,
    ServiceMessage,
    SpamCheck,
    TgAccount,
    TgSession,
)
from app.services.crypto import decrypt_text, encrypt_text, mask_phone
from app.services.pagination import ACCOUNT_CAPACITY_LOCK_ID, require_account_capacity

logger = logging.getLogger(__name__)

STATUS_NORMAL = "normal"
STATUS_LIMITED = "limited"
STATUS_BANNED = "banned"
STATUS_UNKNOWN = "unknown"
def sent_code_delivery_info(sent: types.auth.SentCode) -> dict[str, str | int | None]:
    """把 Telegram 返回的 SentCode 描述成给人看的验证码投递信息。

    返回 type / label / length / pattern / timeout / next_type。
    pattern 和 timeout 都可能是 None（不同投递方式字段不同），调用方不能假设存在。
    """

    code_type = sent.type
    type_name = type(code_type).__name__
    labels = {
        "SentCodeTypeApp": "Telegram App 内验证码",
        "SentCodeTypeSms": "短信验证码",
        "SentCodeTypeCall": "电话语音验证码",
        "SentCodeTypeFlashCall": "闪电来电验证码",
        "SentCodeTypeMissedCall": "未接来电验证码",
        "SentCodeTypeFragmentSms": "Fragment 短信验证码",
        "SentCodeTypeEmailCode": "邮箱验证码",
    }
    return {
        "type": type_name,
        "label": labels.get(type_name, type_name),
        "length": getattr(code_type, "length", None),
        "pattern": getattr(code_type, "pattern", None)
        or getattr(code_type, "phone_code_pattern", None)
        or getattr(code_type, "email_pattern", None),
        "timeout": getattr(sent, "timeout", None),
        "next_type": type(getattr(sent, "next_type", None)).__name__ if getattr(sent, "next_type", None) else None,
    }


async def start_login(phone: str) -> tuple[TelegramClient, str, dict[str, str | int | None]]:
    """发起手机号登录，返回 (client, phone_code_hash, 投递信息)。

    调用方负责后续的 complete_login，并且必须在两条路径上都断开 client ——
    这里不注册任何清理钩子。"""
    client = TelegramClient(StringSession(), settings.tg_api_id, settings.tg_api_hash)
    await client.connect()
    try:
        sent = await client.send_code_request(phone)
    except Exception:
        await client.disconnect()
        raise
    delivery = sent_code_delivery_info(sent)
    logger.info(
        "Telegram accepted login code request for %s: type=%s next_type=%s timeout=%s",
        mask_phone(phone),
        delivery["type"],
        delivery["next_type"],
        delivery["timeout"],
    )
    return client, sent.phone_code_hash, delivery


async def start_qr_login() -> tuple[TelegramClient, QRLogin]:
    """创建二维码登录会话，返回 (client, qr_login)。

    qr_login 需要调用方反复 wait()，超时后 Telegram 会让二维码失效。"""
    client = TelegramClient(StringSession(), settings.tg_api_id, settings.tg_api_hash)
    await client.connect()
    try:
        qr_login = await client.qr_login()
    except Exception:
        await client.disconnect()
        raise
    return client, qr_login


async def finish_authorized_login(client: TelegramClient) -> tuple[str, types.User]:
    """登录成功后把 client 的 session 落库并返回账号。

    会先取 me 再走 save_logged_in_account，因此这一步失败时 client 仍然是
    已登录状态，调用方要自己断开。"""
    me = await client.get_me()
    session_str = client.session.save()
    await client.disconnect()
    return session_str, me


def phone_from_user(user: types.User) -> str:
    """取 Telegram 返回的 User 上的手机号，取不到时返回空串。

    自己可见的账号才有 phone 字段，导入的 session 常常是空的，不能假设非空。"""
    phone = str(getattr(user, "phone", "") or "").strip()
    if not phone:
        raise ValueError("Telegram 未返回账号手机号，无法保存二维码登录结果")
    return phone if phone.startswith("+") else f"+{phone}"


async def complete_login(
    phone: str,
    code: str,
    phone_code_hash: str,
    password: str | None,
    transient_client: TelegramClient | None = None,
) -> tuple[str, types.User]:
    """提交短信 / App 内验证码完成登录。

    密码保护的账号会抛 SessionPasswordNeededError，调用方据此转到
    complete_password_login，这里不吞掉它。"""
    client = transient_client or TelegramClient(StringSession(), settings.tg_api_id, settings.tg_api_hash)
    if not client.is_connected():
        await client.connect()
    try:
        await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
    except SessionPasswordNeededError:
        if not password:
            raise
        await client.sign_in(password=password)
    me = await client.get_me()
    session_str = client.session.save()
    await client.disconnect()
    return session_str, me


async def complete_password_login(
    client: TelegramClient,
    password: str,
) -> tuple[str, types.User]:
    """提交 2FA 密码完成登录。密码错误由 Telegram 抛异常，这里不转换。"""
    if not client.is_connected():
        await client.connect()
    await client.sign_in(password=password)
    me = await client.get_me()
    session_str = client.session.save()
    await client.disconnect()
    return session_str, me


async def save_logged_in_account(
    session: AsyncSession,
    phone: str,
    session_str: str,
    me: types.User,
    twofa_password: str | None = None,
) -> TgAccount:
    # Serialize account creation so concurrent login/import flows cannot cross
    # the system-wide account limit together. This is a PostgreSQL transaction lock.
    """把已授权的登录写入数据库，返回 TgAccount。

    这是唯一新建账号的入口。开头拿 advisory 事务锁把「计数 + 插入」串行化，
    否则两个并发登录会各自看到未达上限而同时通过检查。

    先按 user_id 找已有账号，找不到再按解密后的手机号匹配 —— 后者要解密全表
    同掩码的行，因此只在首次登录路径上发生。

    函数内部自己 commit，调用方不要包在更长的事务里。"""
    await session.execute(text(f"SELECT pg_advisory_xact_lock({ACCOUNT_CAPACITY_LOCK_ID})"))
    account = await session.scalar(select(TgAccount).where(TgAccount.user_id == me.id))
    if account is None:
        phone_masked = mask_phone(phone)
        candidates = list(
            (await session.scalars(select(TgAccount).where(TgAccount.phone_masked == phone_masked))).all()
        )
        account = next(
            (row for row in candidates if decrypt_text(row.phone_encrypted) == phone),
            None,
        )
        if account is None:
            total = int(await session.scalar(select(func.count()).select_from(TgAccount)) or 0)
            require_account_capacity(total)
            account = TgAccount(phone_encrypted=encrypt_text(phone), phone_masked=phone_masked)
            session.add(account)
            await session.flush()
    else:
        account.phone_encrypted = encrypt_text(phone)
        account.phone_masked = mask_phone(phone)
    account.user_id = me.id
    account.username = me.username
    account.first_name = me.first_name
    account.last_name = me.last_name
    if account.status in {"new", "active", "session_invalid"}:
        account.status = STATUS_UNKNOWN
    account.last_login_at = datetime.now(UTC)
    account.last_error = None
    await session.execute(
        update(TgSession).where(TgSession.account_id == account.id).values(is_active=False)
    )
    session.add(
        TgSession(
            account_id=account.id,
            session_encrypted=encrypt_text(session_str),
            session_type="string",
            is_active=True,
        )
    )
    security = await session.get(AccountSecurity, account.id)
    if security is None:
        security = AccountSecurity(account_id=account.id)
        session.add(security)
    if twofa_password:
        security.has_2fa = True
        security.twofa_encrypted = encrypt_text(twofa_password)
    await session.commit()
    return account


async def import_session(session: AsyncSession, phone: str, session_str: str) -> TgAccount:
    """用已有的 StringSession 导入账号。

    session 无效时抛异常，不会留下半条记录 —— 所有写库都在
    save_logged_in_account 里，且那一步自己 commit。"""
    client = TelegramClient(StringSession(session_str), settings.tg_api_id, settings.tg_api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise ValueError("session 未授权或已失效")
        me = await client.get_me()
    finally:
        await client.disconnect()
    return await save_logged_in_account(session, phone, session_str, me)


async def sync_me(session: AsyncSession, account: TgAccount, client: TelegramClient) -> TgAccount:
    """从 Telegram 拉取最新资料并更新本地账号行。调用方负责 commit。"""
    me = await client.get_me()
    account.user_id = me.id
    account.username = me.username
    account.first_name = me.first_name
    account.last_name = me.last_name
    if account.status in {"new", "active"}:
        account.status = STATUS_UNKNOWN
    account.last_login_at = datetime.now(UTC)
    account.last_error = None
    await session.commit()
    return account


async def set_name(client: TelegramClient, first_name: str, last_name: str | None) -> None:
    """改姓名。Telegram 侧失败时抛异常，本地不写。"""
    await client(functions.account.UpdateProfileRequest(first_name=first_name, last_name=last_name or ""))


async def set_bio(client: TelegramClient, bio: str) -> None:
    """改简介。空字符串表示清空，不是「不修改」。"""
    await client(functions.account.UpdateProfileRequest(about=bio))


async def set_username(client: TelegramClient, username: str) -> None:
    """改用户名。空字符串表示清除用户名，占用冲突由 Telegram 抛异常。"""
    await client(functions.account.UpdateUsernameRequest(username=username))


async def set_avatar(client: TelegramClient, file_path: str) -> None:
    """上传头像。path 必须是服务器上可读的文件。"""
    uploaded = await client.upload_file(Path(file_path))
    await client(functions.photos.UploadProfilePhotoRequest(file=uploaded))


async def check_2fa(client: TelegramClient) -> bool:
    """查询账号是否启用 2FA，返回布尔值。"""
    password = await client(functions.account.GetPasswordRequest())
    return bool(password.has_password)


async def get_2fa_info(client: TelegramClient) -> dict[str, str | bool | None]:
    """取 2FA 的提示语和恢复邮箱用于展示，不返回密码本身。"""
    password = await client(functions.account.GetPasswordRequest())
    login_email_pattern = getattr(password, "login_email_pattern", None)
    email_unconfirmed_pattern = getattr(password, "email_unconfirmed_pattern", None)
    return {
        "has_2fa": bool(password.has_password),
        "hint": getattr(password, "hint", None),
        "email_pattern": login_email_pattern or email_unconfirmed_pattern,
        "login_email_pattern": login_email_pattern,
        "email_unconfirmed_pattern": email_unconfirmed_pattern,
    }


async def edit_2fa(
    client: TelegramClient,
    current_password: str | None,
    new_password: str | None,
    hint: str | None = None,
    email: str | None = None,
    email_code_callback=None,
) -> None:
    """设置 / 修改 / 关闭 2FA，三个动作共用这一个函数。

    修改和关闭都需要旧密码，设置不需要；密码错误由 Telegram 抛异常。"""
    await client.edit_2fa(
        current_password=current_password,
        new_password=new_password,
        hint=hint or "",
        email=email,
        email_code_callback=email_code_callback,
    )


async def send_login_email_code(client: TelegramClient, email: str) -> dict[str, str | int]:
    """请求 Telegram 向指定邮箱发送登录验证码。

    这一步会真的触发 Telegram 发信，因此有频率限制，调用方要处理 FloodError。"""
    sent = await client(
        functions.account.SendVerifyEmailCodeRequest(
            purpose=types.EmailVerifyPurposeLoginChange(),
            email=email,
        ),
        flood_sleep_threshold=0,
    )
    return {"email_pattern": sent.email_pattern, "length": sent.length}


async def confirm_login_email(client: TelegramClient, code: str) -> Any:
    """提交邮箱验证码完成登录邮箱换绑。失败原因需要转成可读中文再给用户。"""
    return await client(
        functions.account.VerifyEmailRequest(
            purpose=types.EmailVerifyPurposeLoginChange(),
            verification=types.EmailVerificationCode(code),
        ),
        flood_sleep_threshold=0,
    )


PRIVACY_KEYS = {
    "phone": types.InputPrivacyKeyPhoneNumber,
    "last_seen": types.InputPrivacyKeyStatusTimestamp,
    "profile_photo": types.InputPrivacyKeyProfilePhoto,
    "forwards": types.InputPrivacyKeyForwards,
    "calls": types.InputPrivacyKeyPhoneCall,
    "groups": types.InputPrivacyKeyChatInvite,
}

PRIVACY_RULES = {
    "everybody": types.InputPrivacyValueAllowAll,
    "contacts": types.InputPrivacyValueAllowContacts,
    "nobody": types.InputPrivacyValueDisallowAll,
}


async def set_privacy(client: TelegramClient, key_name: str, rule_name: str) -> dict[str, str]:
    """修改单项隐私设置。key 必须是 Telegram 支持的隐私项，否则抛异常。"""
    key_factory = PRIVACY_KEYS[key_name]
    rule_factory = PRIVACY_RULES[rule_name]
    await client(functions.account.SetPrivacyRequest(key=key_factory(), rules=[rule_factory()]))
    return {key_name: rule_name}


def detect_spam_status(text: str) -> str:
    """解析 SpamBot 的回复文本，判断账号是否被限制。

    返回 STATUS_* 常量之一。SpamBot 的文案会变，因此按关键词而不是整句匹配。"""
    lowered = text.lower()
    if "no limits" in lowered or "free as a bird" in lowered:
        return STATUS_NORMAL
    if any(word in lowered for word in ["banned", "deleted", "permanently", "forever"]):
        return STATUS_BANNED
    if any(word in lowered for word in ["limited", "restriction", "restricted", "spam", "reported"]):
        return STATUS_LIMITED
    return STATUS_UNKNOWN


async def spam_check(session: AsyncSession, account_id: int, client: TelegramClient) -> SpamCheck:
    """向 SpamBot 发 /start 并读回它的判定结果。

    会给 SpamBot 发真实消息，因此有频率限制；结果同时落库供后续查询。"""
    await client.send_message("SpamBot", "/start")
    await asyncio.sleep(2)
    messages = await client.get_messages("SpamBot", limit=1)
    text = messages[0].message if messages else ""
    status = detect_spam_status(text)
    record = SpamCheck(account_id=account_id, response_text=text, status_detected=status)
    session.add(record)
    account = await session.get(TgAccount, account_id)
    if account is not None:
        account.status = status
    await session.commit()
    return record


async def service_check(session: AsyncSession, account_id: int, client: TelegramClient, limit: int = 10) -> int:
    """拉取与 777000 服务号的最近历史消息并落库。

    用于补齐进程停机期间漏掉的系统通知。落库走 save_service_message 去重。"""
    messages = await client.get_messages(777000, limit=limit)
    inserted = 0
    for msg in messages:
        text = msg.message or ""
        inserted += int(
            await save_service_message(
                session,
                account_id,
                777000,
                msg.id,
                text,
                msg.date or datetime.now(UTC),
            )
        )
    await session.commit()
    return inserted


async def save_service_message(
    session: AsyncSession,
    account_id: int,
    source_user_id: int,
    message_id: int,
    text: str,
    received_at: datetime,
) -> bool:
    """把一条服务消息落库，返回是否是新记录。

    按 (account_id, message_id) 去重，重复调用返回 False 而不是抛错。"""
    if not text:
        return False
    text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    exists = await session.scalar(
        select(ServiceMessage).where(
            ServiceMessage.account_id == account_id,
            ServiceMessage.source_user_id == source_user_id,
            ServiceMessage.message_id == message_id,
        )
    )
    if exists:
        if exists.text != text or exists.text_preview != text[:1000] or exists.text_hash != text_hash:
            exists.text = text
            exists.text_preview = text[:1000]
            exists.text_hash = text_hash
        return False
    session.add(
        ServiceMessage(
            account_id=account_id,
            source_user_id=source_user_id,
            message_id=message_id,
            text_hash=text_hash,
            text=text,
            text_preview=text[:1000],
            received_at=received_at,
        )
    )
    return True


async def update_security_snapshot(
    session: AsyncSession,
    account_id: int,
    has_2fa: bool,
    password: str | None = None,
    hint: str | None = None,
    email: str | None = None,
) -> None:
    """更新账号安全快照（2FA 状态、登录邮箱等）。调用方负责 commit。"""
    security = await session.get(AccountSecurity, account_id)
    if security is None:
        security = AccountSecurity(account_id=account_id)
        session.add(security)
    security.has_2fa = has_2fa
    if not has_2fa:
        security.twofa_encrypted = None
        security.hint_encrypted = None
        security.email_encrypted = None
        await session.commit()
        return
    if password is not None:
        security.twofa_encrypted = encrypt_text(password)
    if hint is not None:
        security.hint_encrypted = encrypt_text(hint)
    if email is not None:
        security.email_encrypted = encrypt_text(email)
    await session.commit()


async def save_privacy_snapshot(session: AsyncSession, account_id: int, values: dict[str, str]) -> None:
    """保存一次隐私设置的完整快照，覆盖旧值。"""
    row = await session.get(PrivacySettings, account_id)
    if row is None:
        row = PrivacySettings(account_id=account_id, rules_json={})
        session.add(row)
    row.rules_json = {**(row.rules_json or {}), **values}
    await session.commit()


async def get_decrypted_session(session: AsyncSession, account_id: int) -> str:
    """取出账号当前 active 的 session 字符串并解密。

    没有 active session 时返回 None。返回的是明文凭证，调用方不要落日志。"""
    row = await session.scalar(
        select(TgSession)
        .where(TgSession.account_id == account_id, TgSession.is_active.is_(True))
        .order_by(TgSession.id.desc())
    )
    if not row:
        raise ValueError("账号没有可用 session")
    decrypted = decrypt_text(row.session_encrypted)
    if not decrypted:
        raise ValueError("session 解密失败")
    return decrypted
