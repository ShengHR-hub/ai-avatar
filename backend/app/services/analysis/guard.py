"""参考图分析的付费闸门。

一次「看图写描述」= 一次真实出网的付费调用。没有闸门时，三条路都会把同一笔钱花两遍
或花到失控，所以本模块各自对应一条：

1. **缓存回放**——同一 `(用户, 图片内容摘要)` 在 TTL 内直接复用上一次的成功描述，
   不再出网。用户对着同一张图连点两次、或者换个标签页回来再点，都应当是免费且一致的；
2. **单飞锁**——同一张图并发提交时只允许一次出网。抢不到锁的一方拿到
   `IDEMPOTENCY_IN_PROGRESS`（409）而不是第二次付费调用；
3. **每日配额**——每用户与全局各一条按 UTC 日历日计的配额，超限拿到 `RATE_LIMITED`（429）。
   计数用原子 `INCR` 先占后判：并发请求不可能各自读到「还剩 0」然后双双放行。
   **这一层现在可以被关掉**（两个限额传 `None`）：Redis 是缓存，重启或 `FLUSHALL`
   之后计数归零而钱已经花掉，所以生产装配把每日配额交给
   `analysis_dispatches` 表（见 `app.services.analysis.quota`），本类只留缓存与单飞锁。
   关掉计数后，第 2 道锁的性质也要看清：Redis 丢失时锁会失效，此时并发裁决
   由持久层的唯一索引承担——这才是「闸门不只有一个」。

两条口径是刻意选择的，不要当成疏漏改回去：

- **配额数的是「出网尝试次数」，不是成功次数**。一旦请求送出，无论供应商最终返回什么，
  这笔钱都已经可能花掉了（本供应商没有幂等键，超时也无法证明未受理，见
  `docs/handoff/2026-09-27-current-handoff.md` 第 3.4 节）。失败就退还额度等于允许
  一个报错的脚本把余额刷光；
- **闸门本身出错一律拦住**（fail-closed 到 `NOT_READY`/503）。读不到额度状态时继续出网，
  等于在最不确定成本的那一刻放弃成本控制权。

缓存的是**描述文本**而不是图片：图片继续走 `purpose=REFERENCE` 资产的既有 7 天生命周期，
本模块不新增存储路径、不落数据库、不加迁移。描述文本可能带用户私人信息，因此
只在 Redis 里活 TTL 这么久，不写日志、不写库。

依赖形状与 `RedisReplayGuard` 一致：注入的是**独立** Redis 客户端
（需要原子 `SET ... NX EX` 与 `INCR`，都不在冻结 `RedisPort` 的表面上），
本模块不导入 `redis` SDK。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from app.errors.codes import ErrorCode
from app.errors.exceptions import ApiError

__all__ = [
    "CACHE_TTL_SECONDS",
    "LOCK_TTL_SECONDS",
    "NAMESPACE",
    "AnalysisGuard",
    "AnalysisLease",
    "CachedDescription",
    "RedisAnalysisGuard",
]

#: Redis 键前缀（与 `RedisReplayGuard` 的命名空间风格一致）
NAMESPACE = "ai-avatar:v1:analysis"

#: 描述缓存存活时间：6 小时。够长——用户当天反复改同一段描述不必再付费；
#: 够短——描述里若含私人信息，不会在 Redis 里长期停留。
CACHE_TTL_SECONDS = 6 * 60 * 60

#: 单飞锁存活时间。必须明显大于视觉调用的总超时
#: （`quanzil_vision.VISION_READ_TIMEOUT_SECONDS` 45s + connect 10s），
#: 否则锁先过期，第二个请求会在第一次仍可能出网时再打一次。
#: 但也不能太长：持有者进程崩掉时，锁过期是唯一的恢复路径。
LOCK_TTL_SECONDS = 150


@dataclass(frozen=True, slots=True)
class CachedDescription:
    """一次缓存命中。"""

    description: str
    model: str


@dataclass(slots=True)
class AnalysisLease:
    """本次请求占到的额度与锁。

    守卫实例按请求构造，因此「谁占的」不需要额外的进程内登记表：抢不到锁的一方
    根本拿不到带 `lock_key` 的租约，也就绝不会把赢家的锁抹掉。
    """

    #: 成功后把描述写回这个键（与锁键同源，`commit` 不必再拿 user_id/sha256）
    cache_key: str = ""
    lock_key: str | None = None
    #: 已经占下的计数键（回滚时不动它们——见模块文档「配额数出网尝试」）
    counter_keys: tuple[str, ...] = field(default=())
    settled: bool = False


@runtime_checkable
class AnalysisGuard(Protocol):
    """付费调用的三道闸门。"""

    def cached(self, *, user_id: UUID, sha256: str) -> CachedDescription | None:
        """命中返回上次成功的描述；未命中返回 `None`。"""
        ...

    def begin(self, *, user_id: UUID, sha256: str) -> AnalysisLease:
        """抢单飞锁并占下当日额度；超限或已有在途请求时抛 `ApiError`。"""
        ...

    def commit(self, lease: AnalysisLease, *, description: str, model: str) -> None:
        """成功后写缓存并释放锁。"""
        ...

    def rollback(self, lease: AnalysisLease) -> None:
        """失败后只释放锁，**不退还额度**。"""
        ...


class RedisAnalysisGuard:
    """`AnalysisGuard` 的 Redis 实现。

    需要的客户端形状：`set(name, value, *, ex=None, nx=False)` / `get(name)` /
    `delete(*names)` / `incr(name)` / `expire(name, seconds)`——与 `redis.Redis`
    的同名方法一致，因此装配时直接注入独立连接。

    **两个限额可以传 `None`**，表示「这一层不再做每日计数」：付费闸门的权威来源已经
    移到 PostgreSQL 的 `analysis_dispatches`（见 `quota.py`），因为 Redis 是缓存，
    重启或 `FLUSHALL` 之后计数归零而钱已经花掉。传 `None` 时本实现只保留
    缓存回放与单飞锁——两者都是纯加速，不承担费用裁决。
    """

    def __init__(
        self,
        client: Any,
        *,
        per_user_daily_limit: int | None,
        global_daily_limit: int | None,
        cache_ttl_seconds: int = CACHE_TTL_SECONDS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        for limit in (per_user_daily_limit, global_daily_limit):
            if limit is not None and limit <= 0:
                raise ValueError("每日限额必须是正整数或 None。")
        self._client = client
        self._per_user = None if per_user_daily_limit is None else int(per_user_daily_limit)
        self._global = None if global_daily_limit is None else int(global_daily_limit)
        self._cache_ttl = int(cache_ttl_seconds)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    # ------------------------------------------------------------------ 端口
    def cached(self, *, user_id: UUID, sha256: str) -> CachedDescription | None:
        raw = self._command(self._client.get, self._cache_name(user_id, sha256))
        if raw is None:
            return None
        payload = _load_description(str(raw))
        if payload is None:
            # 缓存内容不可信就当作未命中，绝不把半截文本回给用户
            return None
        return CachedDescription(description=payload[0], model=payload[1])

    def begin(self, *, user_id: UUID, sha256: str) -> AnalysisLease:
        cache_name = self._cache_name(user_id, sha256)
        lock_name = f"{cache_name}:lock"
        # 一次原子占用，而不是「先 GET 再 SET」：两个并发请求会双双读到「没有」
        occupied = self._command(
            self._client.set, lock_name, "processing", ex=LOCK_TTL_SECONDS, nx=True
        )
        if not occupied:
            raise ApiError(ErrorCode.IDEMPOTENCY_IN_PROGRESS)

        lease = AnalysisLease(cache_key=cache_name, lock_key=lock_name)
        if self._per_user is not None or self._global is not None:
            try:
                if self._per_user is not None:
                    self._reserve(
                        lease, self._user_counter_name(user_id), self._per_user, "user"
                    )
                if self._global is not None:
                    self._reserve(lease, self._global_counter_name(), self._global, "global")
            except ApiError:
                # 额度不够时把刚抢到的锁还回去，否则这次失败会一直挡住同一张图到 TTL 到期
                self._release_lock(lease)
                raise
        return lease

    def commit(self, lease: AnalysisLease, *, description: str, model: str) -> None:
        if lease.settled:
            return
        lease.settled = True
        payload = json.dumps({"description": description, "model": model})
        self._command(self._client.set, lease.cache_key, payload, ex=self._cache_ttl)
        self._release_lock(lease)

    def rollback(self, lease: AnalysisLease) -> None:
        if lease.settled:
            return
        lease.settled = True
        self._release_lock(lease)

    # ------------------------------------------------------------------ 内部
    def _reserve(self, lease: AnalysisLease, name: str, limit: int, scope: str) -> None:
        """原子占一格；占超了就拒绝（多占的那一格不再回退，见模块文档）。"""

        count = int(self._command(self._client.incr, name))
        lease.counter_keys = (*lease.counter_keys, name)
        if count == 1:
            # 新建的计数器要在 UTC 日界自己清掉，否则昨天的占用会永久挡住今天
            self._command(self._client.expire, name, self._seconds_to_utc_midnight())
        if count > limit:
            raise ApiError(
                ErrorCode.RATE_LIMITED,
                details={"scope": scope},
                headers={"Retry-After": str(self._seconds_to_utc_midnight())},
            )

    def _release_lock(self, lease: AnalysisLease) -> None:
        if lease.lock_key is None:
            return
        self._command(self._client.delete, lease.lock_key)
        lease.lock_key = None

    def _cache_name(self, user_id: UUID, sha256: str) -> str:
        return f"{NAMESPACE}:desc:{user_id}:{sha256}"

    def _user_counter_name(self, user_id: UUID) -> str:
        return f"{NAMESPACE}:day:user:{user_id}:{self._day()}"

    def _global_counter_name(self) -> str:
        return f"{NAMESPACE}:day:global:{self._day()}"

    def _day(self) -> str:
        return self._clock().astimezone(timezone.utc).strftime("%Y%m%d")

    def _seconds_to_utc_midnight(self) -> int:
        now = self._clock().astimezone(timezone.utc)
        midnight = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return max(1, int((midnight - now).total_seconds()))

    def _command(self, action: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """所有 Redis 命令都走这里：任何客户端异常都不允许变成「闸门没看见」。"""

        try:
            return action(*args, **kwargs)
        except ApiError:
            raise
        except Exception as error:  # noqa: BLE001 - 只取类型名，异常文本可能含连接信息
            raise ApiError(
                ErrorCode.NOT_READY,
                details={"dependency": type(error).__name__},
            ) from error


def _load_description(raw: str) -> tuple[str, str] | None:
    """把缓存值读成 `(描述, 型号)`；结构不符即视为未命中。"""

    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    description = payload.get("description")
    model = payload.get("model")
    if not isinstance(description, str) or not description.strip():
        return None
    if not isinstance(model, str) or not model.strip():
        return None
    return description, model
