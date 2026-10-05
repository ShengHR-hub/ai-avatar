"""把每日付费闸门落到 PostgreSQL 上（Redis 只做加速层）。

问题（本轮收口项）：原先的额度计数只在 Redis 里（`guard.py` 的 `INCR`）。Redis 是缓存：
重启、`FLUSHALL`、内存压力驱逐之后计数归零，用户当天额度也跟着归零，**而钱已经花出去了**。
所以这里补一层以 PostgreSQL 为事实来源的闸门，Redis 继续负责它擅长且只能负责的部分：
缓存回放与单飞锁。

三道保证各自怎么成立：

1. **同一用户/同一图片并发不重复付费**：`analysis_dispatches` 的
   `uq_..._user_id_usage_day_digest` 由数据库裁决；先取「全局日锁 → 用户日锁」
   把「按日计数 → 判额 → 插行」串成一次事务，两个进程同时到也只能有一个插进去。
2. **Redis 丢失后额度不会归零**：判额读的是表里的行数，与缓存无关。
3. **超时且执行状态不明时不自动重试、不突破上限**：登记发生在出网**之前**，
   默认 `outcome='UNKNOWN'`；调用没有明确成功就永远保持 UNKNOWN，这行继续占额度，
   同一张图当天再点会撞唯一索引。结算写回失败也只记日志、不改闸门状态——
   宁可让用户少用一次，也不让脚本把余额刷穿。

错误码口径（**不新增码**）：超限复用 `RATE_LIMITED`（429），`details.scope`
取 `user` / `global` / `image`；闸门自己出错（数据库不可用等）一律 fail-closed 到
`NOT_READY`（503），沿用 `guard.py` 已经确立的「看不到额度状态就不出网」。

图片摘要（sha256 十六进制）是这张表里唯一的"内容相关"字段：它不可逆推图片，
也不是描述，因此允许长期留存。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Protocol, runtime_checkable

from app.errors.codes import ErrorCode
from app.errors.exceptions import ApiError
from app.repositories.analysis import (
    DispatchDraft,
    DuplicateAnalysisDispatch,
    SqlAlchemyAnalysisDispatchRepository,
)
from app.services.analysis.guard import (
    AnalysisGuard,
    AnalysisLease,
    CachedDescription,
)

__all__ = [
    "AnalysisQuota",
    "DurableAnalysisGuard",
    "PostgresAnalysisQuota",
    "seconds_to_utc_midnight",
]

_LOGGER = logging.getLogger("ai_avatar.analysis.quota")

#: 登记行的三种终态（与 `analysis_dispatches.outcome` 的 CHECK 一致）
OUTCOME_UNKNOWN = "UNKNOWN"
OUTCOME_SUCCEEDED = "SUCCEEDED"
OUTCOME_FAILED = "FAILED"


def seconds_to_utc_midnight(now: datetime) -> int:
    """到下一个 UTC 零点的秒数（`Retry-After` 与日界口径同源）。"""

    moment = now.astimezone(timezone.utc)
    midnight = (moment + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return max(1, int((midnight - moment).total_seconds()))


@runtime_checkable
class AnalysisQuota(Protocol):
    """持久额度闸门（与具体存储解耦，便于装配与测试）。"""

    def reserve(
        self,
        *,
        user_id: uuid.UUID,
        digest: str,
        provider_name: str,
        model_name: str,
    ) -> uuid.UUID:
        """占下一次付费名额并返回登记行 ID；拒绝时抛 `ApiError`。"""
        ...

    def settle(self, dispatch_id: uuid.UUID, *, outcome: str) -> None:
        """把登记行收敛到终态；**不退还额度**。"""
        ...


class PostgresAnalysisQuota:
    """`AnalysisQuota` 的 PostgreSQL 实现。

    每次 `reserve` 用一个**独立、短**的事务：名额必须在出网之前就已持久化，
    所以不能把它挂在请求级工作单元上（那次事务要等响应结束才提交，
    进程在出网后、提交前崩溃就会丢掉这笔支出）。
    """

    def __init__(
        self,
        session_factory: Callable[[], Any],
        *,
        per_user_daily_limit: int,
        global_daily_limit: int,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if per_user_daily_limit <= 0 or global_daily_limit <= 0:
            raise ValueError("每日限额必须是正整数。")
        self._session_factory = session_factory
        self._per_user = int(per_user_daily_limit)
        self._global = int(global_daily_limit)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def reserve(
        self,
        *,
        user_id: uuid.UUID,
        digest: str,
        provider_name: str,
        model_name: str,
    ) -> uuid.UUID:
        moment = self._clock().astimezone(timezone.utc)
        usage_day = moment.date()
        session = self._session_factory()
        try:
            with session:
                repository = SqlAlchemyAnalysisDispatchRepository(session)
                # 固定顺序：全局日锁 → 用户日锁（反了会与另一个请求互相等待）
                repository.lock_day(usage_day=usage_day)
                repository.lock_user_day(user_id=user_id, usage_day=usage_day)
                self._require_capacity(repository, user_id=user_id, usage_day=usage_day)
                try:
                    row = repository.record(
                        DispatchDraft(
                            user_id=user_id,
                            usage_day=usage_day,
                            digest=digest,
                            provider_name=provider_name,
                            model_name=model_name,
                            # 与 `usage_day` 同源：两列必须来自同一个时间点，
                            # 否则 UTC 日界附近会写出被 CHECK 拒绝的半套记录
                            created_at=moment,
                        )
                    )
                except DuplicateAnalysisDispatch as error:
                    raise _rate_limited(
                        scope="image", retry_after=seconds_to_utc_midnight(moment)
                    ) from error
                # **必须显式提交**：`with session:` 只保证退出时关闭会话，
                # 未提交的事务会被回滚。Repository 按项目约定不自行提交
                # （`06-development-standard.md` 第 2 节），事务边界在这里。
                # 漏掉这一行的后果是静默的：登记行消失、额度永不累积、同一张图
                # 每天可以无限次付费出网——真实 PostgreSQL 测试会立刻抓到。
                # 咨询锁是事务级的，提交即释放，所以「计数 → 判额 → 插行」整段
                # 仍在锁的保护下串行执行。
                session.commit()
                dispatch_id = row.id
        except ApiError:
            raise
        except Exception as error:  # noqa: BLE001 - 闸门看不见就不出网
            _LOGGER.warning(
                "analysis quota reserve failed user=%s dependency=%s",
                user_id,
                type(error).__name__,
            )
            raise ApiError(
                ErrorCode.NOT_READY, details={"dependency": type(error).__name__}
            ) from error
        return dispatch_id

    def settle(self, dispatch_id: uuid.UUID, *, outcome: str) -> None:
        try:
            session = self._session_factory()
            with session:
                SqlAlchemyAnalysisDispatchRepository(session).set_outcome(
                    dispatch_id, outcome=outcome
                )
                # 同上：终态收敛也要显式提交，否则每次结算都白写
                session.commit()
        except Exception as error:  # noqa: BLE001 - 结算写回失败不改变闸门结论
            # 行仍留在 UNKNOWN：它继续占额度，这正是我们想要的保守方向。
            _LOGGER.warning(
                "analysis quota settle failed dispatch=%s target=%s dependency=%s",
                dispatch_id,
                outcome,
                type(error).__name__,
            )

    # ------------------------------------------------------------------ 内部
    def _require_capacity(
        self,
        repository: SqlAlchemyAnalysisDispatchRepository,
        *,
        user_id: uuid.UUID,
        usage_day: date,
    ) -> None:
        retry_after = seconds_to_utc_midnight(self._clock().astimezone(timezone.utc))
        # `>=`：已经登记过的每一次出网都算用掉一格，先看已用量再决定放不放行。
        if repository.count_for_user(user_id=user_id, usage_day=usage_day) >= self._per_user:
            raise _rate_limited(scope="user", retry_after=retry_after)
        if repository.count_for_day(usage_day=usage_day) >= self._global:
            raise _rate_limited(scope="global", retry_after=retry_after)


def _rate_limited(*, scope: str, retry_after: int) -> ApiError:
    return ApiError(
        ErrorCode.RATE_LIMITED,
        details={"scope": scope},
        headers={"Retry-After": str(retry_after)},
    )


@dataclass(slots=True)
class DurableLease:
    """一次请求占到的两层资源：Redis 租约 + 持久登记行。"""

    redis: AnalysisLease
    dispatch_id: uuid.UUID | None = None
    settled: bool = field(default=False)


class DurableAnalysisGuard:
    """`AnalysisGuard` 的生产实现：Redis 做加速，PostgreSQL 做费用裁决。

    装配时 Redis 的两个限额传 `None`（只做缓存与单飞锁），每日配额由
    `PostgresAnalysisQuota` 判。两层不会互相漂：Redis 不再计数，
    也就不存在「持久层拒绝、缓存层却已扣了一格」的退还问题。

    顺序仍然是先 Redis 后持久层：单飞锁便宜，能给同一张图的第二次点击一个明确的
    `IDEMPOTENCY_IN_PROGRESS`（409），而真正的付费裁决只在锁拿到之后才落库。
    """

    def __init__(
        self,
        *,
        redis_guard: AnalysisGuard,
        quota: AnalysisQuota,
        provider_name: str,
        model_name: str,
    ) -> None:
        self._redis = redis_guard
        self._quota = quota
        self._provider_name = provider_name
        self._model_name = model_name

    def cached(self, *, user_id: uuid.UUID, sha256: str) -> CachedDescription | None:
        return self._redis.cached(user_id=user_id, sha256=sha256)

    def begin(self, *, user_id: uuid.UUID, sha256: str) -> DurableLease:
        redis_lease = self._redis.begin(user_id=user_id, sha256=sha256)
        try:
            dispatch_id = self._quota.reserve(
                user_id=user_id,
                digest=sha256,
                provider_name=self._provider_name,
                model_name=self._model_name,
            )
        except Exception:
            # 这次根本没出网：把刚抢到的单飞锁放回去，错误原样上抛
            self._redis.rollback(redis_lease)
            raise
        return DurableLease(redis=redis_lease, dispatch_id=dispatch_id)

    def commit(self, lease: Any, *, description: str, model: str) -> None:
        if not isinstance(lease, DurableLease) or lease.settled:
            return
        lease.settled = True
        self._redis.commit(lease.redis, description=description, model=model)
        if lease.dispatch_id is not None:
            self._quota.settle(lease.dispatch_id, outcome=OUTCOME_SUCCEEDED)

    def rollback(self, lease: Any) -> None:
        """失败：释放单飞锁，但**保留**持久登记行的额度占用。

        行不删、也不写 `SUCCEEDED`，只把终态收敛成 `FAILED`：调用可能已经被供应商受理
        （超时尤其如此），这笔支出按「已经花掉」处理才是安全方向。
        """

        if not isinstance(lease, DurableLease) or lease.settled:
            return
        lease.settled = True
        self._redis.rollback(lease.redis)
        if lease.dispatch_id is not None:
            self._quota.settle(lease.dispatch_id, outcome=OUTCOME_FAILED)
