"""空库 → 显式定价 → Mock 走完一次生成（真实 PostgreSQL）。

这条链路要钉住的是新环境的第一个真实故障：迁移到 head 之后
`generation_cost_configs` 是空的，用户提交生成会拿到服务端错误，而错误本身并不说明
「部署少跑了一步初始化」。因此本文件按部署的实际顺序跑一遍：

1. 空表状态下创建任务 → 必须是 `NOT_READY` + `reason=cost_config_missing`，且**不写任何行**；
2. 执行 `bootstrap_cost_configs` 写入**显式**价格（故意用 13/17 这种非整值，
   证明代码里没有「默认 20 点」这类影子定价）；
3. 重复执行同一条初始化 → 不新增行（可重跑是部署脚本的基本要求）；
4. 用同一套真库装配（`SqlAlchemyUnitOfWork` + Mock Provider + 内存对象存储）
   创建任务并由 Worker 侧 `submit()` 跑完 → `SUCCEEDED` + `CONSUMED`，
   扣点等于配置里那一项，流水四条不变量在真库上成立。

Session 挂在夹具事务的 SAVEPOINT 上：编排器会开多个事务，每个都在同一连接里
保存点提交，用例结束整体回滚，测试库不残留合成数据。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.orm import Session, sessionmaker

from app.bootstrap.cost_configs import (
    PriceInput,
    bootstrap_cost_configs,
    expected_cost_modes,
    missing_cost_modes,
)
from app.errors.codes import ErrorCode
from app.errors.exceptions import ApiError
from app.providers.mock import MockProvider
from app.repositories.generation import SqlAlchemyGenerationCostConfigRepository
from app.repositories.unit_of_work import SqlAlchemyUnitOfWork
from app.services.generation.commands import NewGenerationCommand
from app.services.generation.orchestrator import (
    OUTCOME_SUCCEEDED,
    GenerationOrchestrator,
)
from app.services.generation.provider_factory import provider_identity
from app.services.generation.service import GenerationService
from app.services.points.service import PointsService
from app.storage.memory import InMemoryStorageAdapter
from tests.services.fakes import RecordingGenerationQueuePort

#: 显式定价：刻意不是任何「常用默认值」
TEXT_PRICE = 13
PHOTO_PRICE = 17

PROMPT = "一只圆脸猫正对镜头，蜡笔质感，背景暖色"


@pytest.fixture
def session_factory(db: Connection) -> sessionmaker[Session]:
    """把 Repository/UnitOfWork 绑到夹具连接上，提交只作用于 SAVEPOINT。"""

    return sessionmaker(
        bind=db,
        autoflush=False,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )


@pytest.fixture
def empty_costs(db: Connection) -> int:
    """会话临时库刚迁移到 head：这张表必须是空的，否则整条用例的前提就假了。"""

    count = db.execute(text("SELECT count(*) FROM generation_cost_configs")).scalar_one()
    assert count == 0, "generation_cost_configs 非空，无法证明「空库初始化」路径"
    return count


@pytest.fixture
def funded_user(
    db: Connection, make_user: Callable[..., uuid.UUID], make_wallet: Callable[..., uuid.UUID]
) -> uuid.UUID:
    """已验证邮箱、余额足够的合成用户（生成入口要求邮箱验证通过）。"""

    user_id = make_user()
    wallet_id = make_wallet(user_id)
    db.execute(
        text(
            """
            UPDATE point_wallets
               SET available_points = 100, lifetime_credited = 100
             WHERE id = :wallet_id
            """
        ),
        {"wallet_id": wallet_id},
    )
    db.execute(
        text(
            """
            INSERT INTO user_emails (user_id, email, verified_at, is_primary, created_at)
            VALUES (:user_id, :email, now(), true, now())
            """
        ),
        {"user_id": user_id, "email": f"synthetic-{uuid.uuid4().hex[:10]}@example.com"},
    )
    return user_id


def test_empty_table_blocks_generation_with_operable_not_ready(
    db: Connection,
    session_factory: sessionmaker[Session],
    empty_costs: int,
    funded_user: uuid.UUID,
) -> None:
    queue = RecordingGenerationQueuePort()
    provider_name, model_name = provider_identity(ai_provider="mock", ai_model=None)
    uow = SqlAlchemyUnitOfWork(session_factory)
    with uow:
        service = GenerationService(
            uow=uow,
            points=PointsService(uow=uow),
            queue=queue,
            enable_reference_image=False,
            provider_name=provider_name,
            model_name=model_name,
        )
        with pytest.raises(ApiError) as excinfo:
            service.create_generation(
                user_id=funded_user,
                command=NewGenerationCommand(
                    mode="TEXT_PROMPT",
                    prompt=PROMPT,
                    output_count=2,
                    input_asset_ids=(),
                ),
                idempotency_key=f"synthetic-empty-{uuid.uuid4().hex}",
            )

    assert excinfo.value.code is ErrorCode.NOT_READY
    assert excinfo.value.status_code == 503
    assert excinfo.value.details["reason"] == "cost_config_missing"
    assert "app.bootstrap.cost_configs" in excinfo.value.details["remediation"]
    # 失败必须干净：没有任务行、没有流水、没有投递
    assert db.execute(text("SELECT count(*) FROM generation_tasks")).scalar_one() == 0
    assert db.execute(text("SELECT count(*) FROM point_ledger")).scalar_one() == 0
    assert queue.generations == []


def test_bootstrap_then_mock_generation_completes_on_real_database(
    db: Connection,
    session_factory: sessionmaker[Session],
    empty_costs: int,
    funded_user: uuid.UUID,
) -> None:
    prices = [
        PriceInput(mode="TEXT_PROMPT", points_cost=TEXT_PRICE),
        PriceInput(mode="PHOTO_PROMPT", points_cost=PHOTO_PRICE),
    ]

    first = bootstrap_cost_configs(session_factory, prices, at=datetime.now(timezone.utc))
    assert first.ok is True
    assert len(first.created_ids) == 2
    assert sorted(
        row[0]
        for row in db.execute(
            text(
                "SELECT points_cost FROM generation_cost_configs ORDER BY points_cost"
            )
        )
    ) == [TEXT_PRICE, PHOTO_PRICE]

    # 可重跑：同价再执行一次不新增行，也不关任何窗口
    second = bootstrap_cost_configs(session_factory, prices, at=datetime.now(timezone.utc))
    assert second.created_ids == ()
    assert second.closed_ids == ()
    assert all(change.kind == "unchanged" for change in second.planned)
    assert (
        db.execute(text("SELECT count(*) FROM generation_cost_configs")).scalar_one() == 2
    )

    # 初始化之后，健康检查口径与实际定价必须一致
    with session_factory() as session:
        assert missing_cost_modes(
            SqlAlchemyGenerationCostConfigRepository(session),
            modes=expected_cost_modes(enable_reference_image=False),
            model_name="any-model",
            at=datetime.now(timezone.utc),
        ) == ()

    # 创建 → 编排 → Mock 出图 → 结算，全程真库
    queue = RecordingGenerationQueuePort()
    provider_name, model_name = provider_identity(ai_provider="mock", ai_model=None)
    create_uow = SqlAlchemyUnitOfWork(session_factory)
    with create_uow:
        service = GenerationService(
            uow=create_uow,
            points=PointsService(uow=create_uow),
            queue=queue,
            enable_reference_image=False,
            provider_name=provider_name,
            model_name=model_name,
        )
        outcome = service.create_generation(
            user_id=funded_user,
            command=NewGenerationCommand(
                mode="TEXT_PROMPT",
                prompt=PROMPT,
                output_count=2,
                input_asset_ids=(),
            ),
            idempotency_key=f"synthetic-bootstrap-{uuid.uuid4().hex}",
        )
        generation_id = outcome.task.id

    assert outcome.task.status == "QUEUED"
    assert outcome.task.reserved_points == TEXT_PRICE
    assert [item.generation_id for item in queue.generations] == [generation_id]

    orchestrator = GenerationOrchestrator(
        uow_factory=lambda: SqlAlchemyUnitOfWork(session_factory),
        points_factory=lambda uow: PointsService(uow=uow),
        provider_factory=lambda: MockProvider(),
        storage=InMemoryStorageAdapter(),
    )
    assert orchestrator.submit(generation_id) == OUTCOME_SUCCEEDED

    task = db.execute(
        text(
            """
            SELECT status, billing_status, reserved_points
              FROM generation_tasks WHERE id = :id
            """
        ),
        {"id": generation_id},
    ).one()
    assert task.status == "SUCCEEDED"
    assert task.billing_status == "CONSUMED"

    ledger_types = [
        row[0]
        for row in db.execute(
            text(
                """
                SELECT entry_type FROM point_ledger
                 WHERE generation_id = :id ORDER BY created_at, id
                """
            ),
            {"id": generation_id},
        )
    ]
    assert ledger_types == ["RESERVE", "CONSUME"]

    wallet = db.execute(
        text(
            """
            SELECT available_points, reserved_points, lifetime_credited, lifetime_consumed
              FROM point_wallets WHERE user_id = :id
            """
        ),
        {"id": funded_user},
    ).one()
    assert wallet == (100 - TEXT_PRICE, 0, 100, TEXT_PRICE)

    outputs = db.execute(
        text("SELECT count(*) FROM generation_outputs WHERE generation_id = :id"),
        {"id": generation_id},
    ).scalar_one()
    assert outputs == 2


def test_bootstrap_is_idempotent_per_mode_and_rejects_unknown_mode(
    session_factory: sessionmaker[Session], empty_costs: int
) -> None:
    with pytest.raises(ValueError):
        from app.bootstrap.cost_configs import parse_price

        parse_price("NOT_A_MODE=5")

    result = bootstrap_cost_configs(
        session_factory, [PriceInput(mode="PHOTO_PROMPT", points_cost=PHOTO_PRICE)]
    )
    assert len(result.created_ids) == 1
    again = bootstrap_cost_configs(
        session_factory, [PriceInput(mode="PHOTO_PROMPT", points_cost=PHOTO_PRICE)]
    )
    assert again.created_ids == ()
    assert _row_count(session_factory, "generation_cost_configs") == 1


def _row_count(session_factory: sessionmaker[Session], table: str) -> int:
    """在同一个 SAVEPOINT 会话里数行：Repository 写的行要由同一事务读得到。"""

    with session_factory() as session:
        return int(
            session.execute(
                text(f"SELECT count(*) FROM {table}")  # noqa: S608 - 表名来自测试常量
            ).scalar_one()
        )


def test_task_row_is_still_readable_after_reprice(
    db: Connection,
    session_factory: sessionmaker[Session],
    empty_costs: int,
    funded_user: uuid.UUID,
) -> None:
    """调价只关窗、不改写历史：老任务的 `cost_config_id` 必须仍指向一行真实配置。"""

    bootstrap_cost_configs(
        session_factory, [PriceInput(mode="TEXT_PROMPT", points_cost=TEXT_PRICE)]
    )
    queue = RecordingGenerationQueuePort()
    provider_name, model_name = provider_identity(ai_provider="mock", ai_model=None)
    with SqlAlchemyUnitOfWork(session_factory) as uow:
        service = GenerationService(
            uow=uow,
            points=PointsService(uow=uow),
            queue=queue,
            enable_reference_image=False,
            provider_name=provider_name,
            model_name=model_name,
        )
        outcome = service.create_generation(
            user_id=funded_user,
            command=NewGenerationCommand(
                mode="TEXT_PROMPT", prompt=PROMPT, output_count=2, input_asset_ids=()
            ),
            idempotency_key=f"synthetic-reprice-{uuid.uuid4().hex}",
        )
        task_id = outcome.task.id

    reprice = datetime.now(timezone.utc)
    changed = bootstrap_cost_configs(
        session_factory,
        [PriceInput(mode="TEXT_PROMPT", points_cost=TEXT_PRICE + 7)],
        at=reprice,
        allow_reprice=True,
    )
    assert len(changed.created_ids) == 1
    assert len(changed.closed_ids) == 1

    row = db.execute(
        text(
            """
            SELECT t.reserved_points, c.points_cost, c.status
              FROM generation_tasks t
              JOIN generation_cost_configs c ON c.id = t.cost_config_id
             WHERE t.id = :id
            """
        ),
        {"id": task_id},
    ).one()
    assert row.reserved_points == TEXT_PRICE
    assert row.points_cost == TEXT_PRICE
    assert row.status == "INACTIVE"


def test_bootstrap_only_touches_the_config_table(
    db: Connection, session_factory: sessionmaker[Session], empty_costs: int
) -> None:
    """初始化只碰配置表：不产生任务、流水或钱包行。"""

    bootstrap_cost_configs(
        session_factory,
        [PriceInput(mode="TEXT_PROMPT", points_cost=TEXT_PRICE)],
    )
    for table in ("generation_tasks", "point_ledger", "audit_logs"):
        assert db.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() == 0


#: 后补的兜底价：刻意与 TEXT_PRICE 不同，便于分辨命中的是哪一条
WILDCARD_PRICE = 41


def test_exact_model_price_wins_over_a_newer_wildcard_row_on_real_database(
    db: Connection,
    session_factory: sessionmaker[Session],
    empty_costs: int,
    funded_user: uuid.UUID,
) -> None:
    """真实 SQL 实现：**精确型号优先，NULL 兜底**，且创建任务与 readiness 同口径。

    这是「后来补的兜底价把已有专价顶掉」和「/ready 绿了、创建任务却 503」
    两处不一致的直接反证：兜底行的 `effective_from` **更晚**，仍然不能覆盖专价。
    """

    now = datetime.now(timezone.utc)
    exact_model = "mock-image-v1"

    exact = bootstrap_cost_configs(
        session_factory,
        [PriceInput(mode="TEXT_PROMPT", points_cost=TEXT_PRICE, model_name=exact_model)],
        at=now - timedelta(days=2),
    )
    assert exact.ok is True
    assert len(exact.created_ids) == 1

    wildcard = bootstrap_cost_configs(
        session_factory,
        [PriceInput(mode="TEXT_PROMPT", points_cost=WILDCARD_PRICE)],
        at=now - timedelta(days=1),
    )
    assert wildcard.ok is True
    assert len(wildcard.created_ids) == 1

    with session_factory() as session:
        costs = SqlAlchemyGenerationCostConfigRepository(session)
        hit_exact = costs.find_active(
            mode="TEXT_PROMPT", model_name=exact_model, output_count=2, at=now
        )
        hit_wildcard = costs.find_active(
            mode="TEXT_PROMPT", model_name=None, output_count=2, at=now
        )
        hit_other = costs.find_active(
            mode="TEXT_PROMPT", model_name="another-model", output_count=2, at=now
        )
        # readiness 与创建任务用同一个 `find_active`
        missing_exact = missing_cost_modes(
            costs, modes=("TEXT_PROMPT",), model_name=exact_model, at=now
        )
        missing_other = missing_cost_modes(
            costs, modes=("TEXT_PROMPT",), model_name="another-model", at=now
        )

    assert hit_exact is not None and hit_exact.id == exact.created_ids[0]
    assert hit_exact.points_cost == TEXT_PRICE
    assert hit_wildcard is not None and hit_wildcard.id == wildcard.created_ids[0]
    assert hit_wildcard.points_cost == WILDCARD_PRICE
    assert hit_other is not None and hit_other.id == wildcard.created_ids[0]
    assert missing_exact == ()
    assert missing_other == ()

    # 创建任务：按精确型号定价，并把**命中的那一条**冻结进快照
    queue = RecordingGenerationQueuePort()
    provider_name, model_name = provider_identity(ai_provider="mock", ai_model=None)
    assert model_name == exact_model

    uow = SqlAlchemyUnitOfWork(session_factory)
    with uow:
        service = GenerationService(
            uow=uow,
            points=PointsService(uow=uow),
            queue=queue,
            enable_reference_image=False,
            provider_name=provider_name,
            model_name=model_name,
        )
        outcome = service.create_generation(
            user_id=funded_user,
            command=NewGenerationCommand(
                mode="TEXT_PROMPT",
                prompt=PROMPT,
                output_count=2,
                input_asset_ids=(),
            ),
            idempotency_key=f"synthetic-exact-{uuid.uuid4().hex}",
        )

    task = outcome.task
    assert task.cost_config_id == exact.created_ids[0]
    assert task.reserved_points == TEXT_PRICE
    assert task.cost_snapshot["model_name"] == exact_model
    assert task.cost_snapshot["points_cost"] == TEXT_PRICE
    assert [item.generation_id for item in queue.generations] == [task.id]
