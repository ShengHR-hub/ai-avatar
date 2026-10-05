"""Worker 心跳读写测试（ENG-06 第 4 步）。

冻结依据：

- `docs/architecture/05-system-architecture.md` 第 3 节：心跳由 Beat 投递、在 Worker 内
  执行，因此「心跳新鲜」同时证明 Beat 在投递、队列在通、Worker 在消费；
- `docs/architecture/03-api-contract.md` 第 4.5 节：`/ready` 覆盖 `worker_heartbeat`
  探针；读取侧阈值 60 秒冻结在 `app/api/health/checks.py`
  （`WORKER_HEARTBEAT_THRESHOLD_SECONDS`，技术负责人 2026-09-26 裁决）；
- `docs/architecture/09-repository-boundary.md` 第 7 节：只存时间戳，不存任务 ID、
  Prompt、URL 或凭据。

Redis 用 `tests.services.fakes.FakeRedis`（`RedisPort` 内存实现）+ 注入时钟，不连真实
Redis。本模块只 **读取** `app/api/health/checks.py` 的阈值与工厂来验证互操作性
（读取器接线属后续授权范围）。
"""

from __future__ import annotations

import functools
import re
from datetime import datetime, timedelta, timezone

import pytest

from app.api.health.checks import (
    WORKER_HEARTBEAT_THRESHOLD_SECONDS,
    make_worker_heartbeat_check,
)
from app.tasks.celery_app import HEARTBEAT_INTERVAL_SECONDS, HEARTBEAT_TTL_SECONDS
from app.tasks.heartbeat import WORKER_HEARTBEAT_KEY, publish_heartbeat, read_heartbeat
from tests.services.fakes import FakeRedis, MutableClock

#: 明显不是 ISO-8601 的破坏值：读取侧一律返回 `None`，绝不抛异常
CORRUPT_VALUES = (
    "",
    "not-a-timestamp",
    "None",
    "0",
    "2026-13-45T99:99:99",
    "2026-03-01T12:00:00+99:00",
    '{"beat": true}',
)

#: 无时区的墙上时间（模拟历史/外部写入方留下的裸时间戳）
NAIVE_WALL_CLOCK = datetime(2026, 3, 1, 12, 0, 0)

#: 心跳值的形状：只有一个带 UTC 偏移的时间戳，可有可无小数秒
ISO_UTC_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?\+00:00")


def seed_heartbeat(
    redis: FakeRedis, value: str, *, ttl_seconds: int | None = None
) -> None:
    """直接写键值（模拟外部/历史写入方），绕开被测的 `publish_heartbeat`。"""

    redis.set(WORKER_HEARTBEAT_KEY, value, ex=ttl_seconds)


# --------------------------------------------------------------------------- #
# 写入 / 读取回环
# --------------------------------------------------------------------------- #
def test_publish_then_read_round_trips_an_iso8601_utc_timestamp(
    fake_redis: FakeRedis, clock: MutableClock
) -> None:
    """写入的是带时区的 ISO-8601 UTC 串，读回来是同一时刻的 aware datetime。"""

    moment = clock()
    publish_heartbeat(fake_redis, now=moment)

    raw = fake_redis.get(WORKER_HEARTBEAT_KEY)
    assert raw == moment.isoformat()
    assert raw is not None and raw.endswith("+00:00"), "必须显式带 UTC 偏移，不得写裸时间"
    assert ISO_UTC_PATTERN.fullmatch(raw or "") is not None, raw

    stored = read_heartbeat(fake_redis)
    assert stored == moment
    assert stored is not None and stored.tzinfo is timezone.utc
    assert fake_redis.keys() == (WORKER_HEARTBEAT_KEY,), "心跳只写这一个键"


def test_publish_normalises_a_non_utc_input_to_utc(fake_redis: FakeRedis) -> None:
    """带其他偏移的输入必须换算成 UTC 后落库（比较侧只认 UTC）。"""

    moment = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
    tokyo = moment.astimezone(timezone(timedelta(hours=9)))
    assert tokyo.isoformat().endswith("+09:00")

    publish_heartbeat(fake_redis, now=tokyo)

    assert fake_redis.get(WORKER_HEARTBEAT_KEY) == moment.isoformat()
    assert read_heartbeat(fake_redis) == moment


