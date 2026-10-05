"""可注入的依赖就绪检查（ENG-02 第 3 步；ENG-06 修复批次接线真实探针）。

契约依据：`03-api-contract.md` 第 4.5 节（`/api/v1/health/ready` 检查
PostgreSQL、Redis、对象存储、Worker 心跳，`200`/`503`）与第 7 节（`NOT_READY`）。

设计要点：

- **默认探针都指向真依赖**：PostgreSQL 走 `SELECT 1`，Redis 走连通性读，
  对象存储走桶级可达性，Worker 心跳读 Redis 里那份时间戳，成本配置读
  `generation_cost_configs` 的激活价格（新环境只迁移不定价时这里是唯一能发现缺口的地方）；
  占位实现会让 `/ready` 永远返回 503，看起来「安全」实则从未验证过任何一条链路；
- **构造不触网**：探针都是惰性闭包，只有真正执行检查时才建立连接，因此导入本模块
  或装配依赖集合都不会连库、不会连 Redis/MinIO；
- **检查函数全部可注入**：路由经 `get_dependency_checks` 依赖获取，测试可用
  `app.dependency_overrides` 或替换各 provider 入口，不需要改路由；
- **SDK 不进业务层**：对象存储的桶级能力由驱动自身提供（`bucket_available()`），
  本层只经 `app.storage.probe_object_storage` 调用端口面/能力探测，`app/services/**`
  里没有 `minio`/`redis` 的导入（`09-repository-boundary.md` 第 4 节）；
- **探针异常一律视为未就绪**，且异常文本不进入响应（可能包含连接串等敏感信息）；
- Worker 心跳阈值固定 **60 秒**（技术负责人 2026-09-26 裁决）。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from sqlalchemy import text

__all__ = [
    "DEPENDENCY_NAMES",
    "REDIS_PROBE_KEY",
    "WORKER_HEARTBEAT_THRESHOLD_SECONDS",
    "CheckResult",
    "DependencyCheck",
    "HeartbeatReader",
    "Probe",
    "default_dependency_checks",
    "get_dependency_checks",
    "make_check",
    "make_cost_config_check",
    "make_object_storage_probe",
    "make_postgresql_probe",
    "make_redis_probe",
    "make_worker_heartbeat_check",
    "worker_heartbeat_result",
]

#: Worker 心跳新鲜度阈值（秒），契约未规定数值，由技术负责人裁决固定为 60 秒
WORKER_HEARTBEAT_THRESHOLD_SECONDS = 60

#: Redis 连通性探针用的键：只证明一次往返，不承载任何业务数据
REDIS_PROBE_KEY = "ai-avatar:v1:health:redis"

#: `/ready` 必须覆盖的依赖名称（契约第 4.5 节 + 点数成本配置初始化自检）
DEPENDENCY_NAMES: tuple[str, ...] = (
    "postgresql",
    "redis",
    "object_storage",
    "worker_heartbeat",
    "generation_costs",
)


class Probe(Protocol):
    """依赖探针：返回 `True` 表示就绪；抛异常视为未就绪。"""

    def __call__(self) -> bool: ...


class HeartbeatReader(Protocol):
    """Worker 心跳读取器：返回最近一次心跳时间；`None` 表示读取不到。"""

    def __call__(self) -> datetime | None: ...


@dataclass(frozen=True, slots=True)
class CheckResult:
    """单个依赖的检查结果。`detail` 必须是安全、非敏感的描述。"""

    name: str
    ready: bool
    detail: str | None = None


DependencyCheck = Callable[[], CheckResult]


def worker_heartbeat_result(
    *,
    last_beat_at: datetime | None,
    now: datetime,
    threshold_seconds: int = WORKER_HEARTBEAT_THRESHOLD_SECONDS,
) -> CheckResult:
    """Worker 心跳新鲜度判定（纯函数）。

    - 读取不到心跳 → 未就绪；
    - 心跳年龄 `> threshold_seconds` → 未就绪；
    - 心跳年龄 `<= threshold_seconds`（含边界 60 秒）→ 就绪；
    - 无时区的时间按 UTC 处理；负年龄（时钟回拨）按就绪处理。
    """

    name = "worker_heartbeat"
    if last_beat_at is None:
        return CheckResult(name, False, "no worker heartbeat available")

    if last_beat_at.tzinfo is None:
        last_beat_at = last_beat_at.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    age_seconds = (now - last_beat_at).total_seconds()
    if age_seconds > threshold_seconds:
        return CheckResult(
            name,
            False,
            f"heartbeat stale: {int(age_seconds)}s > {threshold_seconds}s",
        )
    return CheckResult(name, True)


def make_check(name: str, probe: Probe | None, *, owner: str) -> DependencyCheck:
    """把探针包装成检查函数；`probe=None` 表示尚未接线。"""

    def _check() -> CheckResult:
        if probe is None:
            return CheckResult(name, False, f"probe not wired yet (owner: {owner})")
        try:
            ready = bool(probe())
        except Exception:  # noqa: BLE001 - 探针异常一律视为未就绪，且不回显异常文本
            return CheckResult(name, False, "probe raised an error")
        return CheckResult(name, ready, None if ready else "probe reported not ready")

    return _check


def make_worker_heartbeat_check(
    reader: HeartbeatReader | None,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    threshold_seconds: int = WORKER_HEARTBEAT_THRESHOLD_SECONDS,
) -> DependencyCheck:
    """构造 Worker 心跳检查；`reader=None` 表示心跳来源尚未接线。"""

    def _check() -> CheckResult:
        if reader is None:
            return CheckResult(
                "worker_heartbeat",
                False,
                "heartbeat reader not wired yet (owner: ENG-06)",
            )
        try:
            last_beat_at = reader()
        except Exception:  # noqa: BLE001
            return CheckResult("worker_heartbeat", False, "heartbeat reader raised an error")
        return worker_heartbeat_result(
            last_beat_at=last_beat_at, now=now(), threshold_seconds=threshold_seconds
        )

    return _check


# --------------------------------------------------------------------------- #
# 真实依赖入口
#
# 各 provider 都写成「调用时才解析」的模块级函数：`/ready` 的装配因此不触网，
# 而测试可以逐个替换入口来证明接线，不必改探针逻辑本身。
# --------------------------------------------------------------------------- #
def _postgresql_engine() -> object:
    """默认数据库引擎（`get_engine` 是进程级单例，第一次用才建连接池）。"""

    from app.db.session import get_engine

    return get_engine()


def _redis_port() -> object:
    """Redis 端口（惰性连接）。"""

    from app.services.ports import get_redis_port

    return get_redis_port()


def _storage_adapter() -> object:
    """对象存储驱动（按 `STORAGE_ENDPOINT` 等配置装配；构造不发起网络请求）。"""

    from app.api.deps import get_storage_adapter

    return get_storage_adapter()


def _heartbeat_timestamp() -> datetime | None:
    """Worker 最近一次心跳；读不到即为 `None`（键名与解析只有心跳模块一个出处）。"""

    from app.tasks.heartbeat import read_heartbeat

    return read_heartbeat(_redis_port())


def make_redis_probe(port_factory: Callable[[], object] | None = None) -> Probe:
    """Redis 连通性探针。

    优先用 `ping()`（`RedisClientAdapter` 早就提供，且**不在**冻结 `RedisPort` 的
    协议面上，因此这里按能力探测）；没有 `ping` 的实现退回一次读操作——能返回就说明
    服务在线，键不存在是正常状态而不是故障。任何连接故障都以异常上抛，由 `make_check`
    归为未就绪。
    """

    def _probe() -> bool:
        # provider 在调用时解析：装配检查集合本身不得顺手连上 Redis
        port = port_factory() if port_factory is not None else _redis_port()
        ping = getattr(port, "ping", None)
        if callable(ping):
            return bool(ping())
        port.get(REDIS_PROBE_KEY)  # type: ignore[attr-defined]
        return True

    return _probe


def make_object_storage_probe(
    adapter_factory: Callable[[], object] | None = None,
) -> Probe:
    """对象存储可达性探针。

    必须做**桶级**检查：驱动把 `NoSuchBucket` 也归一成 `ObjectNotFound`，只用对象级
    `exists()` 的话「桶不存在」会被读成「可达」，`/ready` 就永远发现不了这类配置错误。
    SDK 的导入仍然留在驱动里，本层只调端口/能力探测。
    """

    from app.storage import probe_object_storage

    def _probe() -> bool:
        adapter = (
            adapter_factory() if adapter_factory is not None else _storage_adapter()
        )
        return probe_object_storage(adapter)  # type: ignore[arg-type]

    return _probe


def make_postgresql_probe(
    engine_factory: Callable[[], object] | None = None,
) -> Probe:
    """PostgreSQL 真实探针：执行 `SELECT 1`（连接由 `get_engine()` 惰性给出）。"""

    def _probe() -> bool:
        engine = (
            engine_factory() if engine_factory is not None else _postgresql_engine()
        )
        with engine.connect() as connection:  # type: ignore[attr-defined]
            connection.execute(text("SELECT 1"))
        return True

    return _probe


def _cost_config_probe() -> tuple[str, ...]:
    """返回**缺少激活点数价格**的模式集合；空元组表示当前配置下都能定到价。

    判定与创建任务时同一套取值规则（复用 `find_active`），因此不会出现
    「健康检查绿了但生成仍报缺配置」。
    """

    from app.bootstrap.cost_configs import expected_cost_modes, missing_cost_modes
    from app.config import get_settings
    from app.db.session import get_session_factory
    from app.repositories.generation import SqlAlchemyGenerationCostConfigRepository
    from app.services.generation.provider_factory import provider_identity

    settings = get_settings()
    _provider, model_name = provider_identity(
        ai_provider=settings.ai_provider, ai_model=settings.ai_model
    )
    modes = expected_cost_modes(enable_reference_image=settings.enable_reference_image)
    with get_session_factory()() as session:
        return missing_cost_modes(
            SqlAlchemyGenerationCostConfigRepository(session),
            modes=modes,
            model_name=model_name,
            at=datetime.now(timezone.utc),
        )


def make_cost_config_check(probe: Callable[[], Sequence[str]] | None = None) -> DependencyCheck:
    """点数成本配置自检：新环境只迁移、不定价时这张表是空的。

    为什么放进 `/ready`，而不只是等用户提交时报错：创建任务会拿到 `NOT_READY`（503），
    但那是一次一次地失败；`/ready` 提前红一次就把「部署少了一步」变成运维能直接看见、
    直接修的信号（初始化命令见 `app.bootstrap.cost_configs`）。探针异常一律视为未就绪，
    且异常文本不回显（可能含连接串）。
    """

    name = "generation_costs"

    def _check() -> CheckResult:
        probe_fn = probe if probe is not None else _cost_config_probe
        try:
            missing = tuple(probe_fn())
        except Exception:  # noqa: BLE001 - 未就绪即可，不回显异常文本
            return CheckResult(name, False, "cost config probe raised an error")
        if missing:
            return CheckResult(
                name, False, "missing active point prices for: " + ", ".join(missing)
            )
        return CheckResult(name, True)

    return _check


def default_dependency_checks(
    *,
    heartbeat_reader: HeartbeatReader | None = None,
) -> tuple[DependencyCheck, ...]:
    """默认检查集合：顺序与 `DEPENDENCY_NAMES` 一致，五项全部接真依赖。

    构造本身不建立任何连接——探针是闭包，只有被调用时才访问依赖。
    """

    return (
        make_check("postgresql", make_postgresql_probe(), owner="ENG-01"),
        make_check("redis", make_redis_probe(), owner="ENG-06"),
        make_check("object_storage", make_object_storage_probe(), owner="ENG-05"),
        make_worker_heartbeat_check(heartbeat_reader or _heartbeat_timestamp),
        make_cost_config_check(),
    )


def get_dependency_checks() -> tuple[DependencyCheck, ...]:
    """FastAPI 依赖：返回默认检查集合。

    测试或后续卡片可通过 `app.dependency_overrides[get_dependency_checks]` 替换，
    不需要改动路由。
    """

    return default_dependency_checks()
