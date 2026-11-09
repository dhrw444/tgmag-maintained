"""Telethon 客户端池与后台监控。

一个进程内维护 `account_id -> TelegramClient` 的映射。设计上刻意不做池化上限：
每个受管理账号都需要一条常驻 MTProto 连接，账号数由 pagination.MAX_SYSTEM_ACCOUNTS
在入库时限制，这里不再重复限制。

三条后台任务：
- `_monitor_loop`：周期性检查连接，掉线的重连。间隔来自配置，默认较长。
- 服务消息监听：对每个 client 注册 NewMessage 事件，只收 777000（Telegram 官方
  服务号）的消息，用来识别换绑邮箱、封禁等系统通知。
- 登录邮箱保护的窗口等待任务：由 LoginEmailProtector 自己管，这里只提供 client。

并发要点：`get_client` 用单把锁串行化创建，因为 Telethon 的 connect 不是幂等的，
两个协程同时给同一个账号建 client 会产生两条 session。锁内做网络 IO 是刻意的取舍 ——
创建路径低频，正确性优先。

`_deleting_account_ids` 是删除流程的互斥标记：删除期间所有会碰该账号的入口先查它，
避免删到一半又被监控重连起来。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from contextlib import suppress
from datetime import UTC, datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from telethon import TelegramClient, events
from telethon.errors import AuthKeyDuplicatedError
from telethon.sessions import StringSession

from app.config import settings
from app.db.models import LoginEmailProtectionEvent, ServiceMessage, TgAccount, TgSession
from app.services.crypto import decrypt_text
from app.services.login_email_protection import (
    LoginEmailProtector,
    LoginEmailWindowNotice,
    is_login_email_protection_alert,
)

logger = logging.getLogger(__name__)
REALTIME_SERVICE_SOURCE_IDS = {777000}


class ClientPool:
    """受管理账号的 Telethon 客户端集合。

    生命周期与进程一致，由 app.main 创建并传给 Bot 与 Mini App。
    """

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], bot: Bot):
        """建池并把登录邮箱保护器一起带上（两者共享同一个 sessionmaker/bot）。

        监控默认开启，但真正的任务要等 start_service_monitor 才起；
        所有账号级状态（客户端、锁、正在删除的标记）都按账号 id 分槽。
        """
        self.sessionmaker = sessionmaker
        self.bot = bot
        self.clients: dict[int, TelegramClient] = {}
        self._lock = asyncio.Lock()
        self.monitor_enabled = True
        self._monitor_task: asyncio.Task[None] | None = None
        self.login_email_protector = LoginEmailProtector(sessionmaker, bot)
        self._protection_tasks: set[asyncio.Task[None]] = set()
        self._deleting_account_ids: set[int] = set()
        self._service_message_locks: dict[int, asyncio.Lock] = {}
        self._login_email_health_lock = asyncio.Lock()
        self.login_email_health_checked_at: datetime | None = None
        self.login_email_health_error: str | None = None

    @property
    def connected_account_ids(self) -> set[int]:
        """当前真正处于连接状态的账号 id 集合，取自客户端实时状态而非登记表。"""
        return {account_id for account_id, client in self.clients.items() if client.is_connected()}

    @property
    def service_monitor_running(self) -> bool:
        """服务监控任务是否真的在跑 —— 只看开关会漏掉任务已退出的情况。"""
        task = self._monitor_task
        return bool(self.monitor_enabled and task is not None and not task.done())

    async def start_service_monitor(self) -> None:
        """连上所有 active 账号，可选做一次登录邮箱保护自检，然后起监控循环。

        幂等：循环已在跑时不会重复创建任务。启动时会顺带把库里没跑完的换绑窗口
        恢复回来，因此这个函数在每次重连后也被复用。
        """
        self.monitor_enabled = True
        await self.connect_all_active()
        if settings.login_email_protection_enabled:
            await self.check_login_email_health()
        if self._monitor_task is None or self._monitor_task.done():
            self._monitor_task = asyncio.create_task(
                self._monitor_loop(),
                name="telegram-service-monitor",
            )
        await self.restore_pending_protection_windows()

    async def restore_pending_protection_windows(self) -> None:
        """把库里停在 waiting_window / waiting_email 的事件重新挂上。

        只处理 parent_event_id 为空的顶层事件；子事件由父事件驱动。
        已经在等待的任务不重复挂 —— 这是幂等的关键，监控循环每分钟都会调一次。
        """
        async with self.sessionmaker() as session:
            events_to_restore = list(
                (
                    await session.scalars(
                        select(LoginEmailProtectionEvent)
                        .where(
                            LoginEmailProtectionEvent.status.in_(
                                {"waiting_window", "waiting_email"}
                            ),
                            LoginEmailProtectionEvent.parent_event_id.is_(None),
                        )
                        .order_by(LoginEmailProtectionEvent.window_ends_at)
                    )
                ).all()
            )
        for event in events_to_restore:
            if event.status == "waiting_window" and self.login_email_protector.has_window_waiter(
                event.id
            ):
                continue
            if (
                event.status == "waiting_email"
                and self.login_email_protector.has_change_in_progress(event.account_id)
            ):
                continue
            try:
                client = await self.get_client(event.account_id)
            except Exception:
                logger.exception(
                    "Failed to restore login email window %s for account %s",
                    event.id,
                    event.account_id,
                )
                continue
            if event.status == "waiting_email":
                coroutine = self.login_email_protector.resume_waiting_email(event.id, client)
                task_name = f"login-email-resume-{event.id}"
            else:
                coroutine = self.login_email_protector.wait_for_window(event.id, client)
                task_name = f"login-email-window-{event.id}"
            task = asyncio.create_task(coroutine, name=task_name)
            self._track_protection_task(task)

    async def stop_service_monitor(self) -> None:
        """停监控、取消全部保护任务、断开所有连接。

        顺序不能反：先取消任务再断开连接，否则任务在断开后醒来会拿到死 client。
        """
        self.monitor_enabled = False
        task, self._monitor_task = self._monitor_task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        protection_tasks = list(self._protection_tasks)
        for protection_task in protection_tasks:
            protection_task.cancel()
        if protection_tasks:
            await asyncio.gather(*protection_tasks, return_exceptions=True)
        await self.disconnect_all()

    async def _monitor_loop(self) -> None:
        """周期性重连 + 恢复未完成窗口。单次失败只记日志，不终止循环。

        间隔下限 30 秒是硬编码的：更密的检查对掉线恢复没有实质帮助，
        却会让每个账号每分钟多几次无意义的 is_connected 判断。
        """
        while self.monitor_enabled:
            await asyncio.sleep(max(30, settings.service_monitor_interval_seconds))
            if not self.monitor_enabled:
                break
            try:
                await self.connect_all_active()
                await self.restore_pending_protection_windows()
            except Exception:
                logger.exception("Periodic Telegram client reconnect failed")

    async def connect_all_active(self) -> None:
        """连上所有 is_active 的 session。单个账号失败不影响其他账号。"""
        async with self.sessionmaker() as session:
            rows = await session.scalars(
                select(TgSession.account_id)
                .where(TgSession.is_active.is_(True))
                .order_by(TgSession.account_id)
            )
            account_ids = list(rows.all())
        for account_id in account_ids:
            try:
                await self.get_client(account_id)
            except Exception:
                logger.exception("Failed to connect account %s", account_id)

    async def disconnect_all(self) -> None:
        """断开全部客户端但保留 clients 字典，便于之后重连时复用同一批对象。"""
        for account_id, client in list(self.clients.items()):
            try:
                await client.disconnect()
            except Exception:
                logger.exception("Failed to disconnect account %s", account_id)
        self.clients.clear()

    async def check_login_email_health(self) -> bool:
        """Validate all email backends used by configured domains."""
        async with self._login_email_health_lock:
            self.login_email_health_checked_at = datetime.now(UTC)
            try:
                await self.login_email_protector.reader.validate_connection()
            except Exception as exc:
                self.login_email_health_error = f"{type(exc).__name__}: {str(exc)[:500]}"
                logger.exception("Login email protection mail backend health check failed")
                return False
            self.login_email_health_error = None
            return True

    def _track_protection_task(self, task: asyncio.Task[None]) -> None:
        self._protection_tasks.add(task)
        task.add_done_callback(self._protection_task_done)

    def _protection_task_done(self, task: asyncio.Task[None]) -> None:
        self._protection_tasks.discard(task)
        if task.cancelled():
            return
        exception = task.exception()
        if exception is not None:
            logger.error(
                "Unhandled login email protection task failure",
                exc_info=(type(exception), exception, exception.__traceback__),
            )

    async def drop(self, account_id: int) -> None:
        """断开并移除单个账号的客户端。账号不存在时静默返回。"""
        client = self.clients.pop(account_id, None)
        if client:
            try:
                await client.disconnect()
            except Exception:
                logger.exception("Failed to disconnect dropped account %s", account_id)

    async def begin_account_deletion(self, account_id: int) -> None:
        """Fence an account from reconnects, cancel its workers, then disconnect it."""
        async with self._lock:
            if account_id in self._deleting_account_ids:
                raise ValueError("账号正在删除，请勿重复操作")
            self._deleting_account_ids.add(account_id)
        try:
            async with self.sessionmaker() as session:
                event_ids = set(
                    (
                        await session.scalars(
                            select(LoginEmailProtectionEvent.id).where(
                                LoginEmailProtectionEvent.account_id == account_id
                            )
                        )
                    ).all()
                )
            correlated_tasks = {
                task
                for task in self._protection_tasks
                if not task.done()
                and any(task.get_name().endswith(f"-{event_id}") for event_id in event_ids)
            }
            for task in correlated_tasks:
                task.cancel()
            if correlated_tasks:
                await asyncio.gather(*correlated_tasks, return_exceptions=True)
            await self.login_email_protector.cancel_account_tasks(account_id, event_ids)
            self._service_message_locks.pop(account_id, None)
            await self.drop(account_id)
        except Exception:
            await self.end_account_deletion(account_id)
            raise

    async def end_account_deletion(self, account_id: int) -> None:
        """解除删除互斥标记。删除流程无论成功失败都必须在 finally 里调用。"""
        async with self._lock:
            self._deleting_account_ids.discard(account_id)

    async def retry_login_email_protection(self, event_id: int, domain: str) -> None:
        """后台重跑一次邮箱换绑，不阻塞调用方。

        事件被删掉时抛 ValueError；重跑自身失败只记日志，不冒泡到任务外
        —— 异步任务里抛出的异常没人接。
        """
        async with self.sessionmaker() as session:
            event = await session.get(LoginEmailProtectionEvent, event_id)
            if event is None:
                raise ValueError("保护事件不存在")
            account_id = event.account_id
        client = await self.get_client(account_id)

        async def run() -> None:
            """真正执行重跑；异常只记日志，不让任务静默带崩。"""
            try:
                await self.login_email_protector.retry(event_id, domain, client)
            except Exception:
                logger.exception(
                    "Manual login email protection retry failed for event %s", event_id
                )

        task = asyncio.create_task(run(), name=f"login-email-protection-retry-{event_id}")
        self._track_protection_task(task)

    async def get_client(self, account_id: int) -> TelegramClient:
        """取（必要时创建）账号的 Telethon 客户端并确保已连接。

        锁内完成创建和连接：Telethon 的 connect 不幂等，并发创建同一账号会产生
        两条 session。已经连接的直接返回，不走锁。

        删除中的账号抛 ValueError，session 无效的账号会先标记再抛。
        """
        async with self._lock:
            if account_id in self._deleting_account_ids:
                raise ValueError(f"账号 {account_id} 正在删除")
            existing = self.clients.get(account_id)
            if existing and existing.is_connected():
                return existing
            async with self.sessionmaker() as session:
                tg_session = await session.scalar(
                    select(TgSession)
                    .where(TgSession.account_id == account_id, TgSession.is_active.is_(True))
                    .order_by(TgSession.id.desc())
                )
                if not tg_session:
                    raise ValueError(f"账号 {account_id} 没有可用 session")
                session_str = decrypt_text(tg_session.session_encrypted)
            client = TelegramClient(
                StringSession(session_str), settings.tg_api_id, settings.tg_api_hash
            )
            try:
                await client.connect()
                if not await client.is_user_authorized():
                    await client.disconnect()
                    await self._mark_session_invalid(account_id, "session 未授权或已失效")
                    raise ValueError(f"账号 {account_id} session 已失效，请重新登录")
            except AuthKeyDuplicatedError as exc:
                await client.disconnect()
                await self._mark_session_invalid(account_id, str(exc))
                raise ValueError(f"账号 {account_id} session 授权密钥已失效，请重新登录") from exc
            client.add_event_handler(
                lambda event, aid=account_id: self._handle_service_message(aid, event),
                events.NewMessage(incoming=True),
            )
            self.clients[account_id] = client
            await self.catch_up_recent_login_alerts(account_id, client)
            return client

    async def _mark_session_invalid(self, account_id: int, error: str) -> None:
        """把账号的 session 标记为失效并同步账号状态。

        只在 AuthKeyDuplicatedError 时调用 —— 那意味着同一 session 在别处登录过，
        本地这份已经作废，重连没有意义。
        """
        async with self.sessionmaker() as session:
            await session.execute(
                update(TgSession)
                .where(TgSession.account_id == account_id, TgSession.is_active.is_(True))
                .values(is_active=False, rotated_at=datetime.now(UTC))
            )
            account = await session.get(TgAccount, account_id)
            if account is not None:
                account.status = "session_invalid"
                account.last_error = error[:2000]
            await session.commit()

    async def catch_up_recent_login_alerts(
        self,
        account_id: int,
        client: TelegramClient,
    ) -> None:
        """补处理最近一段时间内的换绑提醒。

        服务消息监听只在进程运行期间有效，重启会漏掉期间到达的提醒。
        这个函数在启动后拉一次历史补上。时间窗口由配置决定，不处理全部历史，
        否则首次启动会对老账号做大量无意义的 API 调用。
        """
        catchup_seconds = settings.login_email_catchup_seconds
        if (
            not self.monitor_enabled
            or not settings.login_email_protection_enabled
            or not catchup_seconds
        ):
            return
        cutoff = datetime.now(UTC) - timedelta(seconds=catchup_seconds)
        try:
            messages = await client.get_messages(777000, limit=10)
        except Exception:
            logger.exception("Failed to catch up recent 777000 messages for account %s", account_id)
            return
        for message in reversed(messages):
            text = message.message or ""
            received_at = message.date or datetime.now(UTC)
            if received_at.tzinfo is None:
                received_at = received_at.replace(tzinfo=UTC)
            if received_at < cutoff or not is_login_email_protection_alert(text):
                continue
            await self._ingest_service_message(
                account_id,
                777000,
                int(message.id),
                text,
                received_at,
                client,
            )

    def _schedule_login_email_window(
        self,
        notice: LoginEmailWindowNotice,
        client: TelegramClient,
    ) -> None:
        """为一条提醒安排窗口等待任务，并把任务登记进 _protection_tasks 便于取消。

        任务名带 event_id 后缀，begin_account_deletion 靠它找出关联任务。
        """
        if not notice.starts_new_window:
            return
        task = asyncio.create_task(
            self.login_email_protector.wait_for_window(notice.event_id, client),
            name=f"login-email-window-{notice.event_id}",
        )
        self._track_protection_task(task)

    @staticmethod
    def _window_notice_text(notice: LoginEmailWindowNotice) -> str:
        deadline = notice.window_ends_at
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)
        hours = notice.window_hours
        if notice.starts_new_window and hours == 0:
            return (
                "⚡ 已触发即时登录邮箱保护\n"
                f"当前累计：{notice.alert_count} 次\n"
                "系统正在立即换绑登录邮箱并等待验证邮件。"
            )
        if not notice.starts_new_window and deadline <= datetime.now(UTC):
            return (
                "🔄 登录邮箱保护正在处理\n"
                f"当前累计：{notice.alert_count} 次\n"
                "这条新提醒已合并，本次不会重复换绑或重复发码。"
            )
        heading = (
            f"🕗 已开启 {hours} 小时登录邮箱保护窗口"
            if notice.starts_new_window
            else "🕗 当前处于登录邮箱保护窗口"
        )
        return (
            f"{heading}\n"
            f"截止时间：{deadline.astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
            f"当前累计：{notice.alert_count} 次\n"
            "窗口内仅转发提醒，不换绑且不顺延；到期后自动换绑并发送汇总。"
        )

    async def _is_post_session_login_alert(
        self,
        account_id: int,
        text: str,
        received_at: datetime,
    ) -> bool:
        """Only protect login alerts created after the active session was admitted."""
        if not is_login_email_protection_alert(text):
            return False
        async with self.sessionmaker() as session:
            session_created_at = await session.scalar(
                select(TgSession.created_at)
                .where(TgSession.account_id == account_id, TgSession.is_active.is_(True))
                .order_by(TgSession.id.desc())
                .limit(1)
            )
        if session_created_at is None:
            logger.warning(
                "Ignoring login alert for account %s because no active session baseline exists",
                account_id,
            )
            return False
        if received_at.tzinfo is None:
            received_at = received_at.replace(tzinfo=UTC)
        if session_created_at.tzinfo is None:
            session_created_at = session_created_at.replace(tzinfo=UTC)
        return received_at > session_created_at

    async def _ingest_service_message(
        self,
        account_id: int,
        source_user_id: int,
        message_id: int,
        text: str,
        received_at: datetime,
        client: TelegramClient,
    ) -> None:
        """把一条 777000 服务消息落库并分发处理。

        用 per-account 锁串行化：同一条消息可能从实时监听和历史补拉两条路径到达，
        锁内按 (account_id, message_id) 去重，保证只处理一次。
        """
        lock = self._service_message_locks.setdefault(account_id, asyncio.Lock())
        async with lock:
            text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            async with self.sessionmaker() as session:
                record = await session.scalar(
                    select(ServiceMessage).where(
                        ServiceMessage.account_id == account_id,
                        ServiceMessage.source_user_id == source_user_id,
                        ServiceMessage.message_id == message_id,
                    )
                )
                created = record is None
                if record is None:
                    record = ServiceMessage(
                        account_id=account_id,
                        source_user_id=source_user_id,
                        message_id=message_id,
                        text_hash=text_hash,
                        text=text,
                        text_preview=text[:1000],
                        received_at=received_at,
                        notified_at=datetime.now(UTC),
                    )
                    session.add(record)
                    await session.flush()
                elif text and (
                    record.text != text
                    or record.text_preview != text[:1000]
                    or record.text_hash != text_hash
                ):
                    record.text = text
                    record.text_preview = text[:1000]
                    record.text_hash = text_hash
                await session.commit()
                service_message_id = record.id

        # The alert used to create/import the current Session predates that
        # Session and is only the enrollment baseline. Protect strictly newer
        # alerts, including existing rows saved by a manual history pull.
        window_notice = None
        if source_user_id == 777000 and await self._is_post_session_login_alert(
            account_id, text, received_at
        ):
            try:
                window_notice = await self.login_email_protector.record_alert(
                    account_id, service_message_id
                )
                if window_notice is not None:
                    self._schedule_login_email_window(window_notice, client)
            except Exception:
                logger.exception(
                    "Failed to record login email protection window for account %s",
                    account_id,
                )
        elif is_login_email_protection_alert(text):
            logger.info(
                "Stored baseline login alert without protection for account %s, message %s",
                account_id,
                message_id,
            )
        if not created:
            return
        notification_text = (
            f"服务消息\n账号ID: {account_id}\n来源: {source_user_id}\n"
            f"消息ID: {message_id}\n内容:\n{text[:3500]}"
        )
        if window_notice is not None:
            notification_text += f"\n\n{self._window_notice_text(window_notice)}"
        for admin_id in settings.admin_ids:
            try:
                await self.bot.send_message(admin_id, notification_text)
            except TelegramAPIError:
                logger.warning(
                    "Failed to notify admin %s for service message %s",
                    admin_id,
                    message_id,
                    exc_info=True,
                )
            except Exception:
                logger.exception("Unexpected notify failure for admin %s", admin_id)

    async def _handle_service_message(
        self, account_id: int, event: events.NewMessage.Event
    ) -> None:
        """识别消息类型并触发对应动作。

        只有换绑邮箱类提醒会进入保护流程；其他类型（封禁、登录提醒）目前只落库
        供人查，不自动处理 —— 自动应对这类消息的风险远大于收益。
        """
        if not self.monitor_enabled:
            return
        if not event.is_private:
            return
        source_user_id = int(event.sender_id or 0)
        if source_user_id <= 0:
            return
        if source_user_id == settings.bot_user_id:
            return
        if source_user_id not in REALTIME_SERVICE_SOURCE_IDS:
            return
        text = event.raw_text or ""
        if not text.strip():
            return
        received_at = event.message.date or datetime.now(UTC)
        client = self.clients.get(account_id)
        if client is None:
            logger.error("Received a service message for untracked account %s", account_id)
            return
        await self._ingest_service_message(
            account_id,
            source_user_id,
            int(event.message.id),
            text,
            received_at,
            client,
        )
