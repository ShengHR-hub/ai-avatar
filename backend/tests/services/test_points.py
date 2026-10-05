"""ENG-04 第 2–6 步：点数账务、结算、人工复核与审计测试（**不依赖 PostgreSQL**）。

覆盖：

- `apply_wallet_delta` 的纯算术与 `version` 递增；
- 钱包/流水替身满足 `PointWalletRepository` / `PointLedgerRepository` 协议；
- `SqlAlchemyPointWalletRepository.lock_by_user_id` 必须生成 `FOR UPDATE`（行锁）；
- Repository 只 `flush`、**从不 `commit`**（`06-development-standard.md` 第 2 节）；
- 流水的 `find_*` 语义与只追加特性；
- `PointsService` 的幂等回放、同键不同参数 `IDEMPOTENCY_CONFLICT`、反向终局结算拒绝、
  钱包快照与流水的四条不变量、先锁钱包后写流水的顺序；
- `TransactionalPointsPort` 的事务边界（失败回滚 + 关闭会话）；
- **第 2 步扩围**：`GenerationTaskRepository` 的行锁/只读语义、
  `AuditLogRepository` 的只追加语义与审计上下文留痕、`UnitOfWork` 聚合出
  `generation_tasks` 与 `audit_logs`；
- **第 3 步**：结算入口固定锁顺序（任务 → 钱包）、金额只取
  `generation_tasks.reserved_points`、`billing_status` 推进且任务 `status` 不变；
- **第 4 步**：`resolve_needs_review` 只接受 `NEEDS_REVIEW + RESERVED`、决策取值、
  同向幂等回放、反向 `IDEMPOTENCY_CONFLICT`、审计留痕与前后快照；
- **第 5 步**：`adjust` / `adjust_wallet` 的管理员留痕、前后钱包快照、
  `ADJUSTMENT` 流水与独立审计日志，且不改预留、不覆盖历史；
- **第 6 步**：跨仓储事件流验证顺序，以及写流水失败时的整体回滚语义；
- **修正批次（总控 2026-09-27 裁决 A/C）**：任务不存在与归属不符 → `GENERATION_NOT_FOUND`
  （404）、`NEEDS_REVIEW` 前置状态不符 → `IDEMPOTENCY_CONFLICT`（409）；账务不变量破坏
  站点锁定为 `WalletLedgerError`（**断言不是 `ApiError`**，对外由既有 500 处理器映射为
  `INTERNAL_ERROR`）；同向幂等回放与反向拒绝决定都**不**追加审计，只保留首次成功那条。

真实 PostgreSQL 上的行锁、CHECK 约束、部分唯一索引与追加保护留待集成阶段验证。
"""

from __future__ import annotations

import inspect
import uuid
from datetime import datetime, timezone

import pytest

from app.errors.codes import ErrorCode
from app.errors.exceptions import ApiError

from app.db.models.audit import AuditLog
from app.db.models.points import PointWallet
from app.repositories.audit import (
    AuditEntryDraft,
    AuditLogRepository,
    SqlAlchemyAuditLogRepository,
)
from app.repositories.generation import (
    GenerationTaskRepository,
    SqlAlchemyGenerationTaskRepository,
)
from app.repositories.points import (
    LedgerEntryDraft,
    PointLedgerRepository,
    PointWalletRepository,
    SqlAlchemyPointLedgerRepository,
    SqlAlchemyPointWalletRepository,
    WalletDelta,
    apply_wallet_delta,
)
from app.repositories.unit_of_work import SqlAlchemyUnitOfWork, UnitOfWork
from tests.services.fakes import (
    InMemoryAuditLogRepository,
    InMemoryGenerationTaskRepository,
    InMemoryPointLedgerRepository,
    InMemoryPointWalletRepository,
    MutableClock,
    PointsUnitOfWork,
    RecordingSession,
    make_generation_task_row,
    make_user_row,
)

USER_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")


# --------------------------------------------------------------------------- #
# 纯算术
# --------------------------------------------------------------------------- #
def test_apply_wallet_delta_updates_snapshot_and_version() -> None:
    wallet = PointWallet(
        user_id=USER_ID,
        available_points=100,
        reserved_points=0,
        lifetime_credited=100,
        lifetime_consumed=0,
        version=3,
    )

    apply_wallet_delta(
        wallet, WalletDelta(available=-20, reserved=20)
    )

    assert wallet.available_points == 80
    assert wallet.reserved_points == 20
    assert wallet.lifetime_credited == 100
    assert wallet.lifetime_consumed == 0
    assert wallet.version == 4


def test_apply_wallet_delta_consume_moves_reserved_to_lifetime_consumed() -> None:
    wallet = PointWallet(
        user_id=USER_ID,
        available_points=80,
        reserved_points=20,
        lifetime_credited=100,
        lifetime_consumed=0,
        version=1,
    )

    apply_wallet_delta(wallet, WalletDelta(reserved=-20, lifetime_consumed=20))

    assert wallet.available_points == 80
    assert wallet.reserved_points == 0
    assert wallet.lifetime_consumed == 20
    assert wallet.version == 2


def test_wallet_delta_defaults_are_zero() -> None:
    delta = WalletDelta()

    assert (delta.available, delta.reserved) == (0, 0)
    assert (delta.lifetime_credited, delta.lifetime_consumed) == (0, 0)


def test_ledger_entry_draft_defaults() -> None:
    draft = LedgerEntryDraft(
        wallet_id=uuid.uuid4(),
        user_id=USER_ID,
        entry_type="CREDIT",
        available_delta=100,
        reserved_delta=0,
        amount=100,
        idempotency_key="synthetic-key",
        reason="invite",
    )

    assert draft.generation_id is None
    assert draft.order_id is None
    assert draft.metadata == {}


# --------------------------------------------------------------------------- #
# 协议一致性
# --------------------------------------------------------------------------- #
def test_in_memory_repositories_satisfy_protocols(
    points_uow,
) -> None:
    assert isinstance(points_uow.point_wallets, PointWalletRepository)
    assert isinstance(points_uow.point_ledger, PointLedgerRepository)


def test_sqlalchemy_repositories_satisfy_protocols(recording_session: RecordingSession) -> None:
    assert isinstance(
        SqlAlchemyPointWalletRepository(recording_session), PointWalletRepository
    )
    assert isinstance(
        SqlAlchemyPointLedgerRepository(recording_session), PointLedgerRepository
    )


# --------------------------------------------------------------------------- #
# 行锁（关键：账务事务第一步）
# --------------------------------------------------------------------------- #
def test_wallet_lock_uses_for_update(recording_session: RecordingSession) -> None:
    repository = SqlAlchemyPointWalletRepository(recording_session)

    repository.lock_by_user_id(USER_ID)

    assert recording_session.statements, "未生成查询语句"
    assert "FOR UPDATE" in recording_session.statements[0].upper()


def test_wallet_read_does_not_lock(recording_session: RecordingSession) -> None:
    repository = SqlAlchemyPointWalletRepository(recording_session)

    repository.get_by_user_id(USER_ID)

    assert "FOR UPDATE" not in recording_session.statements[0].upper()


# --------------------------------------------------------------------------- #
# 事务边界：Repository 不得 commit
# --------------------------------------------------------------------------- #
def test_wallet_repository_never_commits(recording_session: RecordingSession) -> None:
    repository = SqlAlchemyPointWalletRepository(recording_session)
    wallet = PointWallet(
        user_id=USER_ID,
        available_points=0,
        reserved_points=0,
        lifetime_credited=0,
        lifetime_consumed=0,
        version=0,
    )

    repository.lock_by_user_id(USER_ID)
    repository.create(user_id=USER_ID)
    repository.apply_delta(wallet, WalletDelta(available=10))

    assert recording_session.commits == 0
    assert recording_session.flushes >= 2


def test_ledger_repository_never_commits(recording_session: RecordingSession) -> None:
    repository = SqlAlchemyPointLedgerRepository(recording_session)

    repository.append(
        LedgerEntryDraft(
            wallet_id=uuid.uuid4(),
            user_id=USER_ID,
            entry_type="CREDIT",
            available_delta=100,
            reserved_delta=0,
            amount=100,
            idempotency_key="synthetic-key",
            reason="invite",
        )
    )

    assert recording_session.commits == 0
    assert recording_session.flushes == 1


@pytest.mark.parametrize(
    "repository_class",
    [
        SqlAlchemyPointWalletRepository,
        SqlAlchemyPointLedgerRepository,
        SqlAlchemyGenerationTaskRepository,
        SqlAlchemyAuditLogRepository,
    ],
)
def test_repository_source_has_no_commit_call(repository_class) -> None:
    source = inspect.getsource(repository_class)

    assert ".commit(" not in source


# --------------------------------------------------------------------------- #
# 流水只追加与查询语义
# --------------------------------------------------------------------------- #
def test_ledger_append_is_append_only(points_uow) -> None:
    ledger = points_uow.point_ledger
    generation_id = uuid.uuid4()

    ledger.append(
        LedgerEntryDraft(
            wallet_id=uuid.uuid4(),
            user_id=USER_ID,
            entry_type="RESERVE",
            available_delta=-20,
            reserved_delta=20,
            amount=20,
            idempotency_key="reserve-1",
            reason="generation",
            generation_id=generation_id,
        )
    )

    assert not hasattr(ledger, "update")
    assert not hasattr(ledger, "delete")
    assert len(ledger.entries) == 1


def test_ledger_find_by_idempotency_key(points_uow) -> None:
    ledger = points_uow.point_ledger
    ledger.append(
        LedgerEntryDraft(
            wallet_id=uuid.uuid4(),
            user_id=USER_ID,
            entry_type="CREDIT",
            available_delta=100,
            reserved_delta=0,
            amount=100,
            idempotency_key="credit-1",
            reason="invite",
        )
    )

    assert ledger.find_by_idempotency_key("credit-1") is not None
    assert ledger.find_by_idempotency_key("missing") is None


def test_ledger_finds_reserve_and_final_per_generation(points_uow) -> None:
    ledger = points_uow.point_ledger
    generation_id = uuid.uuid4()
    wallet_id = uuid.uuid4()

    ledger.append(
        LedgerEntryDraft(
            wallet_id=wallet_id,
            user_id=USER_ID,
            entry_type="RESERVE",
            available_delta=-20,
            reserved_delta=20,
            amount=20,
            idempotency_key="reserve-1",
            reason="generation",
            generation_id=generation_id,
        )
    )
    assert ledger.find_reserve_for_generation(generation_id) is not None
    assert ledger.find_final_for_generation(generation_id) is None

    ledger.append(
        LedgerEntryDraft(
            wallet_id=wallet_id,
            user_id=USER_ID,
            entry_type="RELEASE",
            available_delta=20,
            reserved_delta=-20,
            amount=20,
            idempotency_key="release-1",
            reason="generation",
            generation_id=generation_id,
        )
    )

    final = ledger.find_final_for_generation(generation_id)
    assert final is not None
    assert final.entry_type == "RELEASE"


# --------------------------------------------------------------------------- #
# 钱包替身语义
# --------------------------------------------------------------------------- #
def test_in_memory_wallet_lock_records_calls(points_uow) -> None:
    wallets = points_uow.point_wallets
    wallets.seed(USER_ID, available_points=50)

    locked = wallets.lock_by_user_id(USER_ID)

    assert locked is not None
    assert locked.available_points == 50
    assert wallets.lock_calls == [USER_ID]


def test_in_memory_wallet_create_starts_at_zero(points_uow) -> None:
    wallet = points_uow.point_wallets.create(user_id=USER_ID)

    assert wallet.available_points == 0
    assert wallet.reserved_points == 0
    assert wallet.version == 0
    assert points_uow.point_wallets.get_by_user_id(USER_ID) is wallet


