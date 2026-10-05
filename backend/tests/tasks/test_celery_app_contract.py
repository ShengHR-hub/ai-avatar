"""Celery 应用装配契约测试（ENG-06 第 4 步）。

冻结依据：

- `docs/architecture/05-system-architecture.md` 第 3 节：进程模型
  `celery -A app.tasks.celery_app worker` / `celery -A app.tasks.celery_app beat`，
  「Beat 不直接写 PostgreSQL、MinIO 或点数账务；Worker 不绕过 Service 直接修改账务表」；
- `docs/task-cards/LEAD-04-points-and-state-machine.md` 第 6 节：提交后再投递 Celery、
  投递失败不回滚已提交账务、「事务失败整体回滚，Worker 可安全重投」——
  只有处理完成之后才确认（`acks_late`）才能做到；第 7 节：自动重试每次都要留下
  独立 `provider_attempts` 记录；第 11 节第 1 条：Task 只解析参数、建立上下文、调用 Service；
- `docs/architecture/04-ai-provider.md` 第 3 节：connect 10s / read 180s /
  **整体处理上限 240s**，超过整体时限判 `UNKNOWN`，重试由 Service 用 `countdown` 显式投递。

本模块只做配置面与注册面断言：不启动 Worker/Beat，不连接 Broker，不读写数据库。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from celery import Celery, _state
from celery.app.task import Task
from kombu.utils.imports import symbol_by_name

from app.config import get_settings, reset_settings_cache
from app.tasks import worker
from app.tasks.celery_app import (
    BEAT_SCHEDULE,
    HEARTBEAT_INTERVAL_SECONDS,
    HEARTBEAT_TTL_SECONDS,
    LIFECYCLE_CLEANUP_BATCH_SIZE,
    LIFECYCLE_CLEANUP_INTERVAL_SECONDS,
    OVERALL_PROCESSING_LIMIT_SECONDS,
    RECOVERY_SCAN_BATCH_LIMIT,
    RECOVERY_SCAN_INTERVAL_SECONDS,
    RECOVERY_STALE_AFTER_SECONDS,
    TASK_HARD_TIME_LIMIT_SECONDS,
    TASK_LIFECYCLE_CLEANUP,
    TASK_POLL_STATUS,
    TASK_RECOVERY_SCAN,
    TASK_RUN_GENERATION,
    TASK_SOFT_TIME_LIMIT_SECONDS,
    TASK_WORKER_HEARTBEAT,
    VISIBILITY_TIMEOUT_SECONDS,
)
from tests.tasks.source_scan import code_only

#: 第 3 节要求 `celery -A app.tasks.celery_app worker` 一启动就能发现的任务名
#: （ENG-09 追加 `lifecycle.cleanup_media`）
REGISTERED_TASK_NAMES: frozenset[str] = frozenset(
    {
        TASK_RUN_GENERATION,
        TASK_POLL_STATUS,
        TASK_RECOVERY_SCAN,
        TASK_WORKER_HEARTBEAT,
        TASK_LIFECYCLE_CLEANUP,
    }
)

#: 任务名 -> `app.tasks.worker` 里的任务代理（证明注册在实例上的就是这些函数）
TASK_PROXIES: dict[str, Any] = {
    TASK_RUN_GENERATION: worker.run_generation,
    TASK_POLL_STATUS: worker.poll_generation_status,
    TASK_RECOVERY_SCAN: worker.recovery_scan,
    TASK_WORKER_HEARTBEAT: worker.worker_heartbeat,
    TASK_LIFECYCLE_CLEANUP: worker.cleanup_media,
}

#: 任务名 -> 装饰器下的原函数名
TASK_FUNCTIONS: dict[str, str] = {
    TASK_RUN_GENERATION: "run_generation",
    TASK_POLL_STATUS: "poll_generation_status",
    TASK_RECOVERY_SCAN: "recovery_scan",
    TASK_WORKER_HEARTBEAT: "worker_heartbeat",
    TASK_LIFECYCLE_CLEANUP: "cleanup_media",
}

#: Celery 自己在 `Task.bind()`（`from_config` 回写）与 `_task_from_fun()` 里写进任务类的
#: 属性。它们不是本仓库声明的选项，判定「有没有开自动重试」时必须先排除。
_CELERY_OWN_TASK_ATTRS: frozenset[str] = frozenset(
    {
        "app",
        "_app",
        "name",
        "run",
        "_decorated",
        "_exec_options",
        "request_stack",
        "__bound__",
        "typing",
        "serializer",
        "rate_limit",
        "priority",
        "track_started",
        "acks_late",
        "acks_on_failure_or_timeout",
        "reject_on_worker_lost",
        "ignore_result",
        "store_eager_result",
        "store_errors_even_if_ignored",
    }
)

#: Celery 自动重试相关的全部选项名（任一出现即视为在本层开启了重试）
_AUTORETRY_OPTION_NAMES: frozenset[str] = frozenset(
    {
        "autoretry_for",
        "retry_backoff",
        "retry_backoff_max",
        "retry_jitter",
        "retry_kwargs",
        "max_retries",
        "default_retry_delay",
        "throws",
    }
)


def declared_task_options(task_class: type) -> dict[str, Any]:
    """返回任务类**由本仓库声明**的选项（类自有字典减去 Celery 注入项）。

    `Celery._task_from_fun()` 把装饰器的关键字参数原样写进生成的任务类字典，
    所以「类 `__dict__` 去掉 Celery 自己写的属性」就是本仓库声明的选项集合。
    """

    return {
        key: value
        for key, value in vars(task_class).items()
        if not key.startswith("__") and key not in _CELERY_OWN_TASK_ATTRS
    }


def executable_source(module: ModuleType) -> str:
    """取模块源码并剥掉模块/类/函数文档串所在行（保留注释与可执行代码）。

    实现与 `app/tasks/*.py` 的边界扫描共用同一个助手，见 `tests/tasks/source_scan.py`。
    """

    return code_only(Path(module.__file__).resolve().read_text(encoding="utf-8"))


@pytest.fixture
def rebuilt_celery(
    celery_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[..., Celery]]:
    """按临时环境变量重建 Celery 实例（走 `celery_app._new_celery()`）。

    `_new_celery()` 读的是 `get_settings()` 的进程级缓存，因此每次重建前必须
    `reset_settings_cache()`；它还会把新实例设成「当前 app」，用例结束后要还原
    `celery._state`，否则同进程后续测试会解析到哨兵 broker。
    """

    previous_default = _state.default_app
    previous_current = _state._tls.current_app

    def _rebuild(**env: str) -> Celery:
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        reset_settings_cache()
        return celery_module._new_celery()

    yield _rebuild

    reset_settings_cache()
    _state.set_default_app(previous_default)
    _state._set_current_app(previous_current)


# --------------------------------------------------------------------------- #
# 1. `celery -A app.tasks.celery_app worker|beat` 必须继续可用
# --------------------------------------------------------------------------- #
def test_module_exposes_exactly_one_celery_instance(celery_module: ModuleType) -> None:
    """模块与包命名空间各自只能暴露一个 Celery 实例：多于一个会让 `-A` 装配到错误的应用。"""

    import app.tasks as tasks_package

    in_module = [
        name for name, value in vars(celery_module).items() if isinstance(value, Celery)
    ]
    in_package = [
        name for name, value in vars(tasks_package).items() if isinstance(value, Celery)
    ]

    assert in_module == ["celery_app"], sorted(in_module)
    assert in_package == ["celery_app"], sorted(in_package)
    assert vars(tasks_package)["celery_app"] is vars(celery_module)["celery_app"]


def test_celery_dash_a_entry_point_resolves_to_the_instance(celery: Celery) -> None:
    """`celery -A app.tasks.celery_app` 的点分路径必须解析到被测实例本身。"""

    resolved = symbol_by_name("app.tasks.celery_app")

    assert isinstance(resolved, Celery)
    assert resolved is celery
    assert celery.main == "ai_avatar"


def test_worker_command_discovers_tasks_through_conf_imports(celery: Celery) -> None:
    """`celery worker` 不带 `-I` 时靠 include 发现任务模块（第 3 节启动约定）。"""

    assert "app.tasks.worker" in tuple(celery.conf.include or ())
    assert celery.conf.task_default_queue == "ai-avatar-v1"


def test_importing_app_tasks_registers_exactly_the_frozen_task_names(celery: Celery) -> None:
    """导入 `app.tasks` 后，全部任务名都注册在这个实例上，且就是 worker 里的函数。"""

    registered = {name for name in celery.tasks if not name.startswith("celery.")}

    assert registered == set(REGISTERED_TASK_NAMES)
    for name, proxy in TASK_PROXIES.items():
        task = celery.tasks[name]
        assert proxy.__wrapped__.__self__ is task, name
        assert task.run.__name__ == TASK_FUNCTIONS[name], name


def test_beat_schedule_holds_only_the_planned_entries(celery: Celery) -> None:
    """Beat 只计划「恢复扫描 / 心跳 / 生命周期清理」：一次性任务由投递侧触发。"""

    assert dict(celery.conf.beat_schedule) == dict(BEAT_SCHEDULE)
    assert {str(entry["task"]) for entry in BEAT_SCHEDULE.values()} == {
        TASK_RECOVERY_SCAN,
        TASK_WORKER_HEARTBEAT,
        TASK_LIFECYCLE_CLEANUP,
    }
    assert set(BEAT_SCHEDULE) == {
        "generation-recovery-scan",
        "worker-heartbeat",
        "lifecycle-media-cleanup",
    }


@pytest.mark.parametrize(
    "entry_name",
    ["generation-recovery-scan", "worker-heartbeat", "lifecycle-media-cleanup"],
)
def test_beat_entries_use_integer_second_schedules_with_expiry(entry_name: str) -> None:
    """计划项必须是「整数秒 + expires」。

    浮点秒或 `crontab` 会让投递时刻依赖调度器实现；缺少 `expires` 时 Beat 停机
    期间积压的旧消息会在恢复瞬间集中投给 Worker。
    """

    entry = BEAT_SCHEDULE[entry_name]
    schedule = entry["schedule"]
    options = entry["options"]

    assert isinstance(schedule, int) and not isinstance(schedule, bool)
    assert 0 < schedule <= 3600, schedule
    assert isinstance(options, dict), options
    assert "expires" in options
    expires = options["expires"]
    assert isinstance(expires, int) and not isinstance(expires, bool)
    assert 0 < expires <= 3600, expires
    # 过期窗口不得大于一个投递周期，否则同一计划任务的两条消息会同时在队列里
    assert expires <= schedule, entry_name


def test_beat_intervals_are_consistent_with_their_own_thresholds() -> None:
    """计划间隔与模块内阈值必须自洽，否则定时在语义上是坏的。

    - 心跳必须在一个 TTL 内至少写两次，否则键会在两次投递之间过期（`/ready` 抖动）；
    - 恢复扫描的「停留过久」阈值必须大于扫描间隔，否则会补投仍在正常投递中的任务；
    - 扫描批量上限必须是正整数（它决定单轮补投/超时判定的工作量上界）。
    """

    assert HEARTBEAT_INTERVAL_SECONDS * 2 <= HEARTBEAT_TTL_SECONDS
    assert RECOVERY_STALE_AFTER_SECONDS > RECOVERY_SCAN_INTERVAL_SECONDS
    assert isinstance(RECOVERY_SCAN_BATCH_LIMIT, int) and RECOVERY_SCAN_BATCH_LIMIT > 0
    assert BEAT_SCHEDULE["generation-recovery-scan"]["schedule"] == RECOVERY_SCAN_INTERVAL_SECONDS
    assert BEAT_SCHEDULE["worker-heartbeat"]["schedule"] == HEARTBEAT_INTERVAL_SECONDS
    # ENG-09：生命周期清理的周期与每批上限由技术负责人裁决固定（300s / 50 条）
    assert LIFECYCLE_CLEANUP_INTERVAL_SECONDS == 300
    assert LIFECYCLE_CLEANUP_BATCH_SIZE == 50
    assert (
        BEAT_SCHEDULE["lifecycle-media-cleanup"]["schedule"]
        == LIFECYCLE_CLEANUP_INTERVAL_SECONDS
    )
    assert (
        BEAT_SCHEDULE["lifecycle-media-cleanup"]["options"]["expires"]
        == LIFECYCLE_CLEANUP_INTERVAL_SECONDS
    )


# --------------------------------------------------------------------------- #
# 2. 投递 / 确认语义
# --------------------------------------------------------------------------- #
def test_delivery_and_ack_semantics_are_frozen(celery: Celery) -> None:
    """`acks_late` + `reject_on_worker_lost` + 单条预取，并且不启用结果后端。"""

    conf = celery.conf

    assert conf.task_acks_late is True
    assert conf.task_reject_on_worker_lost is True
    assert conf.worker_prefetch_multiplier == 1
    assert conf.task_ignore_result is True
    assert conf.result_backend is None
    assert type(celery.backend).__name__ == "DisabledBackend"


@pytest.mark.parametrize("name", sorted(REGISTERED_TASK_NAMES))
def test_every_task_effectively_late_acks_and_ignores_results(celery: Celery, name: str) -> None:
    """配置面为真还不够：四个任务实例上的生效值同样必须是 late-ack / 忽略结果。"""

    task = celery.tasks[name]

    assert task.acks_late is True, name
    assert task.reject_on_worker_lost is True, name
    assert task.ignore_result is True, name
    assert task.store_eager_result in (None, False), name


def test_time_limits_are_ordered_and_tied_to_the_provider_budget(celery: Celery) -> None:
    """软限 = 契约冻结的整体处理上限 240s < 硬限 < visibility_timeout。

    - 软限必须等于 `04-ai-provider.md` 第 3 节的 240s，超时判定才有统一口径；
    - 硬限必须严格大于软限，否则 `SoftTimeLimitExceeded` 来不及被捕获处理；
    - `visibility_timeout`（`acks_late` 下消息重投窗口）必须**严格大于**硬限，
      否则一条仍在合法执行中的消息会被 Broker 重投，出现同一生成并发双跑。
    """

    conf = celery.conf

    assert OVERALL_PROCESSING_LIMIT_SECONDS == 240
    assert conf.task_soft_time_limit == TASK_SOFT_TIME_LIMIT_SECONDS == 240
    assert conf.task_time_limit == TASK_HARD_TIME_LIMIT_SECONDS
    assert TASK_HARD_TIME_LIMIT_SECONDS > TASK_SOFT_TIME_LIMIT_SECONDS
    assert conf.broker_transport_options["visibility_timeout"] == VISIBILITY_TIMEOUT_SECONDS
    assert VISIBILITY_TIMEOUT_SECONDS > TASK_HARD_TIME_LIMIT_SECONDS
    # 兜底窗口自身也要合理（大于一天通常说明单位写错）
    assert VISIBILITY_TIMEOUT_SECONDS <= 24 * 3600


def test_serializers_only_accept_json(celery: Celery) -> None:
    """任务参数只有 UUID 与整数：只接受 JSON，排除 pickle/yaml 反序列化面。"""

    conf = celery.conf

    assert conf.task_serializer == "json"
    assert conf.result_serializer == "json"
    assert list(conf.accept_content) == ["json"]


def test_broker_url_is_taken_from_server_settings(rebuilt_celery: Callable[..., Celery]) -> None:
    """Broker URL 的唯一来源是 `app.config.get_settings().redis_url`。"""

    sentinel = "redis://sentinel-broker:6399/3"
    assert get_settings().redis_url != sentinel, "前提：默认值确实来自配置而不是哨兵"

    rebuilt = rebuilt_celery(REDIS_URL=sentinel)

    assert get_settings().redis_url == sentinel
    assert rebuilt.conf.broker_url == sentinel
    assert rebuilt.conf.result_backend is None
    assert rebuilt.conf.broker_transport_options["visibility_timeout"] == (
        VISIBILITY_TIMEOUT_SECONDS
    )


def test_fallback_broker_url_matches_the_compose_default(
    rebuilt_celery: Callable[..., Celery]
) -> None:
    """未设置 `REDIS_URL` 时的回退值必须与 `docker/compose.yml` 的默认值一致。"""

    rebuilt = rebuilt_celery(REDIS_URL="")

    assert rebuilt.conf.broker_url == "redis://redis:6379/0"


def test_rebuilding_does_not_mutate_the_shipped_app(
    celery: Celery, rebuilt_celery: Callable[..., Celery]
) -> None:
    """哨兵配置只落在重建出的新实例上：`celery -A` 装配的那个实例不受影响。"""

    shipped_broker = celery.conf.broker_url
    rebuilt = rebuilt_celery(REDIS_URL="redis://throwaway:6399/9")

    assert rebuilt is not celery
    assert rebuilt.conf.broker_url == "redis://throwaway:6399/9"
    assert celery.conf.broker_url == shipped_broker


def test_ai_provider_and_credentials_never_reach_the_celery_layer(
    rebuilt_celery: Callable[..., Celery]
) -> None:
    """`AI_PROVIDER` 与凭据不得参与本层任何配置：换 Provider 不能换出另一个 Broker。

    Provider 选择属于 Service 侧装配（`build_orchestrator` 把 `settings.ai_provider`
    交给 `provider_factory`），Broker/队列/时限都与 Provider 无关；同时确认凭据不会
    以配置值的形式残留在 `celery inspect conf` 之类可导出的地方。
    """

    broker = "redis://shared-broker:6379/1"
    credential = "SENTINEL-AI-KEY-must-not-appear"
    watched = (
        "broker_url",
        "broker_transport_options",
        "result_backend",
        "task_default_queue",
        "task_serializer",
        "accept_content",
        "task_acks_late",
        "task_time_limit",
        "task_soft_time_limit",
        "worker_prefetch_multiplier",
    )

    mockish = rebuilt_celery(
        REDIS_URL=broker, AI_PROVIDER="mock", AI_API_KEY="", AI_BASE_URL="", AI_MODEL=""
    )
    realish = rebuilt_celery(
        REDIS_URL=broker,
        AI_PROVIDER="some-real-vendor",
        AI_API_KEY=credential,
        AI_BASE_URL="https://provider.example.invalid/v1",
        AI_MODEL="billable-model",
    )

    for key in watched:
        assert mockish.conf[key] == realish.conf[key], key
        assert credential not in repr(realish.conf[key]), key
    assert credential not in repr(dict(realish.conf))


# --------------------------------------------------------------------------- #
# 3. Celery 自动重试必须整体关闭
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", sorted(REGISTERED_TASK_NAMES))
def test_no_task_declares_celery_autoretry_options(celery: Celery, name: str) -> None:
    """四个任务都不得声明 Celery 层自动重试选项。

    `04-ai-provider.md` 第 3 节与 `LEAD-04` 第 7 节要求每次传输层重试都由 Generation
    Service 显式投递并留下独立 `provider_attempts` 行；Celery 的 `autoretry_for`
    既不写 attempt 行、也不做「可证明未接受」判定，开着它就会对同一笔预留双重计数。
    """

    task_class = type(celery.tasks[name])

    assert not set(declared_task_options(task_class)) & _AUTORETRY_OPTION_NAMES, (
        name,
        sorted(declared_task_options(task_class)),
    )
    for option in _AUTORETRY_OPTION_NAMES:
        assert option not in vars(task_class), (name, option)
    task = celery.tasks[name]
    assert getattr(task, "autoretry_for", None) in (None, ()), name
    assert getattr(task, "retry_backoff", None) in (None, False), name


def test_max_retries_is_only_the_inherited_celery_default(celery: Celery) -> None:
    """`max_retries` 只能来自 Celery 基类默认值，不能是本仓库设置的正数。"""

    assert "max_retries" in vars(Task), "前提：3 这个默认值来自 celery.app.task.Task"

    for name in sorted(REGISTERED_TASK_NAMES):
        task_class = type(celery.tasks[name])
        assert "max_retries" not in vars(task_class), name
        assert declared_task_options(task_class) == {}, (
            name,
            sorted(declared_task_options(task_class)),
        )


def test_no_global_annotations_inject_retry_behaviour(celery: Celery) -> None:
    """`task_annotations` 能在不改任务代码的前提下注入重试策略，必须保持为空。"""

    assert celery.conf.task_annotations is None


@pytest.mark.parametrize(
    "module_name", ["__init__", "celery_app", "queue", "worker", "heartbeat"]
)
def test_task_sources_never_call_retry(module_name: str) -> None:
    """源码侧兜底：任务模块的可执行代码里不得出现 `self.retry(...)` / `autoretry_for`。

    `celery_app.py` 的模块文档串里解释了「为什么关闭 autoretry」，因此这里只扫描
    剥掉文档串之后的代码，避免把说明文字当成实现。
    """

    module = __import__(f"app.tasks.{module_name}", fromlist=["__file__"])
    source = executable_source(module)

    assert "autoretry_for" not in source, module_name
    assert "retry_backoff" not in source, module_name
    assert ".retry(" not in source, module_name
    assert "max_retries" not in source, module_name
