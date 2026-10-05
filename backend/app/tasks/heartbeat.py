"""Worker 心跳的写入与读取（ENG-06 第 4 步）。

`/ready` 的 `worker_heartbeat` 探针需要一个「Worker 还活着」的服务端事实
（`03-api-contract.md` 第 4.5 节，读取侧阈值 60 秒已冻结在
`app/api/health/checks.py`）。本模块只负责这份短期状态：

- 由 Beat 计划投递 `worker.heartbeat`，**在 Worker 进程里执行**，
  因此心跳新鲜同时证明「Beat 在投递、队列在通、Worker 在消费」；
- 值写入 Redis 并带 TTL，过期即视为无心跳；
- 只存时间戳：不存任务 ID、Prompt、URL 或凭据（`09-repository-boundary.md` 第 7 节）。

读取侧接线属 `app/api/health/checks.py`（本卡未授权修改），故本模块只提供实现。
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.services.ports import RedisPort
from app.tasks.celery_app import HEARTBEAT_TTL_SECONDS

__all__ = ["WORKER_HEARTBEAT_KEY", "publish_heartbeat", "read_heartbeat"]

#: 心跳键（Redis 单一用途命名空间；不进入任何对外响应）
WORKER_HEARTBEAT_KEY = "ai-avatar:v1:worker:heartbeat"


def publish_heartbeat(redis: RedisPort, *, now: datetime) -> None:
    """写入心跳时间戳，TTL 到期即自动失效。"""

    redis.set(WORKER_HEARTBEAT_KEY, now.astimezone(timezone.utc).isoformat(), ex=HEARTBEAT_TTL_SECONDS)


def read_heartbeat(redis: RedisPort) -> datetime | None:
    """读取最近一次心跳；缺失或无法解析时返回 `None`（探针按未就绪处理）。"""

    raw = redis.get(WORKER_HEARTBEAT_KEY)
    if raw is None:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
