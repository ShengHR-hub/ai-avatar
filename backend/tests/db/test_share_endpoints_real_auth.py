"""ENG-09 第三轮返修：**真实鉴权栈**在隔离数据库上的分享/资产端点验证。

## 为什么要这个文件

第三轮报告里写过「`get_uow` 覆盖不到当前用户查询」——**这句是错的**。
实际代码是：

```text
app/api/deps.py:166  def get_uow(): with SqlAlchemyUnitOfWork(get_session_factory()) as uow: yield uow
app/api/deps.py:199  def get_current_user(current, uow=Depends(get_uow)):
app/api/deps.py:205      user = uow.users.get(current.record.user_id)
```

`get_current_user` 本身就依赖 `get_uow`，邮箱验证也走同一个 `uow`。
因此"端点 + 真实数据库"的组合**完全可以**构造：覆盖 `get_uow` 指向隔离库、
把会话写进真的会话存储（内存 Redis 替身），就能走完整鉴权栈。

本文件就是这么做的：**不跳过鉴权**，不伪造 `CurrentSession`，
用真实 Cookie 通过 `get_current_session → get_current_user → require_verified_email`。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import sessionmaker

from app.api.deps import get_redis_port, get_session_store, get_uow
from app.config import reset_settings_cache
from app.main import create_app
from app.repositories.unit_of_work import SqlAlchemyUnitOfWork
from app.services.auth.session_store import RedisSessionStore
from tests.services.fakes import FakeRedis

SESSION_SECRET = "synthetic-session-secret-for-db-tests"
SHARES_PATH = "/api/v1/shares"
BASE_URL = "https://testserver"
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


class BoundSessionStore(RedisSessionStore):
    """真实会话存储 + 一个显式的"已验证邮箱"会话。

    `RedisSessionStore.create` 的签名由 ENG-04 冻结，这里不改它，
    只用子类在创建时带上 `email_verified=True`（等价于用户点过验证链接）。
    """

    def create_verified(self, *, user_id: uuid.UUID, email: str) -> str:
        token, _record = self.create(user_id=user_id, email_verified=True)
        self._email = email
        return token


@pytest.fixture
def db_engine(test_database_url: str) -> Iterator[Engine]:
    engine = create_engine(test_database_url, poolclass=None)
    try:
        yield engine
    finally:
        engine.dispose()


def _factory(engine: Engine):
    maker = sessionmaker(bind=engine, autoflush=False, expire_on_commit=True)
    return lambda: maker()


@pytest.fixture
def harness(
    monkeypatch: pytest.MonkeyPatch, db_engine: Engine
) -> Iterator[tuple[TestClient, Engine, FakeRedis]]:
    """真实鉴权 + 真实数据库的端点 harness（Redis 用内存替身，不启动容器）。

    `get_uow` 的覆盖必须和真实实现一样**进入上下文**：
    `SqlAlchemyUnitOfWork.__enter__` 才是绑定 `users`/`emails`/`shares`... 的地方
    （见 `app/repositories/unit_of_work.py` 第 136 行）。直接 `lambda: uow` 返回一个
    没有进过上下文的实例，`uow.users` 根本不存在 —— 上一轮报告把它误判成了
    "覆盖不到"，实际是覆盖方式不对。
    """

    monkeypatch.setenv("SESSION_SECRET", SESSION_SECRET)
    reset_settings_cache()

    redis = FakeRedis(clock=lambda: NOW)
    app: FastAPI = create_app()
    factory = _factory(db_engine)

    def _bound_uow():
        with SqlAlchemyUnitOfWork(factory) as uow:
            yield uow

    app.dependency_overrides[get_uow] = _bound_uow
    app.dependency_overrides[get_redis_port] = lambda: redis

    with TestClient(app, base_url=BASE_URL) as client:
        yield client, db_engine, redis

    app.dependency_overrides.clear()
    reset_settings_cache()


def _seed_verified_user(engine: Engine) -> tuple[uuid.UUID, str]:
    with engine.connect() as conn:
        user_id = conn.execute(
            text(
                "INSERT INTO users (status, locale, timezone, updated_at)"
                " VALUES ('ACTIVE','zh-CN','UTC', now()) RETURNING id"
            )
        ).scalar_one()
        email = f"synthetic-{uuid.uuid4().hex[:8]}@example.com"
        conn.execute(
            text(
                "INSERT INTO user_emails (id, user_id, email, is_primary, verified_at,"
                " created_at)"
                " VALUES (gen_random_uuid(), :u, :e, true, now(), now())"
            ),
            {"u": user_id, "e": email},
        )
        conn.commit()
    return user_id, email


def _seed_shareable_task(
    engine: Engine, *, user_id: uuid.UUID
) -> tuple[uuid.UUID, list[uuid.UUID]]:
    with engine.connect() as conn:
        cost_id = conn.execute(
            text(
                "INSERT INTO generation_cost_configs (id, mode, model_name, output_count,"
                " points_cost, cost_multiplier, effective_from, status, created_at)"
                " VALUES (gen_random_uuid(), 'TEXT_PROMPT', NULL, 2, 10, 1, now(),"
                " 'ACTIVE', now()) RETURNING id"
            )
        ).scalar_one()
        generation_id = conn.execute(
            text(
                "INSERT INTO generation_tasks (id, user_id, mode, status, billing_status,"
                " prompt, requested_count, cost_config_id, cost_snapshot, reserved_points,"
                " idempotency_key, retry_count, created_at, updated_at)"
                " VALUES (gen_random_uuid(), :u, 'TEXT_PROMPT', 'SUCCEEDED', 'CONSUMED',"
                " 'synthetic', 2, :c, '{}'::jsonb, 10, :k, 0, now(), now()) RETURNING id"
            ),
            {"u": user_id, "c": cost_id, "k": f"gen-{uuid.uuid4().hex[:12]}"},
        ).scalar_one()
        asset_ids: list[uuid.UUID] = []
        for index in range(2):
            asset_id = conn.execute(
                text(
                    """
                    INSERT INTO media_assets (id, user_id, asset_type, purpose, status,
                        visibility, storage_provider, storage_key, mime_type, size_bytes,
                        sha256, expires_at, created_at)
                    VALUES (gen_random_uuid(), :u, 'RESULT', 'RESULT', 'READY', 'PRIVATE',
                            'MINIO', :key, 'image/png', 1024, :sha, :exp, now())
                    RETURNING id
                    """
                ),
                {
                    "u": user_id,
                    "key": f"{user_id}/r{index}",
                    "sha": "a" * 64,
                    "exp": NOW + timedelta(days=30),
                },
            ).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO generation_outputs (generation_id, asset_id, output_index,"
                    " created_at) VALUES (:g, :a, :i, now())"
                ),
                {"g": generation_id, "a": asset_id, "i": index},
            )
            asset_ids.append(asset_id)
        conn.commit()
    return generation_id, asset_ids


def _cleanup(engine: Engine) -> None:
    with engine.connect() as conn:
        conn.execute(text("DELETE FROM share_assets"))
        conn.execute(text("DELETE FROM shares"))
        conn.execute(text("DELETE FROM feedback"))
        conn.execute(text("DELETE FROM generation_outputs"))
        conn.execute(text("DELETE FROM generation_tasks"))
        conn.execute(text("DELETE FROM generation_cost_configs"))
        conn.execute(text("DELETE FROM media_assets"))
        conn.execute(text("DELETE FROM user_emails"))
        conn.execute(text("DELETE FROM users"))
        conn.commit()


def _login(client: TestClient, redis: FakeRedis, *, user_id: uuid.UUID) -> None:
    """走真实会话存储写一个 Cookie（不是伪造 CurrentSession 依赖）。"""

    store = RedisSessionStore(redis, secret=SESSION_SECRET)
    token, _record = store.create(user_id=user_id, email_verified=True)
    client.headers["Cookie"] = f"session={token}"


# --------------------------------------------------------------------------- #
# 用例
# --------------------------------------------------------------------------- #
def test_authenticated_share_creation_works_end_to_end_on_real_database(
    harness: tuple[TestClient, Engine, FakeRedis],
) -> None:
    """**完整鉴权栈** + 真实数据库：创建分享 → 201，且资产真的被置为 SHARED。"""

    client, engine, redis = harness
    try:
        user_id, _email = _seed_verified_user(engine)
        generation_id, asset_ids = _seed_shareable_task(engine, user_id=user_id)
        _login(client, redis, user_id=user_id)

        response = client.post(
            SHARES_PATH,
            json={
                "generation_id": str(generation_id),
                "output_asset_ids": [str(value) for value in asset_ids],
            },
            headers={"Idempotency-Key": "synthetic-real-auth-key"},
        )

        assert response.status_code == 201, response.text
        slug = response.json()["data"]["slug"]
        assert response.json()["data"]["outputs"].__len__() == 2

        with engine.connect() as conn:
            visibilities = list(
                conn.execute(
                    text("SELECT visibility FROM media_assets WHERE user_id = :u"),
                    {"u": user_id},
                ).scalars()
            )
            shares = conn.execute(
                text("SELECT count(*) FROM shares WHERE user_id = :u"), {"u": user_id}
            ).scalar_one()
        assert visibilities == ["SHARED", "SHARED"]
        assert shares == 1

        # 同载荷重复提交：回放同一条（幂等键真实生效）
        replay = client.post(
            SHARES_PATH,
            json={
                "generation_id": str(generation_id),
                "output_asset_ids": [str(value) for value in asset_ids],
            },
            headers={"Idempotency-Key": "synthetic-real-auth-key"},
        )
        assert replay.status_code == 201
        assert replay.json()["data"]["slug"] == slug
    finally:
        _cleanup(engine)


def test_email_verification_gate_is_enforced_and_then_satisfied(
    harness: tuple[TestClient, Engine, FakeRedis],
) -> None:
    """鉴权没有被跳过：邮箱验证这道门真的会拦人，补上验证记录后才放行。

    注意这道门读的是**数据库事实**（`user_emails.verified_at`），而不是会话里的
    标记 —— 所以这里把两步都测掉：先造一个没有验证记录的用户（必须被拒），
    再补上验证记录（必须放行）。只测其中一步都证明不了"门在链路上"。
    """

    client, engine, redis = harness
    try:
        # 用户存在，但**没有任何已验证邮箱**
        user_id, _email = _seed_verified_user(engine)
        with engine.connect() as conn:
            conn.execute(
                text("UPDATE user_emails SET verified_at = NULL WHERE user_id = :u"),
                {"u": user_id},
            )
            conn.commit()
        generation_id, asset_ids = _seed_shareable_task(engine, user_id=user_id)

        store = RedisSessionStore(redis, secret=SESSION_SECRET)
        token, _record = store.create(user_id=user_id, email_verified=True)
        client.headers["Cookie"] = f"session={token}"

        blocked = client.post(
            SHARES_PATH,
            json={
                "generation_id": str(generation_id),
                "output_asset_ids": [str(value) for value in asset_ids],
            },
            headers={"Idempotency-Key": "synthetic-unverified-key"},
        )
        assert blocked.status_code in {401, 403}, blocked.text
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM shares")).scalar_one() == 0

        # 补上验证记录 → 同一会话、同一请求必须放行
        with engine.connect() as conn:
            conn.execute(
                text("UPDATE user_emails SET verified_at = now() WHERE user_id = :u"),
                {"u": user_id},
            )
            conn.commit()
        allowed = client.post(
            SHARES_PATH,
            json={
                "generation_id": str(generation_id),
                "output_asset_ids": [str(value) for value in asset_ids],
            },
            headers={"Idempotency-Key": "synthetic-unverified-key"},
        )
        assert allowed.status_code == 201, allowed.text
    finally:
        _cleanup(engine)


def test_no_session_is_rejected_by_the_real_auth_stack(
    harness: tuple[TestClient, Engine, FakeRedis],
) -> None:
    """没有 Cookie 时 401：鉴权确实在链路上。"""

    client, engine, _redis = harness
    try:
        user_id, _email = _seed_verified_user(engine)
        generation_id, asset_ids = _seed_shareable_task(engine, user_id=user_id)

        response = client.post(
            SHARES_PATH,
            json={
                "generation_id": str(generation_id),
                "output_asset_ids": [str(value) for value in asset_ids],
            },
            headers={"Idempotency-Key": "synthetic-anon-key"},
        )

        assert response.status_code == 401, response.text
    finally:
        _cleanup(engine)


def test_anonymous_share_read_returns_404_before_and_200_after_auth_stack_round_trip(
    harness: tuple[TestClient, Engine, FakeRedis],
) -> None:
    """匿名读取走的是真实路由（`security: []`），并与创建结果一致。"""

    client, engine, redis = harness
    try:
        user_id, _email = _seed_verified_user(engine)
        generation_id, asset_ids = _seed_shareable_task(engine, user_id=user_id)
        _login(client, redis, user_id=user_id)

        created = client.post(
            SHARES_PATH,
            json={
                "generation_id": str(generation_id),
                "output_asset_ids": [str(asset_ids[0])],
            },
            headers={"Idempotency-Key": "synthetic-anon-read-key"},
        )
        assert created.status_code == 201, created.text
        slug = created.json()["data"]["slug"]

        # 先确认一个不存在的 slug 是 404（证明 404 不是"什么都 404"）
        missing = client.get(f"{SHARES_PATH}/nonexistent-slug-aaaaaaaaaaaaaaaaaaa")
        assert missing.status_code == 404

        # 去掉 Cookie：匿名读取真实存在的分享必须 200
        client.headers.pop("Cookie")
        read = client.get(f"{SHARES_PATH}/{slug}")
        assert read.status_code == 200, read.text
        assert read.json()["data"]["slug"] == slug
    finally:
        _cleanup(engine)