def test_points_uow_counts_commits() -> None:
    uow = PointsUnitOfWork()

    uow.commit()
    uow.rollback()

    assert (uow.commits, uow.rollbacks) == (1, 1)


def test_wallet_and_ledger_fakes_are_independent_instances() -> None:
    first = InMemoryPointWalletRepository()
    second = InMemoryPointWalletRepository()

    assert first is not second
    assert isinstance(InMemoryPointLedgerRepository(), PointLedgerRepository)


# --------------------------------------------------------------------------- #
# ENG-04 第 2 步 ②：Points Service
# --------------------------------------------------------------------------- #
def _service(uow, clock=None):
    from app.services.points.service import PointsService

    return PointsService(uow=uow, clock=clock)


def _make_invite_code(
    *,
    raw: str = "AVTR-ABCD-EFGH",
    status: str = "ACTIVE",
    initial_points: int = 100,
    max_redemptions: int = 1,
    redeemed_count: int = 0,
    expires_at=None,
):
    from app.db.models.invites import InviteCode
    from app.services.auth.invite_code import hash_invite_code

    code = InviteCode(
        code_hash=hash_invite_code(raw),
        status=status,
        initial_points=initial_points,
        max_redemptions=max_redemptions,
        redeemed_count=redeemed_count,
        expires_at=expires_at,
        created_at=datetime.now(timezone.utc),
    )
    code.id = uuid.uuid4()
    return code


def test_credit_updates_wallet_and_writes_ledger(uow) -> None:
    user = uow.users.seed(make_user_row())

    entry = _service(uow).credit(
        user_id=user.id, amount=100, idempotency_key="credit-1", reason="invite"
    )

    wallet = uow.point_wallets.get_by_user_id(user.id)
    assert entry.entry_type == "CREDIT"
    assert (entry.available_delta, entry.reserved_delta, entry.amount) == (100, 0, 100)
    assert wallet.available_points == 100
    assert wallet.lifetime_credited == 100
    assert wallet.version == 1
    # 钱包在创建前会先尝试锁定，因此可能出现两次；关键是「先锁再写」
    assert set(uow.point_wallets.lock_calls) == {user.id}
    assert uow.point_wallets.lock_calls[0] == user.id
    assert uow.commits == 0  # Service 不提交


def test_credit_is_idempotent(uow) -> None:
    user = uow.users.seed(make_user_row())
    service = _service(uow)

    first = service.credit(user_id=user.id, amount=100, idempotency_key="k", reason="r")
    second = service.credit(user_id=user.id, amount=100, idempotency_key="k", reason="r")

    assert first is second
    assert uow.point_wallets.get_by_user_id(user.id).available_points == 100
    assert len(uow.point_ledger.entries) == 1


def test_credit_rejects_non_positive_amount(uow) -> None:
    user = uow.users.seed(make_user_row())

    with pytest.raises(ApiError) as excinfo:
        _service(uow).credit(user_id=user.id, amount=0, idempotency_key="k", reason="r")

    assert excinfo.value.code is ErrorCode.VALIDATION_ERROR


def test_reserve_moves_available_to_reserved(uow) -> None:
    user = uow.users.seed(make_user_row())
    generation_id = uuid.uuid4()
    service = _service(uow)
    service.credit(user_id=user.id, amount=100, idempotency_key="c", reason="r")

    entry = service.reserve(
        user_id=user.id,
        generation_id=generation_id,
        amount=20,
        idempotency_key="r1",
        reason="generation",
    )

    wallet = uow.point_wallets.get_by_user_id(user.id)
    assert (entry.available_delta, entry.reserved_delta) == (-20, 20)
    assert (wallet.available_points, wallet.reserved_points) == (80, 20)
    assert wallet.version == 2


def test_reserve_requires_sufficient_available_points(uow) -> None:
    user = uow.users.seed(make_user_row())

    with pytest.raises(ApiError) as excinfo:
        _service(uow).reserve(
            user_id=user.id,
            generation_id=uuid.uuid4(),
            amount=10,
            idempotency_key="r1",
            reason="generation",
        )

    assert excinfo.value.code is ErrorCode.INSUFFICIENT_POINTS
    assert excinfo.value.status_code == 409  # 契约第 7 节的冻结映射


def test_reserve_is_unique_per_generation(uow) -> None:
    user = uow.users.seed(make_user_row())
    generation_id = uuid.uuid4()
    service = _service(uow)
    service.credit(user_id=user.id, amount=100, idempotency_key="c", reason="r")

    first = service.reserve(
        user_id=user.id, generation_id=generation_id, amount=20,
        idempotency_key="r1", reason="g",
    )
    second = service.reserve(
        user_id=user.id, generation_id=generation_id, amount=20,
        idempotency_key="r2", reason="g",
    )

    assert first is second
    assert len(uow.point_ledger.entries) == 2  # CREDIT + 一条 RESERVE


def _reserved_task(
    uow,
    user,
    *,
    reserved_points: int = 20,
    status: str = "PROCESSING",
    billing_status: str = "RESERVED",
    generation_id: uuid.UUID | None = None,
    reason: str = "g",
    clock=None,
):
    """预置 `02-database-design.md` §6.1 完成后的状态：充值、预留、任务行可结算。

    返回 `(service, task)`；`billing_status` 刻意可由测试指定，
    以便构造「账务标志已被推进」的第二道防线场景。
    """

    service = _service(uow, clock)
    service.credit(user_id=user.id, amount=100, idempotency_key="c", reason="r")
    task = uow.generation_tasks.seed(
        make_generation_task_row(
            generation_id=generation_id,
            user_id=user.id,
            status=status,
            billing_status=billing_status,
            reserved_points=reserved_points,
        )
    )
    service.reserve(
        user_id=user.id,
        generation_id=task.id,
        amount=reserved_points,
        idempotency_key="r1",
        reason=reason,
    )
    return service, task


def test_consume_settles_reserved_into_lifetime_consumed(uow) -> None:
    user = uow.users.seed(make_user_row())
    service, task = _reserved_task(uow, user)

    entry = service.consume(
        user_id=user.id, generation_id=task.id, idempotency_key="f1", reason="g"
    )

    wallet = uow.point_wallets.get_by_user_id(user.id)
    assert (entry.available_delta, entry.reserved_delta, entry.amount) == (0, -20, 20)
    assert (wallet.available_points, wallet.reserved_points) == (80, 0)
    assert wallet.lifetime_consumed == 20


def test_release_returns_reserved_to_available(uow) -> None:
    user = uow.users.seed(make_user_row())
    service, task = _reserved_task(uow, user)

    entry = service.release(
        user_id=user.id, generation_id=task.id, idempotency_key="f1", reason="g"
    )

    wallet = uow.point_wallets.get_by_user_id(user.id)
    assert (entry.available_delta, entry.reserved_delta) == (20, -20)
    assert (wallet.available_points, wallet.reserved_points) == (100, 0)
    assert wallet.lifetime_consumed == 0


@pytest.mark.parametrize(
    ("entry_type", "expected_billing_status", "expected_consumed"),
    [("CONSUME", "CONSUMED", 20), ("RELEASE", "RELEASED", 0)],
)
def test_settlement_advances_billing_status_but_not_task_status(
    uow, entry_type: str, expected_billing_status: str, expected_consumed: int
) -> None:
    """第 3 步：结算只推进 `billing_status`；任务生命周期 `status` 属于 Generation Service。"""

    user = uow.users.seed(make_user_row())
    service, task = _reserved_task(uow, user)
    assert uow.generation_tasks.billing_status_calls == []

    getattr(service, entry_type.lower())(
        user_id=user.id, generation_id=task.id, idempotency_key="f1", reason="g"
    )

    assert task.billing_status == expected_billing_status
    assert task.status == "PROCESSING"
    assert uow.generation_tasks.billing_status_calls == [(task.id, expected_billing_status)]
    wallet = uow.point_wallets.get_by_user_id(user.id)
    assert wallet.lifetime_consumed == expected_consumed
    assert wallet.reserved_points == 0


def test_settlement_amount_comes_from_the_task_not_the_caller(uow) -> None:
    """`consume` / `release` 不再接受 `amount` 入参：金额只取 `reserved_points`。"""

    import inspect

    from app.services.points.service import PointsService

    for name in ("consume", "release"):
        assert "amount" not in inspect.signature(getattr(PointsService, name)).parameters

    user = uow.users.seed(make_user_row())
    service, task = _reserved_task(uow, user, reserved_points=7)

    entry = service.consume(
        user_id=user.id, generation_id=task.id, idempotency_key="f1", reason="g"
    )

    wallet = uow.point_wallets.get_by_user_id(user.id)
    assert entry.amount == 7
    assert (wallet.available_points, wallet.reserved_points) == (93, 0)
    assert wallet.lifetime_consumed == 7


def test_consume_and_release_are_mutually_exclusive(uow) -> None:
    """已 `CONSUMED` 的任务再 `RELEASE`：409，且不写反向流水、不改余额。"""

    user = uow.users.seed(make_user_row())
    service, task = _reserved_task(uow, user)
    consumed = service.consume(
        user_id=user.id, generation_id=task.id, idempotency_key="f1", reason="g"
    )

    with pytest.raises(ApiError) as excinfo:
        service.release(
            user_id=user.id, generation_id=task.id, idempotency_key="f2", reason="g"
        )

    assert excinfo.value.code is ErrorCode.IDEMPOTENCY_CONFLICT
    assert excinfo.value.status_code == 409
    finals = [e for e in uow.point_ledger.entries if e.entry_type in ("CONSUME", "RELEASE")]
    assert finals == [consumed]
    wallet = uow.point_wallets.get_by_user_id(user.id)
    assert (wallet.available_points, wallet.reserved_points, wallet.version) == (80, 0, 3)
    assert task.billing_status == "CONSUMED"


@pytest.mark.parametrize(
    ("first", "second", "expected_available"),
    [("consume", "release", 80), ("release", "consume", 100)],
)
def test_reverse_settlement_never_writes_the_opposite_entry(
    uow, first: str, second: str, expected_available: int
) -> None:
    user = uow.users.seed(make_user_row())
    service, task = _reserved_task(uow, user)
    getattr(service, first)(
        user_id=user.id, generation_id=task.id, idempotency_key="f1", reason="g"
    )
    entries_before = len(uow.point_ledger.entries)
    locks_before = len(uow.point_wallets.lock_calls)
    billing_calls_before = len(uow.generation_tasks.billing_status_calls)

    with pytest.raises(ApiError) as excinfo:
        getattr(service, second)(
            user_id=user.id, generation_id=task.id, idempotency_key="f2", reason="g"
        )

    assert excinfo.value.code is ErrorCode.IDEMPOTENCY_CONFLICT
    assert len(uow.point_ledger.entries) == entries_before
    # 拒绝发生在锁定钱包之前；只有第一把任务锁
    assert len(uow.point_wallets.lock_calls) == locks_before
    assert len(uow.generation_tasks.billing_status_calls) == billing_calls_before
    wallet = uow.point_wallets.get_by_user_id(user.id)
    assert wallet.available_points == expected_available
    assert wallet.reserved_points == 0
    assert uow.commits == 0


def test_same_direction_settlement_replays_even_with_a_new_idempotency_key(uow) -> None:
    """`LEAD-04` §5.5：已完成**相同**结算操作是幂等成功（换键也回放原流水）。"""

    user = uow.users.seed(make_user_row())
    service, task = _reserved_task(uow, user)
    first = service.consume(
        user_id=user.id, generation_id=task.id, idempotency_key="f1", reason="g"
    )
    replay = service.consume(
        user_id=user.id, generation_id=task.id, idempotency_key="f2", reason="g"
    )
    same_key = service.consume(
        user_id=user.id, generation_id=task.id, idempotency_key="f1", reason="g"
    )

    assert replay is first
    assert same_key is first
    assert len(uow.point_ledger.entries) == 3
    assert uow.generation_tasks.billing_status_calls == [(task.id, "CONSUMED")]


