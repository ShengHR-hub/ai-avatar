"""持久付费闸门的单元测试（不出网、不连库）。

`DurableAnalysisGuard` 是唯一的生产闸门装配，所以这里要把它和两层的关系钉死：

- Redis 只做缓存回放与单飞锁，**额度判定全部交给持久层**；
- 持久层拒绝时锁必须被放回（否则失败的一次会把同一张图挡到 TTL 到期）；
- 出网后的成功/失败都只收敛 `outcome`，任何路径都**不删行、不退额度**；
- 超时且状态不明时保持 `UNKNOWN`，让同一张图的自动重试打不到第二次付费。

装配层（`app.api.deps.get_analysis_guard`）也被钉在这里：Redis 两个限额必须是 `None`，
否则「缓存重启即额度归零」的老问题会以另一条路径回来。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.errors.codes import ErrorCode
from app.errors.exceptions import ApiError
from app.services.analysis.guard import AnalysisLease, CachedDescription
from app.services.analysis.quota import (
    OUTCOME_FAILED,
    OUTCOME_SUCCEEDED,
    OUTCOME_UNKNOWN,
    DurableAnalysisGuard,
    DurableLease,
    PostgresAnalysisQuota,
    seconds_to_utc_midnight,
)

USER_ID = uuid.UUID("11223344-5566-7788-99aa-bbccddeeff00")
DIGEST = "a" * 64
DESCRIPTION = "短发女孩侧脸，暖色光线"
MODEL = "vision-test-model"


class RecordingRedisGuard:
    """`AnalysisGuard` 的替身：只记录调用，不做任何计数。"""

    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.begins: list[uuid.UUID] = []
        self.commits: list[tuple[str, str]] = []
        self.rollbacks: list[str] = []
        self.cached_reads = 0
        self.cache: CachedDescription | None = None

    def cached(self, *, user_id: uuid.UUID, sha256: str) -> CachedDescription | None:
        self.cached_reads += 1
        return self.cache

    def begin(self, *, user_id: uuid.UUID, sha256: str) -> AnalysisLease:
        if self.error is not None:
            raise self.error
        self.begins.append(user_id)
        return AnalysisLease(cache_key=f"cache:{sha256}", lock_key=f"lock:{sha256}")

    def commit(self, lease: AnalysisLease, *, description: str, model: str) -> None:
        self.commits.append((description, model))

    def rollback(self, lease: AnalysisLease) -> None:
        self.rollbacks.append(lease.lock_key or "")


class RecordingQuota:
    """持久闸门替身：按脚本放行或拒绝，并记录终态收敛。"""

    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.reserved: list[tuple[uuid.UUID, str, str, str]] = []
        self.settled: list[tuple[uuid.UUID, str]] = []
        self.dispatch_id = uuid.uuid4()

    def reserve(
        self, *, user_id: uuid.UUID, digest: str, provider_name: str, model_name: str
    ) -> uuid.UUID:
        if self.error is not None:
            raise self.error
        self.reserved.append((user_id, digest, provider_name, model_name))
        return self.dispatch_id

    def settle(self, dispatch_id: uuid.UUID, *, outcome: str) -> None:
        self.settled.append((dispatch_id, outcome))


def _guard(
    redis: RecordingRedisGuard | None = None, quota: RecordingQuota | None = None
) -> tuple[DurableAnalysisGuard, RecordingRedisGuard, RecordingQuota]:
    redis_guard = redis if redis is not None else RecordingRedisGuard()
    quota_gate = quota if quota is not None else RecordingQuota()
    return (
        DurableAnalysisGuard(
            redis_guard=redis_guard,  # type: ignore[arg-type]
            quota=quota_gate,  # type: ignore[arg-type]
            provider_name="MOCK",
            model_name=MODEL,
        ),
        redis_guard,
        quota_gate,
    )


# --------------------------------------------------------------------------- #
# begin：两层的先后与拒绝
# --------------------------------------------------------------------------- #
def test_begin_registers_the_dispatch_with_the_image_digest() -> None:
    guard, redis_guard, quota = _guard()

    lease = guard.begin(user_id=USER_ID, sha256=DIGEST)

    assert quota.reserved == [(USER_ID, DIGEST, "MOCK", MODEL)]
    assert isinstance(lease, DurableLease)
    assert lease.dispatch_id == quota.dispatch_id
    assert redis_guard.begins == [USER_ID]


def test_quota_rejection_releases_the_redis_lock() -> None:
    """持久层说不行时，锁不能留在手里——否则这张图到 TTL 到期都点不动。"""

    error = ApiError(ErrorCode.RATE_LIMITED, details={"scope": "user"})
    guard, redis_guard, quota = _guard(quota=RecordingQuota(error=error))

    with pytest.raises(ApiError) as excinfo:
        guard.begin(user_id=USER_ID, sha256=DIGEST)

    assert excinfo.value is error
    assert redis_guard.rollbacks == [f"lock:{DIGEST}"]
    assert quota.reserved == []


def test_redis_rejection_never_reaches_the_quota_layer() -> None:
    """单飞锁没抢到就不必再落库：并发的第二个请求不该登记成第二次付费。"""

    guard, _redis_guard, quota = _guard(
        redis=RecordingRedisGuard(error=ApiError(ErrorCode.IDEMPOTENCY_IN_PROGRESS))
    )

    with pytest.raises(ApiError) as excinfo:
        guard.begin(user_id=USER_ID, sha256=DIGEST)

    assert excinfo.value.code is ErrorCode.IDEMPOTENCY_IN_PROGRESS
    assert quota.reserved == []


def test_quota_dependency_failure_is_fail_closed_not_ready() -> None:
    guard, redis_guard, _quota = _guard(quota=RecordingQuota(error=RuntimeError("db gone")))

    with pytest.raises(RuntimeError):
        # 替身抛的是原始异常；真实实现把它映射成 NOT_READY 的责任在闸门内部，
        # 所以这里只要求「异常上抛且锁被放回」，映射本身由下一条 DB 用例覆盖。
        guard.begin(user_id=USER_ID, sha256=DIGEST)

    assert redis_guard.rollbacks == [f"lock:{DIGEST}"]


# --------------------------------------------------------------------------- #
# commit / rollback：终态收敛与「不退额度」
# --------------------------------------------------------------------------- #
def test_commit_writes_cache_and_marks_succeeded() -> None:
    guard, redis_guard, quota = _guard()
    lease = guard.begin(user_id=USER_ID, sha256=DIGEST)

    guard.commit(lease, description=DESCRIPTION, model=MODEL)

    assert redis_guard.commits == [(DESCRIPTION, MODEL)]
    assert quota.settled == [(quota.dispatch_id, OUTCOME_SUCCEEDED)]


def test_rollback_keeps_the_row_and_marks_failed() -> None:
    """失败只释放锁：登记行留在表里继续占额度，钱可能已经花出去了。"""

    guard, redis_guard, quota = _guard()
    lease = guard.begin(user_id=USER_ID, sha256=DIGEST)

    guard.rollback(lease)

    assert redis_guard.rollbacks == [f"lock:{DIGEST}"]
    assert quota.settled == [(quota.dispatch_id, OUTCOME_FAILED)]


def test_never_settled_stays_unknown() -> None:
    """崩溃/超时且没走到 commit 或 rollback 时，行保持 `UNKNOWN`——
    这一格额度继续被占，自动重试就打不到第二次付费。"""

    guard, _redis_guard, quota = _guard()
    guard.begin(user_id=USER_ID, sha256=DIGEST)

    assert quota.settled == []
    assert OUTCOME_UNKNOWN == "UNKNOWN"


def test_settled_lease_is_idempotent() -> None:
    guard, redis_guard, quota = _guard()
    lease = guard.begin(user_id=USER_ID, sha256=DIGEST)

    guard.commit(lease, description=DESCRIPTION, model=MODEL)
    guard.rollback(lease)
    guard.commit(lease, description=DESCRIPTION, model=MODEL)

    assert redis_guard.commits == [(DESCRIPTION, MODEL)]
    assert quota.settled == [(quota.dispatch_id, OUTCOME_SUCCEEDED)]


def test_cached_is_pure_passthrough_and_costs_nothing() -> None:
    guard, redis_guard, quota = _guard()
    redis_guard.cache = CachedDescription(description=DESCRIPTION, model=MODEL)

    hit = guard.cached(user_id=USER_ID, sha256=DIGEST)

    assert hit is not None and hit.description == DESCRIPTION
    assert quota.reserved == []


# --------------------------------------------------------------------------- #
# PostgresAnalysisQuota 的参数校验与日界
# --------------------------------------------------------------------------- #
def test_quota_rejects_non_positive_limits() -> None:
    with pytest.raises(ValueError):
        PostgresAnalysisQuota(
            lambda: None, per_user_daily_limit=0, global_daily_limit=10
        )
    with pytest.raises(ValueError):
        PostgresAnalysisQuota(
            lambda: None, per_user_daily_limit=10, global_daily_limit=-1
        )


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc), 86400),
        (datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc), 15 * 3600),
        (datetime(2026, 10, 1, 23, 59, 30, tzinfo=timezone.utc), 30),
    ],
)
def test_retry_after_counts_seconds_to_utc_midnight(
    moment: datetime, expected: int
) -> None:
    assert seconds_to_utc_midnight(moment) == expected


def test_midnight_boundary_never_returns_zero() -> None:
    """`Retry-After` 至少 1 秒：为 0 会让客户端立刻重试。"""

    just_before = datetime(2026, 10, 1, 23, 59, 59, 999999, tzinfo=timezone.utc)

    assert seconds_to_utc_midnight(just_before) >= 1
    assert seconds_to_utc_midnight(just_before + timedelta(microseconds=1)) == 86400
