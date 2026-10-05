"""真实 PostgreSQL 上的点数流水翻页（ENG-04 补口的数据库层验证）。

为什么必须在真库跑，而不是只用内存替身：

- `created_at` 是 `timestamptz`，同一事务里 `now()` 恒定不变，因此本文件的主要用例
  天然落在 **`(created_at, id)` 的并列分支**上——游标必须用 `id` 打破平局，
  否则会漏行或重复，这正是键集分页最容易写错的地方；
- 游标比较依赖 PostgreSQL 对 `uuid` 的原生排序语义（`id < :cursor_id`）；
- `limit + 1` 的 `has_more` 判定与 `ix_point_ledger_user_id_created_at_id` 的行过滤
  只能当真库执行才算数。

写入用既有 `make_ledger_entry` 工厂，读取走 `SqlAlchemyPointLedgerRepository`；
Session 以 `create_savepoint` 模式挂在夹具事务上，用例结束整体回滚，不残留合成数据。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.orm import Session

from app.repositories.points import SqlAlchemyPointLedgerRepository


@pytest.fixture
def ledger_repository(db: Connection) -> Iterator[SqlAlchemyPointLedgerRepository]:
    """把 Repository 绑到夹具的 Connection 上：读同一事务内写入的行，结束即回滚。"""

    session = Session(bind=db, join_transaction_mode="create_savepoint")
    try:
        yield SqlAlchemyPointLedgerRepository(session)
    finally:
        session.close()


@pytest.fixture
def make_clocked_ledger_entry(db: Connection) -> Callable[..., uuid.UUID]:
    """插入一条**指定 `created_at`** 的流水（用于证明时间倒序，而不是 id 倒序）。"""

    def _make(
        wallet_id: uuid.UUID,
        user_id: uuid.UUID,
        *,
        created_at: str,
        key: str,
        entry_type: str = "CREDIT",
    ) -> uuid.UUID:
        return db.execute(
            text(
                """
                INSERT INTO point_ledger (
                    wallet_id, user_id, entry_type, available_delta, reserved_delta,
                    amount, idempotency_key, reason, metadata, created_at
                ) VALUES (
                    :wallet_id, :user_id, :entry_type, 10, 0, 10,
                    :key, 'synthetic', '{}'::jsonb,
                    (now() AT TIME ZONE 'UTC') + make_interval(secs => :offset_seconds)
                )
                RETURNING id
                """
            ),
            {
                "wallet_id": wallet_id,
                "user_id": user_id,
                "entry_type": entry_type,
                "key": key,
                "offset_seconds": created_at,
            },
        ).scalar_one()

    return _make


def _expected_order(rows: list[uuid.UUID]) -> list[uuid.UUID]:
    """同一时间戳下数据库按 `id DESC` 返回：uuid 的字节序由 PostgreSQL 定义。"""

    return sorted(rows, reverse=True)


def test_first_page_returns_newest_rows_and_flags_more(
    make_user: Callable[..., uuid.UUID],
    make_wallet: Callable[..., uuid.UUID],
    make_ledger_entry: Callable[..., uuid.UUID],
    ledger_repository: SqlAlchemyPointLedgerRepository,
) -> None:
    user_id = make_user()
    wallet_id = make_wallet(user_id)
    ids = [
        make_ledger_entry(wallet_id, user_id, entry_type="CREDIT", amount=index + 1)
        for index in range(5)
    ]

    page = ledger_repository.list_for_user(user_id=user_id, cursor=None, limit=3)

    assert [row.id for row in page.rows] == _expected_order(ids)[:3]
    assert page.has_more is True


def test_cursor_walk_covers_every_row_exactly_once(
    make_user: Callable[..., uuid.UUID],
    make_wallet: Callable[..., uuid.UUID],
    make_ledger_entry: Callable[..., uuid.UUID],
    ledger_repository: SqlAlchemyPointLedgerRepository,
) -> None:
    """并列时间戳 + 奇偶页大小都要走完：漏行与重复都在这一条里现形。"""

    user_id = make_user()
    wallet_id = make_wallet(user_id)
    ids = [
        make_ledger_entry(wallet_id, user_id, entry_type="CREDIT", amount=index + 1)
        for index in range(7)
    ]
    ordered = _expected_order(ids)

    seen: list[uuid.UUID] = []
    cursor: tuple[object, uuid.UUID] | None = None
    for _ in range(10):
        page = ledger_repository.list_for_user(user_id=user_id, cursor=cursor, limit=3)
        seen.extend(row.id for row in page.rows)
        if not page.has_more:
            break
        cursor = (page.rows[-1].created_at, page.rows[-1].id)

    assert seen == ordered
    assert len(set(seen)) == len(seen)


def test_other_users_rows_never_appear(
    make_user: Callable[..., uuid.UUID],
    make_wallet: Callable[..., uuid.UUID],
    make_ledger_entry: Callable[..., uuid.UUID],
    ledger_repository: SqlAlchemyPointLedgerRepository,
) -> None:
    mine = make_user()
    theirs = make_user()
    my_wallet = make_wallet(mine)
    their_wallet = make_wallet(theirs)
    mine_ids = [
        make_ledger_entry(my_wallet, mine, entry_type="CREDIT", amount=index + 1)
        for index in range(3)
    ]
    for index in range(4):
        make_ledger_entry(their_wallet, theirs, entry_type="CREDIT", amount=index + 1)

    page = ledger_repository.list_for_user(user_id=mine, cursor=None, limit=10)

    assert [row.id for row in page.rows] == _expected_order(mine_ids)
    assert page.has_more is False


def test_ordering_follows_created_at_not_id(
    make_user: Callable[..., uuid.UUID],
    make_wallet: Callable[..., uuid.UUID],
    make_clocked_ledger_entry: Callable[..., uuid.UUID],
    ledger_repository: SqlAlchemyPointLedgerRepository,
) -> None:
    """最晚写入的一行必须排在最前，即使它的 `id` 字节序更小。"""

    user_id = make_user()
    wallet_id = make_wallet(user_id)
    oldest = make_clocked_ledger_entry(wallet_id, user_id, created_at=-3600, key="old")
    newest = make_clocked_ledger_entry(wallet_id, user_id, created_at=3600, key="new")

    page = ledger_repository.list_for_user(user_id=user_id, cursor=None, limit=10)

    assert [row.id for row in page.rows] == [newest, oldest]
    assert page.rows[0].created_at > page.rows[1].created_at


def test_cursor_from_a_newer_row_excludes_older_pages(
    make_user: Callable[..., uuid.UUID],
    make_wallet: Callable[..., uuid.UUID],
    make_clocked_ledger_entry: Callable[..., uuid.UUID],
    ledger_repository: SqlAlchemyPointLedgerRepository,
) -> None:
    user_id = make_user()
    wallet_id = make_wallet(user_id)
    for offset in (-2, -1, 0, 1, 2):
        make_clocked_ledger_entry(
            wallet_id, user_id, created_at=offset * 60, key=f"e{offset}"
        )

    first = ledger_repository.list_for_user(user_id=user_id, cursor=None, limit=2)
    anchor = first.rows[-1]
    second = ledger_repository.list_for_user(
        user_id=user_id, cursor=(anchor.created_at, anchor.id), limit=2
    )

    assert len(first.rows) == 2
    assert len(second.rows) == 2
    assert anchor.id not in [row.id for row in second.rows]
    assert all(row.created_at < anchor.created_at for row in second.rows)
    assert second.has_more is True


def test_empty_result_for_user_without_rows(
    make_user: Callable[..., uuid.UUID],
    make_wallet: Callable[..., uuid.UUID],
    ledger_repository: SqlAlchemyPointLedgerRepository,
) -> None:
    user_id = make_user()
    make_wallet(user_id)

    page = ledger_repository.list_for_user(user_id=user_id, cursor=None, limit=20)

    assert page.rows == ()
    assert page.has_more is False