def test_settlement_requires_existing_reserve(uow) -> None:
    from app.services.points.service import WalletLedgerError

    user = uow.users.seed(make_user_row())
    task = uow.generation_tasks.seed(make_generation_task_row(user_id=user.id))

    with pytest.raises(WalletLedgerError):
        _service(uow).consume(
            user_id=user.id, generation_id=task.id, idempotency_key="f1", reason="g"
        )


# --------------------------------------------------------------------------- #
# ENG-04 第 5 步：人工调整的管理员留痕
# --------------------------------------------------------------------------- #
ADMIN_ID = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
OTHER_ADMIN_ID = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-ffffffffffff")


def _adjust(
    service,
    user_id,
    *,
    amount: int = 30,
    kind: str = "POSITIVE_CREDIT",
    key: str = "a1",
    reason: str = "admin",
    admin_id=ADMIN_ID,
    request_id: str = "req_adjust_0001",
    case_reference: str = "CASE-42",
    metadata: dict[str, object] | None = None,
):
    params: dict[str, object] = {
        "user_id": user_id,
        "amount": amount,
        "adjustment_kind": kind,
        "idempotency_key": key,
        "reason": reason,
        "admin_id": admin_id,
        "request_id": request_id,
        "case_reference": case_reference,
    }
    if metadata is not None:
        params["metadata"] = metadata
    return service.adjust(**params)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("kind", "signed"),
    [("POSITIVE_CREDIT", 30), ("NEGATIVE_CONSUMPTION", -30)],
)
def test_adjust_does_not_touch_reserved(uow, kind: str, signed: int) -> None:
    user = uow.users.seed(make_user_row())
    service = _service(uow)
    service.credit(user_id=user.id, amount=100, idempotency_key="c", reason="r")

    entry = _adjust(service, user.id, kind=kind)

    wallet = uow.point_wallets.get_by_user_id(user.id)
    assert entry.entry_type == "ADJUSTMENT"
    assert entry.available_delta == signed
    assert entry.reserved_delta == 0
    assert entry.metadata_["adjustment_kind"] == kind
    assert wallet.available_points == 100 + signed
    assert wallet.reserved_points == 0
    if signed > 0:
        assert wallet.lifetime_credited == 130
    else:
        assert wallet.lifetime_consumed == 30


def test_adjust_rejects_unknown_kind(uow) -> None:
    user = uow.users.seed(make_user_row())

    with pytest.raises(ApiError) as excinfo:
        _adjust(_service(uow), user.id, kind="OTHER")

    assert excinfo.value.code is ErrorCode.VALIDATION_ERROR


def test_adjust_negative_below_zero_is_rejected(uow) -> None:
    user = uow.users.seed(make_user_row())

    with pytest.raises(ApiError) as excinfo:
        _adjust(_service(uow), user.id, amount=10, kind="NEGATIVE_CONSUMPTION")

    assert excinfo.value.code is ErrorCode.INSUFFICIENT_POINTS


def test_adjust_is_idempotent(uow) -> None:
    user = uow.users.seed(make_user_row())
    service = _service(uow)

    first = _adjust(service, user.id, amount=10)
    second = _adjust(service, user.id, amount=10)

    assert first is second
    assert uow.point_wallets.get_by_user_id(user.id).available_points == 10


@pytest.mark.parametrize(
    "missing",
    [
        {"admin_id": None},
        {"request_id": ""},
        {"case_reference": "   "},
        {"reason": ""},
    ],
)
def test_adjust_requires_full_admin_context(uow, missing: dict[str, object]) -> None:
    """第 5 步：缺管理员 / 请求 ID / 工单引用 / 理由一律 `VALIDATION_ERROR`，且不锁钱包。"""

    user = uow.users.seed(make_user_row())
    overrides = {
        "admin_id": ADMIN_ID,
        "request_id": "req_adjust_0001",
        "case_reference": "CASE-42",
        "reason": "admin",
    }
    overrides.update(missing)

    with pytest.raises(ApiError) as excinfo:
        _service(uow).adjust(
            user_id=user.id,
            amount=10,
            adjustment_kind="POSITIVE_CREDIT",
            idempotency_key="a1",
            **overrides,  # type: ignore[arg-type]
        )

    assert excinfo.value.code is ErrorCode.VALIDATION_ERROR
    assert uow.point_wallets.lock_calls == []
    assert uow.point_ledger.entries == ()
    assert uow.audit_logs.entries == ()


def test_balance_reflects_snapshot_and_defaults_to_zero(uow) -> None:
    user = uow.users.seed(make_user_row())
    service = _service(uow)
    service.credit(user_id=user.id, amount=42, idempotency_key="c", reason="r")

    balance = service.balance(user_id=user.id)
    empty = service.balance(user_id=uuid.uuid4())

    assert balance.available_points == 42
    assert balance.lifetime_credited == 42
    assert empty.available_points == 0
    assert empty.reserved_points == 0


# --------------------------------------------------------------------------- #
# redeem_invite（PointsPort 真实适配）
# --------------------------------------------------------------------------- #
def _redeem_service(uow, session_store):
    from app.services.points.service import InviteRedeemService

    return InviteRedeemService(uow=uow, session_store=session_store)


def test_redeem_invite_creates_user_email_redemption_credit_and_session(
    uow, fake_redis, session_secret
) -> None:
    from app.services.auth.session_store import RedisSessionStore

    code = uow.invite_codes.seed(_make_invite_code(initial_points=150))
    store = RedisSessionStore(fake_redis, secret=session_secret)

    outcome = _redeem_service(uow, store).redeem_invite(
        invite_code="avtr-abcd-efgh", email="new@example.com", session_ttl_seconds=600
    )

    # 用户 + 主邮箱
    assert outcome.user.primary_email == "new@example.com"
    assert outcome.user.email_verified is False
    # 点数
    assert outcome.points.available_points == 150
    assert outcome.points.lifetime_credited == 150
    # 兑换记录 + 计数
    assert uow.invite_redemptions.find(
        invite_code_id=code.id, user_id=outcome.user.id
    ) is not None
    assert code.redeemed_count == 1
    assert code.status == "USED"  # max_redemptions=1 用尽
    # CREDIT 流水
    credits = [e for e in uow.point_ledger.entries if e.entry_type == "CREDIT"]
    assert len(credits) == 1
    assert credits[0].available_delta == 150
    # Session
    assert store.read(outcome.session_token) is not None
    assert outcome.session.session_id == store.read(outcome.session_token).session_id
    # 一次提交
    assert uow.commits == 1


def test_redeem_invite_rejects_unknown_code(uow, fake_redis, session_secret) -> None:
    from app.services.auth.session_store import RedisSessionStore

    store = RedisSessionStore(fake_redis, secret=session_secret)

    with pytest.raises(ApiError) as excinfo:
        _redeem_service(uow, store).redeem_invite(
            invite_code="AVTR-NOPE-NOPE", email="a@example.com", session_ttl_seconds=600
        )

    assert excinfo.value.code is ErrorCode.INVITE_INVALID
    assert uow.commits == 0


@pytest.mark.parametrize(
    ("status", "expected"),
    [("USED", ErrorCode.INVITE_INVALID), ("DISABLED", ErrorCode.INVITE_INVALID)],
)
def test_redeem_invite_rejects_non_active_code(
    uow, fake_redis, session_secret, status: str, expected: ErrorCode
) -> None:
    from app.services.auth.session_store import RedisSessionStore

    uow.invite_codes.seed(_make_invite_code(status=status))
    store = RedisSessionStore(fake_redis, secret=session_secret)

    with pytest.raises(ApiError) as excinfo:
        _redeem_service(uow, store).redeem_invite(
            invite_code="AVTR-ABCD-EFGH", email="a@example.com", session_ttl_seconds=600
        )

    assert excinfo.value.code is expected


def test_redeem_invite_rejects_exhausted_code(uow, fake_redis, session_secret) -> None:
    from app.services.auth.session_store import RedisSessionStore

    uow.invite_codes.seed(_make_invite_code(max_redemptions=2, redeemed_count=2))
    store = RedisSessionStore(fake_redis, secret=session_secret)

    with pytest.raises(ApiError) as excinfo:
        _redeem_service(uow, store).redeem_invite(
            invite_code="AVTR-ABCD-EFGH", email="a@example.com", session_ttl_seconds=600
        )

    assert excinfo.value.code is ErrorCode.INVITE_ALREADY_USED


def test_redeem_invite_rejects_duplicate_redemption(
    uow, fake_redis, session_secret
) -> None:
    from app.services.auth.session_store import RedisSessionStore

    code = uow.invite_codes.seed(_make_invite_code(max_redemptions=5))
    store = RedisSessionStore(fake_redis, secret=session_secret)
    service = _redeem_service(uow, store)

    first = service.redeem_invite(
        invite_code="AVTR-ABCD-EFGH", email="a@example.com", session_ttl_seconds=600
    )
    with pytest.raises(ApiError) as excinfo:
        service.redeem_invite(
            invite_code="AVTR-ABCD-EFGH", email="a@example.com", session_ttl_seconds=600
        )

    assert excinfo.value.code is ErrorCode.INVITE_ALREADY_USED
    assert code.redeemed_count == 1
    assert uow.point_wallets.get_by_user_id(first.user.id).available_points == 100


def test_redeem_invite_rejects_suspended_user(uow, fake_redis, session_secret) -> None:
    from app.services.auth.session_store import RedisSessionStore

    suspended = uow.users.seed(make_user_row(status="SUSPENDED"))
    uow.emails.add(user_id=suspended.id, email="a@example.com", is_primary=True)
    uow.invite_codes.seed(_make_invite_code())
    store = RedisSessionStore(fake_redis, secret=session_secret)

    with pytest.raises(ApiError) as excinfo:
        _redeem_service(uow, store).redeem_invite(
            invite_code="AVTR-ABCD-EFGH", email="a@example.com", session_ttl_seconds=600
        )

    assert excinfo.value.code is ErrorCode.ACCOUNT_SUSPENDED


def test_transactional_points_port_uses_injected_uow(uow, fake_redis, session_secret) -> None:
    from app.services.auth.session_store import RedisSessionStore
    from app.services.ports import PointsPort, TransactionalPointsPort

    uow.invite_codes.seed(_make_invite_code(initial_points=77))
    store = RedisSessionStore(fake_redis, secret=session_secret)
    port = TransactionalPointsPort(uow_factory=lambda: uow, session_store=store)

    assert isinstance(port, PointsPort)
    outcome = port.redeem_invite(
        invite_code="AVTR-ABCD-EFGH", email="port@example.com", session_ttl_seconds=600
    )

    assert outcome.points.available_points == 77
    assert uow.commits == 1


# --------------------------------------------------------------------------- #
# ENG-04 第 2 步收敛：幂等键语义（`LEAD-04` §5.4）
# --------------------------------------------------------------------------- #
def _credit(uow, user_id, *, amount=100, key="k", reason="invite"):
    return _service(uow).credit(
        user_id=user_id, amount=amount, idempotency_key=key, reason=reason
    )


def test_replay_with_identical_parameters_returns_first_entry(uow) -> None:
    first_user = uow.users.seed(make_user_row())

    first = _credit(uow, first_user.id)
    second = _credit(uow, first_user.id)

    assert second is first
    assert len(uow.point_ledger.entries) == 1


def test_replay_ignores_free_text_reason(uow) -> None:
    """`reason` 是展示/审计文本，不参与幂等一致性比较。"""

    user = uow.users.seed(make_user_row())

    first = _credit(uow, user.id, reason="invite_redeem")
    second = _credit(uow, user.id, reason="manual_backfill")

    assert second is first


