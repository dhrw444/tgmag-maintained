"""服务入口。

一个进程里同时跑三件事：Telegram Bot 长轮询、账号后台监控、可选的 Mini App
HTTP 服务。三者共用同一个数据库会话工厂和同一个 ClientPool，因此必须同生共死 ——
任一环节起不来就整体退出，不做降级运行，否则会出现「Bot 在回消息但账号全断线」
这种更难排查的半死状态。

部署形态由 systemd 单实例托管（见 ops/systemd/tg-account-bot.service），
所以这里没有多副本协调逻辑，advisory lock 只用来防同进程内的并发。
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramAPIError

from app.bot.handlers import configure_bot_ui, router
from app.bot.keyboards import main_menu
from app.config import settings
from app.db.session import init_db, sessionmaker
from app.services.bootstrap import bootstrap_defaults
from app.services.login_email_protection import recover_incomplete_events
from app.tg.client_pool import ClientPool
from app.webapp.server import start_webapp

logger = logging.getLogger(__name__)


async def restore_admin_keyboards(bot: Bot) -> None:
    """Re-send the canonical reply keyboard after every successful service start."""
    for admin_id in settings.admin_ids:
        try:
            await bot.send_message(
                admin_id,
                "服务已启动。主菜单可通过输入框旁的小键盘按钮随时展开或收起。",
                reply_markup=main_menu(),
            )
        except TelegramAPIError:
            logger.warning("Failed to restore reply keyboard for admin %s", admin_id, exc_info=True)


async def main() -> None:
    """服务入口：初始化依赖，然后进入 Bot 长轮询。

    启动顺序有依赖，不能调换：建目录 → init_db（校验 alembic 版本）→
    写默认配置 → 恢复未完成的换绑事件 → 起 Mini App → 起账号监控 →
    进 polling。前四步任何一步失败都应该让进程退出，因为后面的模块
    都假设数据库结构已经就绪。

    finally 里逆序清理：先停 Mini App，再停监控，最后关 Bot 的 aiohttp session。
    """
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    settings.session_dir.mkdir(parents=True, exist_ok=True)
    settings.backup_dir.mkdir(parents=True, exist_ok=True)
    await init_db()
    await bootstrap_defaults()
    recovered_events = await recover_incomplete_events(sessionmaker)
    if recovered_events:
        logger.warning("Marked %s incomplete login email protection events as interrupted", recovered_events)
    logger.info(
        "Login email protection: enabled=%s, backends=%s, domains=%s",
        settings.login_email_protection_enabled,
        ",".join(sorted(settings.configured_login_email_backends)) or "none",
        len(settings.login_email_alias_domains),
    )

    bot = Bot(token=settings.bot_token)
    try:
        await configure_bot_ui(bot)
    except TelegramAPIError:
        logger.warning("Failed to configure Telegram command menu", exc_info=True)
    pool = ClientPool(sessionmaker=sessionmaker, bot=bot)
    dp = Dispatcher(sessionmaker=sessionmaker, client_pool=pool)
    dp.include_router(router)

    web_runner = await start_webapp(sessionmaker, pool)
    if web_runner is not None:
        logger.info("Mini App server started on %s:%s", settings.mini_app_host, settings.mini_app_port)

    await pool.start_service_monitor()
    await restore_admin_keyboards(bot)
    try:
        await dp.start_polling(bot)
    finally:
        if web_runner is not None:
            await web_runner.cleanup()
        await pool.stop_service_monitor()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
