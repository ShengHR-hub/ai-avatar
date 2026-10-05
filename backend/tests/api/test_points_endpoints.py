"""点数读取端点测试（ENG-04 补口）。

覆盖 `GET /api/v1/me/points` 与 `GET /api/v1/me/points/ledger` 的**端点层**语义：
认证门槛、余额直接来自钱包行（前端不做算术）、钱包缺失时的全零快照、
流水只返回本人行且**不泄露审计元数据**、`(created_at, id)` 键集翻页、
以及分页查询参数的越界拒绝。

依赖全部注入内存替身：不启动 PostgreSQL / Redis，也不产生任何账务写入。
写侧矩阵（CREDIT/RESERVE/CONSUME/RELEASE/ADJUSTMENT 与幂等）在
`tests/services/test_points.py` 与 `tests/db/test_points_invariants.py` 覆盖，此处不重复。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_redis_port, get_session_store, get_uow
from app.config import reset_settings_cache
from app.main import create_app
from app.repositories.points import LedgerEntryDraft
from app.services.points.service import PointsService
from tests.services.fakes import (
    FakeRedis,
    InMemoryUnitOfWork,
    MutableClock,
    make_user_row,
)

BASE_URL = "https://testserver"
SESSION_SECRET = "synthetic-session-secret-for-tests"
BALANCE_PATH = "/api/v1/me/points"
LEDGER_PATH = "/api/v1/me/points/ledger"

USER_ID = uuid.UUID("11223344-5566-7788-99aa-bbccddeeff00")
OTHER_ID = uuid.UUID("99887766-5544-3322-1100-ffeeddccbbaa")
WALLET_ID = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")


class Harness:
    def __init__(
        self,
        app: FastAPI,
        client: TestClient,
        uow: InMemoryUnitOfWork,
        redis: FakeRedis,
        clock: MutableClock,
    ) -> None:
        self.app = app
        self.client = client
        self.uow = uow
        self.redis = redis
        self.clock = clock
        self.wallet_id = WALLET_ID


def _store(redis: FakeRedis):
    from app.services.auth.session_store import RedisSessionStore

    return RedisSessionStore(redis, secret=SESSION_SECRET)


@pytest.fixture
def harness() -> Iterator[Harness]:
    clock = MutableClock()
    redis = FakeRedis(clock=clock)
    uow = InMemoryUnitOfWork(clock=clock)

    app = create_app()
    app.dependency_overrides[get_uow] = lambda: uow
    app.dependency_overrides[get_redis_port] = lambda: redis
    app.dependency_overrides[get_session_store] = lambda: _store(redis)

    with TestClient(app, base_url=BASE_URL) as client:
        yield Harness(app, client, uow, redis, clock)

    app.dependency_overrides.clear()
    reset_settings_cache()


def _seed_user(harness: Harness, user_id: uuid.UUID = USER_ID) -> None:
    user = make_user_row(now=harness.clock())
    user.id = user_id
    harness.uow.users.seed(user)


def _login(harness: Harness, user_id: uuid.UUID = USER_ID) -> None:
    token, _record = _store(harness.redis).create(user_id=user_id, email_verified=True)
    harness.client.headers["Cookie"] = f"session={token}"


def _append(
    harness: Harness,
    *,
    user_id: uuid.UUID = USER_ID,
    wallet_id: uuid.UUID = WALLET_ID,
    entry_type: str = "CREDIT",
    amount: int = 10,
    available_delta: int = 10,
    reserved_delta: int = 0,
    reason: str = "invite_redeem",
    key: str | None = None,
    metadata: dict[str, object] | None = None,
) -> uuid.UUID:
    """推进时钟后追加一条流水，保证 `(created_at, id)` 顺序可预期。"""

    harness.clock.advance(1)
    entry = harness.uow.point_ledger.append(
        LedgerEntryDraft(
            wallet_id=wallet_id,
            user_id=user_id,
            entry_type=entry_type,
            available_delta=available_delta,
            reserved_delta=reserved_delta,
            amount=amount,
            idempotency_key=key or f"synthetic-{uuid.uuid4()}",
            reason=reason,
            metadata=dict(metadata or {}),
        )
    )
    return entry.id


# --------------------------------------------------------------------------- #
# 认证
# --------------------------------------------------------------------------- #
def test_balance_requires_session(harness: Harness) -> None:
    response = harness.client.get(BALANCE_PATH)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"


def test_ledger_requires_session(harness: Harness) -> None:
    response = harness.client.get(LEDGER_PATH)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"


def test_balance_rejects_unknown_session_user(harness: Harness) -> None:
    _login(harness)

    response = harness.client.get(BALANCE_PATH)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "SESSION_INVALID"


# --------------------------------------------------------------------------- #
# 余额
# --------------------------------------------------------------------------- #
def test_balance_returns_wallet_snapshot_verbatim(harness: Harness) -> None:
    _seed_user(harness)
    _login(harness)
    harness.uow.point_wallets.seed(
        USER_ID,
        available_points=80,
        reserved_points=20,
        lifetime_credited=100,
        lifetime_consumed=0,
    )

    response = harness.client.get(BALANCE_PATH)

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["available_points"] == 80
    assert data["reserved_points"] == 20
    assert data["lifetime_credited"] == 100
    assert data["lifetime_consumed"] == 0
    assert response.json()["request_id"] == response.headers["X-Request-Id"]


def test_balance_reads_current_rows_not_a_stale_response(harness: Harness) -> None:
    """余额必须是**当次读取**的钱包行：入账后再读要变，前端无从缓存或自行相加。"""

    _seed_user(harness)
    _login(harness)
    points = PointsService(uow=harness.uow)
    points.credit(user_id=USER_ID, amount=30, idempotency_key="k1", reason="seed")

    first = harness.client.get(BALANCE_PATH).json()["data"]
    points.credit(user_id=USER_ID, amount=45, idempotency_key="k2", reason="seed")
    second = harness.client.get(BALANCE_PATH).json()["data"]

    assert first["available_points"] == 30
    assert second["available_points"] == 75


def test_missing_wallet_returns_zero_snapshot_without_creating_row(harness: Harness) -> None:
    """无钱包 = 零余额，而不是报错，也不能顺手创建钱包行。"""

    _seed_user(harness)
    _login(harness)

    response = harness.client.get(BALANCE_PATH)

    assert response.status_code == 200
    assert response.json()["data"]["available_points"] == 0
    assert response.json()["data"]["reserved_points"] == 0
    assert harness.uow.point_wallets.get_by_user_id(USER_ID) is None


def test_balance_response_has_no_extra_or_missing_keys(harness: Harness) -> None:
    _seed_user(harness)
    _login(harness)

    data = harness.client.get(BALANCE_PATH).json()["data"]

    assert set(data) == {
        "available_points",
        "reserved_points",
        "lifetime_credited",
        "lifetime_consumed",
        "updated_at",
    }


# --------------------------------------------------------------------------- #
# 流水：权限与载荷
# --------------------------------------------------------------------------- #
def test_ledger_only_returns_own_rows(harness: Harness) -> None:
    _seed_user(harness)
    _login(harness)
    mine = _append(harness, key="mine")
    _append(harness, user_id=OTHER_ID, wallet_id=uuid.uuid4(), key="theirs")

    body = harness.client.get(LEDGER_PATH).json()

    assert [row["id"] for row in body["data"]] == [str(mine)]


def test_ledger_row_shape_excludes_audit_metadata(harness: Harness) -> None:
    """用户侧条目只给账务字段：管理员身份与工单引用属于审计上下文，不外泄。"""

    _seed_user(harness)
    _login(harness)
    admin_id = uuid.uuid4()
    _append(
        harness,
        entry_type="ADJUSTMENT",
        available_delta=5,
        reason="support_ticket",
        key="adj",
        metadata={"adjustment_kind": "POSITIVE_CREDIT", "admin_id": str(admin_id)},
    )

    row = harness.client.get(LEDGER_PATH).json()["data"][0]

    assert set(row) == {
        "id",
        "entry_type",
        "amount",
        "available_delta",
        "reserved_delta",
        "reason",
        "generation_id",
        "order_id",
        "created_at",
    }
    assert row["entry_type"] == "ADJUSTMENT"
    assert "metadata" not in row
    assert admin_id.hex not in harness.client.get(LEDGER_PATH).text


# --------------------------------------------------------------------------- #
# 流水：键集分页
# --------------------------------------------------------------------------- #
def test_ledger_pages_newest_first_without_gaps_or_duplicates(harness: Harness) -> None:
    _seed_user(harness)
    _login(harness)
    ids = [_append(harness, key=f"e{i}") for i in range(5)]

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(5):
        params: dict[str, object] = {"limit": 2}
        if cursor is not None:
            params["cursor"] = cursor
        body = harness.client.get(LEDGER_PATH, params=params).json()
        seen.extend(row["id"] for row in body["data"])
        cursor = body["page"]["next_cursor"]
        if cursor is None:
            break

    assert seen == [str(entry_id) for entry_id in reversed(ids)]
    assert cursor is None


def test_ledger_page_flags_last_page(harness: Harness) -> None:
    _seed_user(harness)
    _login(harness)
    ids = [_append(harness, key=f"e{index}") for index in range(3)]

    first = harness.client.get(LEDGER_PATH, params={"limit": 2}).json()
    assert [row["id"] for row in first["data"]] == [str(ids[2]), str(ids[1])]
    assert first["page"]["has_more"] is True
    assert first["page"]["next_cursor"]

    second = harness.client.get(
        LEDGER_PATH, params={"limit": 2, "cursor": first["page"]["next_cursor"]}
    ).json()
    assert [row["id"] for row in second["data"]] == [str(ids[0])]
    assert second["page"]["has_more"] is False
    assert second["page"]["next_cursor"] is None


def test_empty_ledger_returns_empty_page(harness: Harness) -> None:
    _seed_user(harness)
    _login(harness)

    body = harness.client.get(LEDGER_PATH).json()

    assert body["data"] == []
    assert body["page"] == {"next_cursor": None, "has_more": False}


# --------------------------------------------------------------------------- #
# 查询参数边界
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("limit", ["0", "-1", "101", "abc"])
def test_ledger_rejects_out_of_range_limit(harness: Harness, limit: str) -> None:
    _seed_user(harness)
    _login(harness)

    response = harness.client.get(LEDGER_PATH, params={"limit": limit})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert response.json()["error"]["details"] == {"fields": ["limit"]}


def test_ledger_treats_blank_limit_as_default(harness: Harness) -> None:
    """`?limit=` 与缺省同义（与 `GET /me/generations` 一致），不是参数错误。"""

    _seed_user(harness)
    _login(harness)

    assert harness.client.get(LEDGER_PATH, params={"limit": ""}).status_code == 200


@pytest.mark.parametrize("cursor", ["", "   ", "not-a-cursor", "2026-01-01T00:00:00+00:00|nope"])
def test_ledger_rejects_tampered_cursor(harness: Harness, cursor: str) -> None:
    _seed_user(harness)
    _login(harness)
    _append(harness)

    response = harness.client.get(LEDGER_PATH, params={"cursor": cursor})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert response.json()["error"]["details"] == {"fields": ["cursor"]}


def test_ledger_defaults_to_twenty_rows(harness: Harness) -> None:
    _seed_user(harness)
    _login(harness)
    for index in range(25):
        _append(harness, key=f"e{index}")

    body = harness.client.get(LEDGER_PATH).json()

    assert len(body["data"]) == 20
    assert body["page"]["has_more"] is True
