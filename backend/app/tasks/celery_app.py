"""Celery 应用与配置（ENG-06 第 4 步）。

启动约定（`09-repository-boundary.md`，`docker/compose.yml:58,72` 已按此命令装配）：

```text
celery -A app.tasks.celery_app worker
celery -A app.tasks.celery_app beat
```

进程边界（`05-system-architecture.md` 第 3 节）：Beat 只向 Redis 投递计划任务，
只有 Worker 执行任务；Beat 与 Worker 都不直接写 PostgreSQL、对象存储或点数账务。

为什么用 `acks_late` + 关闭自动重试：

- 任务必须在处理完成后才确认，进程被杀时消息可重投（`LEAD-04` §6:126 要求终局事务
  失败可安全重投）；
- Celery 自带的 `autoretry` 不会新增 `provider_attempts` 行，也不会按第 5 节错误分类
  判定「是否可证明未接受」，因此一律关闭；传输层重试由 Generation Service 用
  `countdown` 显式投递，保证每次重试都留下独立 attempt 记录（`LEAD-04` §7、R7）。

时间限制不是账务判定：软限 240 秒对应 `04-ai-provider.md` 第 3 节的整体处理上限，
硬限 300 秒只是兜底杀进程；被杀的任务**不做任何结算**，由恢复扫描按
「超过整体时限」归类为 attempt `UNKNOWN` + 任务 `NEEDS_REVIEW`、点数保持 `RESERVED`。

未由契约冻结、本卡自行取值并需技术负责人确认的参数，集中在本模块底部常量。
"""

from __future__ import annotations

from celery import Celery

__all__ = [
    "BROKER_URL_SETTING",
    "TASK_LIFECYCLE_CLEANUP",
    "TASK_POLL_STATUS",
    "TASK_RECOVERY_SCAN",
    "TASK_RUN_GENERATION",
    "TASK_WORKER_HEARTBEAT",
    "celery_app",
]

#: 队列与短期状态都放在 Redis；本应用不启用 Celery 结果后端（`task_ignore_result`）
BROKER_URL_SETTING = "redis_url"

#: 任务名（Producer 只按名字投递，Web 进程无需导入 Worker 侧模块）
TASK_RUN_GENERATION = "generation.run"
TASK_POLL_STATUS = "generation.poll_status"
TASK_RECOVERY_SCAN = "generation.recovery_scan"
TASK_WORKER_HEARTBEAT = "worker.heartbeat"
#: ENG-09：到期媒体与分享的生命周期收敛
TASK_LIFECYCLE_CLEANUP = "lifecycle.cleanup_media"


def _broker_url() -> str:
    """从服务端配置读取 Redis URL（唯一 env 读取入口是 `app/config.py`）。"""

    from app.config import get_settings

    return get_settings().redis_url


def _new_celery() -> Celery:
    app = Celery(
        "ai_avatar",
        broker=None,
        backend=None,
        include=("app.tasks.worker",),
    )

    # `broker_url`/`result_backend` 用小写键，Celery 4/5 官方写法；显式不启用结果后端
    app.conf.update(
        broker_url=_broker_url(),
        result_backend=None,
        task_default_queue="ai-avatar-v1",
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        timezone="UTC",
        enable_utc=True,
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        task_ignore_result=True,
        worker_prefetch_multiplier=1,
        worker_send_task_events=True,
        broker_transport_options={"visibility_timeout": VISIBILITY_TIMEOUT_SECONDS},
        task_soft_time_limit=TASK_SOFT_TIME_LIMIT_SECONDS,
        task_time_limit=TASK_HARD_TIME_LIMIT_SECONDS,
        beat_schedule=BEAT_SCHEDULE,
    )
    return app


# --------------------------------------------------------------------------- #
# 本卡自行取值（契约未冻结：`LEAD-04` §12.3 明确扫描周期与投递确认参数未定）
# --------------------------------------------------------------------------- #

#: 整体处理上限（`04-ai-provider.md` 第 3 节：connect 10s / read 180s / overall 240s）
OVERALL_PROCESSING_LIMIT_SECONDS = 240

#: 软时限取整体上限，硬时限留 60 秒收尾余量
TASK_SOFT_TIME_LIMIT_SECONDS = OVERALL_PROCESSING_LIMIT_SECONDS
TASK_HARD_TIME_LIMIT_SECONDS = OVERALL_PROCESSING_LIMIT_SECONDS + 60

#: `acks_late` 下消息在确认前对 Broker 不可见的窗口，必须大于硬时限，否则会被重复投递
VISIBILITY_TIMEOUT_SECONDS = TASK_HARD_TIME_LIMIT_SECONDS + 300

#: Worker 心跳写入间隔与键 TTL（读取侧阈值 60 秒见 `app/api/health/checks.py`）
HEARTBEAT_INTERVAL_SECONDS = 15
HEARTBEAT_TTL_SECONDS = 60

#: 恢复扫描间隔与「停留过久」判定阈值（阈值 = 2 个扫描周期，避免与正常投递竞态）
RECOVERY_SCAN_INTERVAL_SECONDS = 60
RECOVERY_STALE_AFTER_SECONDS = RECOVERY_SCAN_INTERVAL_SECONDS * 2
RECOVERY_SCAN_BATCH_LIMIT = 50

#: 状态轮询间隔：整体时限内约 12 次，超过时限由恢复扫描判定
POLL_INTERVAL_SECONDS = 20

#: 生命周期清理间隔与每批上限（技术负责人 2026-10-02 裁决：每 300 秒、每批最多 50 条）
LIFECYCLE_CLEANUP_INTERVAL_SECONDS = 300
LIFECYCLE_CLEANUP_BATCH_SIZE = 50

BEAT_SCHEDULE: dict[str, dict[str, object]] = {
    "generation-recovery-scan": {
        "task": TASK_RECOVERY_SCAN,
        "schedule": RECOVERY_SCAN_INTERVAL_SECONDS,
        "options": {"expires": RECOVERY_SCAN_INTERVAL_SECONDS},
    },
    "worker-heartbeat": {
        "task": TASK_WORKER_HEARTBEAT,
        "schedule": HEARTBEAT_INTERVAL_SECONDS,
        "options": {"expires": HEARTBEAT_INTERVAL_SECONDS},
    },
    # Beat 只投递任务名；到期判定、对象删除与状态改写全部在 Worker 侧 Service 里
    "lifecycle-media-cleanup": {
        "task": TASK_LIFECYCLE_CLEANUP,
        "schedule": LIFECYCLE_CLEANUP_INTERVAL_SECONDS,
        "options": {"expires": LIFECYCLE_CLEANUP_INTERVAL_SECONDS},
    },
}


#: 模块级实例：`celery -A app.tasks.celery_app` 解析到的就是它
celery_app = _new_celery()
