"""持久付费闸门的真实 PostgreSQL 测试（全程 Mock，不出网）。

这一组用例是「分析额度不再只存 Redis」这条收口要求的直接证据：

- 判额、去重、并发裁决全部发生在 `analysis_dispatches` 上；
- **换一个新的空 Redis** 再问一次额度，答案必须不变——原先 Redis 一重启当天额度就归零；
- 同一 `(用户, 日, 图片摘要)` 并发登记时，唯一索引 + 事务级咨询锁只放行一次；
- 超时/未结算的行保持 `UNKNOWN` 并继续占额度，重试打不到第二次付费；
- 三个 CHECK（`outcome`、`usage_day` 与 `created_at` 同日、摘要长度）由数据库裁决。

行写入用独立连接并回滚（`db` 夹具事务），并发用例为了让两个线程真的互相看不到
未提交行，必须各自提交，因此结束后显式清掉本用例造的行——临时库整体会在会话结束时
删除，不影响任何共享库。
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

import pytest

from app.db.models.analysis import AnalysisDispatch
from app.errors.codes import ErrorCode
from app.errors.exceptions import ApiError
from app.repositories.analysis import (
    DispatchDraft,
    SqlAlchemyAnalysisDispatchRepository,
)
from app.services.analysis.guard import RedisAnalysisGuard
from app.services.analysis.quota import (
    OUTCOME_FAILED,
    OUTCOME_SUCCEEDED,
    DurableAnalysisGuard,
    PostgresAnalysisQuota,
)
from tests.services.fakes import FakeRedis, MutableClock

PER_USER = 3
GLOBAL_HEADROOM = 2


@pytest.fixture
def moment() -> MutableClock:
    """可控时钟，但**日期取真实当天**。

    `usage_day` 与 `created_at` 由数据库 CHECK 钉成同一天，而 `created_at` 的写入时间
    来自数据库时钟（默认 `now()`），不是这个注入时钟。如果这里固定一个历史/未来日期，
    用例测到的就只是「时钟与数据库日期不一致会被 CHECK 拒绝」，而不是闸门本身。
    """

    now = datetime.now(timezone.utc)
    return MutableClock(start=now.replace(hour=8, minute=0, second=0, microsecond=0))


@pytest.fixture
def quota_factory(
    db: Connection, moment: MutableClock
) -> Callable[[int, int], PostgresAnalysisQuota]:
    """用夹具连接造 savepoint 会话：闸门写的行在同一事务里可读、结束即回滚。"""

    factory = sessionmaker(
        bind=db, autoflush=False, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )

    def _make(
        per_user: int = PER_USER, global_limit: int = PER_USER + GLOBAL_HEADROOM
    ) -> PostgresAnalysisQuota:
        return PostgresAnalysisQuota(
            factory,
            per_user_daily_limit=per_user,
            global_daily_limit=global_limit,
            clock=moment,
        )

    return _make


@pytest.fixture
def make_user_id(db: Connection, make_user: Callable[..., uuid.UUID]) -> uuid.UUID:
    return make_user()


def _digest(seed: int) -> str:
    return f"{seed:064x}"


def _reserve(
    quota: PostgresAnalysisQuota, user_id: uuid.UUID, *, digest: str | None = None, seed: int = 1
) -> uuid.UUID:
    return quota.reserve(
        user_id=user_id,
        digest=digest or _digest(seed),
        provider_name="MOCK",
        model_name="vision-test-model",
    )


def _rows(db: Connection, user_id: uuid.UUID) -> list[AnalysisDispatch]:
    return list(
        db.execute(
            text(
                """
                SELECT id, usage_day, digest, provider_name, model_name, outcome
                  FROM analysis_dispatches
                 WHERE user_id = :user_id
                 ORDER BY digest
                """
            ),
            {"user_id": user_id},
        ).mappings()
    )


# --------------------------------------------------------------------------- #
# 登记与终态
# --------------------------------------------------------------------------- #
def test_reserve_registers_unknown_dispatch(
    db: Connection,
    quota_factory: Callable[..., PostgresAnalysisQuota],
    make_user_id: uuid.UUID,
) -> None:
    dispatch_id = _reserve(quota_factory(), make_user_id, seed=7)

    rows = _rows(db, make_user_id)
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == dispatch_id
    assert row["outcome"] == "UNKNOWN"
    assert row["digest"] == _digest(7)
    assert row["provider_name"] == "MOCK"
    assert row["model_name"] == "vision-test-model"
    assert row["usage_day"] == datetime.now(timezone.utc).date()


def test_settle_converges_outcome_without_removing_the_row(
    quota_factory: Callable[..., PostgresAnalysisQuota],
    db: Connection,
    make_user_id: uuid.UUID,
) -> None:
    quota = quota_factory()
    dispatch_id = _reserve(quota, make_user_id)

    quota.settle(dispatch_id, outcome=OUTCOME_SUCCEEDED)

    assert _rows(db, make_user_id)[0]["outcome"] == "SUCCEEDED"
    quota.settle(dispatch_id, outcome=OUTCOME_FAILED)
    assert _rows(db, make_user_id)[0]["outcome"] == "FAILED"


# --------------------------------------------------------------------------- #
# 每日上限
# --------------------------------------------------------------------------- #
def test_per_user_daily_cap_is_enforced_by_the_database(
    quota_factory: Callable[..., PostgresAnalysisQuota], make_user_id: uuid.UUID
) -> None:
    quota = quota_factory()

    ids = [_reserve(quota, make_user_id, seed=seed) for seed in range(1, PER_USER + 1)]
    assert len(ids) == PER_USER

    with pytest.raises(ApiError) as excinfo:
        _reserve(quota, make_user_id, seed=99)

    assert excinfo.value.code is ErrorCode.RATE_LIMITED
    assert excinfo.value.details == {"scope": "user"}
    assert excinfo.value.headers and "Retry-After" in excinfo.value.headers


def test_global_daily_cap_is_enforced_across_users(
    db: Connection,
    quota_factory: Callable[..., PostgresAnalysisQuota],
    make_user: Callable[..., uuid.UUID],
) -> None:
    """全局额度按「当日已登记行数」判，与用户是谁无关。

    基线先数一次：同一会话里别的用例可能已经提交过行，硬编码阈值会让这条用例
    变成一个看运行顺序脸色的假阳性。
    """

    today = datetime.now(timezone.utc).date()
    baseline = int(
        db.execute(
            text("SELECT count(*) FROM analysis_dispatches WHERE usage_day = :day"),
            {"day": today},
        ).scalar_one()
    )

    allowed = PER_USER + GLOBAL_HEADROOM
    quota = quota_factory(per_user=100, global_limit=baseline + allowed)
    users = [make_user() for _ in range(allowed + 1)]

    for user_id in users[:allowed]:
        _reserve(quota, user_id, seed=1)

    with pytest.raises(ApiError) as excinfo:
        _reserve(quota, users[-1], seed=1)

    assert excinfo.value.code is ErrorCode.RATE_LIMITED
    assert excinfo.value.details == {"scope": "global"}


def test_same_image_same_day_is_never_charged_twice(
    quota_factory: Callable[..., PostgresAnalysisQuota],
    db: Connection,
    make_user_id: uuid.UUID,
) -> None:
    """同一张图当天第二次点击：唯一索引拒绝，且只占一格额度。

    这是 Redis 缓存（6 小时）过期后的真实场景——用户下午再点一次同一张图，
    系统不能「因为缓存没了」就再付一次钱。
    """

    quota = quota_factory()
    _reserve(quota, make_user_id, digest=_digest(42))

    with pytest.raises(ApiError) as excinfo:
        _reserve(quota, make_user_id, digest=_digest(42))

    assert excinfo.value.code is ErrorCode.RATE_LIMITED
    assert excinfo.value.details == {"scope": "image"}
    assert len(_rows(db, make_user_id)) == 1


def test_unsettled_dispatch_keeps_consuming_the_slot(
    quota_factory: Callable[..., PostgresAnalysisQuota], make_user_id: uuid.UUID
) -> None:
    """供应商超时、进程崩溃：行留在 UNKNOWN，额度不回退，重试打不出去。"""

    quota = quota_factory()
    consumed = [
        _reserve(quota, make_user_id, seed=seed) for seed in range(1, PER_USER + 1)
    ]
    # 没有任何 settle —— 模拟 worker 在出网后崩溃
    assert consumed and len(consumed) == PER_USER

    with pytest.raises(ApiError) as excinfo:
        _reserve(quota, make_user_id, seed=PER_USER + 1)

    assert excinfo.value.details == {"scope": "user"}


def test_redis_restart_does_not_restore_the_daily_cap(
    quota_factory: Callable[..., PostgresAnalysisQuota],
    make_user_id: uuid.UUID,
    moment: MutableClock,
) -> None:
    """收口要求的正面证明：换一个全新的空 Redis，额度结论必须不变。

    原先计数器住在 Redis，`INCR` 的键一消失用户当天就能再花 PER_USER 次；
    现在 Redis 只做缓存与单飞锁，判额读的是 PostgreSQL。
    """

    quota = quota_factory()
    for seed in range(1, PER_USER + 1):
        _reserve(quota, make_user_id, seed=seed)

    # 「重启 Redis」= 造一个全新的空实例，并让守卫重新装配
    fresh_redis = FakeRedis(clock=moment)
    guard = DurableAnalysisGuard(
        redis_guard=RedisAnalysisGuard(
            fresh_redis, per_user_daily_limit=None, global_daily_limit=None
        ),
        quota=quota,
        provider_name="MOCK",
        model_name="vision-test-model",
    )

    assert fresh_redis.keys() == ()  # 空缓存：旧计数确实没了
    with pytest.raises(ApiError) as excinfo:
        guard.begin(user_id=make_user_id, sha256=_digest(900))

    assert excinfo.value.code is ErrorCode.RATE_LIMITED
    assert excinfo.value.details == {"scope": "user"}


def test_guard_does_not_count_quota_in_redis(
    quota_factory: Callable[..., PostgresAnalysisQuota],
    make_user_id: uuid.UUID,
    moment: MutableClock,
) -> None:
    """装配口径：Redis 侧不再产生任何计数器键，只留描述缓存与单飞锁。"""

    redis_client = FakeRedis(clock=moment)
    guard = DurableAnalysisGuard(
        redis_guard=RedisAnalysisGuard(
            redis_client, per_user_daily_limit=None, global_daily_limit=None
        ),
        quota=quota_factory(),
        provider_name="MOCK",
        model_name="vision-test-model",
    )

    guard.begin(user_id=make_user_id, sha256=_digest(5))

    stored = list(redis_client.keys())
    assert any(":lock" in name for name in stored)
    assert not [name for name in stored if ":day:" in name]


def test_database_failure_is_not_reported_as_a_quota_rejection(
    db: Connection,
    make_user: Callable[..., uuid.UUID],
) -> None:
    """CHECK 失败 ≠ 「今天这张图已经读过」。

    只有 SQLSTATE 23505（唯一冲突）才算重复登记；其余 `IntegrityError` 必须走失败关闭
    （`NOT_READY`/503），而不是被伪装成 `scope=image` 的 429。否则一次真正的写入故障
    会被读成正常的额度拒绝，运维永远看不到。

    构造方式：把写入时刻整体前移一天（只影响这次 INSERT 的 `created_at`），于是
    `usage_day` 与 `created_at` 的 UTC 日历日不再相同，
    `ck_analysis_dispatches_usage_day_matches_created_at` 必然拒绝。判据是 SQLSTATE，
    与被拒原因无关，所以这里用最直接的方式造出「非唯一冲突的 IntegrityError」。
    """

    user_id = make_user()
    factory = sessionmaker(
        bind=db, autoflush=False, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    quota = PostgresAnalysisQuota(
        factory,
        per_user_daily_limit=PER_USER,
        global_daily_limit=PER_USER + GLOBAL_HEADROOM,
    )
    original_record = SqlAlchemyAnalysisDispatchRepository.record

    def _skewed_record(self, draft):  # noqa: ANN001, ANN202
        return original_record(
            self,
            DispatchDraft(
                user_id=draft.user_id,
                usage_day=draft.usage_day,
                digest=draft.digest,
                provider_name=draft.provider_name,
                model_name=draft.model_name,
                created_at=draft.created_at + timedelta(days=1),
            ),
        )

    SqlAlchemyAnalysisDispatchRepository.record = _skewed_record  # type: ignore[method-assign]
    try:
        with pytest.raises(ApiError) as excinfo:
            _reserve(quota, user_id, seed=4242)
    finally:
        SqlAlchemyAnalysisDispatchRepository.record = original_record  # type: ignore[method-assign]

    assert excinfo.value.code is ErrorCode.NOT_READY
    assert excinfo.value.status_code == 503
    assert excinfo.value.details == {"dependency": "IntegrityError"}
    # 失败关闭：一行都没留下
    assert _rows(db, user_id) == []


# --------------------------------------------------------------------------- #
# 并发（各自提交，结束后清理）
# --------------------------------------------------------------------------- #
@pytest.fixture
def isolated_engine(test_database_url: str) -> Iterator[Engine]:
    """并发用例需要两个真事务互相看得见，因此用独立连接并提交，结束后删行。"""

    engine = create_engine(test_database_url, poolclass=NullPool)
    try:
        yield engine
    finally:
        engine.dispose()


def _isolated_user(engine: Engine) -> uuid.UUID:
    """在**已提交**的独立连接上造一个合成用户。

    并发用例的 worker 必须能看见这个用户：`make_user` 夹具写在外层用例事务里、
    对别的连接不可见，用它做并发会得到外键冲突（`analysis_dispatches.user_id` →
    `users.id`），看起来像闸门故障，其实是夹具的事务边界。因此这里自己提交。
    """

    with engine.connect() as conn:
        user_id = conn.execute(
            text(
                "INSERT INTO users (status, locale, timezone, updated_at)"
                " VALUES ('ACTIVE','zh-CN','UTC', now()) RETURNING id"
            )
        ).scalar_one()
        conn.commit()
    return user_id


def _concurrent_reserve(
    engine: Engine, user_id: uuid.UUID, *, digest: str, barrier: threading.Barrier
) -> str:
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=True)
    quota = PostgresAnalysisQuota(
        factory,
        per_user_daily_limit=PER_USER,
        global_daily_limit=1_000_000,
    )
    barrier.wait()
    try:
        quota.reserve(
            user_id=user_id, digest=digest, provider_name="MOCK", model_name="m"
        )
        return "reserved"
    except ApiError as error:
        return f"{error.code.value}:{error.details.get('scope')}"


def test_concurrent_same_image_registers_exactly_one_paid_dispatch(
    isolated_engine: Engine,
) -> None:
    user_id = _isolated_user(isolated_engine)
    digest = _digest(31)
    barrier = threading.Barrier(4)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(
                lambda _: _concurrent_reserve(
                    isolated_engine, user_id, digest=digest, barrier=barrier
                ),
                range(4),
            )
        )

    try:
        with isolated_engine.connect() as conn:
            count = conn.execute(
                text(
                    "SELECT count(*) FROM analysis_dispatches WHERE user_id = :u AND digest = :d"
                ),
                {"u": user_id, "d": digest},
            ).scalar_one()
        assert results.count("reserved") == 1
        assert set(results) == {"reserved", "RATE_LIMITED:image"}
        assert count == 1
    finally:
        _cleanup(isolated_engine, user_id)


def test_concurrent_distinct_images_never_breach_the_cap(
    isolated_engine: Engine,
) -> None:
    """并发打不同图片时，「先计数再插行」必须被咨询锁串起来：总额度不被突破。"""

    user_id = _isolated_user(isolated_engine)
    workers = PER_USER + 3
    barrier = threading.Barrier(workers)

    def _one(seed: int) -> str:
        return _concurrent_reserve(
            isolated_engine, user_id, digest=_digest(seed), barrier=barrier
        )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(_one, range(1, workers + 1)))

    try:
        with isolated_engine.connect() as conn:
            count = conn.execute(
                text("SELECT count(*) FROM analysis_dispatches WHERE user_id = :u"),
                {"u": user_id},
            ).scalar_one()
        assert results.count("reserved") == PER_USER
        assert count == PER_USER, "并发下写入了超出上限的行：咨询锁没有生效"
    finally:
        _cleanup(isolated_engine, user_id)


def _cleanup(engine: Engine, user_id: uuid.UUID) -> None:
    with engine.connect() as conn:
        conn.execute(
            text("DELETE FROM analysis_dispatches WHERE user_id = :u"), {"u": user_id}
        )
        conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": user_id})
        conn.commit()


# --------------------------------------------------------------------------- #
# 数据库约束
# --------------------------------------------------------------------------- #
#: 显式写入 `created_at`：CHECK 要求 `usage_day` 与它同日。
#: 这里刻意**不加**任何时间偏移（早期版本加 `+12 hours`，在 UTC 日界后就会把
#: `created_at` 推到第二天，于是「合法行」的用例先被 CHECK 拒掉，测到的是夹具问题）。
_INSERT = """
    INSERT INTO analysis_dispatches (
        user_id, usage_day, digest, provider_name, model_name, outcome, created_at
    ) VALUES (
        :user_id, CAST(:day AS date), :digest, 'MOCK', 'm', :outcome,
        (now() AT TIME ZONE 'UTC')
    )
