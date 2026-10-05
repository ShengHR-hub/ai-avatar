"""Celery 任务函数体的委托契约测试（ENG-06 第 4 步）。

`test_celery_app_contract.py` 冻结装配层、`test_queue_delivery.py` 冻结投递层，
本模块冻结**入口层**：

- `docs/task-cards/LEAD-04-points-and-state-machine.md` 第 11 节第 1/2 条：
  「Task 只解析参数、建立上下文、调用 Service」「Worker 领取时只做
  `QUEUED → PROCESSING`，不直接碰账务表」；
- `docs/architecture/05-system-architecture.md` 第 3 节：依赖方向
  `Beat → Redis → Worker → Service`（心跳这类旁路能力也不得落到数据库）；
- `docs/architecture/04-ai-provider.md` 第 3 节：整体处理上限 240s——恢复扫描必须把
  这个口径真正交给 Service；第 4 节：轮询与 Webhook 归一到同一条对账路径。

任务体用 `build_orchestrator` 记录器替身、Redis 用 `tests.services.fakes.FakeRedis`：
不启动 Worker、不连接 Broker/Redis/PostgreSQL/MinIO。
"""

from __future__ import annotations

import inspect
import logging
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any

import pytest

from app.services import ports as service_ports
from app.tasks import heartbeat as heartbeat_module
from app.tasks import worker
from app.tasks.celery_app import (
    HEARTBEAT_TTL_SECONDS,
    LIFECYCLE_CLEANUP_BATCH_SIZE,
    OVERALL_PROCESSING_LIMIT_SECONDS,
    RECOVERY_SCAN_BATCH_LIMIT,
    RECOVERY_SCAN_INTERVAL_SECONDS,
    RECOVERY_STALE_AFTER_SECONDS,
)
from app.tasks.heartbeat import WORKER_HEARTBEAT_KEY, read_heartbeat
from tests.services.fakes import FakeRedis, MutableClock

#: 确定性 ID 与结果码：断言不得依赖用例的实际运行时刻
GENERATION_ID = uuid.UUID("44f2b1a0-7c31-4e58-9a11-2b6f0c1d3e40")
POLL_GENERATION_ID = uuid.UUID("123e4567-e89b-12d3-a456-426614174000")

SUBMIT_OUTCOME = "synthetic-submit-outcome"
RECONCILE_OUTCOME = "synthetic-reconcile-outcome"
RECOVERY_COUNTS = {"reenqueued": 3, "needs_review": 1}

#: 非法 ID 样本：空串、非十六进制、长度不对、尾部多一位
MALFORMED_IDS = ("", "not-a-uuid", "44f2b1a0-7c31-4e58-9a11", str(GENERATION_ID) + "x")