def test_replay_with_different_amount_is_idempotency_conflict(uow) -> None:
    user = uow.users.seed(make_user_row())
    _credit(uow, user.id, amount=100)

    with pytest.raises(ApiError) as excinfo:
        _credit(uow, user.id, amount=101)

    assert excinfo.value.code is ErrorCode.IDEMPOTENCY_CONFLICT
    assert excinfo.value.status_code == 409


def test_replay_with_different_entry_type_is_idempotency_conflict(uow) -> None:
    user = uow.users.seed(make_user_row())
    _credit(uow, user.id, key="shared-key")
    service = _service(uow)
    service.credit(user_id=user.id, amount=5, idempotency_key="fund", reason="r")

    with pytest.raises(ApiError) as excinfo:
        service.reserve(
            user_id=user.id,
            generation_id=uuid.uuid4(),
            amount=5,
            idempotency_key="shared-key",
            reason="generation",
        )

    assert excinfo.value.code is ErrorCode.IDEMPOTENCY_CONFLICT


def test_replay_with_different_user_is_idempotency_conflict(uow) -> None:
    """`point_ledger.idempotency_key` 全局唯一（`02-database-design.md` §3.2）。"""

    alice = uow.users.seed(make_user_row())
    bob = uow.users.seed(make_user_row())
    _credit(uow, alice.id, key="cross-user")

    with pytest.raises(ApiError) as excinfo:
        _credit(uow, bob.id, key="cross-user")

    assert excinfo.value.code is ErrorCode.IDEMPOTENCY_CONFLICT


def test_replay_with_different_generation_is_idempotency_conflict(uow) -> None:
    user = uow.users.seed(make_user_row())
    service = _service(uow)
    service.credit(user_id=user.id, amount=100, idempotency_key="c", reason="r")
    first_generation = uuid.uuid4()
    service.reserve(
        user_id=user.id, generation_id=first_generation, amount=20,
        idempotency_key="r1", reason="g",
    )

    with pytest.raises(ApiError) as excinfo:
        service.reserve(
            user_id=user.id,
            generation_id=uuid.uuid4(),
            amount=20,
            idempotency_key="r1",
            reason="g",
        )

    assert excinfo.value.code is ErrorCode.IDEMPOTENCY_CONFLICT


def test_replay_with_flipped_adjustment_kind_is_idempotency_conflict(uow) -> None:
    user = uow.users.seed(make_user_row())
    _credit(uow, user.id)
    service = _service(uow)
    _adjust(service, user.id, kind="POSITIVE_CREDIT")

    with pytest.raises(ApiError) as excinfo:
        _adjust(service, user.id, kind="NEGATIVE_CONSUMPTION")

    assert excinfo.value.code is ErrorCode.IDEMPOTENCY_CONFLICT
    # 冲突路径不得留下新流水或新审计，只有第一次成功调整的那条审计
    assert len(uow.audit_logs.entries) == 1
    assert len(uow.point_ledger.entries) == 2


def test_idempotency_conflict_writes_no_entry_and_keeps_snapshot(uow) -> None:
    user = uow.users.seed(make_user_row())
    _credit(uow, user.id, amount=100)
    before = uow.point_wallets.get_by_user_id(user.id)
    snapshot = (before.available_points, before.reserved_points, before.version)
    entries_before = len(uow.point_ledger.entries)
    locks_before = len(uow.point_wallets.lock_calls)

    with pytest.raises(ApiError):
        _credit(uow, user.id, amount=7)

    assert len(uow.point_ledger.entries) == entries_before
    assert (before.available_points, before.reserved_points, before.version) == snapshot
    # 冲突在锁定钱包之前返回：不产生行锁，也不提交
    assert len(uow.point_wallets.lock_calls) == locks_before
    assert uow.commits == 0


# --------------------------------------------------------------------------- #
# ENG-04 第 2 步收敛：账务不变量（`02-database-design.md` §3.2 四条快照等式）
# --------------------------------------------------------------------------- #
def _assert_snapshot_matches_ledger(wallet, entries) -> None:
    credited = [
        e
        for e in entries
        if e.entry_type == "CREDIT"
        or (
            e.entry_type == "ADJUSTMENT"
            and e.metadata_.get("adjustment_kind") == "POSITIVE_CREDIT"
        )
    ]
    consumed = [
        e
        for e in entries
        if e.entry_type == "CONSUME"
        or (
            e.entry_type == "ADJUSTMENT"
            and e.metadata_.get("adjustment_kind") == "NEGATIVE_CONSUMPTION"
        )
    ]

    assert wallet.available_points == sum(e.available_delta for e in entries)
    assert wallet.reserved_points == sum(e.reserved_delta for e in entries)
    assert wallet.lifetime_credited == sum(e.amount for e in credited)
    assert wallet.lifetime_consumed == sum(e.amount for e in consumed)
    # 每条流水恰好推进一次快照版本
    assert wallet.version == len(entries)


def test_snapshot_matches_ledger_after_reserve_and_consume(uow) -> None:
    user = uow.users.seed(make_user_row())
    service, task = _reserved_task(uow, user)
    service.consume(
        user_id=user.id, generation_id=task.id, idempotency_key="f1", reason="g"
    )

    wallet = uow.point_wallets.get_by_user_id(user.id)
    entries = uow.point_ledger.entries
    _assert_snapshot_matches_ledger(wallet, entries)
    assert [e.entry_type for e in entries] == ["CREDIT", "RESERVE", "CONSUME"]
    assert (wallet.available_points, wallet.reserved_points) == (80, 0)


def test_snapshot_matches_ledger_after_reserve_release_and_adjustments(uow) -> None:
    user = uow.users.seed(make_user_row())
    service, task = _reserved_task(uow, user)
    service.release(
        user_id=user.id, generation_id=task.id, idempotency_key="f1", reason="g"
    )
    _adjust(service, user.id, amount=5, key="a1")
    _adjust(service, user.id, amount=3, key="a2", kind="NEGATIVE_CONSUMPTION")

    wallet = uow.point_wallets.get_by_user_id(user.id)
    _assert_snapshot_matches_ledger(wallet, uow.point_ledger.entries)
    # 释放把预留退回可用；ADJUSTMENT 永不改变预留
    assert wallet.reserved_points == 0
    assert wallet.available_points == 102
    assert wallet.lifetime_credited == 105
    assert wallet.lifetime_consumed == 3
    assert task.billing_status == "RELEASED"


def test_every_write_locks_the_wallet_before_appending(uow) -> None:
    """固定顺序：锁钱包 → 插流水 → 更新快照（`02-database-design.md` §6.1）。"""

    user = uow.users.seed(make_user_row())
    service, task = _reserved_task(uow, user)
    service.consume(
        user_id=user.id, generation_id=task.id, idempotency_key="f1", reason="g"
    )

    assert uow.point_wallets.lock_calls[0] == user.id
    assert set(uow.point_wallets.lock_calls) == {user.id}
    assert len(uow.point_ledger.entries) == 3
    assert uow.commits == 0  # 事务边界由调用方持有


# --------------------------------------------------------------------------- #
# ENG-04 第 2 步收敛：默认端口的事务边界（失败必须回滚）
# --------------------------------------------------------------------------- #
class _TransactionRecordingSession:
    """`SqlAlchemyUnitOfWork` 所需的最小 session 替身（只观察事务动词）。"""

    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0
        self.closes = 0
        self.flushes = 0
        self.statements: list[str] = []

    def execute(self, statement, *args, **kwargs):
        self.statements.append(str(statement))

        class _Result:
            def scalar_one_or_none(self_inner):
                return None

        return _Result()

    def add(self, instance):
        return None

    def flush(self):
        self.flushes += 1

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closes += 1


def _sql_port(session, session_store):
    from app.repositories.unit_of_work import SqlAlchemyUnitOfWork
    from app.services.ports import TransactionalPointsPort

    return TransactionalPointsPort(
        uow_factory=lambda: SqlAlchemyUnitOfWork(lambda: session),
        session_store=session_store,
    )


def test_default_port_shape_rolls_back_database_transaction_on_failure(
    fake_redis, session_secret
) -> None:
    """端口抛错时 `SqlAlchemyUnitOfWork` 必须 `rollback()` + `close()`，且从不提交。"""

    from app.errors.exceptions import ApiError
    from app.services.auth.session_store import RedisSessionStore

    session = _TransactionRecordingSession()
    store = RedisSessionStore(fake_redis, secret=session_secret)
    port = _sql_port(session, store)

    # 邀请码查不到 → `INVITE_INVALID`（ApiError），在提交前失败
    with pytest.raises(ApiError):
        port.redeem_invite(
            invite_code="AVTR-ABCD-EFGH",
            email="a@example.com",
            session_ttl_seconds=600,
        )

    assert (session.commits, session.rollbacks, session.closes) == (0, 1, 1)
    # 失败路径不得留下会话
    assert fake_redis.keys() == ()


def test_sqlalchemy_unit_of_work_commits_once_on_success_and_closes() -> None:
    session = _TransactionRecordingSession()

    with SqlAlchemyUnitOfWork(lambda: session) as uow:
        uow.commit()

    assert (session.commits, session.rollbacks, session.closes) == (1, 0, 1)


# --------------------------------------------------------------------------- #
# ENG-04 第 2 步（扩围）：生成任务与审计日志 Repository
# --------------------------------------------------------------------------- #
def test_generation_task_lock_uses_for_update(recording_session: RecordingSession) -> None:
    """结算事务的第一把锁必须是任务行（`LEAD-04` §6:127，裁决 ②）。"""

    repository = SqlAlchemyGenerationTaskRepository(recording_session)

    repository.lock_by_id(uuid.uuid4())

    assert recording_session.statements, "未生成查询语句"
    assert "FOR UPDATE" in recording_session.statements[0].upper()
    assert recording_session.commits == 0


def test_generation_task_read_does_not_lock(recording_session: RecordingSession) -> None:
    repository = SqlAlchemyGenerationTaskRepository(recording_session)

    repository.get_by_id(uuid.uuid4())

    assert "FOR UPDATE" not in recording_session.statements[0].upper()


def test_generation_task_repositories_satisfy_protocol(uow) -> None:
    assert isinstance(
        SqlAlchemyGenerationTaskRepository(RecordingSession()), GenerationTaskRepository
    )
    assert isinstance(uow.generation_tasks, GenerationTaskRepository)


def test_generation_task_repository_exposes_only_the_billing_status_write_path(uow) -> None:
    """第 3 步：任务仓储**只**开放 `billing_status` 写路径。

    原断言（第 2 步扩围）要求仓储零写方法；第 3 步授权 `set_billing_status`
    与唯一调用方（结算入口）一起落地，因此改写本条：
    任务生命周期状态 `set_status` / `update_status` 等仍然**必须不存在**，
    Points Service 不能借此补造任务状态转换（`LEAD-04` §2:43、§8、§11）。
    """

    for repository in (
        uow.generation_tasks,
        SqlAlchemyGenerationTaskRepository(RecordingSession()),
    ):
        for name in (
            "set_status",
            "update_status",
            "set_lifecycle_status",
            "mark_consumed",
            "mark_released",
            "delete",
            "update",
            "save",
        ):
            assert not hasattr(repository, name), name
        assert hasattr(repository, "set_billing_status")
    assert not hasattr(SqlAlchemyGenerationTaskRepository, "commit")


def test_set_billing_status_writes_only_the_billing_column(recording_session) -> None:
    """`set_billing_status` 只改 `billing_status`：不碰 `status`，只 flush 不 commit。"""

    repository = SqlAlchemyGenerationTaskRepository(recording_session)
    task = make_generation_task_row(status="NEEDS_REVIEW", billing_status="RESERVED")

    repository.set_billing_status(task, "CONSUMED")

    assert task.billing_status == "CONSUMED"
    assert task.status == "NEEDS_REVIEW"
    assert recording_session.flushes == 1
    assert recording_session.commits == 0


