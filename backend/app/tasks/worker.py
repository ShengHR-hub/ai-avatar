"""Celery 任务入口（ENG-06 第 4 步）。

职责边界（`app/tasks/README.md`、`LEAD-04` §11:188）：任务函数只做四件事——
解析参数、建立执行上下文、调用 Generation Service、按返回结果决定确认。
**不在这里写任何业务规则，也不在这里碰账务表或任务状态列。**

上下文按需惰性导入：`celery -A app.tasks.celery_app` 加载本模块时不应连数据库、
不应连接对象存储，只有真正执行任务时才装配。
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from app.tasks.celery_app import (
    LIFECYCLE_CLEANUP_BATCH_SIZE,
    OVERALL_PROCESSING_LIMIT_SECONDS,
    RECOVERY_SCAN_BATCH_LIMIT,
    RECOVERY_STALE_AFTER_SECONDS,
    TASK_LIFECYCLE_CLEANUP,
    TASK_POLL_STATUS,
    TASK_RECOVERY_SCAN,
    TASK_RUN_GENERATION,
    TASK_WORKER_HEARTBEAT,
    POLL_INTERVAL_SECONDS,
    celery_app,
)

__all__ = [
    "build_lifecycle_service",
    "build_orchestrator",
    "cleanup_media",
    "poll_generation_status",
    "recovery_scan",
    "run_generation",
    "worker_heartbeat",
]

_logger = logging.getLogger("ai_avatar.tasks.worker")


def _generation_id_of(value: str) -> uuid.UUID:
    """解析任务 ID。

    非法参数是投递侧编程错误，直接抛出让消息进入失败处理；绝不猜测或降级。
    """

    return uuid.UUID(str(value))


def build_orchestrator() -> Any:
    """装配一次执行所需的 UoW 工厂、Points Service、Provider 与存储驱动。"""

    from app.config import get_settings
    from app.db.session import get_session_factory
    from app.repositories.unit_of_work import SqlAlchemyUnitOfWork
    from app.services.generation.orchestrator import GenerationOrchestrator
    from app.services.generation.provider_factory import build_ai_provider
    from app.services.points.service import PointsService
    from app.storage import build_storage_adapter, storage_provider_label
    from app.tasks.queue import CeleryGenerationQueue

    settings = get_settings()
    storage = build_storage_adapter(
        driver=settings.storage_driver,
        endpoint=settings.storage_endpoint,
        bucket=settings.storage_bucket,
        region=settings.storage_region,
        access_key=settings.storage_access_key,
        secret_key=settings.storage_secret_key,
    )

    return GenerationOrchestrator(
        uow_factory=lambda: SqlAlchemyUnitOfWork(get_session_factory()),
        points_factory=lambda uow: PointsService(uow=uow),
        provider_factory=lambda: build_ai_provider(
            ai_provider=settings.ai_provider,
            base_url=settings.ai_base_url,
            model=settings.ai_model,
            api_key=settings.ai_api_key,
        ),
        storage=storage,
        storage_provider=storage_provider_label(settings.storage_driver),
        queue=CeleryGenerationQueue(),
        poll_interval_seconds=POLL_INTERVAL_SECONDS,
        overall_deadline_seconds=OVERALL_PROCESSING_LIMIT_SECONDS,
    )


@celery_app.task(name=TASK_RUN_GENERATION, bind=True)
def run_generation(self, generation_id: str) -> str:
    """执行一次生成：领取 → Provider 调用（事务外）→ 结果持久化与结算（同一事务）。"""

    outcome = build_orchestrator().submit(_generation_id_of(generation_id))
    _logger.info("generation_task_finished outcome=%s", outcome)
    return outcome


@celery_app.task(name=TASK_POLL_STATUS, bind=True)
def poll_generation_status(self, generation_id: str, attempt_no: int) -> str:
    """轮询一次外部状态；与 Webhook 归一到同一个对账入口。

    `attempt_no` 只用于消息可追溯：迟到的轮询由「终态单调 + attempt 行锁」兜住，
    不会重开已终结的 attempt（`04-ai-provider.md` 第 4 节）。
    """

    outcome = build_orchestrator().reconcile(generation_id=_generation_id_of(generation_id))
    _logger.info("generation_poll_finished attempt_no=%s outcome=%s", int(attempt_no), outcome)
    return outcome


@celery_app.task(name=TASK_RECOVERY_SCAN, bind=True)
def recovery_scan(self) -> dict[str, int]:
    """恢复扫描：补投丢失的投递，并把超过整体时限的处理归类为 UNKNOWN。

    投递丢失不回滚已提交账务（`LEAD-04` §6:119）；超过时限的处理只转
    `NEEDS_REVIEW + RESERVED`，绝不自动消费、自动退款或盲目重试。
    """

    counts = build_orchestrator().run_recovery_scan(
        stale_after_seconds=RECOVERY_STALE_AFTER_SECONDS,
        deadline_seconds=OVERALL_PROCESSING_LIMIT_SECONDS,
        limit=RECOVERY_SCAN_BATCH_LIMIT,
    )
    _logger.info("generation_recovery_scan %s", counts)
    return dict(counts)


@celery_app.task(name=TASK_WORKER_HEARTBEAT, bind=True)
def worker_heartbeat(self) -> None:
    """由 Beat 投递、在 Worker 内执行：写入心跳时间戳，证明队列与消费链路可用。"""

    from datetime import datetime, timezone

    from app.services.ports import get_redis_port
    from app.tasks.heartbeat import publish_heartbeat

    publish_heartbeat(get_redis_port(), now=datetime.now(timezone.utc))


def build_lifecycle_service() -> Any:
    """装配生命周期清理所需的 UoW 工厂与存储驱动（构造不触网）。

    传**工厂**：`run_once` 按阶段各开一个独立工作单元，退出即关闭 Session；
    异常会先 `rollback()` 再关闭，因此一次任务不会长期占住连接。
    """

    from app.config import get_settings
    from app.db.session import get_session_factory
    from app.repositories.unit_of_work import SqlAlchemyUnitOfWork
    from app.services.shares.lifecycle import MediaLifecycleService
    from app.storage import build_storage_adapter

    settings = get_settings()
    storage = build_storage_adapter(
        driver=settings.storage_driver,
        endpoint=settings.storage_endpoint,
        bucket=settings.storage_bucket,
        region=settings.storage_region,
        access_key=settings.storage_access_key,
        secret_key=settings.storage_secret_key,
    )
    return MediaLifecycleService(
        uow_factory=lambda: SqlAlchemyUnitOfWork(get_session_factory()),
        storage=storage,
        batch_size=LIFECYCLE_CLEANUP_BATCH_SIZE,
    )


@celery_app.task(name=TASK_LIFECYCLE_CLEANUP, bind=True)
def cleanup_media(self) -> dict[str, int]:
    """生命周期清理：到期媒体与分享的收敛。

    任务函数只做「建立上下文 → 调用 Service → 记录安全摘要」；批量上限、删除顺序、
    失败重试与可见性恢复全部在 `MediaLifecycleService` 里。
    """

    report = build_lifecycle_service().run_once()
    counts = report.as_dict()
    _logger.info("lifecycle_cleanup_task %s", counts)
    return counts
