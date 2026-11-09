"""操作节流。

限流是"每 per_seconds 秒内最多 max_actions 次"，窗口在第一次放行时开始计时。
这是固定窗口而不是滑动窗口 —— 固定窗口在窗口边界会短暂允许两倍速率，
但对 Telegram 的操作频率来说这点误差无关紧要，而实现简单到不会出错。

jitter 是每次放行后额外随机等待，用来打散多个账号同时操作的时间点。
批量操作里如果 N 个账号同一毫秒发请求，很容易被判定为自动化行为。
"""

from __future__ import annotations

import asyncio
import random

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import RateLimit


def validate_rate_values(
    max_actions: int,
    per_seconds: int,
    jitter_min: int,
    jitter_max: int,
) -> tuple[int, int, int, int]:
    """校验并原样返回四个参数，不合法抛 ValueError。

    返回入参而不是 None 是为了让调用方能写成
    `max_actions, ... = validate_rate_values(...)`，把校验和取值合成一步。
    上界 3600 秒的 jitter 已经是"一小时随机延迟"，再大没有实际意义。
    """
    if not 1 <= max_actions <= 1000:
        raise ValueError("max_actions 必须在 1 到 1000 之间")
    if not 1 <= per_seconds <= 86400:
        raise ValueError("per_seconds 必须在 1 到 86400 之间")
    if jitter_min < 0 or jitter_max < jitter_min or jitter_max > 3600:
        raise ValueError("jitter 必须满足 0 <= min <= max <= 3600")
    return max_actions, per_seconds, jitter_min, jitter_max


async def get_rate(session: AsyncSession, scope: str = "batch") -> RateLimit:
    """读某个 scope 的限流配置。

    库里没有时返回一个未入库的默认值对象，不自动创建 ——
    自动创建会让"读配置"这个动作产生写事务，在只读场景（例如展示当前设置）
    下不应该改库。真正要落库的地方是 bootstrap_defaults。
    """
    rate = await session.scalar(select(RateLimit).where(RateLimit.scope == scope))
    if rate:
        return rate
    return RateLimit(
        scope=scope,
        max_actions=settings.default_rate_max_actions,
        per_seconds=settings.default_rate_per_seconds,
        jitter_min=settings.default_jitter_min,
        jitter_max=settings.default_jitter_max,
    )


class RateGate:
    """单个操作序列的节流闸门。

    一个实例对应一条批量任务，不要跨任务共享 —— 计数是实例级的，
    共享会让两个任务互相挤占额度。不是线程安全的，只在单个事件循环里用。
    """

    def __init__(self, rate: RateLimit):
        """窗口起点在构造时就取好；调用方必须在事件循环内创建实例。"""
        self.rate = rate
        self._count = 0
        self._window_started = asyncio.get_event_loop().time()

    async def wait(self) -> None:
        """在需要时阻塞，直到本次操作可以放行。

        放行后一定会加上 jitter 延迟，所以调用方不需要自己再 sleep。
        """
        now = asyncio.get_event_loop().time()
        elapsed = now - self._window_started
        if elapsed >= self.rate.per_seconds:
            self._window_started = now
            self._count = 0
        if self._count >= self.rate.max_actions:
            # 本窗口额度用尽，等到窗口结束再开新窗口。
            await asyncio.sleep(max(0, self.rate.per_seconds - elapsed))
            self._window_started = asyncio.get_event_loop().time()
            self._count = 0
        self._count += 1
        if self.rate.jitter_max > 0:
            await asyncio.sleep(random.uniform(self.rate.jitter_min, self.rate.jitter_max))
