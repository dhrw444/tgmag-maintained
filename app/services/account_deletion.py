"""账号删除。

删除一个账号牵涉十几张表，这里的顺序不是随手排的：先删引用 account_id 的子表，
最后删 tg_accounts 本体。所有语句在同一条事务里跑，开头拿 pg_advisory_xact_lock
（和账号容量检查共用一把锁），避免删除和新增并发时容量算错。

比「删行」更容易做错的是**残留引用**：作业参数和审计 payload 里会夹带 account_id，
不清理就等于删了账号但留下了它的标识。这里按 JSON 结构递归剥掉匹配的 account_id，
而不是整段清空 —— 保住同一条记录里其他账号的信息。

删除动作本身只留一条不含任何账号信息的审计记录。这是刻意的：审计日志如果
记下了 phone 或 username，删除就失去意义了。
"""

from __future__ import annotations

import logging
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from cryptography.fernet import InvalidToken
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AccountSecurity,
    Admin,
    AuditLog,
    Job,
    JobItem,
    LoginEmailProtectionEvent,
    LoginEmailWhitelist,
    PrivacySettings,
    ServiceMessage,
    SpamCheck,
    TempMailMessage,
    TgAccount,
    TgSession,
)
from app.services.audit import audit
from app.services.crypto import decrypt_text
from app.services.pagination import ACCOUNT_CAPACITY_LOCK_ID

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AccountDeletionResult:
    """删除结果。deleted_rows 是各表 rowcount 之和，用于向操作者回显。"""

    account_id: int
    phone_masked: str
    deleted_rows: int


def account_owned_delete_statements(account_id: int) -> tuple[Any, ...]:
    """返回按外键依赖排序的删除语句，从叶子表到 tg_accounts 本体。

    每条语句都带 account_id 条件，没有全表删除。调用方必须按返回顺序执行，
    否则可能先删父表而留下悬空外键。
    """
    return (
        delete(LoginEmailProtectionEvent).where(
            LoginEmailProtectionEvent.account_id == account_id
        ),
        delete(LoginEmailWhitelist).where(LoginEmailWhitelist.account_id == account_id),
        delete(ServiceMessage).where(ServiceMessage.account_id == account_id),
        delete(SpamCheck).where(SpamCheck.account_id == account_id),
        delete(PrivacySettings).where(PrivacySettings.account_id == account_id),
        delete(AccountSecurity).where(AccountSecurity.account_id == account_id),
        delete(TgSession).where(TgSession.account_id == account_id),
        delete(JobItem).where(JobItem.account_id == account_id),
        delete(AuditLog).where(
            AuditLog.entity_type == "account",
            AuditLog.entity_id == str(account_id),
        ),
        delete(TgAccount).where(TgAccount.id == account_id),
    )


def _without_account_reference(value: Any, account_id: int, key: str | None = None) -> Any:
    """递归剥掉 JSON 里指向该账号的引用，返回新对象（不修改入参）。

    两种形态都处理：字典里 key 为 account_id 且值等于目标时整条丢弃；
    列表里 key 为 accounts / account_ids 时把匹配项从列表中移除。
    其他位置出现同一个数字不动 —— 它可能是别的账号的 id。
    """
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for child_key, child_value in value.items():
            if child_key == "account_id" and str(child_value) == str(account_id):
                continue
            cleaned[child_key] = _without_account_reference(child_value, account_id, child_key)
        return cleaned
    if isinstance(value, list):
        values = value
        if key in {"accounts", "account_ids"}:
            values = [item for item in value if str(item) != str(account_id)]
        return [_without_account_reference(item, account_id) for item in values]
    return value


async def delete_account_records(
    session: AsyncSession,
    account_id: int,
    admin: Admin | None,
) -> AccountDeletionResult:
    """删除一个账号及其全部可识别它的数据库记录。

    调用方负责事务边界；本函数内部只 flush 不 commit。
    账号不存在时抛 ValueError。已解密的临时邮箱目标会连带删除对应的收件邮件，
    解密失败的条目只记警告并继续 —— 一行读不出来的密文不该让整个删除卡住。
    """
    await session.execute(text(f"SELECT pg_advisory_xact_lock({ACCOUNT_CAPACITY_LOCK_ID})"))
    account = await session.get(TgAccount, account_id, with_for_update=True)
    if account is None:
        raise ValueError("账号不存在或已被删除")
    phone_masked = account.phone_masked

    encrypted_targets = list(
        (
            await session.scalars(
                select(LoginEmailProtectionEvent.target_email_encrypted).where(
                    LoginEmailProtectionEvent.account_id == account_id,
                    LoginEmailProtectionEvent.target_email_encrypted.is_not(None),
                )
            )
        ).all()
    )
    target_emails: set[str] = set()
    for encrypted_target in encrypted_targets:
        try:
            decrypted_target = decrypt_text(encrypted_target)
            if decrypted_target:
                target_emails.add(decrypted_target.lower())
        except (InvalidToken, UnicodeError, ValueError):
            # The protection-event row is still removed below. An unreadable value must
            # never make deleting the account itself impossible.
            logger.warning(
                "Could not decrypt a temp-mail target while deleting account %s",
                account_id,
                exc_info=True,
            )

    jobs = list(
        (
            await session.scalars(
                select(Job)
                .join(JobItem, JobItem.job_id == Job.id)
                .where(JobItem.account_id == account_id)
                .distinct()
            )
        ).all()
    )
    for job in jobs:
        original = deepcopy(job.params_json or {})
        cleaned = _without_account_reference(original, account_id)
        if cleaned != original:
            job.params_json = cleaned

    audit_rows = list((await session.scalars(select(AuditLog))).all())
    for row in audit_rows:
        if row.entity_type == "account" and row.entity_id == str(account_id):
            continue
        original = deepcopy(row.payload_json)
        cleaned = _without_account_reference(original, account_id)
        if cleaned != original:
            row.payload_json = cleaned

    deleted_rows = 0
    for statement in account_owned_delete_statements(account_id):
        result = await session.execute(statement)
        deleted_rows += max(int(result.rowcount or 0), 0)

    if target_emails:
        result = await session.execute(
            delete(TempMailMessage).where(TempMailMessage.recipient.in_(target_emails))
        )
        deleted_rows += max(int(result.rowcount or 0), 0)

    # Preserve only the fact that an administrator performed a deletion. This entry
    # intentionally contains no account id, phone, username, Session, or other link
    # back to the removed account.
    await audit(
        session,
        admin,
        "account_deleted",
        "system",
        payload={"removed": True},
    )
    await session.flush()
    return AccountDeletionResult(account_id, phone_masked, deleted_rows)