class RecordingOrchestrator:
    """`GenerationOrchestrator` 三个公开入口的记录器（不含任何业务规则）。

    - `reconcile` / `run_recovery_scan` 做成关键字专用，与 Service 真实签名一致：
      任务体若改用位置参数调用会直接 `TypeError`，而不是静默通过；
    - `run_recovery_scan` 每次返回**同一个**字典对象，这样「任务体是否做了拷贝」
      是可观察事实，而不是替身自身的性质。
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.recovery_counts: dict[str, int] = dict(RECOVERY_COUNTS)

    def submit(self, generation_id: Any) -> str:
        self.calls.append(("submit", (generation_id,), {}))
        return SUBMIT_OUTCOME

    def reconcile(self, *, generation_id: Any, provider_request_id: Any = None) -> str:
        self.calls.append(
            (
                "reconcile",
                (),
                {"generation_id": generation_id, "provider_request_id": provider_request_id},
            )
        )
        return RECONCILE_OUTCOME

    def run_recovery_scan(
        self, *, stale_after_seconds: int, deadline_seconds: int, limit: int
    ) -> dict[str, int]:
        self.calls.append(
            (
                "run_recovery_scan",
                (),
                {
                    "stale_after_seconds": stale_after_seconds,
                    "deadline_seconds": deadline_seconds,
                    "limit": limit,
                },
            )
        )
        return self.recovery_counts

    @property
    def call_names(self) -> list[str]:
        return [name for name, _, _ in self.calls]

    def only_call(self, name: str) -> tuple[tuple[Any, ...], dict[str, Any]]:
        """取出该名称的唯一一次调用，返回 `(位置参数, 关键字参数)`。"""

        matched = [call for call in self.calls if call[0] == name]
        assert len(matched) == 1, matched
        return matched[0][1], matched[0][2]


class OrchestratorFactorySpy:
    """记录 `build_orchestrator()` 被调了几次：上下文必须按执行装配，不是模块级单例。"""

    def __init__(self, orchestrator: RecordingOrchestrator) -> None:
        self.orchestrator = orchestrator
        self.calls = 0

    def __call__(self) -> RecordingOrchestrator:
        self.calls += 1
        return self.orchestrator


class PublishHeartbeatSpy:
    """记录 `publish_heartbeat()` 的调用形状。"""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append((args, kwargs))


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> OrchestratorFactorySpy:
    """把 `app.tasks.worker.build_orchestrator` 换成记录器。

    任务体在**调用时**才按模块全局名解析这个函数，所以替换模块属性即生效，
    不需要碰 Celery 的注册表。
    """

    factory = OrchestratorFactorySpy(RecordingOrchestrator())
    monkeypatch.setattr(worker, "build_orchestrator", factory)
    return factory


@pytest.fixture
def redis_port(fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeRedis]:
    """把 `get_redis_port()` 换成内存实现，避免任何真实 Redis 连接。"""

    monkeypatch.setattr(service_ports, "get_redis_port", lambda: fake_redis)
    yield fake_redis


# --------------------------------------------------------------------------- #
# generation.run
# --------------------------------------------------------------------------- #
def test_run_generation_passes_a_parsed_uuid_and_returns_the_outcome(
    spy: OrchestratorFactorySpy,
) -> None:
    """任务体只做三件事：解析 ID、装配上下文、把 Service 的结论原样回传。"""

    outcome = worker.run_generation.run(str(GENERATION_ID))

    assert spy.calls == 1, "上下文按执行装配，不得做成模块级单例"
    args, kwargs = spy.orchestrator.only_call("submit")
    assert kwargs == {}
    assert len(args) == 1
    assert type(args[0]) is uuid.UUID, "必须传解析后的 UUID，而不是原始字符串"
    assert args[0] == GENERATION_ID
    assert spy.orchestrator.call_names == ["submit"], "不得额外调用别的 Service 入口"
    assert outcome == SUBMIT_OUTCOME
    assert type(outcome) is str


def test_run_generation_is_directly_callable_as_a_task(spy: OrchestratorFactorySpy) -> None:
    """`task(...)` 与 `task.run(...)` 走同一个函数体（`bind=True` 的 self 由 Celery 注入）。"""

    by_call = worker.run_generation(str(GENERATION_ID))
    by_run = worker.run_generation.run(str(GENERATION_ID))

    assert by_call == by_run == SUBMIT_OUTCOME
    assert spy.calls == 2


def test_run_generation_renormalises_an_uuid_object_input(spy: OrchestratorFactorySpy) -> None:
    """投递方偶发传 UUID 对象时也必须工作：`str()` 归一，而不是做类型检查后放行。"""

    outcome = worker.run_generation.run(GENERATION_ID)  # type: ignore[arg-type]

    args, _ = spy.orchestrator.only_call("submit")
    assert type(args[0]) is uuid.UUID
    assert args[0] == GENERATION_ID
    assert outcome == SUBMIT_OUTCOME


@pytest.mark.parametrize("malformed", MALFORMED_IDS)
def test_run_generation_rejects_a_malformed_id(
    spy: OrchestratorFactorySpy, malformed: str
) -> None:
    """非法 ID 必须抛出（进入 Celery 失败处理），不得被吞成「无事发生」。

    吞掉非法 ID 会让消息在 `acks_late` 下被正常确认，任务于是永远停在 `QUEUED`；
    抛错则消息可重投，或由恢复扫描兜住（`LEAD-04` 第 6 节）。
    """

    with pytest.raises(ValueError):
        worker.run_generation.run(malformed)

    assert spy.orchestrator.calls == [], "非法参数不得进入 Service 执行"
    # 实测顺序：`build_orchestrator().submit(_generation_id_of(...))` 先装配上下文
    # 再解析 ID，所以一次非法投递仍会付出一整轮装配（UoW 工厂/存储适配器构造）的代价。
    assert spy.calls == 1, "参数解析发生在上下文装配之后（当前实现的实际顺序）"


# --------------------------------------------------------------------------- #
# generation.poll_status
# --------------------------------------------------------------------------- #
def test_poll_generation_status_reconciles_by_keyword(
    spy: OrchestratorFactorySpy, caplog: pytest.LogCaptureFixture
) -> None:
    """轮询必须汇入 Service 的对账入口，且 `attempt_no` 只用于日志可追溯。

    `04-ai-provider.md` 第 4 节要求轮询与 Webhook 归一到同一条对账路径；把
    `attempt_no` 透传给 `reconcile` 等于让「消息投递时刻」决定对哪一次尝试，
    与「终态单调 + attempt 行锁」的裁决相冲突。
    """

    with caplog.at_level(logging.INFO, logger="ai_avatar.tasks.worker"):
        outcome = worker.poll_generation_status.run(str(POLL_GENERATION_ID), 3)

    assert spy.calls == 1
    args, kwargs = spy.orchestrator.only_call("reconcile")
    assert args == (), "generation_id 必须按关键字传递"
    assert type(kwargs["generation_id"]) is uuid.UUID
    assert kwargs["generation_id"] == POLL_GENERATION_ID
    assert kwargs["provider_request_id"] is None
    assert "attempt_no" not in kwargs
    assert outcome == RECONCILE_OUTCOME
    assert "attempt_no=3" in caplog.text
    assert RECONCILE_OUTCOME in caplog.text


@pytest.mark.parametrize("malformed", MALFORMED_IDS)
def test_poll_generation_status_rejects_a_malformed_id(
    spy: OrchestratorFactorySpy, malformed: str
) -> None:
    """轮询侧同样不得猜测或降级：非法 ID 直接抛。"""

    with pytest.raises(ValueError):
        worker.poll_generation_status.run(malformed, 1)

    assert spy.orchestrator.calls == []


def test_non_numeric_attempt_no_fails_after_the_reconcile_side_effect(
    spy: OrchestratorFactorySpy,
) -> None:
    """实测行为（不是愿望）：`attempt_no` 只在**日志**里被 `int()` 规范化。

    `app/tasks/worker.py` 的对账调用在前、`_logger.info(..., int(attempt_no), ...)` 在后，
    所以一个非数字的 `attempt_no`（只可能来自手工发布或异构生产者，本仓库的
    `CeleryGenerationQueue` 已经用 `int()` 归一过）会让任务在**副作用已经发生之后**
    才失败：`reconcile()` 已经执行完，消息随后按 `acks_late` 的失败路径处理。
    这里把它钉成事实，因为「attempt_no 只用于可追溯、不参与判定」这条契约
    只有在「它仍然能让任务失败」的前提下才值得被知道。
    """

    with pytest.raises(ValueError):
        worker.poll_generation_status.run(str(POLL_GENERATION_ID), "not-a-number")

    assert spy.orchestrator.call_names == ["reconcile"], "对账已经跑完，异常出自日志格式化"


# --------------------------------------------------------------------------- #
# generation.recovery_scan
# --------------------------------------------------------------------------- #
def test_recovery_scan_forwards_module_thresholds_and_returns_a_plain_dict(
    spy: OrchestratorFactorySpy,
) -> None:
    """恢复扫描的三个阈值全部取自模块常量，返回值必须是普通 dict。

    - `deadline_seconds` = 整体处理上限 240s（`04-ai-provider.md` 第 3 节的超时口径）；
    - `stale_after_seconds` = 2 个扫描周期，避免与正常投递竞态（`LEAD-04` 第 6 节：
      丢失的投递由恢复扫描补投，正在途中的不能被误判成丢失）。
    """

    result = worker.recovery_scan.run()

    assert spy.calls == 1
    args, kwargs = spy.orchestrator.only_call("run_recovery_scan")
    assert args == (), "阈值必须按关键字传递，避免与 Service 签名错位"
    assert kwargs == {
        "stale_after_seconds": RECOVERY_STALE_AFTER_SECONDS,
        "deadline_seconds": OVERALL_PROCESSING_LIMIT_SECONDS,
        "limit": RECOVERY_SCAN_BATCH_LIMIT,
    }
    assert kwargs["deadline_seconds"] == 240
    assert kwargs["stale_after_seconds"] == RECOVERY_SCAN_INTERVAL_SECONDS * 2
    assert kwargs["limit"] == RECOVERY_SCAN_BATCH_LIMIT > 0
    assert type(result) is dict
    assert result == RECOVERY_COUNTS


def test_recovery_scan_returns_a_copy_of_the_service_counts(
    spy: OrchestratorFactorySpy,
) -> None:
    """调用方改动返回值不得影响 Service 返回的那份字典。"""

    counts = spy.orchestrator.recovery_counts

    result = worker.recovery_scan.run()

    assert result == counts
    assert result is not counts, "任务体必须返回拷贝，不能泄漏 Service 内部状态"
    result["injected"] = 999
    assert "injected" not in counts


def test_recovery_scan_is_a_no_argument_task(spy: OrchestratorFactorySpy) -> None:
    """Beat 定时触发的扫描任务不得要求 per-task 参数（`bind=True` 的 self 不算形参）。"""

    assert list(inspect.signature(worker.recovery_scan.run).parameters) == []
    assert worker.recovery_scan.run() == RECOVERY_COUNTS
    assert spy.orchestrator.call_names == ["run_recovery_scan"]


# --------------------------------------------------------------------------- #
# ENG-09：生命周期清理任务（返修项：Worker 必须正确进入并关闭 UoW）
# --------------------------------------------------------------------------- #
class LifecycleServiceSpy:
    """记录 `build_lifecycle_service()` 的调用次数与 `run_once()` 的返回。"""

    def __init__(self, report: Any) -> None:
        self.calls = 0
        self.run_calls = 0
        self._report = report

    def __call__(self) -> "LifecycleServiceSpy":
        self.calls += 1
        return self

    def run_once(self) -> Any:
        self.run_calls += 1
        return self._report


class CleanupReportStub:
    """带 `as_dict()` 的最小报告替身（任务体必须返回普通 dict）。"""

    def __init__(self, counts: dict[str, int]) -> None:
        self._counts = dict(counts)

    def as_dict(self) -> dict[str, int]:
        return dict(self._counts)


def test_cleanup_media_assembles_the_service_per_execution_and_returns_a_plain_dict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """清理任务：每次执行都重新装配 Service，并把报告转成普通 dict 返回。

    上下文必须按执行装配（不是模块级单例），否则一次 Worker 生命周期内会复用
    可能已关闭的工作单元工厂。
    """

    spy = LifecycleServiceSpy(CleanupReportStub({"assets_expired": 2, "shares_expired": 1}))
    monkeypatch.setattr(worker, "build_lifecycle_service", spy)

    result = worker.cleanup_media.run()

    assert spy.calls == 1, "必须每次执行重新装配上下文"
    assert spy.run_calls == 1
    assert result == {"assets_expired": 2, "shares_expired": 1}
    assert isinstance(result, dict)


def test_cleanup_media_returns_a_copy_not_the_service_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """任务体返回的 dict 不得是 Service 内部对象（避免 Worker 侧被下游改写）。"""

    report = CleanupReportStub({"assets_expired": 1})
    spy = LifecycleServiceSpy(report)
    monkeypatch.setattr(worker, "build_lifecycle_service", spy)

    result = worker.cleanup_media.run()
    result["assets_expired"] = 999

    assert report.as_dict() == {"assets_expired": 1}


def test_cleanup_media_is_a_no_argument_task() -> None:
    """Beat 定时投递的任务不得要求 per-task 参数。"""

    assert list(inspect.signature(worker.cleanup_media.run).parameters) == []


def test_build_lifecycle_service_passes_a_unit_of_work_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """装配点必须传**工厂**：清理任务每阶段开一个独立工作单元并关闭它。

    这里不启动 Worker、不连数据库：只观察传给 `MediaLifecycleService` 的关键字参数
    是 `uow_factory`（可调用）而不是一个现成的 `uow`。
    """

    from app.services.shares.lifecycle import MediaLifecycleService

    captured: dict[str, Any] = {}
    original_init = MediaLifecycleService.__init__

    def recording_init(self: Any, **kwargs: Any) -> None:
        captured.update(kwargs)
        original_init(self, **kwargs)

    monkeypatch.setattr(MediaLifecycleService, "__init__", recording_init)
    service = worker.build_lifecycle_service()

    assert isinstance(service, MediaLifecycleService)
    assert "uow_factory" in captured, sorted(captured)
    assert callable(captured["uow_factory"])
    assert "uow" not in captured, "必须传工厂，不能传现成的工作单元（会长期占住 Session）"
    assert service.batch_size == LIFECYCLE_CLEANUP_BATCH_SIZE == 50


# --------------------------------------------------------------------------- #
# worker.heartbeat
# --------------------------------------------------------------------------- #
def test_worker_heartbeat_publishes_a_fresh_utc_heartbeat_without_an_orchestrator(
    redis_port: FakeRedis, spy: OrchestratorFactorySpy
) -> None:
    """心跳任务用 Redis 端口写时间戳，并且完全不装配执行上下文。

    第 3 节要求 Beat/Worker 的旁路能力不触碰数据库：心跳每 15 秒一次，若它去建
    UoW/引擎，`/ready` 的 `worker_heartbeat` 探针就被 PostgreSQL 绑死了。
    """

    before = datetime.now(timezone.utc)
    assert worker.worker_heartbeat.run() is None
    after = datetime.now(timezone.utc)

    assert spy.calls == 0, "心跳不得装配 Orchestrator（不建 UoW/Provider/Storage）"
    assert spy.orchestrator.calls == []
    assert redis_port.keys() == (WORKER_HEARTBEAT_KEY,)

    stored = read_heartbeat(redis_port)
    assert stored is not None
    assert stored.tzinfo is timezone.utc
    # 写的是「现在」，不是任何固定/伪造时间
    assert before.replace(tzinfo=None) <= stored.replace(tzinfo=None) <= after.replace(tzinfo=None)


def test_worker_heartbeat_calls_the_publisher_with_the_redis_port(
    redis_port: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """心跳必须经由 `app.tasks.heartbeat.publish_heartbeat` 写入（读写共用一处实现）。"""

    publisher = PublishHeartbeatSpy()
    monkeypatch.setattr(heartbeat_module, "publish_heartbeat", publisher)

    worker.worker_heartbeat.run()

    assert len(publisher.calls) == 1
    args, kwargs = publisher.calls[0]
    assert args == (redis_port,), "Redis 端口必须按位置参数传给心跳实现"
    assert set(kwargs) == {"now"}
    moment: datetime = kwargs["now"]
    assert moment.tzinfo is timezone.utc, "心跳时间戳必须是带时区的 UTC"
    assert abs((datetime.now(timezone.utc) - moment).total_seconds()) < 120


def test_worker_heartbeat_is_governed_by_the_ttl_constant(
    redis_port: FakeRedis, clock: MutableClock
) -> None:
    """心跳必须带 TTL 写入：TTL 内可读，到期即读不到（探针按未就绪处理）。"""

    worker.worker_heartbeat.run()

    assert redis_port.ttl(WORKER_HEARTBEAT_KEY) == HEARTBEAT_TTL_SECONDS
    assert read_heartbeat(redis_port) is not None
    clock.advance(HEARTBEAT_TTL_SECONDS - 1)
    assert read_heartbeat(redis_port) is not None
    clock.advance(1)
    assert read_heartbeat(redis_port) is None