def test_in_memory_generation_task_lock_records_calls(uow) -> None:
    task = uow.generation_tasks.seed(make_generation_task_row())
    other_id = uuid.uuid4()

    locked = uow.generation_tasks.lock_by_id(task.id)
    missing = uow.generation_tasks.lock_by_id(other_id)

    assert locked is task
    assert locked.status == "PROCESSING"
    assert locked.billing_status == "RESERVED"
    assert locked.reserved_points == 20
    assert missing is None
    assert uow.generation_tasks.lock_calls == [task.id, other_id]
    # 加锁读与只读分别记账，便于断言结算入口确实走行锁
    assert uow.generation_tasks.read_calls == []
    assert uow.generation_tasks.get_by_id(task.id) is task
    assert uow.generation_tasks.read_calls == [task.id]


def test_generation_task_default_row_is_settleable(uow) -> None:
    """替身工厂产出的行必须满足 `02-database-design.md` §3.3 的 CHECK 形态。"""

    task = uow.generation_tasks.seed(make_generation_task_row())

    assert task.reserved_points > 0
    assert task.requested_count > 0
    assert task.cost_snapshot
    assert task.idempotency_key
    assert task.user_id is not None
    assert task.parent_task_id is None


# --------------------------------------------------------------------------- #
# 审计日志：只追加
# --------------------------------------------------------------------------- #
def _audit_draft(**overrides: object) -> AuditEntryDraft:
    params: dict[str, object] = {
        "actor_type": "ADMIN",
        "actor_id": uuid.uuid4(),
        "action": "generation.review_resolution",
        "entity_type": "generation_task",
        "entity_id": uuid.uuid4(),
        "request_id": "req_synthetic_0001",
        "metadata": {"decision": "CONSUME", "evidence_ref": "case-42"},
    }
    params.update(overrides)
    return AuditEntryDraft(**params)  # type: ignore[arg-type]


def test_audit_append_persists_full_context(uow) -> None:
    draft = _audit_draft()

    entry = uow.audit_logs.append(draft)
    entries = uow.audit_logs.entries

    assert entries == (entry,)
    assert entry.actor_type == "ADMIN"
    assert entry.actor_id == draft.actor_id
    assert entry.action == "generation.review_resolution"
    assert entry.entity_type == "generation_task"
    assert entry.entity_id == draft.entity_id
    assert entry.request_id == "req_synthetic_0001"
    assert entry.metadata_ == {"decision": "CONSUME", "evidence_ref": "case-42"}
    assert entry.created_at is not None
    assert uow.commits == 0


def test_audit_repository_is_append_only(uow) -> None:
    """`audit_logs` 只追加：不得暴露更新、删除或覆盖历史的方法。"""

    for repository in (uow.audit_logs, SqlAlchemyAuditLogRepository(RecordingSession())):
        for name in ("update", "delete", "remove", "patch", "overwrite", "set_metadata"):
            assert not hasattr(repository, name), name


def test_audit_metadata_is_copied_not_aliased(uow, recording_session) -> None:
    """调用方事后修改 metadata 字典不得篡改已追加的审计事实。"""

    payload = {"decision": "CONSUME"}
    draft = AuditEntryDraft(
        actor_type="ADMIN",
        action="wallet.adjust",
        entity_type="point_wallet",
        metadata=payload,
    )

    in_memory = uow.audit_logs.append(draft)
    sqlalchemy_row = SqlAlchemyAuditLogRepository(recording_session).append(draft)
    payload["decision"] = "TAMPERED"

    assert in_memory.metadata_ == {"decision": "CONSUME"}
    assert sqlalchemy_row.metadata_ == {"decision": "CONSUME"}


def test_sqlalchemy_audit_repository_appends_without_committing(
    recording_session: RecordingSession,
) -> None:
    repository = SqlAlchemyAuditLogRepository(recording_session)

    entry = repository.append(_audit_draft())

    assert isinstance(entry, AuditLog)
    assert recording_session.added and isinstance(recording_session.added[0], AuditLog)
    assert recording_session.flushes == 1
    assert recording_session.commits == 0


def test_audit_repositories_satisfy_protocol(uow) -> None:
    assert isinstance(SqlAlchemyAuditLogRepository(RecordingSession()), AuditLogRepository)
    assert isinstance(uow.audit_logs, AuditLogRepository)
    assert isinstance(InMemoryAuditLogRepository(), AuditLogRepository)
    assert isinstance(InMemoryGenerationTaskRepository(), GenerationTaskRepository)


# --------------------------------------------------------------------------- #
# UnitOfWork 聚合（第 3 步结算入口的装配前提）
# --------------------------------------------------------------------------- #
def test_unit_of_work_declares_settlement_repositories() -> None:
    annotations = set(UnitOfWork.__annotations__)

    assert {
        "generation_tasks",
        "point_wallets",
        "point_ledger",
        "audit_logs",
    } <= annotations


def test_in_memory_unit_of_work_exposes_both_new_repositories(uow) -> None:
    assert isinstance(uow, UnitOfWork)
    assert isinstance(uow.generation_tasks, GenerationTaskRepository)
    assert isinstance(uow.audit_logs, AuditLogRepository)


def test_sqlalchemy_unit_of_work_exposes_both_new_repositories() -> None:
    """同一 UoW 必须同时持有任务锁与钱包锁入口，才能兑现固定锁顺序且不跨事务。"""

    session = _TransactionRecordingSession()

    with SqlAlchemyUnitOfWork(lambda: session) as uow:
        assert isinstance(uow.generation_tasks, SqlAlchemyGenerationTaskRepository)
        assert isinstance(uow.audit_logs, SqlAlchemyAuditLogRepository)
        assert isinstance(uow.point_wallets, PointWalletRepository)
        uow.generation_tasks.lock_by_id(uuid.uuid4())
        uow.point_wallets.lock_by_user_id(USER_ID)

    assert len(session.statements) == 2
    assert "FOR UPDATE" in session.statements[0].upper()
    assert "FOR UPDATE" in session.statements[1].upper()
    assert session.commits == 0
    assert session.closes == 1


# --------------------------------------------------------------------------- #
# ENG-04 第 3 步：固定锁顺序、billing_status 推进与结算前置校验
# --------------------------------------------------------------------------- #
def _ordered(uow) -> list[str]:
    """只保留会破坏顺序断言的事件（读快照事件不参与锁顺序契约）。"""

    return [
        event
        for event in uow.trace
        if event.startswith(("lock:", "append:", "apply_delta:", "billing_status:"))
    ]


def _index(events: list[str], prefix: str) -> int:
    return next(i for i, event in enumerate(events) if event.startswith(prefix))


@pytest.mark.parametrize(
    ("entry_type", "expected"),
    [("CONSUME", "CONSUMED"), ("RELEASE", "RELEASED")],
)
def test_settlement_follows_the_fixed_lock_order(
    uow, entry_type: str, expected: str
) -> None:
    """`LEAD-04` §6:127 + `02-database-design.md` §6.2：任务 → 钱包 → 流水 → 快照 → 计费状态。"""

    user = uow.users.seed(make_user_row())
    service, task = _reserved_task(uow, user)
    uow.trace.clear()

    getattr(service, entry_type.lower())(
        user_id=user.id, generation_id=task.id, idempotency_key="f1", reason="g"
    )

    events = _ordered(uow)
    assert _index(events, "lock:generation_task") == 0, events
    assert _index(events, "lock:generation_task") < _index(events, "lock:wallet")
    assert _index(events, "lock:wallet") < _index(events, f"append:ledger:{entry_type}")
    assert _index(events, f"append:ledger:{entry_type}") < _index(events, "apply_delta:wallet")
    assert events[-1] == f"billing_status:{expected}"
    assert uow.generation_tasks.lock_calls == [task.id]


def test_settlement_rejects_missing_generation_task(uow) -> None:
    """裁决 A：任务不存在 → 既有 `GENERATION_NOT_FOUND`（404），不再是内部异常。"""

    user = uow.users.seed(make_user_row())
    service, task = _reserved_task(uow, user)
    locks_before = len(uow.point_wallets.lock_calls)
    entries_before = len(uow.point_ledger.entries)

    with pytest.raises(ApiError) as excinfo:
        service.consume(
            user_id=user.id, generation_id=uuid.uuid4(), idempotency_key="f1", reason="g"
        )

    assert excinfo.value.code is ErrorCode.GENERATION_NOT_FOUND
    assert excinfo.value.status_code == 404
    # 任务锁失败发生在锁定钱包之前：不产生第二把锁，也不写账
    assert len(uow.point_wallets.lock_calls) == locks_before
    assert len(uow.point_ledger.entries) == entries_before
    assert uow.commits == 0


@pytest.mark.parametrize("entry_type", ["consume", "release"])
def test_task_owned_by_another_user_reports_not_found(uow, entry_type: str) -> None:
    """裁决 A：归属不符与不存在**同一**错误码，不泄露他人任务是否存在。"""

    owner = uow.users.seed(make_user_row())
    stranger = uow.users.seed(make_user_row())
    service, task = _reserved_task(uow, owner)
    locks_before = len(uow.point_wallets.lock_calls)

    with pytest.raises(ApiError) as excinfo:
        getattr(service, entry_type)(
            user_id=stranger.id, generation_id=task.id, idempotency_key="f1", reason="g"
        )

    assert excinfo.value.code is ErrorCode.GENERATION_NOT_FOUND
    assert excinfo.value.status_code == 404
    # 与「任务不存在」逐字一致：响应既不区分存在性，也不得带上他人身份标识
    missing = ApiError(ErrorCode.GENERATION_NOT_FOUND)
    assert excinfo.value.message == missing.message
    assert excinfo.value.details == {}
    assert len(uow.point_wallets.lock_calls) == locks_before
    assert task.billing_status == "RESERVED"
    assert len(uow.point_ledger.entries) == 2


def test_settlement_rejects_non_reserved_billing_status_without_ledger_fact(uow) -> None:
    """第二道防线：`billing_status` 已被推进却没有终局流水 → 不变量错误，不写账。"""

    from app.services.points.service import WalletLedgerError

    user = uow.users.seed(make_user_row())
    service, task = _reserved_task(uow, user)
    task.billing_status = "CONSUMED"  # 人为制造不一致（无 CONSUME 流水）
    entries_before = len(uow.point_ledger.entries)
    locks_before = len(uow.point_wallets.lock_calls)

    with pytest.raises(WalletLedgerError):
        service.consume(
            user_id=user.id, generation_id=task.id, idempotency_key="f1", reason="g"
        )

    assert len(uow.point_ledger.entries) == entries_before
    assert len(uow.point_wallets.lock_calls) == locks_before
    assert uow.generation_tasks.billing_status_calls == []


def test_settlement_rejects_non_positive_reserved_points(uow) -> None:
    from app.services.points.service import WalletLedgerError

    user = uow.users.seed(make_user_row())
    service, task = _reserved_task(uow, user)
    task.reserved_points = 0

    with pytest.raises(WalletLedgerError):
        service.consume(
            user_id=user.id, generation_id=task.id, idempotency_key="f1", reason="g"
        )

    assert task.billing_status == "RESERVED"
    assert len(uow.point_ledger.entries) == 2


def test_settlement_rejects_when_wallet_reserved_balance_is_short(uow) -> None:
    """钱包预留不足以覆盖任务预留：属于不变量破坏，必须整体拒绝而非部分扣减。"""

    from app.services.points.service import WalletLedgerError

    user = uow.users.seed(make_user_row())
    service, task = _reserved_task(uow, user)
    wallet = uow.point_wallets.get_by_user_id(user.id)
    wallet.reserved_points = 5
    version_before = wallet.version
    entries_before = len(uow.point_ledger.entries)

    with pytest.raises(WalletLedgerError):
        service.consume(
            user_id=user.id, generation_id=task.id, idempotency_key="f1", reason="g"
        )

    assert len(uow.point_ledger.entries) == entries_before
    assert wallet.version == version_before
    assert task.billing_status == "RESERVED"


