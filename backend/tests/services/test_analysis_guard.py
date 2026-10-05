"""`RedisAnalysisGuard` 的付费闸门语义（「参考图片找灵感」）。

覆盖：缓存回放与其 TTL、并发只放行一次、每日配额（用户级与全局级）、
配额按 UTC 日界重置、**失败不退还额度**、闸门自身出错时一律拦住（fail-closed）、
竞态失败的一方不得抹掉赢家的占用。

全部用内存 Redis 替身，不启动 Redis。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.errors.codes import ErrorCode
from app.errors.exceptions import ApiError
from app.services.analysis.guard import (
    CACHE_TTL_SECONDS,
    LOCK_TTL_SECONDS,
    AnalysisLease,
    RedisAnalysisGuard,
)
from tests.services.fakes import SetNxRedisClient

CLOCK_START = datetime(2026, 9, 30, 9, 0, tzinfo=timezone.utc)
USER_ID = uuid.UUID("11223344-5566-7788-99aa-bbccddeeff00")
OTHER_USER = uuid.UUID("99223344-5566-7788-99aa-bbccddeeff00")
DIGEST = "a" * 64


class Clock:
    def __init__(self, start: datetime = CLOCK_START) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


def make_guard(
    clock: Clock | None = None,
    *,
    per_user: int = 3,
    global_limit: int = 100,
    client: object | None = None,
) -> tuple[RedisAnalysisGuard, Clock, SetNxRedisClient]:
    tick = clock or Clock()
    redis = client if client is not None else SetNxRedisClient(clock=tick)
    guard = RedisAnalysisGuard(
        redis,
        per_user_daily_limit=per_user,
        global_daily_limit=global_limit,
        cache_ttl_seconds=CACHE_TTL_SECONDS,
        clock=tick,
    )
    return guard, tick, redis


def begin(guard: RedisAnalysisGuard, *, user: uuid.UUID = USER_ID, digest: str = DIGEST):
    return guard.begin(user_id=user, sha256=digest)


def commit(
    guard: RedisAnalysisGuard, lease: AnalysisLease, text: str = "一段画面描述"
) -> None:
    guard.commit(lease, description=text, model="vision-test-1")


def cached(guard: RedisAnalysisGuard, *, user: uuid.UUID = USER_ID, digest: str = DIGEST):
    return guard.cached(user_id=user, sha256=digest)


# ------------------------------------------------------------------ 缓存回放
def test_cache_is_empty_before_the_first_success() -> None:
    guard, _tick, _redis = make_guard()

    assert cached(guard) is None


def test_commit_then_cached_replays_the_same_description_without_a_second_call() -> None:
    guard, _tick, _redis = make_guard()

    commit(guard, begin(guard))

    hit = cached(guard)
    assert hit is not None
    assert hit.description == "一段画面描述"
    assert hit.model == "vision-test-1"


def test_cached_description_expires_with_the_ttl() -> None:
    guard, tick, _redis = make_guard()
    commit(guard, begin(guard))

    tick.advance(timedelta(seconds=CACHE_TTL_SECONDS - 1))
    assert cached(guard) is not None

    tick.advance(timedelta(seconds=2))
    assert cached(guard) is None


def test_cache_is_per_user_so_two_users_never_share_a_description() -> None:
    guard, _tick, _redis = make_guard()
    commit(guard, begin(guard))

    assert cached(guard, user=OTHER_USER) is None


def test_corrupt_cache_payload_is_treated_as_a_miss() -> None:
    """缓存值读不懂就当没命中：绝不把半截文本回给用户。"""

    tick = Clock()
    redis = SetNxRedisClient(clock=tick)
    redis.set(f"ai-avatar:v1:analysis:desc:{USER_ID}:{DIGEST}", "{not json", ex=60)
    guard, _clock, _client = make_guard(tick, client=redis)

    assert cached(guard) is None


# ------------------------------------------------------------------ 单飞锁
def test_second_concurrent_request_on_the_same_image_is_told_to_wait() -> None:
    guard, _tick, _redis = make_guard()
    begin(guard)

    with pytest.raises(ApiError) as excinfo:
        begin(guard)
    assert excinfo.value.code is ErrorCode.IDEMPOTENCY_IN_PROGRESS
    assert excinfo.value.status_code == 409


def test_losing_the_race_does_not_release_the_winners_lock() -> None:
    """抢不到锁的一方绝不能把赢家的占用抹掉——否则第三个人会插进来再付一次费。"""

    guard, _tick, _redis = make_guard()
    winner = begin(guard)

    with pytest.raises(ApiError):
        begin(guard)
    # 手里没有占用的人（例如竞态失败后被误传进来的空租约）不得删任何东西，
    # 也不得抛错——它只是没有权限，而不是系统坏了。
    guard.rollback(AnalysisLease())

    with pytest.raises(ApiError) as excinfo:
        begin(guard)
    assert excinfo.value.code is ErrorCode.IDEMPOTENCY_IN_PROGRESS

    # 赢家仍然可以正常收口
    commit(guard, winner)
    assert cached(guard) is not None


def test_rollback_after_failure_frees_the_lock_for_a_retry() -> None:
    guard, _tick, _redis = make_guard()
    lease = begin(guard)
    guard.rollback(lease)

    assert begin(guard) is not None


def test_lock_expires_if_the_holder_died_mid_call() -> None:
    """持有者进程崩掉时，锁到期是唯一恢复路径，所以 TTL 必须有上界。"""

    guard, tick, _redis = make_guard()
    begin(guard)

    tick.advance(timedelta(seconds=LOCK_TTL_SECONDS - 1))
    with pytest.raises(ApiError):
        begin(guard)

    tick.advance(timedelta(seconds=2))
    assert begin(guard) is not None


def test_commit_is_idempotent_and_settles_the_lease_once() -> None:
    guard, _tick, _redis = make_guard()
    lease = begin(guard)

    commit(guard, lease)
    commit(guard, lease)  # 第二次不得再写一遍、也不得误删别人的锁

    assert cached(guard) is not None


# ------------------------------------------------------------------ 每日配额
def test_user_daily_limit_blocks_after_the_last_slot_with_retry_after() -> None:
    guard, _tick, _redis = make_guard(per_user=2, global_limit=100)

    guard.rollback(begin(guard))
    guard.rollback(begin(guard))

    with pytest.raises(ApiError) as excinfo:
        begin(guard)
    assert excinfo.value.code is ErrorCode.RATE_LIMITED
    assert excinfo.value.status_code == 429
    assert excinfo.value.details["scope"] == "user"
    # Retry-After 必须正好指向下一个 UTC 日界（09:00 → 次日 00:00 = 15 小时）
    assert excinfo.value.headers["Retry-After"] == str(15 * 3600)


def test_failed_calls_still_consume_quota_by_design() -> None:
    """配额数的是**出网尝试**：请求一旦送出就无法证明没花钱，失败退还等于让
    一个报错的脚本把余额刷光。这条断言是给未来的改动者立拦路石的。"""

    guard, _tick, _redis = make_guard(per_user=2, global_limit=100)

    for _ in range(2):
        guard.rollback(begin(guard))

    with pytest.raises(ApiError) as excinfo:
        begin(guard)
    assert excinfo.value.code is ErrorCode.RATE_LIMITED


def test_quota_counts_each_user_separately() -> None:
    guard, _tick, _redis = make_guard(per_user=1, global_limit=100)

    guard.rollback(begin(guard, user=USER_ID))
    with pytest.raises(ApiError):
        begin(guard, user=USER_ID)

    # 换一个人不受影响
    assert begin(guard, user=OTHER_USER) is not None


def test_global_daily_limit_blocks_everyone() -> None:
    guard, _tick, _redis = make_guard(per_user=100, global_limit=1)

    guard.rollback(begin(guard, user=USER_ID))

    with pytest.raises(ApiError) as excinfo:
        begin(guard, user=OTHER_USER)
    assert excinfo.value.details["scope"] == "global"


def test_counters_reset_at_the_utc_day_boundary() -> None:
    guard, tick, _redis = make_guard(per_user=1, global_limit=100)
    guard.rollback(begin(guard))

    with pytest.raises(ApiError):
        begin(guard)

    # 跨过 UTC 零点：换了日期键，额度重新可用
    next_day = (tick() + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    tick.advance(next_day - tick() + timedelta(minutes=1))

    assert begin(guard) is not None


def test_counter_keys_get_a_ttl_so_they_do_not_block_forever() -> None:
    tick = Clock()
    redis = SetNxRedisClient(clock=tick)
    guard, _clock, _client = make_guard(tick, per_user=1, global_limit=100, client=redis)

    guard.rollback(begin(guard))

    counters = [key for key in redis.keys() if ":day:" in key]
    assert counters, "计数器键必须存在"
    for key in counters:
        assert redis.ttl(key) > 0, key


# ------------------------------------------------------------------ 装配校验
def test_non_positive_limits_are_rejected_at_construction() -> None:
    tick = Clock()
    with pytest.raises(ValueError):
        RedisAnalysisGuard(
            SetNxRedisClient(clock=tick),
            per_user_daily_limit=0,
            global_daily_limit=10,
            clock=tick,
        )


class BrokenClient:
    """任何命令都直接抛错的连接——模拟 Redis 挂了或连不上。"""

    def get(self, name: str):
        raise ConnectionError("connection refused")

    def set(self, name: str, value: str, *, ex=None, nx=False):
        raise ConnectionError("connection refused")

    def incr(self, name: str):
        raise ConnectionError("connection refused")

    def expire(self, name: str, seconds: int):
        raise ConnectionError("connection refused")

    def delete(self, *names: str):
        raise ConnectionError("connection refused")


def test_guard_fails_closed_when_the_client_is_unreachable() -> None:
    """闸门看不见状态时一律拦住：读不到额度就继续出网，等于在最不确定成本的那一刻
    放弃成本控制权。异常文本（可能含连接信息）也不得进 `details`。"""

    tick = Clock()
    guard, _clock, _client = make_guard(tick, client=BrokenClient())

    for call in (
        lambda: cached(guard),
        lambda: begin(guard),
        lambda: guard.commit(AnalysisLease(cache_key="k"), description="x", model="m"),
        lambda: guard.rollback(AnalysisLease(lock_key="k")),
    ):
        with pytest.raises(ApiError) as excinfo:
            call()
        assert excinfo.value.code is ErrorCode.NOT_READY
        assert excinfo.value.status_code == 503
        assert excinfo.value.details == {"dependency": "ConnectionError"}


def test_guard_output_shape_is_json_round_trippable() -> None:
    """缓存内容必须能安全序列化：描述是中文，`ensure_ascii` 关掉只为可读性。"""

    guard, _tick, redis = make_guard()
    commit(guard, begin(guard), text="一个人背对镜头站在草地上")

    stored = redis.get(f"ai-avatar:v1:analysis:desc:{USER_ID}:{DIGEST}")
    assert stored is not None
    assert json.loads(stored)["description"] == "一个人背对镜头站在草地上"
