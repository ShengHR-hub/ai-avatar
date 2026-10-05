"""`GenerationQueuePort` 的 Celery 实现（ENG-06 第 4 步）。

只按**任务名字**投递（`send_task`），因此 Web 进程不需要导入 Worker 侧模块，
也就不会在 API 进程里加载 SQLAlchemy/Provider/Storage 的执行栈。

投递失败**不回滚已提交的账务**（`LEAD-04` §6:119）：返回 `False` 并记安全日志，
丢失的投递由 Beat 的恢复扫描补投。这里不抛异常、不重试、不落库。
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from app.tasks.celery_app import (
    TASK_POLL_STATUS,
    TASK_RUN_GENERATION,
    celery_app,
)

__all__ = ["CeleryGenerationQueue"]

_logger = logging.getLogger("ai_avatar.tasks.queue")


class CeleryGenerationQueue:
    """把「排队」这件事收敛成一个可注入端口，便于测试与替换。"""

    def __init__(self, app: Any = celery_app) -> None:
        self._app = app

    def enqueue_generation(self, generation_id: uuid.UUID, *, countdown_seconds: int = 0) -> bool:
        """投递一次生成执行；`countdown_seconds` 用于退避重试。"""

        return self._send(TASK_RUN_GENERATION, (str(generation_id),), countdown=countdown_seconds)

    def enqueue_status_poll(
        self, generation_id: uuid.UUID, *, attempt_no: int, countdown_seconds: int
    ) -> bool:
        """投递一次状态轮询（与 Webhook 归一到同一条对账路径）。"""

        return self._send(
            TASK_POLL_STATUS,
            (str(generation_id), int(attempt_no)),
            countdown=countdown_seconds,
        )

    def _send(self, task_name: str, args: tuple[Any, ...], *, countdown: int) -> bool:
        try:
            options: dict[str, Any] = {"args": args}
            if countdown > 0:
                options["countdown"] = countdown
            self._app.send_task(task_name, **options)
            return True
        except Exception as exc:  # noqa: BLE001 - 投递失败必须被吞掉，只记安全摘要
            # 摘要只含任务名与异常类型：不得记参数以外的任何内容（Prompt、URL、凭据）
            _logger.warning(
                "celery_delivery_failed task=%s error=%s", task_name, type(exc).__name__
            )
            return False