# --------------------------------------------------------------------------- #
# ENG-04 修正批次（总控 2026-09-27 裁决 A）：错误映射与内部异常边界
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("entry_type", ["consume", "release", "resolve_needs_review"])
def test_missing_task_is_generation_not_found_for_every_entry(
    uow, entry_type: str
) -> None:
    """裁决 A：三个结算入口的「任务不存在」都落到既有 `GENERATION_NOT_FOUND`（404）。"""

    user = uow.users.seed(make_user_row())
    service = _service(uow)
    missing = uuid.uuid4()

    with pytest.raises(ApiError) as excinfo:
        if entry_type == "resolve_needs_review":
            service.resolve_needs_review(
                generation_id=missing,
                decision="CONSUME",
                idempotency_key="rev1",
                admin_id=ADMIN_ID,
                request_id="req_review_0001",
                note="evidence verified",
            )
        else:
            getattr(service, entry_type)(
                user_id=user.id, generation_id=missing, idempotency_key="f1", reason="g"
            )

    assert excinfo.value.code is ErrorCode.GENERATION_NOT_FOUND
    assert excinfo.value.status_code == 404
    # 只有任务锁发生（且失败），不锁钱包、不写流水、不写审计
    assert uow.generation_tasks.lock_calls == [missing]
    assert uow.point_wallets.lock_calls == []
    assert uow.point_ledger.entries == ()
    assert uow.audit_logs.entries == ()
    assert uow.commits == 0


@pytest.mark.parametrize(
    "breakage",
    [
        "missing_reserve",
        "non_positive_reserved_points",
        "billing_advanced_without_final_ledger",
        "wallet_reserved_short",
    ],
)
def test_ledger_invariant_breakage_stays_internal_error(uow, breakage: str) -> None:
    """裁决 A 的边界（防误改）：账务不变量破坏**必须**仍是 `WalletLedgerError`。

    `missing_reserve` / `non_positive_reserved_points` /
    `billing_advanced_without_final_ledger` / `wallet_reserved_short` 都不是业务分支，
    对外由 `app/errors/handlers.py` 已注册的未处理异常处理器统一给出
    `INTERNAL_ERROR`（500）；Service 内**不得**为此新增 `ApiError` 抛出点或错误码。
    """

    from app.services.points.service import WalletLedgerError

    user = uow.users.seed(make_user_row())
    service, task = _reserved_task(uow, user)
    wallet = uow.point_wallets.get_by_user_id(user.id)

    if breakage == "missing_reserve":
        # 任务存在但从未 RESERVE：流水不变量被破坏
        task = uow.generation_tasks.seed(make_generation_task_row(user_id=user.id))
    elif breakage == "non_positive_reserved_points":
        task.reserved_points = 0
    elif breakage == "billing_advanced_without_final_ledger":
        task.billing_status = "CONSUMED"  # 人为制造标志与流水矛盾
    else:
        wallet.reserved_points = 5  # 低于任务声明的 20

    with pytest.raises(WalletLedgerError) as excinfo:
        service.consume(
            user_id=user.id, generation_id=task.id, idempotency_key="f1", reason="g"
        )

    assert type(excinfo.value) is WalletLedgerError
    assert not isinstance(excinfo.value, ApiError)
    # 未写新流水、未推进计费状态、未提交
    assert len(uow.point_ledger.entries) == 2
    assert uow.generation_tasks.billing_status_calls == []
    assert uow.commits == 0


def test_wallet_that_cannot_be_locked_is_internal_error(uow) -> None:
    """钱包无法创建或锁定（裁决 A 保持站点）：内部异常，不是 `ApiError`。"""

    from app.services.points.service import WalletLedgerError

    user = uow.users.seed(make_user_row())
    service = _service(uow)
    uow.point_wallets.lock_by_user_id = lambda user_id: None  # type: ignore[assignment]
    uow.point_wallets.create = lambda **kwargs: None  # type: ignore[assignment]

    with pytest.raises(WalletLedgerError) as excinfo:
        service.credit(user_id=user.id, amount=10, idempotency_key="c2", reason="r")

    assert type(excinfo.value) is WalletLedgerError
    assert not isinstance(excinfo.value, ApiError)
    assert uow.point_ledger.entries == ()


def test_negative_reserved_balance_is_internal_error() -> None:
    """预留余额出现负数（裁决 A 保持站点）：不变量破坏，内部异常。"""

    from app.db.models.points import PointWallet
    from app.services.points.service import PointsService, WalletLedgerError

    wallet = PointWallet(
        user_id=USER_ID,
        available_points=0,
        reserved_points=-1,
        lifetime_credited=0,
        lifetime_consumed=0,
    )

    with pytest.raises(WalletLedgerError) as excinfo:
        PointsService._assert_non_negative(wallet)

    assert type(excinfo.value) is WalletLedgerError
    assert not isinstance(excinfo.value, ApiError)


def test_unusable_final_entry_type_is_internal_error(uow) -> None:
    """非法终局流水类型（裁决 A 保持站点）：内部编程错误，不是业务冲突。"""

    from app.services.points.service import WalletLedgerError

    user = uow.users.seed(make_user_row())
    service, task = _reserved_task(uow, user)

    with pytest.raises(WalletLedgerError) as excinfo:
        service._settle_locked(task, entry_type="REFUND", idempotency_key="x", reason="g")

    assert type(excinfo.value) is WalletLedgerError
    assert not isinstance(excinfo.value, ApiError)
    assert len(uow.point_ledger.entries) == 2
    assert task.billing_status == "RESERVED"


def test_in_memory_generation_task_set_billing_status_records_calls(uow) -> None:
    clock = MutableClock()
    task = uow.generation_tasks.seed(make_generation_task_row(now=clock()))
    task.updated_at = clock()

    uow.generation_tasks.set_billing_status(task, "RELEASED")

    assert task.billing_status == "RELEASED"
    assert uow.generation_tasks.billing_status_calls == [(task.id, "RELEASED")]
    assert f"billing_status:RELEASED" in uow.trace


# --------------------------------------------------------------------------- #
# ENG-04 第 4 步：人工复核 resolve_needs_review
# --------------------------------------------------------------------------- #
def _review_task(uow, user, *, reserved_points: int = 20, clock=None):
    """预置 `NEEDS_REVIEW + RESERVED`（`LEAD-04` §3：未完成人工决定必须保持 RESERVED）。"""

    return _reserved_task(
        uow,
        user,
        reserved_points=reserved_points,
        status="NEEDS_REVIEW",
        clock=clock,
    )


@pytest.mark.parametrize(
    ("decision", "expected_billing_status", "expected_consumed"),
    [("CONSUME", "CONSUMED", 20), ("RELEASE", "RELEASED", 0)],
)
def test_resolve_needs_review_settles_without_changing_task_status(
    uow, decision: str, expected_billing_status: str, expected_consumed: int
) -> None:
    user = uow.users.seed(make_user_row())
    service, task = _review_task(uow, user)

    entry = service.resolve_needs_review(
        generation_id=task.id,
        decision=decision,
        idempotency_key="rev1",
        admin_id=ADMIN_ID,
        request_id="req_review_0001",
        note="provider confirmed charge",
        evidence="attempt-3:request-id-abc",
    )

    wallet = uow.point_wallets.get_by_user_id(user.id)
    assert entry.entry_type == decision
    assert task.status == "NEEDS_REVIEW"  # 决策不补造任务状态转换
    assert task.billing_status == expected_billing_status
    assert wallet.reserved_points == 0
    assert wallet.lifetime_consumed == expected_consumed
    finals = [e for e in uow.point_ledger.entries if e.entry_type in ("CONSUME", "RELEASE")]
    assert len(finals) == 1


def test_resolve_needs_review_amount_comes_from_task_reserved_points(uow) -> None:
    user = uow.users.seed(make_user_row())
    service, task = _review_task(uow, user, reserved_points=9)

    entry = service.resolve_needs_review(
        generation_id=task.id,
        decision="CONSUME",
        idempotency_key="rev1",
        admin_id=ADMIN_ID,
        request_id="req_review_0001",
        note="evidence verified",
    )

    wallet = uow.point_wallets.get_by_user_id(user.id)
    assert entry.amount == 9
    assert (wallet.available_points, wallet.reserved_points) == (91, 0)


@pytest.mark.parametrize("decision", ["consume", "REFUND", "CONSUMED", ""])
def test_resolve_needs_review_rejects_other_decisions(uow, decision: str) -> None:
    user = uow.users.seed(make_user_row())
    service, task = _review_task(uow, user)
    locks_before = len(uow.point_wallets.lock_calls)
    entries_before = len(uow.point_ledger.entries)

    with pytest.raises(ApiError) as excinfo:
        service.resolve_needs_review(
            generation_id=task.id,
            decision=decision,
            idempotency_key="rev1",
            admin_id=ADMIN_ID,
            request_id="req_review_0001",
            note="whatever",
        )

    assert excinfo.value.code is ErrorCode.VALIDATION_ERROR
    assert excinfo.value.details["fields"] == ["decision"]
    # 参数校验发生在任何锁与写之前：既没有任务锁，也没有新的钱包锁
    assert uow.generation_tasks.lock_calls == []
    assert len(uow.point_wallets.lock_calls) == locks_before
    assert len(uow.point_ledger.entries) == entries_before
    assert uow.audit_logs.entries == ()


@pytest.mark.parametrize(
    "overrides",
    [
        {"admin_id": None},
        {"admin_id": str(ADMIN_ID)},
        {"request_id": ""},
        {"request_id": "x" * 129},
        {"note": "  "},
    ],
)
def test_resolve_needs_review_requires_full_admin_context(uow, overrides: dict) -> None:
    user = uow.users.seed(make_user_row())
    service, task = _review_task(uow, user)
    params = {
        "generation_id": task.id,
        "decision": "CONSUME",
        "idempotency_key": "rev1",
        "admin_id": ADMIN_ID,
        "request_id": "req_review_0001",
        "note": "evidence verified",
    }
    params.update(overrides)

    with pytest.raises(ApiError) as excinfo:
        service.resolve_needs_review(**params)

    assert excinfo.value.code is ErrorCode.VALIDATION_ERROR
    assert uow.generation_tasks.lock_calls == []
    assert uow.audit_logs.entries == ()
    assert task.billing_status == "RESERVED"


@pytest.mark.parametrize(
    "status", ["CREATED", "QUEUED", "PROCESSING", "SUCCEEDED", "FAILED"]
)
def test_resolve_needs_review_rejects_non_review_task_status(uow, status: str) -> None:
    """裁决 A：`NEEDS_REVIEW` 前置状态不符 → 既有 `IDEMPOTENCY_CONFLICT`（409）。

    任务存在且流水自洽，属于状态/账务冲突而非不变量破坏，因此不是内部异常。
    """

    from app.services.points.service import WalletLedgerError

    user = uow.users.seed(make_user_row())
    service, task = _reserved_task(uow, user, status=status)  # billing 仍为 RESERVED

    with pytest.raises(ApiError) as excinfo:
        service.resolve_needs_review(
            generation_id=task.id,
            decision="CONSUME",
            idempotency_key="rev1",
            admin_id=ADMIN_ID,
            request_id="req_review_0001",
            note="evidence verified",
        )

    assert excinfo.value.code is ErrorCode.IDEMPOTENCY_CONFLICT
    assert excinfo.value.status_code == 409
    assert not isinstance(excinfo.value, WalletLedgerError)
    assert task.billing_status == "RESERVED"
    assert len(uow.point_ledger.entries) == 2
    assert uow.audit_logs.entries == ()