def test_publish_with_naive_input_still_writes_an_offset(fake_redis: FakeRedis) -> None:
    """naive 输入也必须写出带偏移的值，读取侧永远不必猜时区。

    注意写入侧用 `astimezone()`，即**按进程本地时区解释**裸时间；本用例只钉住
    「输出一定带 `+00:00` 且可被读回」这条与部署时区无关的事实。
    """

    publish_heartbeat(fake_redis, now=NAIVE_WALL_CLOCK)

    raw = fake_redis.get(WORKER_HEARTBEAT_KEY)
    assert raw is not None and raw.endswith("+00:00"), raw
    assert ISO_UTC_PATTERN.fullmatch(raw) is not None, raw
    parsed = read_heartbeat(fake_redis)
    assert parsed is not None and parsed.tzinfo is timezone.utc


def test_publish_overwrites_the_previous_beat(fake_redis: FakeRedis, clock: MutableClock) -> None:
    """同一键覆盖写：Worker 每轮心跳刷新同一个键，不累积历史键。"""

    first = clock()
    publish_heartbeat(fake_redis, now=first)
    clock.advance(HEARTBEAT_INTERVAL_SECONDS)
    second = clock()
    publish_heartbeat(fake_redis, now=second)

    assert fake_redis.keys() == (WORKER_HEARTBEAT_KEY,)
    assert read_heartbeat(fake_redis) == second
    assert second > first


# --------------------------------------------------------------------------- #
# TTL
# --------------------------------------------------------------------------- #
def test_publish_sets_the_ttl_from_the_module_constant(
    fake_redis: FakeRedis, clock: MutableClock
) -> None:
    """TTL 取自 `HEARTBEAT_TTL_SECONDS`，到期后自动读不到（`ex` 真的传给了 Redis 端口）。"""

    publish_heartbeat(fake_redis, now=clock())

    assert fake_redis.ttl(WORKER_HEARTBEAT_KEY) == HEARTBEAT_TTL_SECONDS
    assert read_heartbeat(fake_redis) is not None
    clock.advance(HEARTBEAT_TTL_SECONDS - 1)
    assert read_heartbeat(fake_redis) is not None
    clock.advance(1)
    assert read_heartbeat(fake_redis) is None, "TTL 到期即视为无心跳"


def test_heartbeat_ttl_covers_the_health_check_threshold() -> None:
    """TTL 不得短于 `/ready` 的新鲜度阈值，否则会出现「键还在但按阈值已过期」。

    两侧数值都是 60 秒：这是刻意取值——键消失与判定过期落在同一时刻，不留窗口。
    代价是 Worker 与 API 进程之间没有时钟偏差余量，所以投递间隔必须远小于 TTL
    （见 `test_celery_app_contract.py` 的间隔自洽用例）。
    """

    assert HEARTBEAT_TTL_SECONDS == 60
    assert WORKER_HEARTBEAT_THRESHOLD_SECONDS == 60
    assert HEARTBEAT_TTL_SECONDS >= WORKER_HEARTBEAT_THRESHOLD_SECONDS


def test_heartbeat_interval_leaves_headroom_inside_the_ttl(
    fake_redis: FakeRedis, clock: MutableClock
) -> None:
    """按 Beat 间隔连写两轮：任何一轮刚写完都必须立即可读。"""

    assert HEARTBEAT_INTERVAL_SECONDS * 2 <= HEARTBEAT_TTL_SECONDS

    for _ in range(2):
        clock.advance(HEARTBEAT_INTERVAL_SECONDS)
        publish_heartbeat(fake_redis, now=clock())
        assert read_heartbeat(fake_redis) is not None
        assert fake_redis.ttl(WORKER_HEARTBEAT_KEY) == HEARTBEAT_TTL_SECONDS


# --------------------------------------------------------------------------- #
# 读取侧健壮性
# --------------------------------------------------------------------------- #
def test_reading_a_missing_key_returns_none(fake_redis: FakeRedis) -> None:
    assert read_heartbeat(fake_redis) is None


def test_reading_a_key_the_worker_just_deleted_returns_none(
    fake_redis: FakeRedis, clock: MutableClock
) -> None:
    """键被清掉（例如手工运维 flush）后，读取侧立刻回到「无心跳」。"""

    publish_heartbeat(fake_redis, now=clock())
    assert read_heartbeat(fake_redis) is not None

    fake_redis.delete(WORKER_HEARTBEAT_KEY)

    assert read_heartbeat(fake_redis) is None
    assert fake_redis.keys() == ()


@pytest.mark.parametrize("value", CORRUPT_VALUES)
def test_reading_a_corrupt_value_returns_none_instead_of_raising(
    fake_redis: FakeRedis, value: str
) -> None:
    """Redis 里躺着什么都不能把 `/ready` 打成 500：解析失败一律视为无心跳。"""

    seed_heartbeat(fake_redis, value)

    assert read_heartbeat(fake_redis) is None


