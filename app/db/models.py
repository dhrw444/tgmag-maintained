"""ORM 模型。

所有能标识真实账号的字段都存加密后的值（`*_encrypted`），密钥来自
FERNET_KEY。明文副本只保留展示用的掩码（`phone_masked`）和 Telegram
公开资料（username / first_name / last_name），这几项本来就能被他人看到。

两个部分唯一索引（文件末尾）是这个 schema 的关键约束，不是优化：
同一 Telegram 用户不能重复入库，同一账号同时只能有一个活跃 session。
它们用 `postgresql_where` 表达，因为 PostgreSQL 的普通 UNIQUE 会把
NULL 视作互不相同，挡不住"多条 user_id 为 NULL"或"多条已失效 session"。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """SQLAlchemy 2.0 声明式基类。迁移脚本通过 target_metadata 引用它的 metadata。"""


class Admin(Base):
    """Bot 管理员。role 目前只有 owner，保留字段是为了将来分级授权不用改表。"""

    __tablename__ = "admins"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    role: Mapped[str] = mapped_column(String(32), default="owner")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TgAccount(Base):
    """一个受管理的 Telegram 账号。

    `status` 是调度用的状态机字段（new / active / limited / ...），
    值集合由 app/tg/account_ops.py 里的常量定义，不在数据库层约束 ——
    新增状态时不想每次都写迁移。
    `login_email_window_hours` 是该账号登录时等验证码的窗口长度，
    0 表示不启用窗口，上限 720 小时由 CheckConstraint 兜住。
    """

    __tablename__ = "tg_accounts"
    __table_args__ = (
        CheckConstraint(
            "login_email_window_hours >= 0 AND login_email_window_hours <= 720",
            name="ck_tg_accounts_login_email_window_hours",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    phone_encrypted: Mapped[str] = mapped_column(Text)
    phone_masked: Mapped[str] = mapped_column(String(32), index=True)
    user_id: Mapped[Optional[int]] = mapped_column(BigInteger, index=True)
    username: Mapped[Optional[str]] = mapped_column(String(128), index=True)
    first_name: Mapped[Optional[str]] = mapped_column(String(128))
    last_name: Mapped[Optional[str]] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32), default="new", index=True)
    login_email_window_hours: Mapped[int] = mapped_column(Integer, default=0)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    sessions: Mapped[list[TgSession]] = relationship(back_populates="account", cascade="all, delete-orphan")


class TgSession(Base):
    """账号的登录会话，存的是加密后的 Telethon StringSession。

    session_encrypted 等同账号凭证，能解密它就能操作该账号。
    轮换时把旧行置 is_active=False 而不是删除，保留审计轨迹；
    文件末尾的部分唯一索引保证同一账号同时只有一条 active。
    """

    __tablename__ = "tg_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id", ondelete="CASCADE"), index=True)
    session_encrypted: Mapped[str] = mapped_column(Text)
    session_type: Mapped[str] = mapped_column(String(32), default="string")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    rotated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    account: Mapped[TgAccount] = relationship(back_populates="sessions")


class AccountSecurity(Base):
    """账号安全设置（2FA、邮箱），按账号一行。

    所有秘密值都是加密列：2FA 密码、提示语、恢复邮箱、登录邮箱。
    与 TgAccount 分成两张表是因为这些字段只被安全相关的入口读写，
    而 TgAccount 每个列表页都会查 —— 放一起会把密文一起拖出来。
    """

    __tablename__ = "account_security"

    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id", ondelete="CASCADE"), primary_key=True)
    has_2fa: Mapped[bool] = mapped_column(Boolean, default=False)
    twofa_encrypted: Mapped[Optional[str]] = mapped_column(Text)
    hint_encrypted: Mapped[Optional[str]] = mapped_column(Text)
    email_encrypted: Mapped[Optional[str]] = mapped_column(Text)
    login_email_encrypted: Mapped[Optional[str]] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class PrivacySettings(Base):
    """账号隐私项，按账号一行以 JSON 整体存储。

    用 JSON 而不是每项一列：Telegram 会新增隐私项类型，
    拆列的话每次都要写迁移，而这里只是把 k/v 原样回传给接口。
    """

    __tablename__ = "privacy_settings"

    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id", ondelete="CASCADE"), primary_key=True)
    rules_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ServiceMessage(Base):
    """来自 777000（Telegram 官方）的系统通知。

    text_hash 用于跨次抓取去重：补历史消息时同一条会被反复取到，
    靠 (account_id, source_user_id, message_id) 与 text_hash 双保险。
    登录邮箱保护就是从这里识别 Telegram 的登录提醒。
    """

    __tablename__ = "service_messages"
    __table_args__ = (UniqueConstraint("account_id", "source_user_id", "message_id", name="uq_service_account_source_message"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id", ondelete="CASCADE"), index=True)
    source_user_id: Mapped[int] = mapped_column(BigInteger, default=777000)
    message_id: Mapped[int] = mapped_column(BigInteger)
    text_hash: Mapped[str] = mapped_column(String(64), index=True)
    text: Mapped[Optional[str]] = mapped_column(Text)
    text_preview: Mapped[Optional[str]] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    notified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class SpamCheck(Base):
    """一次 SpamBot 查询的结果快照。

    存原始返回文本而非只存判定结果：SpamBot 文案会变，
    保留原文才能在解析规则失准时回溯到底收到了什么。
    """

    __tablename__ = "spam_checks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id", ondelete="CASCADE"), index=True)
    response_text: Mapped[Optional[str]] = mapped_column(Text)
    status_detected: Mapped[str] = mapped_column(String(64), default="unknown")
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AllowedTarget(Base):
    """允许操作的目标（群/频道/用户）白名单。

    这是防止误操作扩散的闸门：批量发消息、关注等入口先查这里。
    (target_type, target_ref) 唯一，避免同一目标被重复登记成两种写法。
    """

    __tablename__ = "allowed_targets"
    __table_args__ = (UniqueConstraint("target_type", "target_ref", name="uq_allowed_target"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_type: Mapped[str] = mapped_column(String(32))
    target_ref: Mapped[str] = mapped_column(String(256))
    title: Mapped[Optional[str]] = mapped_column(String(256))
    notes: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RateLimit(Base):
    """按 scope 保存的操作频率上限。

    约束写在数据库层（max_actions/per_seconds 必须为正、
    jitter 区间合法）：限流参数错配会直接导致账号被 Telegram 风控，
    这种值不该允许任何入口写进非法状态。
    """

    __tablename__ = "rate_limits"
    __table_args__ = (
        CheckConstraint("max_actions > 0", name="ck_rate_max_actions_positive"),
        CheckConstraint("per_seconds > 0", name="ck_rate_per_seconds_positive"),
        CheckConstraint(
            "jitter_min >= 0 AND jitter_max >= jitter_min",
            name="ck_rate_jitter_valid",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scope: Mapped[str] = mapped_column(String(64), unique=True)
    max_actions: Mapped[int] = mapped_column(Integer)
    per_seconds: Mapped[int] = mapped_column(Integer)
    jitter_min: Mapped[int] = mapped_column(Integer, default=0)
    jitter_max: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class Job(Base):
    """一次批量任务的头部记录。

    params_json 原样保存发起时的入参，使得任务失败后能复现，
    也避免因为运行时设置被改动而无法解释历史任务的行为。
    """

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    type: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    params_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    error: Mapped[Optional[str]] = mapped_column(Text)


class JobItem(Base):
    """批量任务里针对单个账号/目标的一条执行记录。

    account_id 用 SET NULL 而不是级联删除：账号被删后这条历史仍要留下来，
    否则无法回答「那次批量到底动了哪些账号」。
    """

    __tablename__ = "job_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[Optional[int]] = mapped_column(ForeignKey("tg_accounts.id", ondelete="SET NULL"), index=True)
    target_ref: Mapped[Optional[str]] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(String(32), default="pending")
    result_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    error: Mapped[Optional[str]] = mapped_column(Text)


class AuditLog(Base):
    """管理操作审计。

    admin_id 用 SET NULL：管理员被移除后审计记录不能跟着消失，
    否则一次误删就清掉了他做过的全部操作痕迹。
    payload_json 保留当时的完整入参，用于事后核对。
    """

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    admin_id: Mapped[Optional[int]] = mapped_column(ForeignKey("admins.id", ondelete="SET NULL"), index=True)
    action: Mapped[str] = mapped_column(String(128), index=True)
    entity_type: Mapped[Optional[str]] = mapped_column(String(64))
    entity_id: Mapped[Optional[str]] = mapped_column(String(64))
    payload_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RuntimeSetting(Base):
    """可在运行时改动的设置，键值均以字符串存储。

    与 .env 的分工：.env 是部署期不变的基础设施参数，
    这里是运维过程中会调的项（域名、窗口时长、白名单等）。
    值是字符串所以调用方必须自己校验并兜住非法历史值。
    """

    __tablename__ = "runtime_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class LoginEmailWhitelist(Base):
    """登录邮箱保护的白名单账号：只转发提醒，不自动换绑。

    独立成表而不是在 TgAccount 上加布尔列 —— 白名单是运营侧的例外清单，
    存在与否就是全部语义，单独的表让增删都能被审计。
    """

    __tablename__ = "login_email_whitelist"

    account_id: Mapped[int] = mapped_column(
        ForeignKey("tg_accounts.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LoginEmailProtectionEvent(Base):
    """一次登录邮箱保护的全过程状态机。

    status 取值由 app/services/login_email_protection.py 定义
    （waiting_window / waiting_email / requesting / confirmed / failed 等）。
    service_message_id 唯一：同一条 Telegram 提醒只能驱动一个事件，
    否则监控循环重放历史消息会重复发起换绑。
    parent_event_id 让窗口期内的后续提醒挂到首个事件上，
    而不是各自开一条换绑流程。
    """

    __tablename__ = "login_email_protection_events"
    __table_args__ = (
        UniqueConstraint("service_message_id", name="uq_login_email_event_message"),
        Index(
            "ix_login_email_events_waiting_window",
            "account_id",
            "status",
            "window_ends_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("tg_accounts.id", ondelete="CASCADE"), index=True
    )
    service_message_id: Mapped[int] = mapped_column(
        ForeignKey("service_messages.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), index=True)
    selected_domain: Mapped[Optional[str]] = mapped_column(String(253))
    target_email_encrypted: Mapped[Optional[str]] = mapped_column(Text)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    parent_event_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("login_email_protection_events.id", ondelete="CASCADE"), index=True
    )
    window_ends_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_detected_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    alert_count: Mapped[int] = mapped_column(Integer, default=1)
    error: Mapped[Optional[str]] = mapped_column(Text)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    email_requested_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class TempMailMessage(Base):
    """Cloudflare 临时邮箱收到的邮件，是验证码的读取来源。

    字段名与上游 Webhook 的 JSON 对齐（to/from/parsedText 等），
    部分保留驼峰列名，改列名会让入库映射和上游响应对不上。
    主键是 (id, to)：上游的 id 在收件人之间不唯一。
    """

    __tablename__ = "temp_mail_messages"
    __table_args__ = (
        Index("ix_temp_mail_recipient_received", "to", "received_at"),
        Index("ix_temp_mail_domain_received", "domain", "received_at"),
    )

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    recipient: Mapped[str] = mapped_column("to", String(320), primary_key=True)
    sender: Mapped[str] = mapped_column("from", Text)
    domain: Mapped[str] = mapped_column(String(253), nullable=False)
    subject: Mapped[Optional[str]] = mapped_column(Text)
    raw: Mapped[Optional[str]] = mapped_column(Text)
    parsed_text: Mapped[Optional[str]] = mapped_column("parsedText", Text)
    parsed_html: Mapped[Optional[str]] = mapped_column("parsedHtml", Text)
    url: Mapped[Optional[str]] = mapped_column(Text)
    ai_extract_type: Mapped[Optional[str]] = mapped_column("aiExtractType", Text)
    ai_extract_result: Mapped[Optional[Any]] = mapped_column("aiExtractResult", JSON)
    ai_extract_result_text: Mapped[Optional[str]] = mapped_column(
        "aiExtractResultText", Text
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


Index(
    "uq_tg_accounts_user_id_not_null",
    TgAccount.user_id,
    unique=True,
    postgresql_where=TgAccount.user_id.is_not(None),
)
Index(
    "uq_tg_sessions_one_active_per_account",
    TgSession.account_id,
    unique=True,
    postgresql_where=TgSession.is_active.is_(True),
)