def test_resolve_needs_review_replays_the_same_decision_idempotently(uow) -> None:
    user = uow.users.seed(make_user_row())
    service, task = _review_task(uow, user)

    first = service.resolve_needs_review(
        generation_id=task.id,
        decision="CONSUME",
        idempotency_key="rev1",
        admin_id=ADMIN_ID,
        request_id="req_review_0001",
        note="first submission",
    )
    wallet_version = uow.point_wallets.get_by_user_id(user.id).version
    replay = service.resolve_needs_review(
        generation_id=task.id,
        decision="CONSUME",
        idempotency_key="rev2",  # 换键也必须回放原决定
        admin_id=OTHER_ADMIN_ID,
        request_id="req_review_0002",
        note="duplicate submission",
    )

    assert replay is first
    assert task.status == "NEEDS_REVIEW"
    assert task.billing_status == "CONSUMED"
    assert uow.generation_tasks.billing_status_calls == [(task.id, "CONSUMED")]
    assert uow.point_wallets.get_by_user_id(user.id).version == wallet_version
    finals = [e for e in uow.point_ledger.entries if e.entry_type in ("CONSUME", "RELEASE")]
    assert finals == [first]
    # 裁决 C：重复提交既不追加流水，也**不追加审计**——审计条数必须不变，
    # 只保留首次成功决定的那一条（`applied` 恒为 True）。
    audits = uow.audit_logs.entries
    assert len(audits) == 1
    assert [e.metadata_["applied"] for e in audits] == [True]
    assert audits[0].metadata_["idempotency_key"] == "rev1"
    assert audits[0].metadata_["note"] == "first submission"
    assert audits[0].request_id == "req_review_0001"


def test_resolve_needs_review_same_key_replay_writes_no_audit(uow) -> None:
    """裁决 C：同键同参回放（`_replay` 命中）也不写审计，只保留首次成功决定那条。"""

    user = uow.users.seed(make_user_row())
    service, task = _review_task(uow, user)
    params = {
        "generation_id": task.id,
        "decision": "CONSUME",
        "idempotency_key": "rev1",
        "admin_id": ADMIN_ID,
        "request_id": "req_review_0001",
        "note": "first submission",
    }

    first = service.resolve_needs_review(**params)
    locks_before = len(uow.point_wallets.lock_calls)
    replay = service.resolve_needs_review(**params)

    assert replay is first
    assert len(uow.point_ledger.entries) == 3  # CREDIT + RESERVE + CONSUME
    assert len(uow.audit_logs.entries) == 1
    assert [e.metadata_["applied"] for e in uow.audit_logs.entries] == [True]
    assert task.billing_status == "CONSUMED"
    assert uow.generation_tasks.billing_status_calls == [(task.id, "CONSUMED")]
    # 回放路径既不再读钱包快照，也不产生新的钱包锁
    assert len(uow.point_wallets.lock_calls) == locks_before


def test_resolve_needs_review_rejects_the_opposite_decision(uow) -> None:
    """裁决 C：反向决定被拒绝，既不写审计也不写账（留痕未来由 Admin API 独立事务承担）。"""

    user = uow.users.seed(make_user_row())
    service, task = _review_task(uow, user)
    service.resolve_needs_review(
        generation_id=task.id,
        decision="CONSUME",
        idempotency_key="rev1",
        admin_id=ADMIN_ID,
        request_id="req_review_0001",
        note="charge confirmed",
    )
    audit_before = len(uow.audit_logs.entries)
    locks_before = len(uow.point_wallets.lock_calls)
    wallet = uow.point_wallets.get_by_user_id(user.id)
    snapshot = (wallet.available_points, wallet.reserved_points, wallet.version)

    with pytest.raises(ApiError) as excinfo:
        service.resolve_needs_review(
            generation_id=task.id,
            decision="RELEASE",
            idempotency_key="rev2",
            admin_id=OTHER_ADMIN_ID,
            request_id="req_review_0002",
            note="changed my mind",
        )

    # 反向决定复用既有 IDEMPOTENCY_CONFLICT（409），不新增错误码
    assert excinfo.value.code is ErrorCode.IDEMPOTENCY_CONFLICT
    assert excinfo.value.status_code == 409
    assert (wallet.available_points, wallet.reserved_points, wallet.version) == snapshot
    assert task.billing_status == "CONSUMED"
    assert len(uow.point_ledger.entries) == 3
    # 审计条数不变：只有首次成功 CONSUME 的那一条，且 `applied` 仍为 True
    assert len(uow.audit_logs.entries) == audit_before == 1
    assert [e.metadata_["applied"] for e in uow.audit_logs.entries] == [True]
    # 拒绝发生在锁定钱包之前
    assert len(uow.point_wallets.lock_calls) == locks_before
    assert uow.commits == 0


def test_resolve_needs_review_appends_full_audit_context(uow, clock) -> None:
    user = uow.users.seed(make_user_row())
    service, task = _review_task(uow, user, clock=clock)

    service.resolve_needs_review(
        generation_id=task.id,
        decision="CONSUME",
        idempotency_key="rev1",
        admin_id=ADMIN_ID,
        request_id="req_review_0001",
        note="provider billing evidence",
        evidence="attempt-2:external-request-777",
    )

    (entry,) = uow.audit_logs.entries
    assert isinstance(entry, AuditLog)
    assert entry.actor_type == "ADMIN"
    assert entry.actor_id == ADMIN_ID
    assert entry.action == "generation.review_resolution"
    assert entry.entity_type == "generation_task"
    assert entry.entity_id == task.id
    assert entry.request_id == "req_review_0001"
    payload = entry.metadata_
    assert payload["decision"] == "CONSUME"
    assert payload["note"] == "provider billing evidence"
    assert payload["evidence"] == "attempt-2:external-request-777"
    assert payload["applied"] is True
    assert payload["decided_at"] == clock().isoformat()
    assert payload["task_before_snapshot"]["billing_status"] == "RESERVED"
    assert payload["task_before_snapshot"]["status"] == "NEEDS_REVIEW"
    assert payload["task_after_snapshot"]["billing_status"] == "CONSUMED"
    assert payload["wallet_before_snapshot"]["reserved_points"] == 20
    assert payload["wallet_after_snapshot"]["reserved_points"] == 0
    assert payload["wallet_before_snapshot"]["version"] + 1 == (
        payload["wallet_after_snapshot"]["version"]
    )
    assert payload["amount"] == 20


def test_resolve_needs_review_audit_never_records_the_prompt(uow) -> None:
    """脱敏（`02-database-design.md` §3.4）：审计摘要禁止出现完整 Prompt。"""

    user = uow.users.seed(make_user_row())
    service = _service(uow)
    service.credit(user_id=user.id, amount=100, idempotency_key="c", reason="r")
    task = uow.generation_tasks.seed(
        make_generation_task_row(
            user_id=user.id,
            status="NEEDS_REVIEW",
            reserved_points=20,
            prompt="RAW-PROMPT-MUST-NOT-LEAK",
        )
    )
    service.reserve(
        user_id=user.id, generation_id=task.id, amount=20, idempotency_key="r1", reason="g"
    )

    service.resolve_needs_review(
        generation_id=task.id,
        decision="RELEASE",
        idempotency_key="rev1",
        admin_id=ADMIN_ID,
        request_id="req_review_0001",
        note="provider proved non-execution",
    )

    (entry,) = uow.audit_logs.entries
    assert "RAW-PROMPT-MUST-NOT-LEAK" not in str(entry.metadata_)
    assert "prompt" not in entry.metadata_["task_before_snapshot"]


def test_review_resolution_locks_task_before_wallet_and_appends_audit_last(uow) -> None:
    user = uow.users.seed(make_user_row())
    service, task = _review_task(uow, user)
    uow.trace.clear()

    service.resolve_needs_review(
        generation_id=task.id,
        decision="RELEASE",
        idempotency_key="rev1",
        admin_id=ADMIN_ID,
        request_id="req_review_0001",
        note="no external execution",
    )

    events = _ordered(uow)
    assert events[0].startswith("lock:generation_task")
    assert _index(events, "lock:wallet") > _index(events, "lock:generation_task")
    assert events[-1] == "append:audit:generation.review_resolution"
    assert events.index("billing_status:RELEASED") == len(events) - 2


# --------------------------------------------------------------------------- #
# ENG-04 第 5 步：ADJUSTMENT 的前后钱包快照与独立审计
# --------------------------------------------------------------------------- #
def test_adjust_appends_independent_audit_with_before_and_after_snapshots(uow, clock) -> None:
    user = uow.users.seed(make_user_row())
    service = _service(uow, clock)
    service.credit(user_id=user.id, amount=100, idempotency_key="c", reason="r")

    _adjust(service, user.id, amount=30, reason="refund goodwill", case_reference="CASE-777")

    (entry,) = uow.audit_logs.entries
    assert entry.actor_type == "ADMIN"
    assert entry.actor_id == ADMIN_ID
    assert entry.action == "wallet.adjust"
    assert entry.entity_type == "point_wallet"
    assert entry.request_id == "req_adjust_0001"
    payload = entry.metadata_
    assert payload["adjustment_kind"] == "POSITIVE_CREDIT"
    assert payload["signed_amount"] == 30
    assert payload["case_reference"] == "CASE-777"
    assert payload["note"] == "refund goodwill"
    assert payload["applied"] is True
    assert payload["adjusted_at"] == clock().isoformat()
    assert payload["wallet_before_snapshot"]["available_points"] == 100
    assert payload["wallet_after_snapshot"]["available_points"] == 130
    assert payload["wallet_before_snapshot"]["reserved_points"] == 0
    assert payload["wallet_after_snapshot"]["reserved_points"] == 0
    assert payload["wallet_before_snapshot"]["version"] + 1 == (
        payload["wallet_after_snapshot"]["version"]
    )
    assert entry.entity_id == uuid.UUID(payload["wallet_after_snapshot"]["wallet_id"])


def test_adjust_ledger_keeps_admin_and_case_provenance(uow) -> None:
    user = uow.users.seed(make_user_row())
    service = _service(uow)
    service.credit(user_id=user.id, amount=100, idempotency_key="c", reason="r")

    entry = _adjust(service, user.id, amount=7, kind="NEGATIVE_CONSUMPTION")

    assert entry.metadata_["adjustment_kind"] == "NEGATIVE_CONSUMPTION"
    assert entry.metadata_["admin_id"] == str(ADMIN_ID)
    assert entry.metadata_["case_reference"] == "CASE-42"


def test_adjust_kind_in_ledger_metadata_cannot_be_spoofed_by_caller(uow) -> None:
    """`metadata.adjustment_kind` 是数据库 CHECK 依赖的权威值，调用方不得覆盖。"""

    user = uow.users.seed(make_user_row())
    service = _service(uow)

    entry = _adjust(
        service,
        user.id,
        amount=10,
        metadata={"adjustment_kind": "NEGATIVE_CONSUMPTION", "ticket": "T-1"},
    )

    assert entry.metadata_["adjustment_kind"] == "POSITIVE_CREDIT"
    assert entry.metadata_["ticket"] == "T-1"
    assert entry.available_delta == 10


def test_adjust_never_rewrites_history_or_touches_reserved(uow) -> None:
    user = uow.users.seed(make_user_row())
    service, task = _reserved_task(uow, user)
    consume = service.consume(
        user_id=user.id, generation_id=task.id, idempotency_key="f1", reason="g"
    )
    history_before = [(e.id, e.entry_type, e.available_delta, e.reserved_delta, e.amount)
                      for e in uow.point_ledger.entries]

    _adjust(service, user.id, amount=4, kind="NEGATIVE_CONSUMPTION", key="a9")

    wallet = uow.point_wallets.get_by_user_id(user.id)
    history_after = [(e.id, e.entry_type, e.available_delta, e.reserved_delta, e.amount)
                     for e in uow.point_ledger.entries]
    assert history_after[: len(history_before)] == history_before
    assert consume.entry_type == "CONSUME"
    assert wallet.reserved_points == 0
    assert wallet.available_points == 76
    assert wallet.lifetime_consumed == 24