"""


def _insert_params(user_id: uuid.UUID, *, day: str, digest: str, outcome: str) -> dict[str, str]:
    return {"user_id": str(user_id), "day": day, "digest": digest, "outcome": outcome}


def test_constraints_reject_inconsistent_rows(
    db: Connection,
    make_user: Callable[..., uuid.UUID],
    rejected: Callable[..., str],
) -> None:
    """三条 CHECK 由数据库裁决：摘要必须是 64 位十六进制、终态只有三种、
    冗余的 `usage_day` 不能与写入当天脱钩。"""

    user_id = make_user()
    today = datetime.now(timezone.utc).date().isoformat()

    rejected(
        db,
        _INSERT,
        _insert_params(user_id, day=today, digest="a" * 63, outcome="UNKNOWN"),
        contains="ck_analysis_dispatches_digest_length",
    )
    rejected(
        db,
        _INSERT,
        _insert_params(user_id, day=today, digest="a" * 64, outcome="MAYBE"),
        contains="ck_analysis_dispatches_outcome",
    )
    rejected(
        db,
        _INSERT,
        _insert_params(user_id, day="2020-01-01", digest="b" * 64, outcome="UNKNOWN"),
        contains="usage_day_matches_created_at",
    )
    # 同图同日重复登记由唯一索引拒绝（Service 侧那条用例只证明映射，这里证明约束本身）
    db.execute(text(_INSERT), _insert_params(user_id, day=today, digest="c" * 64, outcome="UNKNOWN"))
    rejected(
        db,
        _INSERT,
        _insert_params(user_id, day=today, digest="c" * 64, outcome="UNKNOWN"),
        contains="uq_analysis_dispatches_user_id_usage_day_digest",
    )


def test_usage_day_is_the_utc_calendar_day_of_the_write(
    db: Connection, make_user: Callable[..., uuid.UUID]
) -> None:
    """`usage_day` 必须与写入当天的 UTC 日历日一致——否则额度会算到错误的一天。

    原先这里留了一段 `... if False else None` 的空壳（永不执行的死断言）：它看着像
    覆盖，实际什么都没验证。这里改成一条真断言：以 UTC「今天」显式写入必须被接受，
    而与之相邻的日期必须被 CHECK 拒绝（拒绝路径已由上一用例覆盖，此处只钉住正向）。
    """

    user_id = make_user()
    today = datetime.now(timezone.utc).date().isoformat()

    db.execute(
        text(_INSERT),
        _insert_params(user_id, day=today, digest="e" * 64, outcome="UNKNOWN"),
    )

    stored = db.execute(
        text(
            """
            SELECT usage_day, (created_at AT TIME ZONE 'UTC')::date AS created_day
              FROM analysis_dispatches WHERE user_id = :user_id AND digest = :digest
            """
        ),
        {"user_id": user_id, "digest": "e" * 64},
    ).one()
    assert stored.usage_day == stored.created_day
