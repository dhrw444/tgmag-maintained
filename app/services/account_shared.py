"""Bot 与 Mini App 共用的账号操作辅助。

这里的函数原先在 app/bot/handlers.py 和 app/webapp/server.py 各存一份，
两份在拆分后就开始漂移（导出时间戳一个用 timezone.utc 一个用 UTC，
大小限制一个叫 MAX_IMAGE_SIZE 一个叫 MAX_REQUEST_SIZE）。放在一处，
两边 import 同一份实现。
"""

from __future__ import annotations

import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.db.models import TgAccount, TgSession
from app.services.crypto import decrypt_text

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

# 随机头像下载与网页上传共用的上限。超过就拒，避免整块读进内存。
MAX_AVATAR_BYTES = 5 * 1024 * 1024
# 单次批量操作允许选中的账号数。
MAX_SELECTION = 200
# 随机头像来源白名单，下载前按完整 URL 精确匹配。
# 只在 https 上，且不接受调用方拼接，download_url_to_file 的 urlopen 目标因此不是用户输入。
RANDOM_AVATAR_URLS = (
    "https://api.btstu.cn/sjbz/api.php?lx=dongman&format=images",
    "https://api.btstu.cn/sjbz/api.php?lx=meizi&format=images",
    "https://img.xjh.me/random_img.php?return=302&type=bg&ctype=acg",
    "https://picsum.photos/1200/1200.jpg",
    "https://picsum.photos/1024/1024.jpg",
)


class EmailCodeRequired(Exception):
    """Telegram 要求邮箱验证码才能继续，调用方需要向用户索取 code_length 位数字。"""

    def __init__(self, code_length: int):
        """把 Telegram 要求的验证码位数带在异常上，交互层据此提示用户。"""
        self.code_length = code_length
        super().__init__(f"email confirmation code required: {code_length}")


def require_email_code(code_length: int) -> str:
    """作为回调传给 account_ops：它要求验证码时用异常把控制权交回交互层。"""
    raise EmailCodeRequired(code_length)


def parse_account_selection(value: str) -> list[int]:
    """把 "1,3,5-7" 这类用户输入解析成去重后的账号 ID 列表。

    中文逗号和空格都当分隔符。区间允许反写（7-5 等价 5-7）。
    超出 MAX_SELECTION 或出现非正整数就抛 ValueError，不做静默截断。
    """
    tokens = [token.strip() for token in value.replace("，", ",").replace(" ", ",").split(",")]
    account_ids: list[int] = []
    seen: set[int] = set()
    for token in tokens:
        if not token:
            continue
        if "-" in token:
            start_raw, end_raw = token.split("-", 1)
            start_id, end_id = int(start_raw), int(end_raw)
            if start_id > end_id:
                start_id, end_id = end_id, start_id
            values = range(start_id, end_id + 1)
        else:
            values = [int(token)]
        for account_id in values:
            if account_id <= 0:
                raise ValueError("账号ID必须为正整数")
            if account_id not in seen:
                seen.add(account_id)
                account_ids.append(account_id)
                if len(account_ids) > MAX_SELECTION:
                    raise ValueError(f"一次最多选择 {MAX_SELECTION} 个账号")
    if not account_ids:
        raise ValueError("没有识别到账号ID")
    return account_ids


def download_url_to_file(url: str, path: Path) -> None:
    """把白名单里的随机头像下载到 path。

    url 只能来自 RANDOM_AVATAR_URLS（常量元组，不接受调用方拼接），
    所以 urlopen 的目标不是用户可控输入。读取上限比 MAX_AVATAR_BYTES 多 1 字节，
    用来区分"刚好等于上限"和"超出上限"。
    """
    if url not in RANDOM_AVATAR_URLS or not url.startswith("https://"):
        raise ValueError("不允许的随机头像来源")
    request = urllib.request.Request(url, headers={"User-Agent": "tg-account-bot/0.1"})
    with urllib.request.urlopen(request, timeout=20) as response:  # nosec B310
        payload = response.read(MAX_AVATAR_BYTES + 1)
    if len(payload) > MAX_AVATAR_BYTES:
        raise ValueError("图片超过 5MB 限制")
    header = payload[:16]
    if not header.startswith((b"\xff\xd8\xff", b"\x89PNG", b"RIFF", b"GIF8")):
        raise ValueError("接口返回的不是可识别图片")
    path.write_bytes(payload)


async def build_session_export(
    session: AsyncSession, account_ids: list[int]
) -> tuple[str, int, list[str]]:
    """导出 StringSession 为可再导入的文本。

    返回 (内容, 成功数, 跳过原因列表)。单条失败不中断整批——
    缺 session、解密失败都只是跳过，因为导出是只读操作，没有需要回滚的状态。
    """
    lines = [
        "# Telethon StringSession export",
        f"# generated_at={datetime.now(UTC).isoformat()}",
        "# 警告：string_session 等同于账号登录凭证，请不要发给不可信的人。",
        "# 导入时使用 phone 和 string_session 两项。",
        "",
    ]
    exported = 0
    skipped: list[str] = []
    for account_id in account_ids:
        account = await session.get(TgAccount, account_id)
        if account is None:
            skipped.append(f"#{account_id}: 账号不存在")
            continue
        tg_session = await session.scalar(
            select(TgSession)
            .where(TgSession.account_id == account.id, TgSession.is_active.is_(True))
            .order_by(TgSession.id.desc())
            .limit(1)
        )
        if tg_session is None:
            skipped.append(f"#{account.id}: 没有 active session")
            continue
        try:
            phone = decrypt_text(account.phone_encrypted) or account.phone_masked
            session_str = decrypt_text(tg_session.session_encrypted)
        except Exception as exc:
            skipped.append(f"#{account.id}: 解密失败 {exc}")
            continue
        if not session_str:
            skipped.append(f"#{account.id}: session 为空")
            continue
        exported += 1
        lines.extend(
            [
                "[account]",
                f"account_id={account.id}",
                f"telegram_user_id={account.user_id or ''}",
                f"username={('@' + account.username) if account.username else ''}",
                f"phone={phone}",
                f"phone_masked={account.phone_masked}",
                "session_type=telethon_string",
                f"string_session={session_str}",
                "",
            ]
        )
    if skipped:
        lines.append("[skipped]")
        lines.extend(skipped)
        lines.append("")
    return "\n".join(lines), exported, skipped