def test_adjust_replay_appends_neither_audit_nor_ledger(uow) -> None:
    """裁决 C：同键同参回放只返回第一次的流水，流水与审计条数都不变。"""

    user = uow.users.seed(make_user_row())
    service = _service(uow)

    first = _adjust(service, user.id, amount=12)
    wallet_version = uow.point_wallets.get_by_user_id(user.id).version
    locks_before = len(uow.point_wallets.lock_calls)
    replay = _adjust(service, user.id, amount=12, reason="duplicate submission")

    assert replay is first
    assert uow.point_wallets.get_by_user_id(user.id).version == wallet_version
    assert len(uow.point_ledger.entries) == 1
    audits = uow.audit_logs.entries
    assert len(audits) == 1
    assert [e.metadata_["applied"] for e in audits] == [True]
    assert audits[0].metadata_["note"] == "admin"
    # 回放路径不再为了写审计而读钱包快照
    assert len(uow.point_wallets.lock_calls) == locks_before


@pytest.mark.parametrize(
    ("signed", "kind", "available_delta"),
    [(30, "POSITIVE_CREDIT", 30), (-30, "NEGATIVE_CONSUMPTION", -30)],
)
def test_adjust_wallet_maps_signed_amount_to_kind(
    uow, signed: int, kind: str, available_delta: int
) -> None:
    user = uow.users.seed(make_user_row())
    service = _service(uow)
    service.credit(user_id=user.id, amount=100, idempotency_key="c", reason="r")

    entry = service.adjust_wallet(
        user_id=user.id,
        signed_amount=signed,
        idempotency_key="a1",
        admin_id=ADMIN_ID,
        reason="reconciliation",
        case_reference="CASE-100",
        request_id="req_adjust_0100",
    )

    wallet = uow.point_wallets.get_by_user_id(user.id)
    assert entry.available_delta == available_delta
    assert entry.metadata_["adjustment_kind"] == kind
    assert wallet.available_points == 100 + available_delta
    assert wallet.reserved_points == 0
    assert len(uow.audit_logs.entries) == 1


def test_adjust_wallet_rejects_zero_signed_amount(uow) -> None:
    user = uow.users.seed(make_user_row())

    with pytest.raises(ApiError) as excinfo:
        _service(uow).adjust_wallet(
            user_id=user.id,
            signed_amount=0,
            idempotency_key="a1",
            admin_id=ADMIN_ID,
            reason="reconciliation",
            case_reference="CASE-100",
            request_id="req_adjust_0100",
        )

    assert excinfo.value.code is ErrorCode.VALIDATION_ERROR
    assert excinfo.value.details["fields"] == ["signed_amount"]
    assert uow.point_wallets.lock_calls == []


# --------------------------------------------------------------------------- #
# ENG-04 第 6 步：中途失败的原子性（不留半条流水，事务边界仍由调用方持有）
# --------------------------------------------------------------------------- #
class _ExplodingLedger:
    """在写入流水时模拟数据库失败，用于验证同一事务内的原子性。"""

    def __init__(self, inner) -> None:
        self._inner = inner

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    def append(self, draft):
        raise RuntimeError("synthetic point_ledger INSERT failure")


def test_ledger_append_failure_leaves_wallet_and_billing_status_untouched(uow) -> None:
    from app.db.models.points import PointLedger

    user = uow.users.seed(make_user_row())
    service, task = _reserved_task(uow, user)
    wallet = uow.point_wallets.get_by_user_id(user.id)
    snapshot = (
        wallet.available_points,
        wallet.reserved_points,
        wallet.lifetime_credited,
        wallet.lifetime_consumed,
        wallet.version,
    )
    entries_before: tuple[PointLedger, ...] = uow.point_ledger.entries
    uow.point_ledger = _ExplodingLedger(uow.point_ledger)

    with pytest.raises(RuntimeError):
        service.consume(
            user_id=user.id, generation_id=task.id, idempotency_key="f1", reason="g"
        )

    # 流水先于快照写入：写失败时余额、版本与 billing_status 都不得推进
    assert uow.point_ledger.entries == entries_before
    assert (
        wallet.available_points,
        wallet.reserved_points,
        wallet.lifetime_credited,
        wallet.lifetime_consumed,
        wallet.version,
    ) == snapshot
    assert task.billing_status == "RESERVED"
    assert uow.generation_tasks.billing_status_calls == []
    assert uow.commits == 0


def test_settlement_failure_rolls_back_the_database_transaction(uow) -> None:
    """失败时调用方的 `with` 上下文必须回滚且不提交（可安全重投）。

    裁决 A 之后「查不到任务行」是 `GENERATION_NOT_FOUND`（`ApiError`），
    本用例只证明**事务边界**不受错误类型改判影响：仍然回滚一次、不提交、关闭会话。
    """

    from app.services.points.service import PointsService

    session = _TransactionRecordingSession()

    with pytest.raises(ApiError) as excinfo:
        with SqlAlchemyUnitOfWork(lambda: session) as real_uow:
            # session 替身查不到任务行 → 结算在最前置校验处失败
            PointsService(uow=real_uow).consume(
                user_id=USER_ID,
                generation_id=uuid.uuid4(),
                idempotency_key="f1",
                reason="g",
            )

    assert excinfo.value.code is ErrorCode.GENERATION_NOT_FOUND
    assert (session.commits, session.rollbacks, session.closes) == (0, 1, 1)
    assert uow.commits == 0


def test_in_memory_unit_of_work_trace_records_every_repository_write(uow) -> None:
    """替身必须暴露跨仓储的事件顺序，否则锁顺序契约无法被证明。"""

    user = uow.users.seed(make_user_row())
    service, task = _reserved_task(uow, user)
    service.consume(
        user_id=user.id, generation_id=task.id, idempotency_key="f1", reason="g"
    )
    _adjust(service, user.id, amount=5, key="a1")

    wallet_lock = "lock:wallet:" + str(user.id)
    assert _ordered(uow) == [
        # credit：钱包不存在 → 锁空 → 建钱包 → 再锁 → 写流水 → 更新快照
        wallet_lock,
        wallet_lock,
        "append:ledger:CREDIT",
        "apply_delta:wallet",
        # reserve（Generation Service 的 §6.1 事务）
        wallet_lock,
        "append:ledger:RESERVE",
        "apply_delta:wallet",
        # consume（§6.2：任务锁一定先于钱包锁，计费状态最后推进）
        f"lock:generation_task:{task.id}",
        wallet_lock,
        "append:ledger:CONSUME",
        "apply_delta:wallet",
        "billing_status:CONSUMED",
        # adjust（流水 + 独立审计）
        wallet_lock,
        "append:ledger:ADJUSTMENT",
        "apply_delta:wallet",
        "append:audit:wallet.adjust",
    ]


# --------------------------------------------------------------------------- #
# ENG-04 补口：用户侧流水翻页（`GET /me/points/ledger` 的 Service 侧）
# --------------------------------------------------------------------------- #
OTHER_USER_ID = uuid.UUID("66667777-8888-9999-aaaa-bbbbbbbbbbbb")


def _paging_setup():
    """返回 `(clock, uow, service)`：翻页只读，但时钟仍要可控。"""

    clock = MutableClock()
    uow = PointsUnitOfWork(clock=clock)
    return clock, uow, _service(uow, clock)


def _append_entry(uow, clock, *, key: str, user_id=USER_ID, amount: int = 10) -> uuid.UUID:
    """推进时钟后追加一条流水，让 `(created_at, id)` 的顺序在测试里可预期。"""

    clock.advance(1)
    entry = uow.point_ledger.append(
        LedgerEntryDraft(
            wallet_id=uuid.uuid4(),
            user_id=user_id,
            entry_type="CREDIT",
            available_delta=amount,
            reserved_delta=0,
            amount=amount,
            idempotency_key=key,
            reason="synthetic",
        )
    )
    return entry.id


def test_list_ledger_returns_newest_first_with_next_cursor() -> None:
    clock, uow, service = _paging_setup()
    ids = [_append_entry(uow, clock, key=f"e{i}") for i in range(4)]

    page = service.list_ledger(user_id=USER_ID, cursor=None, limit=2)

    assert [row.id for row in page.rows] == [ids[3], ids[2]]
    assert page.has_more is True
    assert page.next_cursor is not None


def test_list_ledger_last_page_has_no_cursor() -> None:
    clock, uow, service = _paging_setup()
    ids = [_append_entry(uow, clock, key=f"e{i}") for i in range(2)]

    page = service.list_ledger(user_id=USER_ID, cursor=None, limit=5)

    assert [row.id for row in page.rows] == list(reversed(ids))
    assert page.has_more is False
    assert page.next_cursor is None


def test_list_ledger_cursor_walk_covers_every_row_exactly_once() -> None:
    clock, uow, service = _paging_setup()
    ids = [_append_entry(uow, clock, key=f"e{i}") for i in range(9)]

    seen: list[uuid.UUID] = []
    cursor: str | None = None
    for _ in range(10):
        page = service.list_ledger(user_id=USER_ID, cursor=cursor, limit=4)
        seen.extend(row.id for row in page.rows)
        cursor = page.next_cursor
        if cursor is None:
            break

    assert seen == list(reversed(ids))
    assert cursor is None


def test_list_ledger_excludes_other_users_rows() -> None:
    clock, uow, service = _paging_setup()
    mine = _append_entry(uow, clock, key="mine")
    _append_entry(uow, clock, key="theirs", user_id=OTHER_USER_ID)

    page = service.list_ledger(user_id=USER_ID, cursor=None, limit=10)

    assert [row.id for row in page.rows] == [mine]


def test_list_ledger_on_empty_ledger_returns_empty_page() -> None:
    _clock, _uow, service = _paging_setup()

    page = service.list_ledger(user_id=USER_ID, cursor=None, limit=20)

    assert page.rows == ()
    assert (page.next_cursor, page.has_more) == (None, False)


def test_list_ledger_is_read_only() -> None:
    """翻页不锁钱包、不写流水、不提交：余额快照与流水的写侧顺序不受读取影响。"""

    clock, uow, service = _paging_setup()
    _append_entry(uow, clock, key="e0")
    events_after_setup = list(uow.trace)

    service.list_ledger(user_id=USER_ID, cursor=None, limit=10)

    assert uow.trace == events_after_setup
    assert (uow.commits, uow.rollbacks) == (0, 0)


@pytest.mark.parametrize("cursor", ["garbage", "2026-01-01T00:00:00+00:00|not-a-uuid", "||"])
def test_list_ledger_rejects_tampered_cursor(cursor: str) -> None:
    _clock, _uow, service = _paging_setup()

    with pytest.raises(ApiError) as excinfo:
        service.list_ledger(user_id=USER_ID, cursor=cursor, limit=10)

    assert excinfo.value.code is ErrorCode.VALIDATION_ERROR
    assert excinfo.value.details == {"fields": ["cursor"]}


def test_list_ledger_accepts_its_own_cursor_verbatim() -> None:
    """服务端生成的游标必须能原样回传（翻页闭环，否则第二页永远取不到）。"""

    clock, uow, service = _paging_setup()
    for index in range(3):
        _append_entry(uow, clock, key=f"e{index}")

    first = service.list_ledger(user_id=USER_ID, cursor=None, limit=1)
    second = service.list_ledger(user_id=USER_ID, cursor=first.next_cursor, limit=1)

    assert [row.id for row in second.rows] != [row.id for row in first.rows]
