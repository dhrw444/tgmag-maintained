"""数据库引擎与会话工厂。

迁移不在进程启动时自动跑 —— 生产部署要能在失败时看到 alembic 的报错，
而不是被吞进一个 traceback 里。init_db 只做一件事：确认迁移已经跑过，
版本对得上。版本不对就退出，不让服务在半迁移的库上跑。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

# pool_pre_ping 让连接在取出前先探活。长连接被中间设备或数据库端的
# idle timeout 掐掉后，没有这个设置会在第一次查询时才报错，错误位置离
# 真正的原因很远。
engine = create_async_engine(settings.database_url, pool_pre_ping=True)
# expire_on_commit=False：提交后对象仍可读。这个项目大量在 commit 之后
# 继续访问 ORM 属性（例如写审计日志时读 account.id），保持 True 会触发
# 隐式 refresh，在异步环境下表现为难以定位的额外往返。
sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

# 与 alembic 的 head 必须一致。新增迁移时同步改这里，
# 否则老进程连上新库会被自己的版本检查拦住。
EXPECTED_DB_REVISION = "0009_temp_mail_messages"


async def init_db() -> None:
    """确认 alembic 迁移已跑到 head，否则抛 RuntimeError。

    表不存在和版本落后都归到同一个异常里，因为对部署者来说处理方式相同：
    跑 alembic upgrade head。
    """
    async with engine.connect() as conn:
        try:
            revision = await conn.scalar(text("SELECT version_num FROM alembic_version"))
        except Exception as exc:
            raise RuntimeError("数据库尚未初始化，请先执行 alembic upgrade head") from exc
    if revision != EXPECTED_DB_REVISION:
        raise RuntimeError(
            f"数据库迁移版本为 {revision!r}，期望 {EXPECTED_DB_REVISION!r}；请执行 alembic upgrade head"
        )


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI 风格的会话依赖。调用方负责 commit，这里只保证关闭。"""
    async with sessionmaker() as session:
        yield session
