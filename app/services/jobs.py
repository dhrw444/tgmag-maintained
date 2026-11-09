"""批量任务（Job / JobItem）的登记与收尾。

三个函数都不 commit，理由同 audit：任务状态要和它产生的业务变更同事务。
create_job 之后必须 flush，否则调用方拿不到 job.id，而 add_job_item
需要它做外键。

任务本身不在这里执行 —— 这里只记录"谁在跑什么、结果如何"，
执行逻辑在 app/tg/batch_ops.py。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Job, JobItem


async def create_job(session: AsyncSession, job_type: str, params: dict[str, Any]) -> Job:
    """建一条 running 状态的任务并 flush 出 id。

    直接置 running 而不是 pending：调用这个函数的地方都是"马上就要跑"，
    中间不存在排队等待的窗口。如果将来要做队列，这里才是改状态机的地方。
    """
    job = Job(type=job_type, status="running", params_json=params, started_at=datetime.now(UTC))
    session.add(job)
    await session.flush()
    return job


async def add_job_item(
    session: AsyncSession,
    job: Job,
    account_id: int | None,
    target_ref: str | None,
    status: str,
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    """登记任务里的单条明细（一个账号对一个目标的一次操作）。

    account_id 和 target_ref 都允许为空：前者用于账号级操作，
    后者用于不针对具体目标的操作（例如只改资料）。
    """
    session.add(
        JobItem(
            job_id=job.id,
            account_id=account_id,
            target_ref=target_ref,
            status=status,
            result_json=result,
            error=error,
        )
    )


async def finish_job(session: AsyncSession, job: Job, status: str, error: str | None = None) -> None:
    """给任务收尾。status 由调用方决定（success / partial / failed）。"""
    job.status = status
    job.error = error
    job.finished_at = datetime.now(UTC)
