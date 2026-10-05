"""Celery 编排入口（ENG-06 第 4 步）。

- `celery_app`：唯一 Celery 实例（`celery -A app.tasks.celery_app ...`）；
- `worker`：任务函数，只做参数解析、上下文装配与 Service 调用；
- `queue`：`GenerationQueuePort` 的 Celery 实现，Web 进程只按任务名投递；
- `heartbeat`：Worker 心跳的写入/读取（`/ready` 的 `worker_heartbeat` 探针数据源）；
- `beat`：Beat 计划任务的注册表（调度表本身冻结在 `celery_app.BEAT_SCHEDULE`）。

导入本包会加载 `app.tasks.worker`，从而把任务注册到 `celery_app`；
`app.main` 不导入本包，API 进程因此不会加载 Worker 侧执行栈。
"""

from __future__ import annotations

from app.tasks.celery_app import (
    OVERALL_PROCESSING_LIMIT_SECONDS,
    TASK_POLL_STATUS,
    TASK_RECOVERY_SCAN,
    TASK_RUN_GENERATION,
    TASK_WORKER_HEARTBEAT,
    celery_app,
)
from app.tasks.queue import CeleryGenerationQueue

__all__ = [
    "CeleryGenerationQueue",
    "OVERALL_PROCESSING_LIMIT_SECONDS",
    "TASK_POLL_STATUS",
    "TASK_RECOVERY_SCAN",
    "TASK_RUN_GENERATION",
    "TASK_WORKER_HEARTBEAT",
    "celery_app",
]

from app.tasks import worker as _worker  # noqa: E402,F401  (导入即注册任务)
