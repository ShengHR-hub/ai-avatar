"""Generation Orchestrator 测试（ENG-06 第 3 步）。

覆盖 `LEAD-04` §6/§7/§11 与 `04-ai-provider.md` 第 2/3/4/5/6 节在编排层的全部规则：

- 事务边界：领取是短事务，Provider 调用落在任何事务之外，终局「结果 + 状态 + 结算 +
  钱包快照 + attempt」同一个事务（探针会在违规时直接抛错）；
- R4 结算矩阵：成功消费、明确失败释放、`UNKNOWN`/空输出/数量不足/结果校验失败只转
  `NEEDS_REVIEW` 并保持 `RESERVED`；
- R7 传输层重试：只在可证明未接受时重试，退避 5/30/120，每次留独立 attempt 行，
  预算耗尽后按明确失败放款一次；
- R8 轮询与 Webhook 归一 + 终态单调：重复或迟到事件不得重开终态、不得二次结算；
- R9 内部取消：只有确认取消才 `FAILED + RELEASED`，未确认一律 `NEEDS_REVIEW`。

只使用内存替身与本地 `MockProvider` 风格的脚本化 Provider，不接触网络与真实供应商。
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.db.models.generation import GenerationTask
from app.errors.codes import ErrorCode
from app.errors.exceptions import ApiError
from app.providers.ports import (
    GenerateRequest,
    ImageInput,
    ProviderCallError,
    ProviderError,
    ProviderErrorCategory,
    ProviderOutput,
    ProviderResult,
    ProviderState,
    ProviderUsage,
)
from app.repositories.generation import GenerationInputDraft
from app.repositories.media import MediaAssetDraft
from app.repositories.provider_attempt import ProviderAttemptDraft
from app.services.generation.mappers import to_api_generation
from app.services.generation.orchestrator import (
    OUTCOME_FAILED,
    OUTCOME_NEEDS_REVIEW,
    OUTCOME_NOT_CLAIMABLE,
    OUTCOME_PENDING,
    OUTCOME_REPLAYED,
    OUTCOME_RETRY_SCHEDULED,
    OUTCOME_SUCCEEDED,
    GenerationOrchestrator,
)
from app.services.generation.outputs import output_idempotency_key, persist_provider_outputs
from app.services.generation.ports import WebhookEvent
from app.services.generation.provider_factory import build_ai_provider
from app.services.generation.service import CANCEL_FAILURE_CODE, ledger_key
from app.services.generation.state import assert_transition
from app.services.points.service import PointsService
from app.storage import InMemoryStorageAdapter
from app.storage.ports import StorageUnavailable, StoredObject
from tests.services.fakes import (
    InMemoryReplayGuard,
    ProbingProvider,
    ScriptedProvider,
    TransactionProbe,
    make_media_asset_row,
    synthetic_image_bytes,
    synthetic_png_bytes,
)

PROMPT = "保持人物特征，生成专业头像"
POINTS_COST = 10
OUTPUT_COUNT = 2
EXTERNAL_ID = "ext-1"


# --------------------------------------------------------------------------- #
# 构造辅助
# --------------------------------------------------------------------------- #
def png_output(index: int, *, content: bytes | None = None) -> ProviderOutput:
    return ProviderOutput(
        content=content or synthetic_png_bytes(),
        mime_type="image/png",
        provider_output_id=f"ext-out-{index}",
    )


def succeeded(*, count: int = OUTPUT_COUNT) -> ProviderResult:
    outputs = tuple(png_output(index) for index in range(count))
    return ProviderResult(
        state=ProviderState.SUCCEEDED,
        client_request_id="clr",
        provider_request_id=EXTERNAL_ID,
        outputs=outputs,
        usage=ProviderUsage(
            input_tokens=16, output_tokens=None, image_count=count,
            estimated_cost=None, currency=None,
        ),
        error=None,
        retry_after_seconds=None,
        completed_at=None,
    )


def pending() -> ProviderResult:
    return ProviderResult(
        state=ProviderState.PENDING,
        client_request_id="clr",
        provider_request_id=EXTERNAL_ID,
        outputs=(),
        usage=None,
        error=None,
        retry_after_seconds=None,
        completed_at=None,
    )


def failed(
    category: ProviderErrorCategory = ProviderErrorCategory.TEMPORARY,
    *,
    retryable: bool = False,
    request_accepted: bool | None = False,
    code: str = "mock_rejected",
) -> ProviderResult:
    return ProviderResult(
        state=ProviderState.FAILED,
        client_request_id="clr",
        provider_request_id=EXTERNAL_ID,
        outputs=(),
        usage=None,
        error=ProviderError(
            category=category,
            code=code,
            safe_message="供应商明确拒绝该请求",
            retryable=retryable,
            request_accepted=request_accepted,
        ),
        retry_after_seconds=None,
        completed_at=None,
    )


def seed_queued_task(
    gen_uow,
    user_id: uuid.UUID,
    storage,
    *,
    status: str = "QUEUED",
    with_input: bool = True,
    readable_input: bool = True,
) -> GenerationTask:
    """种一行任务并写出 `RESERVE`，得到 R1 提交后的真实起点状态。

    输入资源一并种上：Worker 会在事务外从对象存储读回输入图字节，所以
    `generation_inputs`、`media_assets` 行与存储里的对象三者必须同时存在。
    `readable_input=False` 用来构造「资源行在、对象不在」的故障场景。
    """

    moment = datetime.now(timezone.utc)
    task = gen_uow.generation_tasks.seed(
        _task_row(user_id=user_id, status=status, moment=moment)
    )
    if with_input:
        content = synthetic_png_bytes()
        asset = make_media_asset_row(
            user_id=user_id,
            purpose="SOURCE",
            asset_type="ORIGINAL",
            status="READY",
            size_bytes=len(content),
            width=4,
            height=4,
            sha256=hashlib.sha256(content).hexdigest(),
        )
        if readable_input:
            storage.upload(asset.storage_key, content, "image/png")
        gen_uow.media.register(asset)
        gen_uow.generation_tasks.create_input(
            GenerationInputDraft(
                generation_id=task.id, asset_id=asset.id, role="SOURCE", sort_order=0
            )
        )
    PointsService(uow=gen_uow).reserve(
        user_id=user_id,
        generation_id=task.id,
        amount=POINTS_COST,
        idempotency_key=ledger_key("reserve", task.id),
        reason="generation_reserve",
    )
    gen_uow.commit()
    return task


def _task_row(*, user_id: uuid.UUID, status: str, moment: datetime) -> GenerationTask:
    from tests.services.fakes import make_generation_task_row

    row = make_generation_task_row(
        user_id=user_id,
        status=status,
        billing_status="RESERVED",
        reserved_points=POINTS_COST,
        mode="PHOTO_PROMPT",
        prompt=PROMPT,
        requested_count=OUTPUT_COUNT,
        idempotency_key=f"idem-{uuid.uuid4().hex[:8]}",
        now=moment,
    )
    row.provider_name = "MOCK"
    row.model_name = "mock-image-v1"
    row.retry_count = 0
    return row


def seed_processing_attempt(gen_uow, task: GenerationTask, *, external_id: str = EXTERNAL_ID):
    """种一行已提交外部请求的 attempt（轮询/取消用例的前置状态）。"""

    attempt = gen_uow.provider_attempts.create(
        ProviderAttemptDraft(
            generation_id=task.id,
            attempt_no=1,
            provider_name="MOCK",
            model_name="mock-image-v1",
            status="SUBMITTED",
            started_at=datetime.now(timezone.utc),
            request_id=external_id,
            request_metadata={"client_request_id": f"{task.id}:1"},
        )
    )
    gen_uow.commit()
    return attempt


def seed_attempt(
    gen_uow,
    task: GenerationTask,
    attempt_no: int,
    status: str,
    external_id: str | None = None,
):
    """种一行指定状态的 attempt（重投/迟到结果用例的前置状态）。"""

    moment = datetime.now(timezone.utc)
    attempt = gen_uow.provider_attempts.create(
        ProviderAttemptDraft(
            generation_id=task.id,
            attempt_no=attempt_no,
            provider_name="MOCK",
            model_name="mock-image-v1",
            status=status,
            started_at=moment,
            request_id=external_id,
            finished_at=None if status in ("STARTED", "SUBMITTED") else moment,
            request_metadata={"client_request_id": f"{task.id}:{attempt_no}"},
        )
    )
    gen_uow.commit()
    return attempt


def wallet_state(gen_uow, user_id: uuid.UUID) -> tuple[int, int, int, int]:
    wallet = gen_uow.point_wallets.get_by_user_id(user_id)
    assert wallet is not None
    return (
        wallet.available_points,
        wallet.reserved_points,
        wallet.lifetime_credited,
        wallet.lifetime_consumed,
    )


def ledger_types(gen_uow) -> list[str]:
    return [entry.entry_type for entry in gen_uow.point_ledger.entries]


def attempts(gen_uow, generation_id: uuid.UUID):
    return gen_uow.provider_attempts.list_by_generation_id(generation_id)


# --------------------------------------------------------------------------- #
# 事务边界与领取
# --------------------------------------------------------------------------- #
def test_submit_claims_queued_task_and_starts_a_new_attempt_row(
    make_orchestrator, gen_uow, gen_storage, gen_user
) -> None:
    task = seed_queued_task(gen_uow, gen_user.id, gen_storage)
    orchestrator = make_orchestrator(ScriptedProvider(generate=[succeeded()]))

    assert orchestrator.submit(task.id) == OUTCOME_SUCCEEDED

    rows = attempts(gen_uow, task.id)
    assert [row.attempt_no for row in rows] == [1]
    assert rows[0].provider_name == "MOCK"
    assert rows[0].model_name == "mock-image-v1"
    assert rows[0].status == "SUCCEEDED"
    assert rows[0].request_id == EXTERNAL_ID
    assert rows[0].started_at is not None and rows[0].finished_at is not None


def test_submit_refuses_to_claim_a_terminal_task(make_orchestrator, gen_storage, gen_uow, gen_user) -> None:
    task = seed_queued_task(gen_uow, gen_user.id, gen_storage, status="SUCCEEDED")
    provider = ScriptedProvider(generate=[succeeded()])

    assert make_orchestrator(provider).submit(task.id) == OUTCOME_NOT_CLAIMABLE
    assert provider.generate_calls == 0
    assert attempts(gen_uow, task.id) == ()


def test_provider_calls_never_happen_inside_a_database_transaction(
    gen_uow, gen_user, gen_storage, gen_queue, clock
) -> None:
    """探针在事务开着时观察到任何 Provider 调用即抛错（`LEAD-04` §6:121）。"""

    task = seed_queued_task(gen_uow, gen_user.id, gen_storage)
    inner = ScriptedProvider(generate=[succeeded()])
    orchestrator = GenerationOrchestrator(
        uow_factory=lambda: gen_uow,
        points_factory=lambda uow: PointsService(uow=uow, clock=clock),
        provider_factory=lambda: ProbingProvider(inner, gen_uow.probe),
        storage=gen_storage,
        clock=clock,
        queue=gen_queue,
    )

    assert orchestrator.submit(task.id) == OUTCOME_SUCCEEDED

    events = gen_uow.probe.events
    call_index = events.index("provider:generate")
    # 领取事务必须先结束，Provider 调用才开始；结算事务在调用之后才开始
    assert events[call_index - 1] == "tx:end"
    assert "tx:begin" in events[call_index + 1 :]


def test_attempt_metadata_never_contains_prompt_or_image_bytes(
    make_orchestrator, gen_uow, gen_storage, gen_user
) -> None:
    task = seed_queued_task(gen_uow, gen_user.id, gen_storage)
    orchestrator = make_orchestrator(ScriptedProvider(generate=[succeeded()]))

    orchestrator.submit(task.id)

    attempt = attempts(gen_uow, task.id)[0]
    serialized = f"{attempt.request_metadata}{attempt.response_metadata}"
    assert PROMPT not in serialized
    assert "content" not in serialized


def test_client_request_id_is_stable_for_the_attempt(
    make_orchestrator, gen_uow, gen_storage, gen_user
) -> None:
    task = seed_queued_task(gen_uow, gen_user.id, gen_storage)
    provider = ScriptedProvider(generate=[pending(), succeeded()])
    orchestrator = make_orchestrator(provider)

    assert orchestrator.submit(task.id) == OUTCOME_PENDING

    attempt = attempts(gen_uow, task.id)[0]
    assert attempt.request_metadata["client_request_id"] == f"{task.id}:1"
    assert provider.generate_requests[0].client_request_id == f"{task.id}:1"


# --------------------------------------------------------------------------- #
# 输入资源：字节在事务外从对象存储读回
# --------------------------------------------------------------------------- #
def test_input_images_are_loaded_from_storage_outside_the_transaction(
    gen_uow, gen_user, gen_storage, gen_queue, clock
) -> None:
    task = seed_queued_task(gen_uow, gen_user.id, gen_storage)
    provider = ScriptedProvider(generate=[succeeded()])
    orchestrator = GenerationOrchestrator(
        uow_factory=lambda: gen_uow,
        points_factory=lambda uow: PointsService(uow=uow, clock=clock),
        provider_factory=lambda: ProbingProvider(provider, gen_uow.probe),
        storage=gen_storage,
        clock=clock,
        queue=gen_queue,
    )

    assert orchestrator.submit(task.id) == OUTCOME_SUCCEEDED

    request = provider.generate_requests[0]
    assert len(request.source_images) == 1
    assert request.reference_images == ()
    assert request.source_images[0].sha256 == hashlib.sha256(
        synthetic_png_bytes()
    ).hexdigest()
    assert request.source_images[0].mime_type == "image/png"
    events = gen_uow.probe.events
    assert events[events.index("provider:generate") - 1] == "tx:end"


def test_unreadable_input_fails_definitely_without_calling_the_provider(
    gen_uow, gen_user, gen_storage, gen_queue, clock
) -> None:
    """输入对象读不到 = 可证明没有外部执行：明确失败并放款一次，绝不伪造结果。"""

    task = seed_queued_task(
        gen_uow, gen_user.id, gen_storage, readable_input=False
    )
    provider = ScriptedProvider(generate=[succeeded()])
    orchestrator = GenerationOrchestrator(
        uow_factory=lambda: gen_uow,
        points_factory=lambda uow: PointsService(uow=uow, clock=clock),
        provider_factory=lambda: provider,
        storage=gen_storage,
        clock=clock,
        queue=gen_queue,
    )

    assert orchestrator.submit(task.id) == OUTCOME_FAILED
    assert provider.generate_calls == 0
    assert task.status == "FAILED"
    assert task.billing_status == "RELEASED"
    assert ledger_types(gen_uow) == ["RESERVE", "RELEASE"]
    assert gen_uow.generation_tasks.list_outputs(task.id) == ()


# --------------------------------------------------------------------------- #
# R4：终局结算矩阵
# --------------------------------------------------------------------------- #
def test_success_persists_results_and_consumes_once(
    make_orchestrator, gen_uow, gen_storage, gen_user
) -> None:
    task = seed_queued_task(gen_uow, gen_user.id, gen_storage)
    orchestrator = make_orchestrator(ScriptedProvider(generate=[succeeded()]))

    assert orchestrator.submit(task.id) == OUTCOME_SUCCEEDED

    assert task.status == "SUCCEEDED"
    assert task.billing_status == "CONSUMED"
    assert task.completed_at is not None
    outputs = gen_uow.generation_tasks.list_outputs(task.id)
    assert [row.output_index for row in outputs] == [0, 1]
    assert [row.provider_output_id for row in outputs] == ["ext-out-0", "ext-out-1"]

    for row in outputs:
        asset = gen_uow.media.get(row.asset_id)
        assert asset.status == "READY"
        assert asset.purpose == "RESULT"
        assert asset.asset_type == "RESULT"
        assert asset.width == 4 and asset.height == 4
        assert asset.size_bytes == len(synthetic_png_bytes())
        assert asset.sha256 == hashlib.sha256(synthetic_png_bytes()).hexdigest()
        assert asset.idempotency_key == output_idempotency_key(task.id, row.output_index)
        assert asset.storage_key.startswith(f"assets/{gen_user.id}/")

    assert ledger_types(gen_uow) == ["RESERVE", "CONSUME"]
    assert wallet_state(gen_uow, gen_user.id) == (90, 0, 100, POINTS_COST)


def test_result_bytes_go_through_the_common_image_validator(
    make_orchestrator, gen_uow, gen_storage, gen_user
) -> None:
    """坏图必须整体回滚并转人工复核，绝不落半截资源、绝不按成功计费。"""

    task = seed_queued_task(gen_uow, gen_user.id, gen_storage)
    corrupt = succeeded()
    broken = ProviderResult(
        state=ProviderState.SUCCEEDED,
        client_request_id="clr",
        provider_request_id=EXTERNAL_ID,
        outputs=(png_output(0), png_output(1, content=b"\x89PNG\r\n\x1a\n" + b"garbage")),
        usage=None,
        error=None,
        retry_after_seconds=None,
        completed_at=None,
    )
    assert corrupt.outputs[0].content == broken.outputs[0].content

    outcome = make_orchestrator(ScriptedProvider(generate=[broken])).submit(task.id)

    assert outcome == OUTCOME_NEEDS_REVIEW
    assert task.status == "NEEDS_REVIEW"
    assert task.billing_status == "RESERVED"
    assert ledger_types(gen_uow) == ["RESERVE"]
    assert gen_uow.generation_tasks.list_outputs(task.id) == ()
    assert [row for row in gen_uow.media.rows if row.purpose == "RESULT"] == []


def test_zero_output_success_is_never_billed_as_success(
    make_orchestrator, gen_uow, gen_storage, gen_user
) -> None:
    task = seed_queued_task(gen_uow, gen_user.id, gen_storage)
    empty = succeeded(count=0)

    assert make_orchestrator(ScriptedProvider(generate=[empty])).submit(task.id) == (
        OUTCOME_NEEDS_REVIEW
    )
    assert task.status == "NEEDS_REVIEW"
    assert task.billing_status == "RESERVED"
    assert ledger_types(gen_uow) == ["RESERVE"]


def test_short_output_count_is_not_billed_as_success(
    make_orchestrator, gen_uow, gen_storage, gen_user
) -> None:
    task = seed_queued_task(gen_uow, gen_user.id, gen_storage)

    assert make_orchestrator(
        ScriptedProvider(generate=[succeeded(count=1)])
    ).submit(task.id) == OUTCOME_NEEDS_REVIEW
    assert task.status == "NEEDS_REVIEW"
    assert ledger_types(gen_uow) == ["RESERVE"]
    assert wallet_state(gen_uow, gen_user.id) == (90, POINTS_COST, 100, 0)


def test_definite_failure_releases_exactly_once(
    make_orchestrator, gen_uow, gen_storage, gen_user
) -> None:
    task = seed_queued_task(gen_uow, gen_user.id, gen_storage)

    assert make_orchestrator(
        ScriptedProvider(generate=[failed()])
    ).submit(task.id) == OUTCOME_FAILED
    assert task.status == "FAILED"
    assert task.billing_status == "RELEASED"
    assert task.failure_code == "TEMPORARY"
    assert ledger_types(gen_uow) == ["RESERVE", "RELEASE"]
    assert wallet_state(gen_uow, gen_user.id) == (100, 0, 100, 0)


@pytest.mark.parametrize(
    "category",
    [ProviderErrorCategory.VALIDATION, ProviderErrorCategory.CONTENT_POLICY],
)
def test_non_retryable_categories_fail_without_any_retry(
    make_orchestrator, gen_uow, gen_storage, gen_user, gen_queue, category
) -> None:
    task = seed_queued_task(gen_uow, gen_user.id, gen_storage)

    assert make_orchestrator(
        ScriptedProvider(generate=[failed(category, retryable=True)])
    ).submit(task.id) == OUTCOME_FAILED
    assert task.failure_code == category.value
    assert gen_queue.generations == []
    assert task.retry_count == 0


# --------------------------------------------------------------------------- #
# R5：PENDING、轮询
# --------------------------------------------------------------------------- #
def test_pending_stores_external_id_and_schedules_a_poll(
    make_orchestrator, gen_uow, gen_storage, gen_user, gen_queue
) -> None:
    task = seed_queued_task(gen_uow, gen_user.id, gen_storage)
    orchestrator = make_orchestrator(
        ScriptedProvider(generate=[pending()], check_status=[succeeded()])
    )

    assert orchestrator.submit(task.id) == OUTCOME_PENDING
    assert task.status == "PROCESSING"
    assert task.billing_status == "RESERVED"
    assert attempts(gen_uow, task.id)[0].status == "SUBMITTED"
    assert [(item.generation_id, item.countdown_seconds) for item in gen_queue.polls] == [
        (task.id, 20)
    ]

    assert orchestrator.reconcile(generation_id=task.id) == OUTCOME_SUCCEEDED
    assert task.status == "SUCCEEDED"
    assert ledger_types(gen_uow) == ["RESERVE", "CONSUME"]


def test_pending_without_external_id_cannot_even_be_constructed(make_orchestrator, gen_storage, gen_uow, gen_user, gen_queue) -> None:
    """`PENDING` 必须带外部 ID：这是端口级不变量，编排层不可能收到没有关联的结果。"""

    with pytest.raises(ValueError):
        ProviderResult(
            state=ProviderState.PENDING,
            client_request_id="clr",
            provider_request_id=None,
            outputs=(),
            usage=None,
            error=None,
            retry_after_seconds=None,
            completed_at=None,
        )

    task = seed_queued_task(gen_uow, gen_user.id, gen_storage)
    assert make_orchestrator(ScriptedProvider(generate=[pending()])).submit(task.id) == (
        OUTCOME_PENDING
    )
    assert [(item.generation_id, item.attempt_no, item.countdown_seconds) for item in gen_queue.polls] == [
        (task.id, 1, 20)
    ]


# --------------------------------------------------------------------------- #
# R7：传输层重试
# --------------------------------------------------------------------------- #
def test_pre_acceptance_timeout_retries_with_backoff_then_fails(make_orchestrator, gen_storage, gen_uow, gen_user, gen_queue) -> None:
    task = seed_queued_task(gen_uow, gen_user.id, gen_storage)

    def timeout_error() -> ProviderCallError:
        # `frozen` 异常实例不可重复抛出（Python 会在抛出时写 `__traceback__`），
        # 因此每次调用都给一个全新实例——真实适配器与 `MockProvider` 也都是每次新建
        return ProviderCallError(
            category=ProviderErrorCategory.TIMEOUT,
            code="mock_timeout_before_acceptance",
            safe_message="提交前超时，可证明未接受。",
            request_accepted=False,
        )

    orchestrator = make_orchestrator(
        ScriptedProvider(generate=[timeout_error() for _ in range(4)])
    )

    outcomes = [orchestrator.submit(task.id) for _ in range(4)]

    assert outcomes == [
        OUTCOME_RETRY_SCHEDULED,
        OUTCOME_RETRY_SCHEDULED,
        OUTCOME_RETRY_SCHEDULED,
        OUTCOME_FAILED,
    ]
    assert [item.countdown_seconds for item in gen_queue.generations] == [5, 30, 120]
    assert task.retry_count == 3
    assert [row.attempt_no for row in attempts(gen_uow, task.id)] == [1, 2, 3, 4]
    assert [row.status for row in attempts(gen_uow, task.id)] == [
        "TIMEOUT",
        "TIMEOUT",
        "TIMEOUT",
        "TIMEOUT",
    ]
    assert task.status == "FAILED"
    assert task.billing_status == "RELEASED"
    assert ledger_types(gen_uow) == ["RESERVE", "RELEASE"]


def test_post_acceptance_timeout_never_retries_and_keeps_reservation(
    make_orchestrator, gen_uow, gen_storage, gen_user, gen_queue
) -> None:
    task = seed_queued_task(gen_uow, gen_user.id, gen_storage)
    accepted_timeout = ProviderCallError(
        category=ProviderErrorCategory.TIMEOUT,
        code="mock_timeout_after_acceptance",
        safe_message="接受后超时，外部执行状态无法确认。",
        request_accepted=True,
    )

    assert make_orchestrator(
        ScriptedProvider(generate=[accepted_timeout])
    ).submit(task.id) == OUTCOME_NEEDS_REVIEW
    assert gen_queue.generations == []
    assert task.status == "NEEDS_REVIEW"
    assert task.billing_status == "RESERVED"
    assert ledger_types(gen_uow) == ["RESERVE"]
    assert attempts(gen_uow, task.id)[0].status == "UNKNOWN"


def test_unresolvable_outcome_is_unknown_not_false_terminal(
    make_orchestrator, gen_uow, gen_storage, gen_user
) -> None:
    task = seed_queued_task(gen_uow, gen_user.id, gen_storage)

    def unresolvable() -> ProviderCallError:
        return ProviderCallError(
            category=ProviderErrorCategory.UNKNOWN,
            code="mock_status_unresolvable",
            safe_message="已接受但到期无法解析状态或结果。",
            request_accepted=True,
        )

    assert make_orchestrator(ScriptedProvider(generate=[pending()])).submit(task.id) == (
        OUTCOME_PENDING
    )

    orchestrator = make_orchestrator(ScriptedProvider(check_status=[unresolvable()]))
    assert orchestrator.reconcile(generation_id=task.id) == OUTCOME_NEEDS_REVIEW
    assert task.status == "NEEDS_REVIEW"
    assert attempts(gen_uow, task.id)[0].status == "UNKNOWN"


# --------------------------------------------------------------------------- #
# R8：终态单调与重复事件
# --------------------------------------------------------------------------- #
def test_duplicate_terminal_events_settle_exactly_once(
    make_orchestrator, gen_uow, gen_storage, gen_user
) -> None:
    task = seed_queued_task(gen_uow, gen_user.id, gen_storage, status="PROCESSING")
    attempt = seed_processing_attempt(gen_uow, task)
    orchestrator = make_orchestrator(ScriptedProvider(check_status=[succeeded()]))

    assert orchestrator.reconcile(generation_id=task.id) == OUTCOME_SUCCEEDED
    assert orchestrator.reconcile(generation_id=task.id) == OUTCOME_REPLAYED
    assert orchestrator.reconcile(generation_id=task.id) == OUTCOME_REPLAYED

    assert ledger_types(gen_uow) == ["RESERVE", "CONSUME"]
    assert wallet_state(gen_uow, gen_user.id) == (90, 0, 100, POINTS_COST)
    assert attempt.status == "SUCCEEDED"


def test_late_pending_event_cannot_reopen_a_terminal_attempt(
    make_orchestrator, gen_uow, gen_storage, gen_user, gen_queue
) -> None:
    task = seed_queued_task(gen_uow, gen_user.id, gen_storage, status="PROCESSING")
    seed_processing_attempt(gen_uow, task)
    orchestrator = make_orchestrator(ScriptedProvider(check_status=[succeeded()]))
    assert orchestrator.reconcile(generation_id=task.id) == OUTCOME_SUCCEEDED

    late = make_orchestrator(ScriptedProvider(check_status=[pending()]))
    assert late.reconcile(generation_id=task.id) == OUTCOME_REPLAYED
    assert task.status == "SUCCEEDED"
    assert gen_queue.polls == []
    assert ledger_types(gen_uow) == ["RESERVE", "CONSUME"]


def test_webhook_event_reconciles_through_the_same_path(
    make_orchestrator, gen_uow, gen_storage, gen_user
) -> None:
    task = seed_queued_task(gen_uow, gen_user.id, gen_storage, status="PROCESSING")
    seed_processing_attempt(gen_uow, task)
    orchestrator = make_orchestrator(ScriptedProvider())

    event = WebhookEvent(
        provider_name="MOCK",
        provider_request_id=EXTERNAL_ID,
        result=succeeded(),
    )

    assert orchestrator.reconcile_event(event) == OUTCOME_SUCCEEDED
    assert task.status == "SUCCEEDED"
    assert ledger_types(gen_uow) == ["RESERVE", "CONSUME"]


def test_webhook_event_for_an_unlinkable_id_is_rejected(make_orchestrator, gen_storage, gen_uow, gen_user) -> None:
    seed_queued_task(gen_uow, gen_user.id, gen_storage)
    orchestrator = make_orchestrator(ScriptedProvider())
    event = WebhookEvent(
        provider_name="MOCK",
        provider_request_id="ext-never-submitted",
        result=succeeded(),
    )

    with pytest.raises(ApiError) as excinfo:
        orchestrator.reconcile_event(event)

    assert excinfo.value.code is ErrorCode.WEBHOOK_INVALID
    assert excinfo.value.status_code == 401
    # 只剩种行时那条 `RESERVE`：被拒的事件不得产生任何终局流水
    assert ledger_types(gen_uow) == ["RESERVE"]


# --------------------------------------------------------------------------- #
# R9：内部取消 `PROCESSING`
# --------------------------------------------------------------------------- #
def test_cancel_processing_with_confirmed_cancellation_releases(make_orchestrator, gen_storage, gen_uow, gen_user) -> None:
    task = seed_queued_task(gen_uow, gen_user.id, gen_storage, status="PROCESSING")
    seed_processing_attempt(gen_uow, task)
    provider = ScriptedProvider(cancel=[failed(ProviderErrorCategory.TEMPORARY)])

    assert make_orchestrator(provider).cancel_processing(task.id) == OUTCOME_FAILED
    assert provider.cancel_requests == [EXTERNAL_ID]
    assert task.status == "FAILED"
    assert task.billing_status == "RELEASED"
    assert task.failure_code == CANCEL_FAILURE_CODE
    assert ledger_types(gen_uow) == ["RESERVE", "RELEASE"]
    assert wallet_state(gen_uow, gen_user.id) == (100, 0, 100, 0)


def test_cancel_processing_with_pending_observation_keeps_reservation(
    make_orchestrator, gen_uow, gen_storage, gen_user
) -> None:
    task = seed_queued_task(gen_uow, gen_user.id, gen_storage, status="PROCESSING")
    seed_processing_attempt(gen_uow, task)
    provider = ScriptedProvider(cancel=[pending()])

    assert make_orchestrator(provider).cancel_processing(task.id) == OUTCOME_NEEDS_REVIEW
    assert task.status == "NEEDS_REVIEW"
    assert task.billing_status == "RESERVED"
    assert ledger_types(gen_uow) == ["RESERVE"]
    assert wallet_state(gen_uow, gen_user.id) == (90, POINTS_COST, 100, 0)


def test_cancel_processing_with_unconfirmed_timeout_keeps_reservation(
    make_orchestrator, gen_uow, gen_storage, gen_user
) -> None:
    task = seed_queued_task(gen_uow, gen_user.id, gen_storage, status="PROCESSING")
    seed_processing_attempt(gen_uow, task)
    provider = ScriptedProvider(
        cancel=[
            ProviderCallError(
                category=ProviderErrorCategory.TIMEOUT,
                code="mock_cancel_timeout",
                safe_message="取消请求超时，外部状态无法确认。",
                request_accepted=None,
            )
        ]
    )

    assert make_orchestrator(provider).cancel_processing(task.id) == OUTCOME_NEEDS_REVIEW
    assert task.status == "NEEDS_REVIEW"
    assert task.billing_status == "RESERVED"
    assert ledger_types(gen_uow) == ["RESERVE"]


def test_cancel_after_completion_is_billed_as_success(
    make_orchestrator, gen_uow, gen_storage, gen_user
) -> None:
    """取消接受不是取消成功的证明：结果已完成就按成功结算（第 2 节）。"""

    task = seed_queued_task(gen_uow, gen_user.id, gen_storage, status="PROCESSING")
    seed_processing_attempt(gen_uow, task)
    provider = ScriptedProvider(cancel=[succeeded()])

    assert make_orchestrator(provider).cancel_processing(task.id) == OUTCOME_SUCCEEDED
    assert task.status == "SUCCEEDED"
    assert task.billing_status == "CONSUMED"
    assert ledger_types(gen_uow) == ["RESERVE", "CONSUME"]


def test_cancel_asks_the_provider_outside_any_transaction(
    gen_uow, gen_user, gen_storage, gen_queue, clock
) -> None:
    task = seed_queued_task(gen_uow, gen_user.id, gen_storage, status="PROCESSING")
    seed_processing_attempt(gen_uow, task)
    orchestrator = GenerationOrchestrator(
        uow_factory=lambda: gen_uow,
        points_factory=lambda uow: PointsService(uow=uow, clock=clock),
        provider_factory=lambda: ProbingProvider(
            ScriptedProvider(cancel=[failed()]), gen_uow.probe
        ),
        storage=gen_storage,
        clock=clock,
        queue=gen_queue,
    )

    assert orchestrator.cancel_processing(task.id) == OUTCOME_FAILED
    events = gen_uow.probe.events
    call_index = events.index("provider:cancel")
    assert events[call_index - 1] == "tx:end"


# --------------------------------------------------------------------------- #
# 恢复与超时扫描
# --------------------------------------------------------------------------- #
def test_recovery_scan_honours_the_deadline_it_is_given(
    make_orchestrator, gen_storage, gen_uow, gen_user, clock
) -> None:
    """Beat 传入的时限必须真正生效：装配默认值不能把它静默吞掉。"""

    task = seed_queued_task(gen_uow, gen_user.id, gen_storage, status="PROCESSING")
    task.created_at = clock() - timedelta(seconds=600)
    task.processing_at = clock() - timedelta(seconds=120)
    seed_processing_attempt(gen_uow, task)
    orchestrator = make_orchestrator(ScriptedProvider())

    assert orchestrator.run_recovery_scan(
        stale_after_seconds=120, deadline_seconds=240, limit=10
    ) == {"reenqueued": 0, "needs_review": 0}
    assert task.status == "PROCESSING"

    assert orchestrator.run_recovery_scan(
        stale_after_seconds=120, deadline_seconds=60, limit=10
    ) == {"reenqueued": 0, "needs_review": 1}
    assert task.status == "NEEDS_REVIEW"


def test_resume_stale_only_re_enqueues_unresolved_statuses(
    make_orchestrator, gen_uow, gen_storage, gen_user, gen_queue, clock
) -> None:
    stale_queued = seed_queued_task(gen_uow, gen_user.id, gen_storage)
    stale_queued.created_at = clock() - timedelta(seconds=600)
    fresh = seed_queued_task(gen_uow, gen_user.id, gen_storage)
    fresh.created_at = clock() - timedelta(seconds=5)
    terminal = seed_queued_task(
        gen_uow, gen_user.id, gen_storage, status="SUCCEEDED"
    )
    terminal.created_at = clock() - timedelta(seconds=600)
    orchestrator = make_orchestrator(ScriptedProvider())

    assert orchestrator.resume_stale(older_than_seconds=120, limit=10) == 1
    assert [item.generation_id for item in gen_queue.generations] == [stale_queued.id]
    assert terminal.status == "SUCCEEDED"
    assert fresh.status == "QUEUED"


def test_enforce_deadlines_turns_overdue_processing_into_review(
    make_orchestrator, gen_uow, gen_storage, gen_user, clock
) -> None:
    task = seed_queued_task(gen_uow, gen_user.id, gen_storage, status="PROCESSING")
    task.processing_at = clock() - timedelta(seconds=300)
    seed_processing_attempt(gen_uow, task)
    orchestrator = make_orchestrator(ScriptedProvider())

    assert orchestrator.enforce_deadlines(limit=10) == 1
    assert task.status == "NEEDS_REVIEW"
    assert task.billing_status == "RESERVED"
    assert ledger_types(gen_uow) == ["RESERVE"]
    assert attempts(gen_uow, task.id)[0].status == "TIMEOUT"


def test_recovery_scan_reports_both_counters(
    make_orchestrator, gen_storage, gen_uow, gen_user, gen_queue, clock
) -> None:
    stale = seed_queued_task(gen_uow, gen_user.id, gen_storage)
    stale.created_at = clock() - timedelta(seconds=600)
    overdue = seed_queued_task(
        gen_uow, gen_user.id, gen_storage, status="PROCESSING"
    )
    overdue.created_at = clock() - timedelta(seconds=600)
    overdue.processing_at = clock() - timedelta(seconds=300)
    seed_processing_attempt(gen_uow, overdue)
    orchestrator = make_orchestrator(ScriptedProvider())

    assert orchestrator.run_recovery_scan(
        stale_after_seconds=120, deadline_seconds=240, limit=10
    ) == {"reenqueued": 1, "needs_review": 1}
    assert [item.generation_id for item in gen_queue.generations] == [stale.id]
    assert stale.status == "QUEUED"
    assert overdue.status == "NEEDS_REVIEW"


# --------------------------------------------------------------------------- #
# 装配与不变量守护
# --------------------------------------------------------------------------- #
def test_provider_factory_only_accepts_the_mock_provider() -> None:
    assert isinstance(build_ai_provider(ai_provider="mock"), object)
    for name in ("openai", "dashscope", ""):
        with pytest.raises(RuntimeError):
            build_ai_provider(ai_provider=name)


def test_orchestrator_never_writes_a_status_it_has_not_validated(
    make_orchestrator, gen_uow, gen_storage, gen_user
) -> None:
    """终局必须经过状态机裁决；非法转换（如已 `NEEDS_REVIEW` 再转 `SUCCEEDED`）被拒绝。"""

    with pytest.raises(RuntimeError):
        assert_transition("NEEDS_REVIEW", "SUCCEEDED")

    task = seed_queued_task(gen_uow, gen_user.id, gen_storage)
    orchestrator = make_orchestrator(ScriptedProvider(generate=[succeeded()]))
    assert orchestrator.submit(task.id) == OUTCOME_SUCCEEDED
    assert to_api_generation(task).status.value == "SUCCEEDED"


# --------------------------------------------------------------------------- #
# 修复批次 1/2：重投不得重复提交；迟到结果不得越过当前开放 attempt
# --------------------------------------------------------------------------- #
def test_redelivered_message_for_a_processing_task_never_calls_generate_again(
    make_orchestrator, gen_uow, gen_storage, gen_user, gen_queue
) -> None:
    """acks_late 下 Worker 崩溃会让同一条 `generation.run` 重投。

    重投落到已有开放 attempt 的 `PROCESSING` 任务上时**绝不能再调 `generate`**：
    那会在供应商侧产生第二个外部请求，并可能产生第二份结果与第二次消费。
    唯一正确的续跑方式是按权威源轮询当前 attempt。
    """

    task = seed_queued_task(gen_uow, gen_user.id, gen_storage)
    provider = ScriptedProvider(generate=[pending()], check_status=[succeeded()])
    first_worker = make_orchestrator(provider)

    assert first_worker.submit(task.id) == OUTCOME_PENDING
    assert provider.generate_calls == 1

    # 另一个 Worker 实例收到同一条重投消息
    second_worker = make_orchestrator(provider)
    assert second_worker.submit(task.id) == OUTCOME_SUCCEEDED

    assert provider.generate_calls == 1, "重投不得再次提交外部请求"
    assert provider.status_requests == [EXTERNAL_ID]
    assert attempts(gen_uow, task.id).__len__() == 1
    assert task.status == "SUCCEEDED"
    assert ledger_types(gen_uow) == ["RESERVE", "CONSUME"]


def test_resume_of_an_attempt_without_external_id_converges_to_needs_review(
    make_orchestrator, gen_uow, gen_storage, gen_user
) -> None:
    """崩溃在「已登记 attempt、未知是否被接受」的窗口里：既不重试也不放款。

    `STARTED` 且没有外部 ID 时无法证明请求未被接受，因此按 `UNKNOWN` 收敛到
    `NEEDS_REVIEW + RESERVED`，交人工判定（`04-ai-provider.md` 第 2 节）。
    """

    task = seed_queued_task(gen_uow, gen_user.id, gen_storage, status="PROCESSING")
    seed_attempt(gen_uow, task, 1, "STARTED", None)
    provider = ScriptedProvider(generate=[succeeded()], check_status=[succeeded()])

    assert make_orchestrator(provider).submit(task.id) == OUTCOME_NEEDS_REVIEW
    assert provider.generate_calls == 0
    assert provider.status_calls == 0
    assert task.status == "NEEDS_REVIEW"
    assert task.billing_status == "RESERVED"
    assert ledger_types(gen_uow) == ["RESERVE"]
    assert attempts(gen_uow, task.id)[0].status == "UNKNOWN"


def test_two_workers_cannot_open_two_external_requests(
    make_orchestrator, gen_uow, gen_storage, gen_user
) -> None:
    """并发入口：第二个领取者只能观察，不能重复提交（状态机只有一个写者）。"""

    task = seed_queued_task(gen_uow, gen_user.id, gen_storage)
    provider = ScriptedProvider(generate=[pending()], check_status=[pending()])
    worker_a = make_orchestrator(provider)
    worker_b = make_orchestrator(provider)

    assert worker_a.submit(task.id) == OUTCOME_PENDING
    assert worker_b.submit(task.id) == OUTCOME_PENDING

    assert provider.generate_calls == 1
    assert len(attempts(gen_uow, task.id)) == 1
    assert gen_uow.point_wallets.get_by_user_id(gen_user.id).reserved_points == POINTS_COST


def test_late_result_from_a_finished_attempt_does_not_settle_the_current_attempt(
    make_orchestrator, gen_uow, gen_storage, gen_user
) -> None:
    """旧 attempt 的迟到回调必须被忽略：当前 attempt 才是唯一的结算依据。"""

    task = seed_queued_task(gen_uow, gen_user.id, gen_storage, status="PROCESSING")
    seed_attempt(gen_uow, task, 1, "TIMEOUT", "ext-stale")
    current = seed_attempt(gen_uow, task, 2, "SUBMITTED", EXTERNAL_ID)
    orchestrator = make_orchestrator(ScriptedProvider())

    event = WebhookEvent(
        provider_name="MOCK", provider_request_id="ext-stale", result=succeeded()
    )
    assert orchestrator.reconcile_event(event) == OUTCOME_REPLAYED

    assert task.status == "PROCESSING"
    assert task.billing_status == "RESERVED"
    assert ledger_types(gen_uow) == ["RESERVE"]
    assert gen_uow.generation_tasks.list_outputs(task.id) == ()
    assert current.status == "SUBMITTED"


def test_polling_a_stale_external_id_does_not_resolve_the_open_attempt(
    make_orchestrator, gen_uow, gen_storage, gen_user
) -> None:
    """轮询同样只认当前开放 attempt 的外部 ID。"""

    task = seed_queued_task(gen_uow, gen_user.id, gen_storage, status="PROCESSING")
    seed_attempt(gen_uow, task, 1, "TIMEOUT", "ext-stale")
    seed_attempt(gen_uow, task, 2, "SUBMITTED", EXTERNAL_ID)
    provider = ScriptedProvider(check_status=[succeeded()])

    assert (
        make_orchestrator(provider).reconcile(
            generation_id=task.id, provider_request_id="ext-stale"
        )
        == OUTCOME_NOT_CLAIMABLE
    )
    assert provider.status_calls == 0
    assert task.status == "PROCESSING"
    assert ledger_types(gen_uow) == ["RESERVE"]


def test_late_pending_from_a_finished_attempt_does_not_rewrite_the_open_one(
    make_orchestrator, gen_uow, gen_storage, gen_user, gen_queue
) -> None:
    """迟到的 `PENDING` 也不得改写 attempt 或改投轮询消息。"""

    task = seed_queued_task(gen_uow, gen_user.id, gen_storage, status="PROCESSING")
    seed_attempt(gen_uow, task, 1, "TIMEOUT", "ext-stale")
    current = seed_attempt(gen_uow, task, 2, "SUBMITTED", EXTERNAL_ID)
    orchestrator = make_orchestrator(ScriptedProvider())
    before = len(gen_queue.polls)

    event = WebhookEvent(
        provider_name="MOCK",
        provider_request_id="ext-stale",
        result=pending(),
    )
    assert orchestrator.reconcile_event(event) == OUTCOME_REPLAYED
    assert current.request_id == EXTERNAL_ID
    assert current.status == "SUBMITTED"
    assert len(gen_queue.polls) == before


# --------------------------------------------------------------------------- #
# ENG-07：真实 Quanzil 适配器汇入既有编排（httpx.MockTransport，零网络）
# --------------------------------------------------------------------------- #
def quanzil_adapter(handler) -> object:
    import base64 as _b64

    from httpx import MockTransport

    from app.providers.quanzil import QUANZIL_MODEL, QuanzilImageProvider

    return QuanzilImageProvider(
        base_url="https://quanzil.test/v1",
        api_key="synthetic-key-never-log",
        model=QUANZIL_MODEL,
        transport=MockTransport(handler),
    )


def quanzil_success_body(count: int = 2) -> dict:
    import base64 as _b64

    blob = _b64.b64encode(synthetic_png_bytes()).decode()
    return {
        "created": 1790591007,
        "data": [{"b64_json": blob} for _ in range(count)],
        "usage": {"input_tokens": 2138, "output_tokens": 458, "total_tokens": 2596},
        "quota": 2000,
    }


def test_quanzil_success_settles_once_without_any_poll_or_external_id(
    make_orchestrator, gen_uow, gen_storage, gen_user, gen_queue
) -> None:
    """同步供应商的闭环：一次调用即终态，既不轮询也不留可查询句柄。

    `provider_attempts.request_id` 保持 `NULL` 是刻意的——那里只能放真实外部请求 ID；
    中转层追踪头写进去会让轮询/回调以为手里有个查得动的句柄。
    """

    from httpx import Response

    calls: list[str] = []

    def _handler(request):
        calls.append(str(request.url))
        return Response(200, json=quanzil_success_body(count=OUTPUT_COUNT))

    task = seed_queued_task(gen_uow, gen_user.id, gen_storage)
    orchestrator = make_orchestrator(quanzil_adapter(_handler))

    assert orchestrator.submit(task.id) == OUTCOME_SUCCEEDED
    assert task.status == "SUCCEEDED"
    assert task.billing_status == "CONSUMED"
    assert ledger_types(gen_uow) == ["RESERVE", "CONSUME"]
    assert len(gen_uow.generation_tasks.list_outputs(task.id)) == OUTPUT_COUNT

    attempt = attempts(gen_uow, task.id)[0]
    assert attempt.request_id is None
    assert attempt.status == "SUCCEEDED"
    assert attempt.input_tokens == 2138 and attempt.output_tokens == 458
    # 成本换算率未标定：保持 NULL，绝不伪造 0
    assert attempt.estimated_cost is None and attempt.currency is None
    # 只有那一次出图调用；没有轮询消息
    assert len(calls) == 1 and calls[0].endswith("/images/edits")
    assert gen_queue.polls == []


def test_quanzil_read_timeout_goes_to_review_without_retry(
    make_orchestrator, gen_uow, gen_storage, gen_user, gen_queue
) -> None:
    """读超时=已送达但结果不明：一次都不重试，点数保持预留等人工判定。"""

    import httpx

    calls: list[int] = []

    def _handler(request):
        calls.append(1)
        raise httpx.ReadTimeout("synthetic read timeout", request=request)

    task = seed_queued_task(gen_uow, gen_user.id, gen_storage)
    orchestrator = make_orchestrator(quanzil_adapter(_handler))

    assert orchestrator.submit(task.id) == OUTCOME_NEEDS_REVIEW
    assert task.status == "NEEDS_REVIEW"
    assert task.billing_status == "RESERVED"
    assert ledger_types(gen_uow) == ["RESERVE"]
    assert attempts(gen_uow, task.id)[0].status == "UNKNOWN"
    assert len(calls) == 1, "同步供应商超时不得自动重试"
    assert gen_queue.generations == []


def test_quanzil_incomplete_output_goes_to_review_after_one_call(
    make_orchestrator, gen_uow, gen_storage, gen_user, gen_queue
) -> None:
    """只回 1 张（要 2 张）：转人工复核并保持预留，且绝不为此再发一次请求。"""

    from httpx import Response

    calls: list[int] = []

    def _handler(_request):
        calls.append(1)
        return Response(200, json=quanzil_success_body(count=1))

    task = seed_queued_task(gen_uow, gen_user.id, gen_storage)
    orchestrator = make_orchestrator(quanzil_adapter(_handler))

    assert orchestrator.submit(task.id) == OUTCOME_NEEDS_REVIEW
    assert task.status == "NEEDS_REVIEW"
    assert task.billing_status == "RESERVED"
    assert ledger_types(gen_uow) == ["RESERVE"]
    assert len(gen_uow.generation_tasks.list_outputs(task.id)) == 1
    assert len(calls) == 1


def test_quanzil_model_not_found_fails_and_releases_without_retry(
    make_orchestrator, gen_uow, gen_storage, gen_user, gen_queue
) -> None:
    """编排层断言：`404 model_not_found` 走明确失败放款一次，不重投也不轮询。

    证据不足以区分「渠道临时全断」与「型号配错」，所以自动重试在这里是拿真金赌；
    用户点数必须一次归还，任务留下可排查的失败码。
    """

    from httpx import Response

    calls: list[int] = []

    def _handler(_request):
        calls.append(1)
        return Response(
            404,
            json={
                "error": {
                    "message": "No available channel for this model on the current token"
                    " (request id: 202609281853079486489804NpXFlOW)",
                    "type": "new_api_error",
                    "code": "model_not_found",
                }
            },
        )

    task = seed_queued_task(gen_uow, gen_user.id, gen_storage)
    orchestrator = make_orchestrator(quanzil_adapter(_handler))

    assert orchestrator.submit(task.id) == OUTCOME_FAILED
    assert task.status == "FAILED"
    assert task.billing_status == "RELEASED"
    assert task.failure_code == "VALIDATION"
    assert ledger_types(gen_uow) == ["RESERVE", "RELEASE"]
    assert (wallet_state(gen_uow, gen_user.id)[0]) == 100
    assert attempts(gen_uow, task.id)[0].error_code == "quanzil_model_not_found"
    assert len(calls) == 1, "不得自动重试"
    assert gen_queue.generations == [] and gen_queue.polls == []


# --------------------------------------------------------------------------- #
# 结果落库：格式提示与孤儿对象（ENG-07 跨卡修复第 2 项）
# --------------------------------------------------------------------------- #
def result_output(index: int, *, fmt: str = "PNG") -> ProviderOutput:
    mime = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}[fmt]
    return ProviderOutput(
        content=synthetic_image_bytes(fmt=fmt), mime_type=mime, provider_output_id=f"out-{index}"
    )


def persisted(gen_uow, gen_storage, task, outputs, clock) -> int:
    with gen_uow:
        written = persist_provider_outputs(
            gen_uow,
            storage=gen_storage,
            storage_provider="MINIO",
            task=task,
            outputs=outputs,
            now=clock,
        )
        gen_uow.commit()
    return written


def test_jpeg_and_webp_results_persist_without_a_fabricated_extension(
    gen_uow, gen_storage, gen_user, clock
) -> None:
    """结果图没有客户端文件名，编造的 `.png` 提示会把 JPEG/WebP 判成自相矛盾而拒收。"""

    task = seed_queued_task(gen_uow, gen_user.id, gen_storage)
    outputs = (result_output(0, fmt="JPEG"), result_output(1, fmt="WEBP"))

    assert persisted(gen_uow, gen_storage, task, outputs, clock) == 2

    rows = gen_uow.generation_tasks.list_outputs(task.id)
    assert [row.output_index for row in rows] == [0, 1]
    assets = {row.id: row for row in gen_uow.media.rows}
    assert sorted(assets[row.asset_id].mime_type for row in rows) == ["image/jpeg", "image/webp"]
    assert len(gen_storage.keys()) == 3  # 输入图 + 两张结果图


def test_every_result_is_validated_before_the_first_upload(
    gen_uow, gen_storage, gen_user, clock
) -> None:
    """第二张不合格时**一个对象都不许上传**。

    边校验边传会让第一张的字节留在桶里：事务回滚后没有任何 `media_assets` 行指向它，
    而生命周期清理是按 `media_assets.expires_at` 驱动的，这个孤儿永远无人回收。
    """

    task = seed_queued_task(gen_uow, gen_user.id, gen_storage)
    corrupt = ProviderOutput(
        content=b"\x89PNG\r\n\x1a\n" + b"junk" * 6,
        mime_type="image/png",
        provider_output_id="out-1",
    )
    seeded_keys = set(gen_storage.keys())

    with pytest.raises(ApiError) as raised:
        with gen_uow:
            persist_provider_outputs(
                gen_uow,
                storage=gen_storage,
                storage_provider="MINIO",
                task=task,
                outputs=(result_output(0), corrupt),
                now=clock,
            )

    assert raised.value.details == {"reason": "decode_failed"}
    assert set(gen_storage.keys()) == seeded_keys, "一张结果图都不许落地"
    assert gen_storage.delete_calls == [], "还没有任何对象落地，不该走到补偿删除"
    assert gen_uow.generation_tasks.list_outputs(task.id) == ()


class FlakyStorage(InMemoryStorageAdapter):
    """内存存储替身：从第 `fail_from` 次上传开始报存储不可用。"""

    def __init__(self, *, clock, fail_from: int) -> None:
        super().__init__(clock=clock)
        self._fail_from = fail_from
        self.upload_calls = 0

    def upload(self, key: str, content: bytes, content_type: str) -> StoredObject:
        self.upload_calls += 1
        if self.upload_calls >= self._fail_from:
            raise StorageUnavailable(f"内存对象存储在第 {self.upload_calls} 次上传时故障（测试开关）。")
        return super().upload(key, content, content_type)


def test_an_upload_failure_compensates_objects_already_uploaded_this_call(
    gen_uow, gen_user, clock
) -> None:
    """上传中途故障：本次已落地的对象必须就地补偿删除，原始故障照常上抛。"""

    storage = FlakyStorage(clock=clock, fail_from=2)
    # 输入资源不带对象：让 upload_calls 只数结果图，故障点落在第二张上
    task = seed_queued_task(gen_uow, gen_user.id, storage, readable_input=False)

    with pytest.raises(StorageUnavailable):
        with gen_uow:
            persist_provider_outputs(
                gen_uow,
                storage=storage,
                storage_provider="MINIO",
                task=task,
                outputs=(result_output(0), result_output(1)),
                now=clock,
            )

    assert storage.upload_calls == 2
    assert len(storage.delete_calls) == 1, "第一张的孤儿对象要被补偿删除"
    assert storage.keys() == ()
    assert gen_uow.generation_tasks.list_outputs(task.id) == ()
