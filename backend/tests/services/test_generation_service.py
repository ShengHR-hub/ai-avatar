"""ENG-06 第 3 步：Generation Service 创建、幂等、查询、重试与内部取消测试（**不依赖 PostgreSQL**）。

依据 `LEAD-04` §1-§7、§11 与 `docs/handoff/2026-09-27-current-handoff.md` §4：

- R1 创建事务：成本解析 → 锁钱包 → 建任务（`CREATED + RESERVED`）→ `RESERVE` →
  `QUEUED` → 提交；Celery 投递只在提交之后，投递失败不回滚已提交账务；
- R2 幂等：作用域含 `user_id + idempotency_key`，
  `request_fingerprint = SHA-256(method + canonical_path + normalized_json_body)`；
  同键同指纹回放原任务（不二次预留、不二次入队），同键不同指纹 `IDEMPOTENCY_CONFLICT`，
  竞态输给唯一索引时回滚重查，胜出行仍是 `CREATED` 则 `IDEMPOTENCY_IN_PROGRESS`；
- R6 用户重试只接受 `FAILED`：新任务 + `parent_task_id` + 新幂等键 + 新预留，
  旧任务保持 `FAILED + RELEASED` 不动，`NEEDS_REVIEW` 无法被重试绕过；
- R9 内部取消：`CREATED/QUEUED` → `FAILED + RELEASED`（`CANCELLED_BY_USER`）；
  `PROCESSING` 已确认 → `FAILED + RELEASED`，未确认或超时 → `NEEDS_REVIEW + RESERVED`；
  取消命令在终态任务上是幂等 no-op；
- R10 `REFERENCE_IMAGE` Feature Flag、输入资源与邮箱门禁必须早于任何写入。

断言一律落在任务状态、`billing_status`、钱包快照、流水类型与条数、输入/输出行与
入队调用上（`LEAD-04` §9：不能只断言返回值）。
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from app.db.models.generation import GenerationCostConfig, GenerationTask
from app.errors.codes import ErrorCode
from app.errors.exceptions import ApiError
from app.providers.ports import (
    ProviderCallError,
    ProviderError,
    ProviderErrorCategory,
    ProviderResult,
    ProviderState,
)
from app.repositories.generation import (
    DuplicateIdempotencyKey,
    GenerationInputDraft,
    GenerationOutputDraft,
)
from app.repositories.media import MediaAssetDraft
from app.repositories.provider_attempt import ProviderAttemptDraft
from app.services.assets.policy import RESULT_TTL
from app.services.generation.commands import NewGenerationCommand
from app.services.generation.service import GenerationService
from app.services.points.service import PointsService
from tests.services.fakes import (
    GenerationUnitOfWork,
    RecordingGenerationQueuePort,
    ScriptedProvider,
    TransactionProbe,
    make_generation_cost_config,
    make_generation_task_row,
    make_media_asset_row,
    make_user_row,
    synthetic_png_bytes,
)

PROMPT = "保持人物特征，生成专业头像"
POINTS_COST = 10
PROVIDER_NAME = "MOCK"
MODEL_NAME = "mock-image-v1"
CREATE_KEY = "idem-create-1"


# --------------------------------------------------------------------------- #
# 夹具与构造辅助
# --------------------------------------------------------------------------- #
@pytest.fixture
def cost(gen_uow: GenerationUnitOfWork) -> GenerationCostConfig:
    return seed_cost(gen_uow)


@pytest.fixture
def source_asset(gen_uow, gen_storage, gen_user) -> uuid.UUID:
    return ready_asset(gen_uow, gen_storage, gen_user.id)


def seed_cost(
    uow: GenerationUnitOfWork,
    *,
    mode: str = "PHOTO_PROMPT",
    model_name: str | None = None,
    output_count: int = 2,
    points_cost: int = POINTS_COST,
    cost_multiplier: Decimal = Decimal("1"),
    status: str = "ACTIVE",
    effective_from: datetime | None = None,
) -> GenerationCostConfig:
    """登记一行成本配置（默认对当前时钟生效，`model_name` 为通配）。"""

    return uow.generation_costs.seed(
        make_generation_cost_config(
            mode=mode,
            model_name=model_name,
            output_count=output_count,
            points_cost=points_cost,
            cost_multiplier=cost_multiplier,
            status=status,
            effective_from=effective_from,
        )
    )


def ready_asset(
    uow: GenerationUnitOfWork,
    storage,
    user_id: uuid.UUID,
    *,
    purpose: str = "SOURCE",
    status: str = "READY",
) -> uuid.UUID:
    """登记一张资源并把对象真的放进内存存储（输入图会被 Worker 读取）。"""

    content = synthetic_png_bytes()
    asset = make_media_asset_row(
        user_id=user_id,
        purpose=purpose,
        asset_type={"SOURCE": "ORIGINAL", "REFERENCE": "REFERENCE", "RESULT": "RESULT"}[purpose],
        status=status,
        storage_key=f"assets/{user_id}/{uuid.uuid4().hex}",
        size_bytes=len(content),
        width=4,
        height=4,
        sha256=hashlib.sha256(content).hexdigest(),
        idempotency_key=None,
    )
    storage.upload(asset.storage_key, content, "image/png")
    uow.media.register(asset)
    return asset.id


def new_command(**overrides: object) -> NewGenerationCommand:
    params: dict[str, object] = {
        "mode": "PHOTO_PROMPT",
        "prompt": PROMPT,
        "output_count": 2,
        "input_asset_ids": (),
    }
    params.update(overrides)
    return NewGenerationCommand(**params)  # type: ignore[arg-type]


def create(service: GenerationService, user_id: uuid.UUID, *, key: str = CREATE_KEY, **overrides):
    return service.create_generation(
        user_id=user_id, command=new_command(**overrides), idempotency_key=key
    )


def build_service(
    uow: GenerationUnitOfWork, queue: RecordingGenerationQueuePort, clock, **overrides: object
) -> GenerationService:
    params: dict[str, object] = {
        "uow": uow,
        "points": PointsService(uow=uow, clock=clock),
        "clock": clock,
        "queue": queue,
    }
    params.update(overrides)
    return GenerationService(**params)  # type: ignore[arg-type]


def wallet_state(uow: GenerationUnitOfWork, user_id: uuid.UUID) -> tuple[int, int, int, int]:
    wallet = uow.point_wallets.get_by_user_id(user_id)
    assert wallet is not None
    return (
        wallet.available_points,
        wallet.reserved_points,
        wallet.lifetime_credited,
        wallet.lifetime_consumed,
    )


def ledger_types(uow: GenerationUnitOfWork) -> list[str]:
    return [entry.entry_type for entry in uow.point_ledger.entries]


def assert_no_writes(uow: GenerationUnitOfWork, queue: RecordingGenerationQueuePort) -> None:
    """门禁必须早于任何写入：不锁钱包、不建任务、不写流水、不入队。"""

    assert uow.generation_tasks.rows == ()
    assert uow.point_ledger.entries == ()
    assert uow.point_wallets.lock_calls == []
    assert uow.media.create_calls == []
    assert uow.commits == 0
    assert queue.generations == []


def seed_task(
    uow: GenerationUnitOfWork,
    user_id: uuid.UUID,
    *,
    status: str = "QUEUED",
    billing_status: str = "RESERVED",
    reserved_points: int = POINTS_COST,
    prompt: str = PROMPT,
    input_asset_ids: tuple[uuid.UUID, ...] = (),
) -> GenerationTask:
    task = uow.generation_tasks.seed(
        make_generation_task_row(
            user_id=user_id,
            status=status,
            billing_status=billing_status,
            reserved_points=reserved_points,
            prompt=prompt,
            mode="PHOTO_PROMPT",
            idempotency_key=CREATE_KEY,
        )
    )
    # 幂等比较会重算胜出行自己的输入资源，因此种行时也要把输入一起种上
    for sort_order, asset_id in enumerate(input_asset_ids):
        uow.generation_tasks.create_input(
            GenerationInputDraft(
                generation_id=task.id,
                asset_id=asset_id,
                role="SOURCE",
                sort_order=sort_order,
            )
        )
    return task


def reserve_only(uow: GenerationUnitOfWork, user_id: uuid.UUID, generation_id: uuid.UUID) -> None:
    """把钱包重置为「未预留」再补一条 RESERVE，供直接种行的测试使用。"""

    wallet = uow.point_wallets.get_by_user_id(user_id)
    assert wallet is not None
    wallet.available_points = 100
    wallet.reserved_points = 0
    PointsService(uow=uow).reserve(
        user_id=user_id,
        generation_id=generation_id,
        amount=POINTS_COST,
        idempotency_key=f"reserve:{generation_id}",
        reason="generation_reserve",
    )


def force_failed_release(
    uow: GenerationUnitOfWork, task: GenerationTask, user_id: uuid.UUID, clock
) -> None:
    """按 R4 终局形态构造 `FAILED + RELEASED`（模拟 Worker 的明确失败结算）。"""

    uow.generation_tasks.transition(
        task,
        status="FAILED",
        changed_at=task.created_at,
        failure_code="TEMPORARY",
        failure_message="供应商明确拒绝该请求",
    )
    PointsService(uow=uow, clock=clock).release(
        user_id=user_id,
        generation_id=task.id,
        idempotency_key=f"release:{task.id}",
        reason="generation_failed",
    )
    uow.commit()


def seed_processing_with_attempt(
    uow: GenerationUnitOfWork, user_id: uuid.UUID
) -> GenerationTask:
    """`PROCESSING` + 一条已提交外部请求的 attempt（内部取消用例的前置状态）。"""

    task = seed_task(uow, user_id, status="PROCESSING")
    uow.provider_attempts.create(
        ProviderAttemptDraft(
            generation_id=task.id,
            attempt_no=1,
            provider_name=PROVIDER_NAME,
            model_name=MODEL_NAME,
            status="SUBMITTED",
            started_at=task.created_at,
            request_id="ext-1",
            request_metadata={"client_request_id": "cr-1"},
        )
    )
    return task


def cancelled_result() -> ProviderResult:
    return ProviderResult(
        state=ProviderState.FAILED,
        client_request_id="cr-1",
        provider_request_id="ext-1",
        error=ProviderError(
            category=ProviderErrorCategory.TEMPORARY,
            code="mock_cancelled",
            safe_message="请求已取消且未产生外部执行",
            retryable=False,
            request_accepted=False,
        ),
    )


def pending_result() -> ProviderResult:
    return ProviderResult(
        state=ProviderState.PENDING, client_request_id="cr-1", provider_request_id="ext-1"
    )


def settle_two_outputs(
    uow: GenerationUnitOfWork, task: GenerationTask, user_id: uuid.UUID
) -> list[uuid.UUID]:
    """直接落地两张结果资源与输出行（结果读取视图用例的前置）。"""

    content = synthetic_png_bytes()
    asset_ids: list[uuid.UUID] = []
    for index in range(2):
        asset = uow.media.create(
            MediaAssetDraft(
                user_id=user_id,
                asset_type="RESULT",
                purpose="RESULT",
                storage_provider="MINIO",
                storage_key=f"assets/{user_id}/{uuid.uuid4().hex}",
                mime_type="image/png",
                size_bytes=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
                idempotency_key=f"generation-output:{task.id}:{index}",
                expires_at=task.created_at + RESULT_TTL,
            )
        )
        uow.media.mark_ready(
            asset, mime_type="image/png", size_bytes=len(content), width=4, height=4
        )
        uow.generation_tasks.create_output(
            GenerationOutputDraft(
                generation_id=task.id,
                asset_id=asset.id,
                output_index=index,
                provider_output_id=f"mock-out-{index}",
            )
        )
        asset_ids.append(asset.id)
    return asset_ids


# --------------------------------------------------------------------------- #
# R1：创建 + 预留 + 入队（顺序与事务边界）
# --------------------------------------------------------------------------- #
def test_create_reserves_and_queues_in_one_transaction_then_enqueues(
    generation_service, gen_uow, gen_user, gen_queue, cost, source_asset, clock
) -> None:
    with gen_uow:
        outcome = create(generation_service, gen_user.id, input_asset_ids=(source_asset,))

    task = outcome.task
    assert outcome.replayed is False
    assert task.status == "QUEUED"
    assert task.billing_status == "RESERVED"
    assert task.reserved_points == POINTS_COST
    assert task.requested_count == 2
    assert task.mode == "PHOTO_PROMPT"
    assert task.prompt == PROMPT
    assert task.retry_count == 0
    assert task.parent_task_id is None
    assert task.cost_config_id == cost.id
    assert task.queued_at == clock()
    assert task.provider_name == PROVIDER_NAME
    assert task.model_name == MODEL_NAME

    # 成本快照：整行关键值复制到任务行（`02-database-design.md` §3.3）
    snapshot = task.cost_snapshot
    assert snapshot["cost_config_id"] == str(cost.id)
    assert snapshot["points_cost"] == POINTS_COST
    assert snapshot["output_count"] == 2
    assert snapshot["cost_multiplier"] == "1"
    assert snapshot["mode"] == "PHOTO_PROMPT"

    # 钱包：可用减、预留加，其余不变
    assert wallet_state(gen_uow, gen_user.id) == (90, 10, 100, 0)
    assert ledger_types(gen_uow) == ["RESERVE"]
    entry = gen_uow.point_ledger.entries[0]
    assert (entry.available_delta, entry.reserved_delta, entry.amount) == (-10, 10, 10)
    assert entry.generation_id == task.id

    # 输入关联：PHOTO_PROMPT 的 SOURCE 角色与顺序
    inputs = gen_uow.generation_tasks.list_inputs(task.id)
    assert [(row.role, row.sort_order, row.asset_id) for row in inputs] == [
        ("SOURCE", 0, source_asset)
    ]

    # 一次提交、一次入队，且入队严格发生在提交之后（R1）
    assert gen_uow.commits == 1
    assert gen_uow.rollbacks == 0
    assert [item.generation_id for item in gen_queue.generations] == [task.id]
    assert gen_queue.generations[0].countdown_seconds == 0
    trace = gen_uow.trace
    assert trace.index("tx:commit") < trace.index(f"enqueue:generation:{task.id}")


def test_create_transaction_locks_wallet_before_task_row(
    generation_service, gen_uow, gen_user, cost, source_asset
) -> None:
    """固定顺序：锁钱包 → 建任务 → 写 RESERVE → 推进 QUEUED（`LEAD-04` §6:119）。"""

    with gen_uow:
        create(generation_service, gen_user.id, input_asset_ids=(source_asset,))

    ordered = [
        event
        for event in gen_uow.trace
        if event.startswith(("lock:", "create:generation_task", "append:ledger", "transition:"))
    ]
    task_id = gen_uow.generation_tasks.rows[0].id
    assert ordered[0] == f"lock:wallet:{gen_user.id}"
    assert ordered.index(f"create:generation_task:{task_id}") < ordered.index(
        "append:ledger:RESERVE"
    )
    assert ordered.index("append:ledger:RESERVE") < ordered.index("transition:QUEUED")


def test_create_dispatch_failure_keeps_committed_accounting(
    gen_uow, gen_storage, gen_user, clock, caplog
) -> None:
    """投递失败不回滚已提交账务，也不重复预留（R1，`LEAD-04` §6:119）。"""

    queue = RecordingGenerationQueuePort(trace=gen_uow.trace, deliver=False)
    service = build_service(gen_uow, queue, clock)
    seed_cost(gen_uow)
    asset = ready_asset(gen_uow, gen_storage, gen_user.id)

    with caplog.at_level("WARNING"), gen_uow:
        outcome = create(service, gen_user.id, input_asset_ids=(asset,))

    assert outcome.task.status == "QUEUED"
    assert gen_uow.commits == 1
    assert gen_uow.rollbacks == 0
    assert ledger_types(gen_uow) == ["RESERVE"]
    assert wallet_state(gen_uow, gen_user.id) == (90, 10, 100, 0)
    assert queue.generations == []
    assert "generation_dispatch_failed" in caplog.text
    # 安全日志：不得出现 Prompt 原文
    assert PROMPT not in caplog.text


# --------------------------------------------------------------------------- #
# 前置失败：成本配置缺失 / 点数不足
# --------------------------------------------------------------------------- #
def test_create_without_active_cost_config_is_not_ready_and_writes_nothing(
    generation_service, gen_uow, gen_user, gen_queue, source_asset
) -> None:
    with pytest.raises(ApiError) as excinfo, gen_uow:
        create(generation_service, gen_user.id, input_asset_ids=(source_asset,))

    assert excinfo.value.code is ErrorCode.NOT_READY
    assert excinfo.value.status_code == 503
    assert excinfo.value.details["reason"] == "cost_config_missing"
    # 缺配置是部署少了一步，响应必须自己说清楚该跑哪条初始化命令
    assert "app.bootstrap.cost_configs" in excinfo.value.details["remediation"]
    assert gen_uow.generation_costs.calls, "成本配置查询必须发生"
    assert_no_writes(gen_uow, gen_queue)


def test_create_ignores_inactive_cost_config(
    generation_service, gen_uow, gen_user, gen_queue, source_asset
) -> None:
    seed_cost(gen_uow, status="INACTIVE")

    with pytest.raises(ApiError) as excinfo, gen_uow:
        create(generation_service, gen_user.id, input_asset_ids=(source_asset,))

    assert excinfo.value.code is ErrorCode.NOT_READY
    assert_no_writes(gen_uow, gen_queue)


def test_create_prices_by_exact_model_and_freezes_that_config(
    generation_service, gen_uow, gen_user, cost, source_asset, clock
) -> None:
    """创建任务必须**带上本进程实际使用的型号**去定价：精确型号优先。

    `cost` 夹具是通配（`model_name IS NULL`）行。这里再登记一条**更早生效**的
    精确型号价：如果创建任务不带型号，它只会命中通配价；带上型号后必须命中专价。
    """

    exact = seed_cost(
        gen_uow,
        model_name=MODEL_NAME,
        points_cost=POINTS_COST + 7,
        effective_from=clock() - timedelta(days=1),
    )

    with gen_uow:
        outcome = create(generation_service, gen_user.id, input_asset_ids=(source_asset,))

    task = outcome.task
    assert exact.id != cost.id
    assert task.cost_config_id == exact.id
    assert task.reserved_points == POINTS_COST + 7
    # 冻结快照必须记下**真正命中**的那一条，而不是兜底价
    assert task.cost_snapshot["cost_config_id"] == str(exact.id)
    assert task.cost_snapshot["model_name"] == MODEL_NAME
    assert task.cost_snapshot["points_cost"] == POINTS_COST + 7


def test_create_falls_back_to_the_wildcard_price_when_the_model_is_unpriced(
    generation_service, gen_uow, gen_user, cost, source_asset, clock
) -> None:
    """精确型号没有专价时回落到通配价，且快照记的是通配行（`model_name` 为 `None`）。"""

    seed_cost(
        gen_uow,
        model_name="another-model",
        points_cost=POINTS_COST + 7,
        effective_from=clock() - timedelta(days=1),
    )

    with gen_uow:
        outcome = create(generation_service, gen_user.id, input_asset_ids=(source_asset,))

    task = outcome.task
    assert task.cost_config_id == cost.id
    assert task.reserved_points == POINTS_COST
    assert task.cost_snapshot["model_name"] is None


def test_create_with_insufficient_points_rolls_back_every_write(
    generation_service, gen_uow, gen_user, gen_queue, cost, source_asset
) -> None:
    wallet = gen_uow.point_wallets.get_by_user_id(gen_user.id)
    assert wallet is not None
    wallet.available_points = 5

    with pytest.raises(ApiError) as excinfo, gen_uow:
        create(generation_service, gen_user.id, input_asset_ids=(source_asset,))

    assert excinfo.value.code is ErrorCode.INSUFFICIENT_POINTS
    assert excinfo.value.status_code == 409
    # 事务整体回滚：任务行、输入行与流水都不留下，钱包快照不变
    assert gen_uow.generation_tasks.rows == ()
    assert gen_uow.generation_tasks.list_inputs(uuid.uuid4()) == ()
    assert gen_uow.point_ledger.entries == ()
    assert wallet_state(gen_uow, gen_user.id) == (5, 0, 100, 0)
    assert gen_uow.commits == 0
    assert gen_uow.rollbacks == 1
    assert gen_queue.generations == []


# --------------------------------------------------------------------------- #
# R2：幂等（回放 / 冲突 / 进行中）
# --------------------------------------------------------------------------- #
def test_same_key_and_same_body_replays_without_second_reserve_or_enqueue(
    generation_service, gen_uow, gen_user, gen_queue, cost, source_asset
) -> None:
    with gen_uow:
        first = create(generation_service, gen_user.id, input_asset_ids=(source_asset,))
    with gen_uow:
        second = create(generation_service, gen_user.id, input_asset_ids=(source_asset,))

    assert second.replayed is True
    assert second.task is first.task
    assert second.task.status == "QUEUED"
    assert ledger_types(gen_uow) == ["RESERVE"]
    assert wallet_state(gen_uow, gen_user.id) == (90, 10, 100, 0)
    assert [item.generation_id for item in gen_queue.generations] == [first.task.id]
    assert gen_uow.commits == 1
    assert len(gen_uow.generation_tasks.rows) == 1


def test_same_key_different_prompt_is_idempotency_conflict(
    generation_service, gen_uow, gen_user, gen_queue, cost, source_asset
) -> None:
    with gen_uow:
        first = create(generation_service, gen_user.id, input_asset_ids=(source_asset,))
    entries_before = len(gen_uow.point_ledger.entries)

    with pytest.raises(ApiError) as excinfo, gen_uow:
        create(
            generation_service,
            gen_user.id,
            prompt="换一个完全不同的提示词",
            input_asset_ids=(source_asset,),
        )

    assert excinfo.value.code is ErrorCode.IDEMPOTENCY_CONFLICT
    assert excinfo.value.status_code == 409
    assert len(gen_uow.point_ledger.entries) == entries_before
    assert wallet_state(gen_uow, gen_user.id) == (90, 10, 100, 0)
    assert [item.generation_id for item in gen_queue.generations] == [first.task.id]
    assert len(gen_uow.generation_tasks.rows) == 1


def test_same_key_different_output_count_is_idempotency_conflict(
    generation_service, gen_uow, gen_user, cost, source_asset
) -> None:
    with gen_uow:
        create(generation_service, gen_user.id, input_asset_ids=(source_asset,))

    with pytest.raises(ApiError) as excinfo, gen_uow:
        create(
            generation_service,
            gen_user.id,
            output_count=3,
            input_asset_ids=(source_asset,),
        )

    assert excinfo.value.code is ErrorCode.IDEMPOTENCY_CONFLICT
    assert len(gen_uow.generation_tasks.rows) == 1


def test_same_key_different_input_assets_is_idempotency_conflict(
    generation_service, gen_uow, gen_storage, gen_user, cost, source_asset
) -> None:
    with gen_uow:
        create(generation_service, gen_user.id, input_asset_ids=(source_asset,))
    other = ready_asset(gen_uow, gen_storage, gen_user.id)

    with pytest.raises(ApiError) as excinfo, gen_uow:
        create(generation_service, gen_user.id, input_asset_ids=(other,))

    assert excinfo.value.code is ErrorCode.IDEMPOTENCY_CONFLICT


def test_idempotency_scope_is_per_user(
    generation_service, gen_uow, gen_storage, gen_user, gen_queue, cost, source_asset, clock
) -> None:
    """作用域含 `user_id`：另一用户使用同一个键是另一条独立请求。"""

    other = gen_uow.users.seed(make_user_row(now=clock()))
    email_row = gen_uow.emails.add(user_id=other.id, email="second@example.com", is_primary=True)
    gen_uow.emails.mark_verified(email_row, clock())
    gen_uow.point_wallets.seed(other.id, available_points=100, lifetime_credited=100)
    other_asset = ready_asset(gen_uow, gen_storage, other.id)

    with gen_uow:
        mine = create(generation_service, gen_user.id, input_asset_ids=(source_asset,))
    with gen_uow:
        theirs = create(
            generation_service, other.id, key=CREATE_KEY, input_asset_ids=(other_asset,)
        )

    assert theirs.task.id != mine.task.id
    assert theirs.replayed is False
    assert ledger_types(gen_uow) == ["RESERVE", "RESERVE"]
    assert len(gen_queue.generations) == 2


def test_race_lost_against_created_row_reports_in_progress(
    generation_service, gen_uow, gen_user, gen_queue, cost, source_asset
) -> None:
    """唯一索引裁决：回滚 → 重查 → 胜出行仍是 `CREATED` → `IDEMPOTENCY_IN_PROGRESS`。"""

    winner = seed_task(gen_uow, gen_user.id, status="CREATED")

    def _lose_the_race(*args: object, **kwargs: object) -> GenerationTask:
        raise DuplicateIdempotencyKey(CREATE_KEY)

    gen_uow.generation_tasks.create = _lose_the_race  # type: ignore[method-assign]

    with pytest.raises(ApiError) as excinfo, gen_uow:
        create(generation_service, gen_user.id, input_asset_ids=(source_asset,))

    assert excinfo.value.code is ErrorCode.IDEMPOTENCY_IN_PROGRESS
    assert excinfo.value.status_code == 409
    assert winner.status == "CREATED"
    assert gen_uow.point_ledger.entries == ()
    assert gen_uow.commits == 0
    assert gen_queue.generations == []


def test_race_lost_against_committed_winner_replays(
    generation_service, gen_uow, gen_user, gen_queue, cost, source_asset
) -> None:
    winner = seed_task(gen_uow, gen_user.id, status="QUEUED", input_asset_ids=(source_asset,))
    reserve_only(gen_uow, gen_user.id, winner.id)

    def _lose_the_race(*args: object, **kwargs: object) -> GenerationTask:
        raise DuplicateIdempotencyKey(CREATE_KEY)

    gen_uow.generation_tasks.create = _lose_the_race  # type: ignore[method-assign]

    with gen_uow:
        outcome = create(generation_service, gen_user.id, input_asset_ids=(source_asset,))

    assert outcome.replayed is True
    assert outcome.task is winner
    assert ledger_types(gen_uow) == ["RESERVE"]
    assert gen_queue.generations == []


def test_race_lost_with_different_body_is_conflict(
    generation_service, gen_uow, gen_user, gen_queue, cost
) -> None:
    seed_task(gen_uow, gen_user.id, status="QUEUED", prompt="完全不同的提示词")

    def _lose_the_race(*args: object, **kwargs: object) -> GenerationTask:
        raise DuplicateIdempotencyKey(CREATE_KEY)

    gen_uow.generation_tasks.create = _lose_the_race  # type: ignore[method-assign]

    with pytest.raises(ApiError) as excinfo, gen_uow:
        create(generation_service, gen_user.id)

    assert excinfo.value.code is ErrorCode.IDEMPOTENCY_CONFLICT
    assert gen_uow.point_ledger.entries == ()
    assert gen_queue.generations == []


# --------------------------------------------------------------------------- #
# R10：Feature Flag、输入资源与邮箱门禁（必须早于任何写入）
# --------------------------------------------------------------------------- #
def test_reference_image_mode_is_rejected_before_any_write(
    generation_service, gen_uow, gen_user, gen_queue, cost, source_asset
) -> None:
    with pytest.raises(ApiError) as excinfo, gen_uow:
        create(
            generation_service,
            gen_user.id,
            mode="REFERENCE_IMAGE",
            input_asset_ids=(source_asset,),
        )

    assert excinfo.value.code is ErrorCode.FEATURE_NOT_AVAILABLE
    assert excinfo.value.status_code == 422
    assert_no_writes(gen_uow, gen_queue)


def test_reference_image_with_flag_on_links_source_and_reference(
    gen_uow, gen_storage, gen_user, gen_queue, clock
) -> None:
    service = build_service(gen_uow, gen_queue, clock, enable_reference_image=True)
    seed_cost(gen_uow, mode="REFERENCE_IMAGE")
    source = ready_asset(gen_uow, gen_storage, gen_user.id, purpose="SOURCE")
    reference = ready_asset(gen_uow, gen_storage, gen_user.id, purpose="REFERENCE")

    with gen_uow:
        outcome = create(
            service, gen_user.id, mode="REFERENCE_IMAGE", input_asset_ids=(source, reference)
        )

    rows = gen_uow.generation_tasks.list_inputs(outcome.task.id)
    assert [(row.role, row.sort_order) for row in rows] == [("REFERENCE", 0), ("SOURCE", 0)]
    assert outcome.task.billing_status == "RESERVED"
    assert outcome.task.status == "QUEUED"


def test_reference_image_without_reference_asset_is_rejected_before_write(
    gen_uow, gen_storage, gen_user, gen_queue, clock
) -> None:
    service = build_service(gen_uow, gen_queue, clock, enable_reference_image=True)
    seed_cost(gen_uow, mode="REFERENCE_IMAGE")
    source = ready_asset(gen_uow, gen_storage, gen_user.id, purpose="SOURCE")

    with pytest.raises(ApiError) as excinfo, gen_uow:
        create(service, gen_user.id, mode="REFERENCE_IMAGE", input_asset_ids=(source,))

    assert excinfo.value.code is ErrorCode.VALIDATION_ERROR
    assert_no_writes(gen_uow, gen_queue)


def test_reference_image_rejects_more_than_two_references(
    gen_uow, gen_storage, gen_user, gen_queue, clock
) -> None:
    service = build_service(gen_uow, gen_queue, clock, enable_reference_image=True)
    seed_cost(gen_uow, mode="REFERENCE_IMAGE")
    source = ready_asset(gen_uow, gen_storage, gen_user.id, purpose="SOURCE")
    first = ready_asset(gen_uow, gen_storage, gen_user.id, purpose="REFERENCE")
    second = ready_asset(gen_uow, gen_storage, gen_user.id, purpose="REFERENCE")
    third = ready_asset(gen_uow, gen_storage, gen_user.id, purpose="REFERENCE")

    with pytest.raises(ApiError) as excinfo, gen_uow:
        create(
            service,
            gen_user.id,
            mode="REFERENCE_IMAGE",
            input_asset_ids=(source, first, second, third),
        )

    assert excinfo.value.code is ErrorCode.VALIDATION_ERROR
    assert_no_writes(gen_uow, gen_queue)


def test_text_prompt_rejects_input_assets(
    generation_service, gen_uow, gen_user, gen_queue, cost, source_asset
) -> None:
    with pytest.raises(ApiError) as excinfo, gen_uow:
        create(
            generation_service, gen_user.id, mode="TEXT_PROMPT", input_asset_ids=(source_asset,)
        )

    assert excinfo.value.code is ErrorCode.VALIDATION_ERROR
    assert_no_writes(gen_uow, gen_queue)


def test_photo_prompt_requires_one_source_asset(
    generation_service, gen_uow, gen_user, gen_queue, cost
) -> None:
    with pytest.raises(ApiError) as excinfo, gen_uow:
        create(generation_service, gen_user.id)

    assert excinfo.value.code is ErrorCode.VALIDATION_ERROR
    assert_no_writes(gen_uow, gen_queue)


def test_text_prompt_creates_without_input_assets(
    generation_service, gen_uow, gen_user, gen_queue, clock
) -> None:
    seed_cost(gen_uow, mode="TEXT_PROMPT")

    with gen_uow:
        outcome = create(generation_service, gen_user.id, mode="TEXT_PROMPT")

    assert outcome.task.status == "QUEUED"
    assert gen_uow.generation_tasks.list_inputs(outcome.task.id) == ()
    assert len(gen_queue.generations) == 1


def test_unknown_input_asset_is_rejected_before_write(
    generation_service, gen_uow, gen_user, gen_queue, cost
) -> None:
    with pytest.raises(ApiError) as excinfo, gen_uow:
        create(generation_service, gen_user.id, input_asset_ids=(uuid.uuid4(),))

    assert excinfo.value.code is ErrorCode.ASSET_NOT_FOUND
    assert_no_writes(gen_uow, gen_queue)


def test_input_asset_of_another_user_is_rejected_before_write(
    gen_uow, gen_storage, gen_user, gen_queue, clock, cost
) -> None:
    stranger = gen_uow.users.seed(make_user_row(now=clock()))
    foreign = ready_asset(gen_uow, gen_storage, stranger.id)
    service = build_service(gen_uow, gen_queue, clock)

    with pytest.raises(ApiError) as excinfo, gen_uow:
        create(service, gen_user.id, input_asset_ids=(foreign,))

    assert excinfo.value.code is ErrorCode.ASSET_FORBIDDEN
    assert_no_writes(gen_uow, gen_queue)


def test_pending_upload_input_asset_is_rejected_before_write(
    generation_service, gen_uow, gen_storage, gen_user, gen_queue, cost
) -> None:
    pending = ready_asset(gen_uow, gen_storage, gen_user.id, status="PENDING_UPLOAD")

    with pytest.raises(ApiError) as excinfo, gen_uow:
        create(generation_service, gen_user.id, input_asset_ids=(pending,))

    assert excinfo.value.code is ErrorCode.ASSET_NOT_READY
    assert_no_writes(gen_uow, gen_queue)


def test_reference_purpose_asset_is_rejected_for_photo_prompt(
    generation_service, gen_uow, gen_storage, gen_user, gen_queue, cost
) -> None:
    reference = ready_asset(gen_uow, gen_storage, gen_user.id, purpose="REFERENCE")

    with pytest.raises(ApiError) as excinfo, gen_uow:
        create(generation_service, gen_user.id, input_asset_ids=(reference,))

    assert excinfo.value.code is ErrorCode.VALIDATION_ERROR
    assert_no_writes(gen_uow, gen_queue)


def test_duplicate_input_asset_ids_are_rejected(
    generation_service, gen_uow, gen_user, gen_queue, cost, source_asset
) -> None:
    with pytest.raises(ApiError) as excinfo, gen_uow:
        create(generation_service, gen_user.id, input_asset_ids=(source_asset, source_asset))

    assert excinfo.value.code is ErrorCode.VALIDATION_ERROR
    assert_no_writes(gen_uow, gen_queue)


def test_unverified_email_is_rejected_before_write(
    generation_service, gen_uow, gen_user, gen_queue, cost, source_asset
) -> None:
    row = gen_uow.emails.find_verified_for_user(gen_user.id)
    assert row is not None
    row.verified_at = None

    with pytest.raises(ApiError) as excinfo, gen_uow:
        create(generation_service, gen_user.id, input_asset_ids=(source_asset,))

    assert excinfo.value.code is ErrorCode.EMAIL_NOT_VERIFIED
    assert_no_writes(gen_uow, gen_queue)


# --------------------------------------------------------------------------- #
# 查询：单个任务、结果列表、键集分页
# --------------------------------------------------------------------------- #
def test_get_generation_returns_owner_task_and_hides_foreign(
    generation_service, gen_uow, gen_user, clock, cost, source_asset
) -> None:
    with gen_uow:
        outcome = create(generation_service, gen_user.id, input_asset_ids=(source_asset,))
    stranger = gen_uow.users.seed(make_user_row(now=clock()))

    assert (
        generation_service.get_generation(
            generation_id=outcome.task.id, requester_user_id=gen_user.id
        )
        is outcome.task
    )
    for generation_id, requester in ((outcome.task.id, stranger.id), (uuid.uuid4(), gen_user.id)):
        with pytest.raises(ApiError) as excinfo:
            generation_service.get_generation(
                generation_id=generation_id, requester_user_id=requester
            )
        assert excinfo.value.code is ErrorCode.GENERATION_NOT_FOUND
        assert excinfo.value.status_code == 404


def test_list_generations_pages_over_keyset_cursor(
    generation_service, gen_uow, gen_user, cost, source_asset, clock
) -> None:
    created: list[uuid.UUID] = []
    for index in range(3):
        clock.advance(1)
        with gen_uow:
            outcome = create(
                generation_service,
                gen_user.id,
                key=f"k{index}",
                input_asset_ids=(source_asset,),
            )
        created.append(outcome.task.id)

    page = generation_service.list_generations(user_id=gen_user.id, cursor=None, limit=2)
    assert [task.id for task in page.rows] == [created[2], created[1]]
    assert page.has_more is True
    assert page.next_cursor is not None

    second = generation_service.list_generations(
        user_id=gen_user.id, cursor=page.next_cursor, limit=2
    )
    assert [task.id for task in second.rows] == [created[0]]
    assert second.has_more is False
    assert second.next_cursor is None

    empty = generation_service.list_generations(user_id=uuid.uuid4(), cursor=None, limit=10)
    assert empty.rows == ()
    assert empty.has_more is False


def test_list_generations_rejects_malformed_cursor(
    generation_service, gen_uow, gen_user
) -> None:
    with pytest.raises(ApiError) as excinfo:
        generation_service.list_generations(user_id=gen_user.id, cursor="not-a-cursor", limit=5)

    assert excinfo.value.code is ErrorCode.VALIDATION_ERROR
    assert excinfo.value.details["fields"] == ["cursor"]


def test_list_generation_outputs_exposes_view_fields(
    generation_service, gen_uow, gen_user, cost, source_asset
) -> None:
    with gen_uow:
        task = create(generation_service, gen_user.id, input_asset_ids=(source_asset,)).task
    asset_ids = settle_two_outputs(gen_uow, task, gen_user.id)

    views = generation_service.list_generation_outputs(
        generation_id=task.id, requester_user_id=gen_user.id
    )
    assert [view.output_index for view in views] == [0, 1]
    assert [view.asset_id for view in views] == asset_ids
    for view in views:
        assert view.mime_type == "image/png"
        assert (view.width, view.height) == (4, 4)
        assert view.size_bytes == len(synthetic_png_bytes())

    with pytest.raises(ApiError) as excinfo:
        generation_service.list_generation_outputs(
            generation_id=task.id, requester_user_id=uuid.uuid4()
        )
    assert excinfo.value.code is ErrorCode.GENERATION_NOT_FOUND


# --------------------------------------------------------------------------- #
# R6：用户重试
# --------------------------------------------------------------------------- #
def test_retry_creates_new_task_with_parent_and_new_reservation(
    generation_service, gen_uow, gen_user, gen_queue, cost, source_asset, clock
) -> None:
    with gen_uow:
        original = create(generation_service, gen_user.id, input_asset_ids=(source_asset,)).task
    force_failed_release(gen_uow, original, gen_user.id, clock)

    ledger_before = len(gen_uow.point_ledger.entries)
    with gen_uow:
        outcome = generation_service.retry_generation(
            user_id=gen_user.id, generation_id=original.id, idempotency_key="retry-1"
        )

    retried = outcome.task
    assert outcome.replayed is False
    assert retried.id != original.id
    assert retried.parent_task_id == original.id
    assert retried.status == "QUEUED"
    assert retried.billing_status == "RESERVED"
    assert retried.idempotency_key == "retry-1"
    assert retried.mode == original.mode
    assert retried.prompt == original.prompt
    assert retried.requested_count == original.requested_count
    assert retried.reserved_points == original.reserved_points
    # 父任务保持 FAILED + RELEASED 不动，也不复制旧流水
    assert original.status == "FAILED"
    assert original.billing_status == "RELEASED"
    assert len(gen_uow.point_ledger.entries) == ledger_before + 1
    assert gen_uow.point_ledger.entries[-1].entry_type == "RESERVE"
    assert gen_uow.point_ledger.entries[-1].generation_id == retried.id
    assert wallet_state(gen_uow, gen_user.id) == (90, 10, 100, 0)
    # 输入关联重建
    assert [row.asset_id for row in gen_uow.generation_tasks.list_inputs(retried.id)] == [
        source_asset
    ]
    assert [item.generation_id for item in gen_queue.generations] == [original.id, retried.id]


@pytest.mark.parametrize(
    "status", ["CREATED", "QUEUED", "PROCESSING", "SUCCEEDED", "NEEDS_REVIEW"]
)
def test_retry_only_accepts_failed_tasks(
    generation_service, gen_uow, gen_user, cost, status: str
) -> None:
    """`NEEDS_REVIEW` 不能被重试绕过人工结算（R6 / `LEAD-04` §7）。"""

    task = seed_task(gen_uow, gen_user.id, status=status)
    ledger_before = len(gen_uow.point_ledger.entries)

    with pytest.raises(ApiError) as excinfo, gen_uow:
        generation_service.retry_generation(
            user_id=gen_user.id, generation_id=task.id, idempotency_key="retry-1"
        )

    assert excinfo.value.code is ErrorCode.GENERATION_NOT_RETRYABLE
    assert excinfo.value.status_code == 409
    assert len(gen_uow.point_ledger.entries) == ledger_before
    assert gen_uow.generation_tasks.rows == (task,)
    assert gen_uow.commits == 0


def test_retry_of_foreign_task_is_generation_not_found(
    generation_service, gen_uow, gen_user, cost
) -> None:
    task = seed_task(gen_uow, uuid.uuid4(), status="FAILED", billing_status="RELEASED")

    with pytest.raises(ApiError) as excinfo, gen_uow:
        generation_service.retry_generation(
            user_id=gen_user.id, generation_id=task.id, idempotency_key="retry-1"
        )

    assert excinfo.value.code is ErrorCode.GENERATION_NOT_FOUND


def test_retry_is_idempotent_and_conflicts_on_different_parent(
    generation_service, gen_uow, gen_user, gen_queue, cost, source_asset, clock
) -> None:
    with gen_uow:
        first = create(generation_service, gen_user.id, input_asset_ids=(source_asset,)).task
    with gen_uow:
        second = create(
            generation_service, gen_user.id, key="idem-create-2", input_asset_ids=(source_asset,)
        ).task
    force_failed_release(gen_uow, first, gen_user.id, clock)
    force_failed_release(gen_uow, second, gen_user.id, clock)

    with gen_uow:
        retried = generation_service.retry_generation(
            user_id=gen_user.id, generation_id=first.id, idempotency_key="retry-1"
        )
    with gen_uow:
        replay = generation_service.retry_generation(
            user_id=gen_user.id, generation_id=first.id, idempotency_key="retry-1"
        )

    assert replay.replayed is True
    assert replay.task is retried.task
    reserves = [entry for entry in gen_uow.point_ledger.entries if entry.entry_type == "RESERVE"]
    assert len(reserves) == 3  # 两个原任务 + 一个重试子任务
    assert len(gen_queue.generations) == 3

    with pytest.raises(ApiError) as excinfo, gen_uow:
        generation_service.retry_generation(
            user_id=gen_user.id, generation_id=second.id, idempotency_key="retry-1"
        )
    assert excinfo.value.code is ErrorCode.IDEMPOTENCY_CONFLICT
    assert len(gen_uow.point_ledger.entries) == 5  # 2 RESERVE + 2 RELEASE + 重试的 1 RESERVE


# --------------------------------------------------------------------------- #
# R9：内部取消（无公开 API）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("status", ["CREATED", "QUEUED"])
def test_cancel_before_provider_starts_fails_and_releases(
    generation_service, gen_uow, gen_user, cost, status: str
) -> None:
    task = seed_task(gen_uow, gen_user.id, status=status)
    reserve_only(gen_uow, gen_user.id, task.id)

    with gen_uow:
        cancelled = generation_service.cancel_internally(generation_id=task.id)

    assert cancelled.status == "FAILED"
    assert cancelled.billing_status == "RELEASED"
    assert cancelled.failure_code == "CANCELLED_BY_USER"
    assert ledger_types(gen_uow) == ["RESERVE", "RELEASE"]
    assert wallet_state(gen_uow, gen_user.id) == (100, 0, 100, 0)


@pytest.mark.parametrize(
    ("status", "billing_status"),
    [("SUCCEEDED", "CONSUMED"), ("FAILED", "RELEASED"), ("NEEDS_REVIEW", "RESERVED")],
)
def test_cancel_on_terminal_task_is_idempotent_no_op(
    generation_service, gen_uow, gen_user, cost, status: str, billing_status: str
) -> None:
    task = seed_task(gen_uow, gen_user.id, status=status, billing_status=billing_status)
    transitions_before = len(gen_uow.generation_tasks.transition_calls)
    ledger_before = len(gen_uow.point_ledger.entries)

    with gen_uow:
        again = generation_service.cancel_internally(generation_id=task.id)

    assert again is task
    assert again.status == status
    assert again.billing_status == billing_status
    assert len(gen_uow.generation_tasks.transition_calls) == transitions_before
    assert len(gen_uow.point_ledger.entries) == ledger_before


def test_cancel_missing_task_is_generation_not_found(generation_service, gen_uow) -> None:
    with pytest.raises(ApiError) as excinfo, gen_uow:
        generation_service.cancel_internally(generation_id=uuid.uuid4())

    assert excinfo.value.code is ErrorCode.GENERATION_NOT_FOUND


# --------------------------------------------------------------------------- #
# 命令与常量：非法输入在写入前拒绝
# --------------------------------------------------------------------------- #
def test_command_rejects_unknown_mode() -> None:
    with pytest.raises(ApiError) as excinfo:
        new_command(mode="NOPE")

    assert excinfo.value.code is ErrorCode.VALIDATION_ERROR


def test_command_rejects_blank_prompt_and_non_positive_count() -> None:
    for overrides in ({"prompt": "   "}, {"output_count": 0}, {"output_count": -1}):
        with pytest.raises(ApiError) as excinfo:
            new_command(**overrides)
        assert excinfo.value.code is ErrorCode.VALIDATION_ERROR


def test_command_normalizes_input_asset_ids_to_tuple() -> None:
    asset = uuid.uuid4()

    held = new_command(input_asset_ids=[asset])

    assert held.input_asset_ids == (asset,)
