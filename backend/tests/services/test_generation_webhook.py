"""ENG-06 第 3 步：Webhook 回调入口（验签 → 防重放 → 关联 → 对账）测试。

依据 `04-ai-provider.md` §4「Provider 状态来源与回调协议」与 `LEAD-04` §7：

- 先对**原始请求字节**验签（恒定时间比较），验签通过前绝不解析载荷；
- 时间戳必须落在接受的窗口内；event ID 以「窗口 + 时钟偏移」以上的 TTL 原子占用，
  已消费过的重复事件按 no-op 成功处理，不再对账；
- 只按 `provider_name + provider_request_id` 关联已存在的 attempt，
  回调里带的用户/任务标识一律不作为授权依据；无法关联即 `WEBHOOK_INVALID`（401）
  并做安全日志（不含载荷）；
- 只有真正不可用的重放存储才映射为既有 `PROVIDER_UNAVAILABLE`（503）；
- 日志不得出现签名头、密钥、完整载荷、图片 URL 或 Prompt 原文。

`WebhookAuthenticator` 与 `ReplayGuard` 是端口：这里既用脚本化替身断言语义，
也用第 5 步已落地的真实实现（`MockHmacWebhookAuthenticator` + `RedisReplayGuard`）
断言接线可用。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.db.models.generation import GenerationTask
from app.errors.codes import ErrorCode
from app.errors.exceptions import ApiError
from app.providers.ports import (
    ProviderOutput,
    ProviderResult,
    ProviderState,
)
from app.services.generation.ports import (
    CLAIM_ACQUIRED,
    CLAIM_IN_FLIGHT,
    CLAIM_RECONCILED,
)
from app.services.generation.webhook import (
    WebhookEvent,
    WebhookIngress,
    constant_time_equals,
)
from app.services.points.service import PointsService
from tests.services.fakes import (
    FakeRedis,
    GenerationUnitOfWork,
    InMemoryReplayGuard,
    ScriptedProvider,
    SetNxRedisClient,
    make_generation_cost_config,
    make_media_asset_row,
    synthetic_png_bytes,
)

PROVIDER_NAME = "MOCK"
EXTERNAL_ID = "ext-hook-1"
SECRET = "synthetic-webhook-secret-never-log"
PROMPT_TEXT = "回调里绝不进入日志的提示词"
IMAGE_URL = "https://provider.invalid/signed-image?token=opaque"
EVENT_ID = "evt-0001"


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def terminal_result() -> ProviderResult:
    payload = synthetic_png_bytes()
    return ProviderResult(
        state=ProviderState.SUCCEEDED,
        client_request_id="cr-1",
        provider_request_id=EXTERNAL_ID,
        outputs=tuple(
            ProviderOutput(content=payload, mime_type="image/png", provider_output_id=f"o-{i}")
            for i in range(2)
        ),
    )


def pending_result() -> ProviderResult:
    return ProviderResult(
        state=ProviderState.PENDING,
        client_request_id="cr-1",
        provider_request_id=EXTERNAL_ID,
    )


class ScriptedAuthenticator:
    """`WebhookAuthenticator` 替身：恒定时间验签在前，解析在后，并记录顺序。"""

    def __init__(
        self,
        *,
        secret: str = SECRET,
        event: ProviderResult | None = None,
        provider_name: str = PROVIDER_NAME,
        external_id: str = EXTERNAL_ID,
    ) -> None:
        self._secret = secret.encode("utf-8")
        self._result = event if event is not None else terminal_result()
        self._provider_name = provider_name
        self._external_id = external_id
        self.order: list[str] = []
        self.calls: list[dict[str, object]] = []

    def signature_for(self, raw_body: bytes) -> str:
        return hmac.new(self._secret, raw_body, hashlib.sha256).hexdigest()

    def authenticate(
        self,
        *,
        provider_name: str,
        raw_body: bytes,
        signature: str,
        event_id_header: str,
        timestamp_header: str,
    ) -> WebhookEvent:
        self.calls.append(
            {
                "provider_name": provider_name,
                "signature": signature,
                "event_id": event_id_header,
                "timestamp": timestamp_header,
            }
        )
        # 1) 先验签：恒定时间比较，不解析载荷
        self.order.append("verify")
        expected = hmac.new(self._secret, raw_body, hashlib.sha256).hexdigest()
        if not constant_time_equals(expected, signature):
            raise ApiError(ErrorCode.WEBHOOK_INVALID)
        # 2) 验签通过后才解析
        self.order.append("parse")
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(ErrorCode.WEBHOOK_INVALID) from exc
        if not isinstance(payload, dict):
            raise ApiError(ErrorCode.WEBHOOK_INVALID)

        return WebhookEvent(
            provider_name=self._provider_name,
            provider_request_id=self._external_id,
            result=self._result,
        )


def body(**overrides: object) -> bytes:
    payload: dict[str, object] = {
        "event_id": EVENT_ID,
        "timestamp": "2026-09-28T00:00:00+00:00",
        "provider_request_id": EXTERNAL_ID,
        "prompt": PROMPT_TEXT,
        "image_url": IMAGE_URL,
        "user_id": str(uuid.uuid4()),
        "generation_id": str(uuid.uuid4()),
    }
    payload.update(overrides)
    return json.dumps(payload).encode("utf-8")


def processing_with_attempt(
    uow: GenerationUnitOfWork, storage, queue, user_id: uuid.UUID, clock
) -> GenerationTask:
    """真实链路造出 `PROCESSING + SUBMITTED(ext-hook-1)` 任务（含预留与原图）。"""

    from app.services.generation.orchestrator import GenerationOrchestrator
    from app.services.generation.service import GenerationService
    from app.services.generation.commands import NewGenerationCommand

    uow.generation_costs.seed(make_generation_cost_config(mode="PHOTO_PROMPT", points_cost=10))
    content = synthetic_png_bytes()
    asset = make_media_asset_row(
        user_id=user_id,
        purpose="SOURCE",
        asset_type="ORIGINAL",
        status="READY",
        storage_key=f"assets/{user_id}/{uuid.uuid4().hex}",
        size_bytes=len(content),
        width=4,
        height=4,
        sha256=hashlib.sha256(content).hexdigest(),
        idempotency_key=None,
    )
    storage.upload(asset.storage_key, content, "image/png")
    uow.media.register(asset)

    service = GenerationService(
        uow=uow,
        points=PointsService(uow=uow, clock=clock),
        clock=clock,
        queue=queue,
    )
    with uow:
        task = service.create_generation(
            user_id=user_id,
            command=NewGenerationCommand(
                mode="PHOTO_PROMPT",
                prompt=PROMPT_TEXT,
                output_count=2,
                input_asset_ids=(asset.id,),
            ),
            idempotency_key="idem-hook-1",
        ).task

    orchestrator = GenerationOrchestrator(
        uow_factory=lambda: uow,
        points_factory=lambda inner: PointsService(uow=inner, clock=clock),
        provider_factory=lambda: ScriptedProvider(
            generate=[
                ProviderResult(
                    state=ProviderState.PENDING,
                    client_request_id="cr-1",
                    provider_request_id=EXTERNAL_ID,
                )
            ]
        ),
        storage=storage,
        clock=clock,
        queue=queue,
    )
    assert orchestrator.submit(task.id) == "PENDING"
    return task


@pytest.fixture
def guard() -> InMemoryReplayGuard:
    return InMemoryReplayGuard()


@pytest.fixture
def authenticator() -> ScriptedAuthenticator:
    return ScriptedAuthenticator()


@pytest.fixture
def hooked(gen_uow, gen_storage, gen_queue, gen_user, clock) -> GenerationTask:
    return processing_with_attempt(gen_uow, gen_storage, gen_queue, gen_user.id, clock)


def ingress_for(
    gen_uow,
    gen_storage,
    gen_queue,
    clock,
    authenticator,
    guard,
    *,
    acceptance_window_seconds: int = 300,
    replay_ttl_seconds: int = 300,
):
    from app.services.generation.orchestrator import GenerationOrchestrator

    orchestrator = GenerationOrchestrator(
        uow_factory=lambda: gen_uow,
        points_factory=lambda inner: PointsService(uow=inner, clock=clock),
        provider_factory=lambda: ScriptedProvider(),
        storage=gen_storage,
        clock=clock,
        queue=gen_queue,
    )
    return WebhookIngress(
        authenticator=authenticator,
        replay_guard=guard,
        orchestrator=orchestrator,
        acceptance_window_seconds=acceptance_window_seconds,
        replay_ttl_seconds=replay_ttl_seconds,
    )


def ledger_types(uow: GenerationUnitOfWork) -> list[str]:
    return [entry.entry_type for entry in uow.point_ledger.entries]


def now_iso(clock) -> str:
    return iso(clock())


# --------------------------------------------------------------------------- #
# 正常路径
# --------------------------------------------------------------------------- #
def test_valid_first_delivery_reconciles_and_settles_once(
    gen_uow, gen_storage, gen_queue, gen_user, clock, authenticator, guard, hooked
) -> None:
    ingress = ingress_for(gen_uow, gen_storage, gen_queue, clock, authenticator, guard)
    raw = body(event_id=EVENT_ID, timestamp=now_iso(clock))
    polls_before = len(gen_queue.polls)

    outcome = ingress.ingest(
        provider_name=PROVIDER_NAME,
        raw_body=raw,
        signature=authenticator.signature_for(raw),
        event_id=EVENT_ID,
        timestamp=now_iso(clock),
        idempotency_key=f"webhook:{PROVIDER_NAME}:{EVENT_ID}",
    )

    assert outcome == "SUCCEEDED"
    assert hooked.status == "SUCCEEDED"
    assert hooked.billing_status == "CONSUMED"
    assert ledger_types(gen_uow) == ["RESERVE", "CONSUME"]
    assert len(gen_uow.generation_tasks.list_outputs(hooked.id)) == 2
    assert authenticator.order == ["verify", "parse"]

    (key,) = guard.claims
    assert key.endswith(EVENT_ID)
    # 终态达成后不再安排新的轮询投递
    assert len(gen_queue.polls) == polls_before


def test_replay_guard_ttl_covers_window_plus_skew(
    gen_uow, gen_storage, gen_queue, gen_user, clock, authenticator, guard, hooked
) -> None:
    from app.services.generation.webhook import CLOCK_SKEW_ALLOWANCE_SECONDS

    ingress = ingress_for(
        gen_uow,
        gen_storage,
        gen_queue,
        clock,
        authenticator,
        guard,
        acceptance_window_seconds=300,
        replay_ttl_seconds=300,
    )
    raw = body(timestamp=now_iso(clock))

    ingress.ingest(
        provider_name=PROVIDER_NAME,
        raw_body=raw,
        signature=authenticator.signature_for(raw),
        event_id=EVENT_ID,
        timestamp=now_iso(clock),
        idempotency_key="k",
    )

    ttl = next(iter(guard.claims.values()))
    assert ttl >= 300 + CLOCK_SKEW_ALLOWANCE_SECONDS


def test_duplicate_event_id_is_noop_success_and_does_not_reconcile_again(
    gen_uow, gen_storage, gen_queue, gen_user, clock, authenticator, guard, hooked
) -> None:
    ingress = ingress_for(gen_uow, gen_storage, gen_queue, clock, authenticator, guard)
    raw = body(timestamp=now_iso(clock))
    signature = authenticator.signature_for(raw)
    timestamp = now_iso(clock)

    first = ingress.ingest(
        provider_name=PROVIDER_NAME,
        raw_body=raw,
        signature=signature,
        event_id=EVENT_ID,
        timestamp=timestamp,
        idempotency_key="k",
    )
    commits_after_first = gen_uow.commits
    second = ingress.ingest(
        provider_name=PROVIDER_NAME,
        raw_body=raw,
        signature=signature,
        event_id=EVENT_ID,
        timestamp=timestamp,
        idempotency_key="k",
    )

    assert first == "SUCCEEDED"
    assert second == "REPLAYED"
    assert gen_uow.commits == commits_after_first  # 重放完全不进对账
    assert ledger_types(gen_uow) == ["RESERVE", "CONSUME"]
    assert len(gen_uow.generation_tasks.list_outputs(hooked.id)) == 2
    assert hooked.status == "SUCCEEDED"


def test_late_pending_after_terminal_is_no_op_success(
    gen_uow, gen_storage, gen_queue, gen_user, clock, guard, hooked
) -> None:
    """终态单调：迟到的 `PENDING` 既不重开任务也不重开 attempt。"""

    first_auth = ScriptedAuthenticator(event=terminal_result())
    ingress = ingress_for(gen_uow, gen_storage, gen_queue, clock, first_auth, guard)
    raw = body(timestamp=now_iso(clock))
    assert (
        ingress.ingest(
            provider_name=PROVIDER_NAME,
            raw_body=raw,
            signature=first_auth.signature_for(raw),
            event_id="evt-terminal",
            timestamp=now_iso(clock),
            idempotency_key="k",
        )
        == "SUCCEEDED"
    )

    late_auth = ScriptedAuthenticator(event=pending_result())
    late_ingress = ingress_for(gen_uow, gen_storage, gen_queue, clock, late_auth, guard)
    late_raw = body(timestamp=now_iso(clock))

    assert (
        late_ingress.ingest(
            provider_name=PROVIDER_NAME,
            raw_body=late_raw,
            signature=late_auth.signature_for(late_raw),
            event_id="evt-late-pending",
            timestamp=now_iso(clock),
            idempotency_key="k",
        )
        == "REPLAYED"
    )

    assert hooked.status == "SUCCEEDED"
    assert hooked.billing_status == "CONSUMED"
    assert ledger_types(gen_uow) == ["RESERVE", "CONSUME"]


# --------------------------------------------------------------------------- #
# 拒绝路径：一律 WEBHOOK_INVALID，且不留任何状态变化
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("timestamp_offset", "label"),
    [(-4000.0, "expired"), (4000.0, "too_far_in_future")],
)
def test_timestamp_outside_acceptance_window_is_rejected(
    gen_uow,
    gen_storage,
    gen_queue,
    gen_user,
    clock,
    authenticator,
    guard,
    hooked,
    timestamp_offset: float,
    label: str,
) -> None:
    ingress = ingress_for(gen_uow, gen_storage, gen_queue, clock, authenticator, guard)
    raw = body(timestamp=iso(clock() + timedelta(seconds=timestamp_offset)))
    commits_before = gen_uow.commits

    with pytest.raises(ApiError) as excinfo:
        ingress.ingest(
            provider_name=PROVIDER_NAME,
            raw_body=raw,
            signature=authenticator.signature_for(raw),
            event_id=EVENT_ID,
            timestamp=iso(clock() + timedelta(seconds=timestamp_offset)),
            idempotency_key="k",
        )

    assert excinfo.value.code is ErrorCode.WEBHOOK_INVALID, label
    assert excinfo.value.status_code == 401
    assert guard.claims == {}
    assert gen_uow.commits == commits_before
    assert hooked.status == "PROCESSING"
    assert hooked.billing_status == "RESERVED"


def test_unparsable_timestamp_is_rejected(
    gen_uow, gen_storage, gen_queue, gen_user, clock, authenticator, guard, hooked
) -> None:
    ingress = ingress_for(gen_uow, gen_storage, gen_queue, clock, authenticator, guard)
    raw = body(timestamp="yesterday")

    with pytest.raises(ApiError) as excinfo:
        ingress.ingest(
            provider_name=PROVIDER_NAME,
            raw_body=raw,
            signature=authenticator.signature_for(raw),
            event_id=EVENT_ID,
            timestamp="yesterday",
            idempotency_key="k",
        )

    assert excinfo.value.code is ErrorCode.WEBHOOK_INVALID
    assert guard.claims == {}


@pytest.mark.parametrize(
    "overrides",
    [
        {"signature": ""},
        {"event_id": ""},
        {"timestamp": ""},
        {"idempotency_key": ""},
    ],
)
def test_missing_required_header_is_rejected(
    gen_uow,
    gen_storage,
    gen_queue,
    gen_user,
    clock,
    authenticator,
    guard,
    hooked,
    overrides: dict[str, str],
) -> None:
    ingress = ingress_for(gen_uow, gen_storage, gen_queue, clock, authenticator, guard)
    raw = body(timestamp=now_iso(clock))
    commits_before = gen_uow.commits
    params: dict[str, str] = {
        "provider_name": PROVIDER_NAME,
        "raw_body": raw,
        "signature": authenticator.signature_for(raw),
        "event_id": EVENT_ID,
        "timestamp": now_iso(clock),
        "idempotency_key": "k",
    }
    params.update(overrides)

    with pytest.raises(ApiError) as excinfo:
        ingress.ingest(**params)

    assert excinfo.value.code is ErrorCode.WEBHOOK_INVALID
    assert guard.claims == {}
    # 拒绝不产生任何新的事务提交（前面只有夹具的 3 次）
    assert gen_uow.commits == commits_before


def test_bad_signature_is_rejected_before_the_body_is_parsed(
    gen_uow, gen_storage, gen_queue, gen_user, clock, authenticator, guard, hooked
) -> None:
    ingress = ingress_for(gen_uow, gen_storage, gen_queue, clock, authenticator, guard)
    raw = body(timestamp=now_iso(clock))

    with pytest.raises(ApiError) as excinfo:
        ingress.ingest(
            provider_name=PROVIDER_NAME,
            raw_body=raw,
            signature="0" * 64,
            event_id=EVENT_ID,
            timestamp=now_iso(clock),
            idempotency_key="k",
        )

    assert excinfo.value.code is ErrorCode.WEBHOOK_INVALID
    # 验签失败时绝不解析载荷：只走到 verify 一步
    assert authenticator.order == ["verify"]
    assert guard.claims == {}
    assert hooked.status == "PROCESSING"


def test_malformed_body_with_bad_signature_is_still_unauthorized(
    gen_uow, gen_storage, gen_queue, gen_user, clock, authenticator, guard, hooked
) -> None:
    ingress = ingress_for(gen_uow, gen_storage, gen_queue, clock, authenticator, guard)
    raw = b"{not-json"

    with pytest.raises(ApiError) as excinfo:
        ingress.ingest(
            provider_name=PROVIDER_NAME,
            raw_body=raw,
            signature="0" * 64,
            event_id=EVENT_ID,
            timestamp=now_iso(clock),
            idempotency_key="k",
        )

    assert excinfo.value.code is ErrorCode.WEBHOOK_INVALID
    assert authenticator.order == ["verify"]


def test_unknown_external_request_id_is_rejected_and_security_logged(
    gen_uow, gen_storage, gen_queue, gen_user, clock, guard, hooked, caplog
) -> None:
    orphan = ScriptedAuthenticator(event=terminal_result(), external_id="never-registered")
    ingress = ingress_for(gen_uow, gen_storage, gen_queue, clock, orphan, guard)
    raw = body(timestamp=now_iso(clock))

    with caplog.at_level("WARNING"), pytest.raises(ApiError) as excinfo:
        ingress.ingest(
            provider_name=PROVIDER_NAME,
            raw_body=raw,
            signature=orphan.signature_for(raw),
            event_id=EVENT_ID,
            timestamp=now_iso(clock),
            idempotency_key="k",
        )

    assert excinfo.value.code is ErrorCode.WEBHOOK_INVALID
    assert hooked.status == "PROCESSING"
    assert hooked.billing_status == "RESERVED"
    assert ledger_types(gen_uow) == ["RESERVE"]
    assert gen_uow.generation_tasks.list_outputs(hooked.id) == ()
    assert "webhook_unlinkable" in caplog.text


def test_callback_supplied_task_identity_is_never_trusted(
    gen_uow, gen_storage, gen_queue, gen_user, clock, authenticator, guard, hooked
) -> None:
    """载荷里的 `user_id`/`generation_id` 是伪造值：关联只认外部请求 ID。"""

    ingress = ingress_for(gen_uow, gen_storage, gen_queue, clock, authenticator, guard)
    raw = body(
        timestamp=now_iso(clock),
        generation_id=str(uuid.uuid4()),
        user_id=str(uuid.uuid4()),
    )

    assert (
        ingress.ingest(
            provider_name=PROVIDER_NAME,
            raw_body=raw,
            signature=authenticator.signature_for(raw),
            event_id=EVENT_ID,
            timestamp=now_iso(clock),
            idempotency_key="k",
        )
        == "SUCCEEDED"
    )
    assert hooked.user_id == gen_user.id


def test_missing_signature_headers_are_unauthorized_without_touching_the_guard(
    gen_uow, gen_storage, gen_queue, gen_user, clock, guard, hooked
) -> None:
    auth = ScriptedAuthenticator()
    ingress = ingress_for(gen_uow, gen_storage, gen_queue, clock, auth, guard)

    with pytest.raises(ApiError) as excinfo:
        ingress.ingest(
            provider_name=PROVIDER_NAME,
            raw_body=body(timestamp=now_iso(clock)),
            signature="",
            event_id=EVENT_ID,
            timestamp=now_iso(clock),
            idempotency_key="k",
        )

    assert excinfo.value.code is ErrorCode.WEBHOOK_INVALID
    assert auth.order == []  # 连验签都没开始做
    assert guard.claims == {}


# --------------------------------------------------------------------------- #
# 依赖故障：唯一允许的 503
# --------------------------------------------------------------------------- #
def test_unavailable_replay_store_maps_to_provider_unavailable(
    gen_uow, gen_storage, gen_queue, gen_user, clock, authenticator, hooked
) -> None:
    guard = InMemoryReplayGuard(unavailable=True)
    ingress = ingress_for(gen_uow, gen_storage, gen_queue, clock, authenticator, guard)
    raw = body(timestamp=now_iso(clock))

    with pytest.raises(ApiError) as excinfo:
        ingress.ingest(
            provider_name=PROVIDER_NAME,
            raw_body=raw,
            signature=authenticator.signature_for(raw),
            event_id=EVENT_ID,
            timestamp=now_iso(clock),
            idempotency_key="k",
        )

    assert excinfo.value.code is ErrorCode.PROVIDER_UNAVAILABLE
    assert excinfo.value.status_code == 503
    assert hooked.status == "PROCESSING"
    assert hooked.billing_status == "RESERVED"
    assert ledger_types(gen_uow) == ["RESERVE"]


def test_no_error_code_outside_the_frozen_set_is_raised(
    gen_uow, gen_storage, gen_queue, gen_user, clock, authenticator, guard, hooked
) -> None:
    """所有拒绝路径都只用既有 `WEBHOOK_INVALID`/`PROVIDER_UNAVAILABLE`。"""

    ingress = ingress_for(gen_uow, gen_storage, gen_queue, clock, authenticator, guard)
    cases: list[dict[str, object]] = [
        {"signature": "bad"},
        {"event_id": ""},
        {"timestamp": "not-a-time"},
    ]
    for case in cases:
        raw = body(timestamp=now_iso(clock))
        params: dict[str, object] = {
            "provider_name": PROVIDER_NAME,
            "raw_body": raw,
            "signature": authenticator.signature_for(raw),
            "event_id": EVENT_ID,
            "timestamp": now_iso(clock),
            "idempotency_key": "k",
        }
        params.update(case)
        with pytest.raises(ApiError) as excinfo:
            ingress.ingest(**params)  # type: ignore[arg-type]
        assert excinfo.value.code in {ErrorCode.WEBHOOK_INVALID, ErrorCode.PROVIDER_UNAVAILABLE}


# --------------------------------------------------------------------------- #
# 日志脱敏（06-development-standard.md §6）
# --------------------------------------------------------------------------- #
def test_logs_never_contain_signature_secret_prompt_or_image_url(
    gen_uow, gen_storage, gen_queue, gen_user, clock, hooked, caplog
) -> None:
    auth = ScriptedAuthenticator()
    ingress = ingress_for(gen_uow, gen_storage, gen_queue, clock, auth, InMemoryReplayGuard())
    raw = body(timestamp=now_iso(clock))
    signature = auth.signature_for(raw)
    orphan = ScriptedAuthenticator(external_id="evt-c-unknown")
    orphan_ingress = ingress_for(
        gen_uow, gen_storage, gen_queue, clock, orphan, InMemoryReplayGuard()
    )

    with caplog.at_level("DEBUG"):
        # 一次成功、一次验签失败、一次无法关联
        ingress.ingest(
            provider_name=PROVIDER_NAME,
            raw_body=raw,
            signature=signature,
            event_id="evt-a",
            timestamp=now_iso(clock),
            idempotency_key="k",
        )
        with pytest.raises(ApiError):
            ingress.ingest(
                provider_name=PROVIDER_NAME,
                raw_body=raw,
                signature="0" * 64,
                event_id="evt-b",
                timestamp=now_iso(clock),
                idempotency_key="k",
            )
        with pytest.raises(ApiError):
            orphan_ingress.ingest(
                provider_name=PROVIDER_NAME,
                raw_body=raw,
                signature=orphan.signature_for(raw),
                event_id="evt-c",
                timestamp=now_iso(clock),
                idempotency_key="k",
            )

    dump = caplog.text + "".join(
        str(value) for record in caplog.records for value in record.__dict__.values()
    )
    for value in (SECRET, signature, PROMPT_TEXT, IMAGE_URL, str(hooked.id)):
        assert value not in dump
    # 载荷原文不得整段落日志
    assert raw.decode("utf-8") not in caplog.text
    assert any(record.levelname in {"WARNING", "INFO"} for record in caplog.records)


def test_webhook_invalid_details_never_echo_the_payload(
    gen_uow, gen_storage, gen_queue, gen_user, clock, authenticator, guard, hooked
) -> None:
    ingress = ingress_for(gen_uow, gen_storage, gen_queue, clock, authenticator, guard)
    raw = body(timestamp=now_iso(clock))

    with pytest.raises(ApiError) as excinfo:
        ingress.ingest(
            provider_name=PROVIDER_NAME,
            raw_body=raw,
            signature="0" * 64,
            event_id=EVENT_ID,
            timestamp=now_iso(clock),
            idempotency_key="k",
        )

    assert excinfo.value.details == {}
    assert PROMPT_TEXT not in excinfo.value.message


# --------------------------------------------------------------------------- #
# 与第 5 步真实实现的接线（签名算法与 Redis 重放存储）
# --------------------------------------------------------------------------- #
def test_real_mock_authenticator_and_redis_guard_are_compatible(
    gen_uow, gen_storage, gen_queue, gen_user, clock, hooked
) -> None:
    """第 5 步的 `MockHmacWebhookAuthenticator`/`RedisReplayGuard` 可直接注入本入口。"""

    from app.services.generation.webhook_adapters import (
        MockHmacWebhookAuthenticator,
        RedisReplayGuard,
    )
    from app.services.generation.orchestrator import GenerationOrchestrator

    payload = {
        "event_id": EVENT_ID,
        "timestamp": now_iso(clock),
        "provider_request_id": EXTERNAL_ID,
        "state": ProviderState.SUCCEEDED.value,
        "outputs": [
            {"content_hex": synthetic_png_bytes().hex(), "mime_type": "image/png"}
            for _ in range(2)
        ],
    }
    raw = json.dumps(payload).encode("utf-8")
    signature = hmac.new(SECRET.encode("utf-8"), raw, hashlib.sha256).hexdigest()

    orchestrator = GenerationOrchestrator(
        uow_factory=lambda: gen_uow,
        points_factory=lambda inner: PointsService(uow=inner, clock=clock),
        provider_factory=lambda: ScriptedProvider(),
        storage=gen_storage,
        clock=clock,
        queue=gen_queue,
    )
    ingress = WebhookIngress(
        authenticator=MockHmacWebhookAuthenticator(secret=SECRET),
        replay_guard=RedisReplayGuard(SetNxRedisClient(clock=clock)),
        orchestrator=orchestrator,
    )

    assert (
        ingress.ingest(
            provider_name=PROVIDER_NAME,
            raw_body=raw,
            signature=signature,
            event_id=EVENT_ID,
            timestamp=now_iso(clock),
            idempotency_key=f"webhook:{PROVIDER_NAME}:{EVENT_ID}",
        )
        == "SUCCEEDED"
    )
    assert hooked.status == "SUCCEEDED"
    assert hooked.billing_status == "CONSUMED"
    assert ledger_types(gen_uow) == ["RESERVE", "CONSUME"]

    # 重复投递：Redis 已占用事件 ID → no-op 成功
    assert (
        ingress.ingest(
            provider_name=PROVIDER_NAME,
            raw_body=raw,
            signature=signature,
            event_id=EVENT_ID,
            timestamp=now_iso(clock),
            idempotency_key=f"webhook:{PROVIDER_NAME}:{EVENT_ID}",
        )
        == "REPLAYED"
    )


def test_real_authenticator_rejects_tampered_body(
    gen_uow, gen_storage, gen_queue, gen_user, clock, hooked
) -> None:
    from app.services.generation.webhook import WebhookIngress as _Ingress
    from app.services.generation.webhook_adapters import (
        MockHmacWebhookAuthenticator,
        RedisReplayGuard,
    )
    from app.services.generation.orchestrator import GenerationOrchestrator

    payload = {
        "event_id": EVENT_ID,
        "timestamp": now_iso(clock),
        "provider_request_id": EXTERNAL_ID,
        "state": ProviderState.SUCCEEDED.value,
        "outputs": [],
    }
    raw = json.dumps(payload).encode("utf-8")
    signature = hmac.new(SECRET.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    tampered = json.dumps({**payload, "state": ProviderState.FAILED.value}).encode("utf-8")

    ingress = _Ingress(
        authenticator=MockHmacWebhookAuthenticator(secret=SECRET),
        replay_guard=RedisReplayGuard(SetNxRedisClient(clock=clock)),
        orchestrator=GenerationOrchestrator(
            uow_factory=lambda: gen_uow,
            points_factory=lambda inner: PointsService(uow=inner, clock=clock),
            provider_factory=lambda: ScriptedProvider(),
            storage=gen_storage,
            clock=clock,
            queue=gen_queue,
        ),
    )

    with pytest.raises(ApiError) as excinfo:
        ingress.ingest(
            provider_name=PROVIDER_NAME,
            raw_body=tampered,
            signature=signature,
            event_id=EVENT_ID,
            timestamp=now_iso(clock),
            idempotency_key="k",
        )

    assert excinfo.value.code is ErrorCode.WEBHOOK_INVALID
    assert hooked.status == "PROCESSING"
    assert ledger_types(gen_uow) == ["RESERVE"]


def test_base64_padding_helper_is_available_for_adapters() -> None:
    """`constant_time_equals` 必须对长度差异安全（不泄露早退信息）。"""

    assert constant_time_equals("a" * 64, "a" * 64) is True
    assert constant_time_equals("a" * 64, "b" + "a" * 63) is False
    assert constant_time_equals("a" * 64, "") is False
    assert constant_time_equals(base64.b64encode(b"x").decode(), base64.b64encode(b"x").decode())


def test_webhook_event_is_frozen_and_slots() -> None:
    result = terminal_result()
    event = WebhookEvent(
        provider_name=PROVIDER_NAME, provider_request_id=EXTERNAL_ID, result=result
    )

    with pytest.raises(Exception):
        event.provider_name = "OTHER"  # type: ignore[misc]

    assert not hasattr(event, "__dict__")


# --------------------------------------------------------------------------- #
# 修复批次 3：事件 ID 去重必须原子，且只有「已持久对账」的重复才可回 202
# --------------------------------------------------------------------------- #
def event_key(event_id: str = EVENT_ID) -> str:
    return f"{PROVIDER_NAME}:{event_id}"


def ingest(ingress, raw: bytes, signature: str, clock, *, event_id: str = EVENT_ID) -> str:
    return ingress.ingest(
        provider_name=PROVIDER_NAME,
        raw_body=raw,
        signature=signature,
        event_id=event_id,
        timestamp=now_iso(clock),
        idempotency_key="k",
    )


def test_in_flight_duplicate_is_rejected_with_the_existing_conflict(
    gen_uow, gen_storage, gen_queue, gen_user, clock, authenticator, guard, hooked
) -> None:
    """同一个事件 ID 仍在对账中：不得抢先把它当「重复」回 202。

    重复事件回 202 的含义是「已经处理完，不必再投」。对账尚未落地就给 202，
    供应商便不再重投，一次纯粹的并发竞态会永久变成丢失的结果。
    这里用既有 `IDEMPOTENCY_IN_PROGRESS`（409）表达「正在处理，请稍后重试」。
    """

    commits_before = gen_uow.commits
    assert guard.claim(event_key(), ttl_seconds=360) == CLAIM_ACQUIRED
    ingress = ingress_for(gen_uow, gen_storage, gen_queue, clock, authenticator, guard)
    raw = body(timestamp=now_iso(clock))

    with pytest.raises(ApiError) as raised:
        ingest(ingress, raw, authenticator.signature_for(raw), clock)

    assert raised.value.code is ErrorCode.IDEMPOTENCY_IN_PROGRESS
    assert gen_uow.commits == commits_before
    assert hooked.status == "PROCESSING"
    assert ledger_types(gen_uow) == ["RESERVE"]


def test_reconcile_failure_releases_the_event_id_for_safe_redelivery(
    gen_uow, gen_storage, gen_queue, gen_user, clock, guard
) -> None:
    """对账没落地就不算处理过：必须释放事件 ID，让供应商可以安全重投。"""

    orphan = ScriptedAuthenticator(external_id="never-seen")
    ingress = ingress_for(gen_uow, gen_storage, gen_queue, clock, orphan, guard)
    raw = body(timestamp=now_iso(clock))

    with pytest.raises(ApiError) as raised:
        ingest(ingress, raw, orphan.signature_for(raw), clock)

    assert raised.value.code is ErrorCode.WEBHOOK_INVALID
    assert guard.released == [event_key()]
    assert guard.confirmed == []
    assert guard.claims == {}


def test_released_event_id_can_be_claimed_and_reconciled_again(
    gen_uow, gen_storage, gen_queue, gen_user, clock, authenticator, guard, hooked
) -> None:
    """释放后同一条事件重投必须能正常完成对账（否则一次故障就永久丢结果）。"""

    failing = ingress_for(
        gen_uow,
        gen_storage,
        gen_queue,
        clock,
        ScriptedAuthenticator(external_id="never-seen"),
        guard,
    )
    raw = body(timestamp=now_iso(clock))
    signature = failing_authenticator_signature(authenticator, raw)
    with pytest.raises(ApiError):
        ingest(failing, raw, signature, clock)

    working = ingress_for(gen_uow, gen_storage, gen_queue, clock, authenticator, guard)
    assert ingest(working, raw, signature, clock) == "SUCCEEDED"
    assert hooked.status == "SUCCEEDED"


def failing_authenticator_signature(authenticator, raw: bytes) -> str:
    return authenticator.signature_for(raw)


def test_only_a_durable_reconciliation_marks_the_event_reconciled(
    gen_uow, gen_storage, gen_queue, gen_user, clock, authenticator, guard, hooked
) -> None:
    """「已对账」标记只能在结算事务提交之后写入：它是 202 的唯一依据。"""

    ingress = ingress_for(gen_uow, gen_storage, gen_queue, clock, authenticator, guard)
    raw = body(timestamp=now_iso(clock))
    signature = authenticator.signature_for(raw)

    assert ingest(ingress, raw, signature, clock) == "SUCCEEDED"
    assert guard.confirmed == [event_key()]
    assert guard.is_reconciled(event_key()) is True
    commits = gen_uow.commits

    assert ingest(ingress, raw, signature, clock) == "REPLAYED"
    assert gen_uow.commits == commits
    assert ledger_types(gen_uow) == ["RESERVE", "CONSUME"]


def test_pending_outcome_is_also_a_durable_conclusion_and_confirms_the_event(
    gen_uow, gen_storage, gen_queue, gen_user, clock, guard, hooked
) -> None:
    """`PENDING` 已把外部 ID 落库：这是持久结论，不是「失败可重投」。"""

    pending_authenticator = ScriptedAuthenticator(event=pending_result())
    ingress = ingress_for(
        gen_uow, gen_storage, gen_queue, clock, pending_authenticator, guard
    )
    raw = body(timestamp=now_iso(clock))

    assert ingest(ingress, raw, pending_authenticator.signature_for(raw), clock) == "PENDING"
    assert guard.confirmed == [event_key()]


# --------------------------------------------------------------------------- #
# 真实 Redis 去重实现：原子 SET NX + 归属校验
# --------------------------------------------------------------------------- #
@pytest.fixture
def dedupe_client(clock) -> SetNxRedisClient:
    return SetNxRedisClient(clock=clock)


def guard_on(client: SetNxRedisClient):
    from app.services.generation.webhook_adapters import RedisReplayGuard

    return RedisReplayGuard(client)


def test_event_id_is_occupied_with_one_atomic_set_nx(dedupe_client) -> None:
    """占用必须是单条原子命令：`GET` 之后再 `SET` 在并发下会双份放行。"""

    guard = guard_on(dedupe_client)

    assert guard.claim(event_key(), ttl_seconds=360) == CLAIM_ACQUIRED
    sets = [call for call in dedupe_client.calls if call[0] == "set"]
    assert len(sets) == 1, sets
    assert sets[0][4] is True, "去重必须使用 SET NX"
    assert sets[0][3] == 360, "TTL 必须由调用方给定（覆盖接受窗口 + 时钟偏移）"


def test_second_claim_of_the_same_event_id_reports_in_flight(dedupe_client) -> None:
    """两个 Worker 同时收到同一条事件：只有一个能进入对账。"""

    holder = guard_on(dedupe_client)
    challenger = guard_on(dedupe_client)
    key = event_key()

    assert holder.claim(key, ttl_seconds=360) == CLAIM_ACQUIRED
    assert challenger.claim(key, ttl_seconds=360) == CLAIM_IN_FLIGHT


def test_reconciled_marker_is_visible_to_another_worker(dedupe_client) -> None:
    holder = guard_on(dedupe_client)
    challenger = guard_on(dedupe_client)
    key = event_key()

    holder.claim(key, ttl_seconds=360)
    holder.mark_reconciled(key, ttl_seconds=360)

    assert challenger.claim(key, ttl_seconds=360) == CLAIM_RECONCILED


def test_release_and_confirm_never_touch_a_claim_they_do_not_own(dedupe_client) -> None:
    """竞态失败的一方不得抹掉赢家的占用，否则防重放会被并发自身摧毁。"""

    holder = guard_on(dedupe_client)
    challenger = guard_on(dedupe_client)
    key = event_key()

    assert holder.claim(key, ttl_seconds=360) == CLAIM_ACQUIRED
    assert challenger.claim(key, ttl_seconds=360) == CLAIM_IN_FLIGHT

    challenger.release(key)
    challenger.mark_reconciled(key, ttl_seconds=360)
    assert holder.claim(key, ttl_seconds=360) == CLAIM_IN_FLIGHT


def test_expired_claim_can_be_taken_again(dedupe_client, clock) -> None:
    """TTL 到期后同一事件 ID 可被重新占用（缓存丢失时数据库仍是权威）。"""

    guard = guard_on(dedupe_client)
    key = event_key()

    assert guard.claim(key, ttl_seconds=60) == CLAIM_ACQUIRED
    clock.advance(61)
    assert guard.claim(key, ttl_seconds=60) == CLAIM_ACQUIRED


def test_reconcile_failure_releases_the_redis_claim(dedupe_client) -> None:
    """释放后键真的消失：供应商重投不会被当成重复事件吞掉。"""

    guard = guard_on(dedupe_client)
    key = event_key()

    guard.claim(key, ttl_seconds=360)
    guard.release(key)

    assert guard_on(dedupe_client).claim(key, ttl_seconds=360) == CLAIM_ACQUIRED


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"state": "SUCCEED"}, ErrorCode.WEBHOOK_INVALID),
        ({"state": None}, ErrorCode.WEBHOOK_INVALID),
        ({"outputs": "not-a-list"}, ErrorCode.WEBHOOK_INVALID),
        ({"outputs": [{"mime_type": "image/png"}]}, ErrorCode.WEBHOOK_INVALID),
        ({"outputs": [None]}, ErrorCode.WEBHOOK_INVALID),
        ({"outputs": [{"content_hex": "zz"}]}, ErrorCode.WEBHOOK_INVALID),
        ({"outputs": [{"content_hex": 12}]}, ErrorCode.WEBHOOK_INVALID),
        ({"error": "not-a-dict"}, ErrorCode.WEBHOOK_INVALID),
        ({"error": {"category": "NOPE"}}, ErrorCode.WEBHOOK_INVALID),
        ({"error": {}}, ErrorCode.WEBHOOK_INVALID),
    ],
)
def test_signed_but_unnormalisable_payload_is_webhook_invalid(
    clock, dedupe_client, overrides: dict[str, object], expected: ErrorCode
) -> None:
    """已验签但无法归一成可信结果的载荷统一 401：绝不带着半成品进入对账，也不 500。"""

    from app.services.generation.webhook_adapters import MockHmacWebhookAuthenticator

    payload: dict[str, object] = {
        "event_id": EVENT_ID,
        "timestamp": now_iso(clock),
        "provider_request_id": EXTERNAL_ID,
        "state": "SUCCEEDED",
        "outputs": [{"content_hex": synthetic_png_bytes().hex(), "mime_type": "image/png"}],
    }
    payload["state"] = "FAILED" if "error" in overrides else payload["state"]
    payload.update(overrides)

    raw = json.dumps(payload).encode("utf-8")
    authenticator = MockHmacWebhookAuthenticator(secret=SECRET)

    with pytest.raises(ApiError) as raised:
        authenticator.authenticate(
            provider_name=PROVIDER_NAME,
            raw_body=raw,
            signature=hmac.new(SECRET.encode("utf-8"), raw, hashlib.sha256).hexdigest(),
            event_id=EVENT_ID,
            timestamp=now_iso(clock),
        )
    assert raised.value.code is expected
