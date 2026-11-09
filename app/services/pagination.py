"""账号列表分页与系统账号上限。

分页参数放常量而不是配置项：页大小改动会牵动 Bot 的翻页回调数据
（回调里带的是页码），做成可配置会让同一个键盘在不同部署上行为不同。

ACCOUNT_CAPACITY_LOCK_ID 是 PostgreSQL 事务级 advisory lock 的键。
加账号时必须先拿这个锁再查总数，否则并发两次登录会一起看到"未达上限"
然后双双插入，实际超过 MAX_SYSTEM_ACCOUNTS。这个常量是全局唯一约定，
不要改成随机值 —— 各进程必须用同一个数字才互斥。
"""

from __future__ import annotations

ACCOUNT_PAGE_SIZE = 10
MAX_SYSTEM_ACCOUNTS = 50
ACCOUNT_CAPACITY_LOCK_ID = 84502117


def account_page_window(total: int, requested_page: int) -> tuple[int, int, int]:
    """把请求页码夹到有效范围内，返回 (页码, 总页数, 数据库 offset)。

    页码从 1 开始。总数 0 时总页数返回 1（而不是 0），这样前端永远有
    "第 1 页"可显示，不需要为空列表单独写分支。
    """
    pages = (total + ACCOUNT_PAGE_SIZE - 1) // ACCOUNT_PAGE_SIZE
    page = min(requested_page, max(pages, 1))
    return page, pages, (page - 1) * ACCOUNT_PAGE_SIZE


def require_account_capacity(total: int) -> None:
    """超过上限时抛 ValueError。

    调用方必须在已持有 ACCOUNT_CAPACITY_LOCK_ID 的事务里调用它，
    单独调用只能挡住串行场景。
    """
    if total >= MAX_SYSTEM_ACCOUNTS:
        raise ValueError(
            f"系统账号数量已达到上限（{MAX_SYSTEM_ACCOUNTS} 个），请先移除不再使用的账号"
        )
