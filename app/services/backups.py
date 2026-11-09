"""PostgreSQL 逻辑备份。

用 pg_dump 而不是在 Python 里拼 SQL：custom 格式能并行恢复、支持按表恢复，
而且 pg_dump 会处理好 schema 细节（序列、约束、注释），自己拼很容易漏。

密码通过 PGPASSWORD 环境变量传给子进程，不出现在命令行 —— 命令行参数
在 /proc/<pid>/cmdline 里对同机其他用户可读。dump 文件权限强制 0600，
里面是全部加密字段和账号数据的完整副本。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

from app.config import settings


def create_database_backup(prefix: str = "manual") -> Path:
    """生成一份 custom 格式的备份，返回文件路径。失败时删掉半成品并抛异常。

    prefix 只用来给文件名加区分度（manual / scheduled 等），
    时间戳保证同一前缀多次备份不会互相覆盖。
    超时 300 秒：大库 dump 会慢，但不设上限会让调用方永久挂住。
    """
    parsed = urlparse(settings.sync_database_url)
    if parsed.scheme not in {"postgresql", "postgres"} or not parsed.path:
        raise RuntimeError("DATABASE_URL 不是有效的 PostgreSQL 连接串")

    backup_dir = settings.backup_dir.resolve()
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    path = backup_dir / f"{prefix}_{timestamp}.dump"

    command = [
        "pg_dump",
        "--format=custom",
        # --no-password：没有密码时不交互等待，直接失败。服务进程没有 tty，
        # 交互等待会变成静默挂死。
        "--no-password",
        "--file",
        str(path),
        "--dbname",
        unquote(parsed.path.lstrip("/")),
    ]
    if parsed.hostname:
        command.extend(["--host", parsed.hostname])
    if parsed.port:
        command.extend(["--port", str(parsed.port)])
    if parsed.username:
        command.extend(["--username", unquote(parsed.username)])

    env = os.environ.copy()
    if parsed.password:
        env["PGPASSWORD"] = unquote(parsed.password)
    try:
        result = subprocess.run(
            command,
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError((result.stderr or "pg_dump 执行失败").strip()[:1000])
        path.chmod(0o600)
        return path
    except Exception:
        # 失败时留下的是不完整的 dump，恢复它会得到半个库。
        # 宁可删掉让人重新跑，也不要让后续脚本误把它当成有效备份。
        path.unlink(missing_ok=True)
        raise


async def create_database_backup_async(prefix: str = "manual") -> Path:
    """create_database_backup 的异步包装。

    pg_dump 是阻塞子进程调用，直接在事件循环里跑会卡住所有协程，
    所以走线程池。
    """
    return await asyncio.to_thread(create_database_backup, prefix)