def test_a_naive_stored_value_is_interpreted_as_utc(fake_redis: FakeRedis) -> None:
    """外部写入方留下的裸时间戳按 UTC 解释，并补上时区后返回。"""

    seed_heartbeat(fake_redis, NAIVE_WALL_CLOCK.isoformat())
    assert NAIVE_WALL_CLOCK.isoformat().count("+") == 0, "前提：样本确实无时区"

    stored = read_heartbeat(fake_redis)

    assert stored is not None
    assert stored.tzinfo is timezone.utc
    assert stored.utcoffset() == timedelta(0)
    assert stored.replace(tzinfo=None) == NAIVE_WALL_CLOCK


def test_a_stored_offset_value_keeps_its_absolute_instant(fake_redis: FakeRedis) -> None:
    """带偏移的存量值按偏移解析，不做「抹掉时区」的有损归一。"""

    seed_heartbeat(fake_redis, "2026-03-01T12:00:00+08:00")

    stored = read_heartbeat(fake_redis)

    assert stored is not None
    assert stored.utcoffset() == timedelta(hours=8)
    assert stored == datetime(2026, 3, 1, 4, 0, 0, tzinfo=timezone.utc)


def test_stored_value_carries_no_identifier_url_or_secret(fake_redis: FakeRedis) -> None:
    """值只可能是时间戳：不得出现任务 ID、URL 或凭据形状的内容。"""

    publish_heartbeat(fake_redis, now=datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc))

    raw = fake_redis.get(WORKER_HEARTBEAT_KEY) or ""

    assert ISO_UTC_PATTERN.fullmatch(raw) is not None, raw
    assert "://" not in raw
    assert re.search(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}", raw) is None, raw
    assert fake_redis.raw_values() == (raw,), "心跳键空间里不得夹带其他键值"


# --------------------------------------------------------------------------- #
# 与读取侧探针的互操作（只读 `app/api/health/checks.py`，不修改它）
# --------------------------------------------------------------------------- #
def test_read_heartbeat_is_a_drop_in_heartbeat_reader(
    fake_redis: FakeRedis, clock: MutableClock
) -> None:
    """`functools.partial(read_heartbeat, redis)` 满足 `HeartbeatReader` 的语义。"""

    check = make_worker_heartbeat_check(
        functools.partial(read_heartbeat, fake_redis), now=lambda: clock()
    )

    initial = check()
    assert initial.ready is False
    assert initial.name == "worker_heartbeat"
    assert "://" not in (initial.detail or "")

    publish_heartbeat(fake_redis, now=clock())
    assert check().ready is True

    clock.advance(HEARTBEAT_TTL_SECONDS - 1)
    assert check().ready is True

    # 越过 TTL：键被清掉，读取器返回 None，探针按「读不到心跳」未就绪
    clock.advance(2)
    stale = check()
    assert stale.ready is False
    assert stale.detail is not None and "no worker heartbeat" in stale.detail


def test_heartbeat_ttl_and_threshold_leave_no_stale_window(
    fake_redis: FakeRedis, clock: MutableClock
) -> None:
    """任何还能被读到的心跳，其年龄都不得超过读取侧阈值 60 秒。

    两条约束把 TTL 钉死在 60 秒：TTL 短于阈值会「键还在却已按阈值过期」（上一条），
    TTL 长于阈值则让已经不该作数的心跳继续可读（本条）。因此本用例先断言
    `TTL <= 阈值`，再用逐秒推进证明经验侧同样成立：越过 TTL 的那一刻键消失，
    读取器返回 `None` 而不是一个超龄时间戳。

    副作用：读取侧 `worker_heartbeat_result` 的「年龄 > 阈值」分支在这份数据源上
    永远不会命中（键先没了），阈值实际上被 TTL 遮蔽，且写入/读取两侧没有时钟
    偏差余量——调大阈值必须同时调大 TTL 才有效果。
    """

    assert HEARTBEAT_TTL_SECONDS <= WORKER_HEARTBEAT_THRESHOLD_SECONDS

    publish_heartbeat(fake_redis, now=clock())
    for step in range(HEARTBEAT_TTL_SECONDS):
        clock.advance(1)
        stored = read_heartbeat(fake_redis)
        if stored is None:
            break
        age = (clock() - stored).total_seconds()
        assert age <= WORKER_HEARTBEAT_THRESHOLD_SECONDS, (step, age)

    clock.advance(1)
    assert read_heartbeat(fake_redis) is None, "超过 TTL 后必须读不到，而不是读到一个超龄值"
