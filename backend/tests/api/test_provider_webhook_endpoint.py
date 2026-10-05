"""Provider Webhook 端点测试（ENG-06 第 5 步）。

规则来源：`04-ai-provider.md` 第 4 节（Webhook 安全 5 条）、`03-api-contract.md`
第 4.4、4.5 节、`LEAD-05`「Webhook 安全门禁」。全部依赖以内存替身注入，
不启动 Redis、不连接数据库、不调用任何真实供应商。

端点层该管的事：只把**原始字节 + 未信任的身份头**交给认证器；任何一步失败都是
`WEBHOOK_INVALID`（401）；重复事件是 no-op 成功；本路由绝不自己改状态或结算。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_webhook_ingress
from app.api.internal.webhook_router import (
    EVENT_ID_HEADER_NAME,
    PROVIDER_SIGNATURE_HEADER_NAME,
    TIMESTAMP_HEADER_NAME,
)
from app.config import reset_settings_cache
from app.errors.codes import ErrorCode
from app.errors.exceptions import ApiError
from app.main import create_app
from app.providers.ports import ProviderResult, ProviderState
from app.services.generation.ports import WebhookEvent
from app.services.generation.webhook import OUTCOME_DUPLICATE, WebhookIngress
from app.services.generation.webhook_adapters import (
    MockHmacWebhookAuthenticator,
    RedisReplayGuard,
)
from tests.services.fakes import MutableClock, SetNxRedisClient

BASE_URL = "https://testserver"
SECRET = "synthetic-webhook-secret"
EXTERNAL_ID = "ext-9527"
EVENT_ID = "evt-0001"
WEBHOOK_PATH = "/api/v1/internal/providers/MOCK/webhook"


class RecordingOrchestrator:
    """只记录被调用与否：本文件不测结算，结算矩阵在编排器测试里覆盖。"""

    def __init__(self, *, outcome: str = "SUCCEEDED", error: ApiError | None = None) -> None:
        self.events: list[WebhookEvent] = []
        self.outcome = outcome
        self.error = error

    def reconcile_event(self, event: WebhookEvent) -> str:
        if self.error is not None:
            raise self.error
        self.events.append(event)
        return self.outcome


class Harness:
    def __init__(
        self,
        app: FastAPI,
        client: TestClient,
        orchestrator: RecordingOrchestrator,
        redis: SetNxRedisClient,
        clock: MutableClock,
    ) -> None:
        self.app = app
        self.client = client
        self.orchestrator = orchestrator
        self.redis = redis
        self.clock = clock


def _payload(clock: MutableClock, **overrides: object) -> dict:
    body: dict = {
        "event_id": EVENT_ID,
        "timestamp": clock().isoformat(),
        "provider_request_id": EXTERNAL_ID,
        "state": ProviderState.SUCCEEDED.value,
        "outputs": [
            {
                "content_hex": synthetic_bytes().hex(),
                "mime_type": "image/png",
                "provider_output_id": "ext-out-0",
            }
        ],
    }
    body.update(overrides)
    return body


def synthetic_bytes() -> bytes:
    return b"synthetic-provider-output"


def _sign(raw: bytes) -> str:
    return hmac.new(SECRET.encode("utf-8"), raw, hashlib.sha256).hexdigest()


def _post(
    harness: Harness,
    *,
    body: dict | None = None,
    signature: str | None = None,
    event_id: str = EVENT_ID,
    timestamp: str | None = None,
    idempotency_key: str = "wh-key-001",
    raw: bytes | None = None,
    path: str = WEBHOOK_PATH,
    drop: tuple[str, ...] = (),
):
    payload = json.dumps(body if body is not None else _payload(harness.clock)).encode("utf-8")
    content = raw if raw is not None else payload
    stamp = timestamp or str(payload_json(content).get("timestamp", ""))
    headers = {
        PROVIDER_SIGNATURE_HEADER_NAME: _sign(content) if signature is None else signature,
        EVENT_ID_HEADER_NAME: event_id,
        TIMESTAMP_HEADER_NAME: stamp,
        "Idempotency-Key": idempotency_key,
    }
    for name in drop:
        headers.pop(name, None)
    return harness.client.post(path, content=content, headers=headers)


def payload_json(content: bytes) -> dict:
    try:
        parsed = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


@pytest.fixture
def harness() -> Iterator[Harness]:
    clock = MutableClock()
    app = create_app()
    redis = SetNxRedisClient(clock=clock)
    orchestrator = RecordingOrchestrator()
    _patch_ingress(app, orchestrator, redis)

    with TestClient(app, base_url=BASE_URL) as client:
        yield Harness(app, client, orchestrator, redis, clock)

    app.dependency_overrides.clear()
    reset_settings_cache()


def _patch_ingress(
    app: FastAPI, orchestrator: RecordingOrchestrator, redis: SetNxRedisClient
) -> None:
    app.dependency_overrides[get_webhook_ingress] = lambda: WebhookIngress(
        authenticator=MockHmacWebhookAuthenticator(secret=SECRET, provider_name="MOCK"),
        replay_guard=RedisReplayGuard(redis),
        orchestrator=orchestrator,
    )


# --------------------------------------------------------------------------- #
# 正常路径
# --------------------------------------------------------------------------- #
def test_valid_event_is_accepted_and_reconciled_once(harness: Harness) -> None:
    response = _post(harness)

    assert response.status_code == 202, response.text
    assert response.json()["data"] == {"accepted": True, "outcome": "SUCCEEDED"}
    assert set(response.json()) == {"data", "request_id"}
    assert len(harness.orchestrator.events) == 1
    event = harness.orchestrator.events[0]
    assert event.provider_name == "MOCK"
    assert event.provider_request_id == EXTERNAL_ID
    assert event.result.state is ProviderState.SUCCEEDED
    assert len(event.result.outputs) == 1


def test_duplicate_event_id_is_a_noop_success(harness: Harness) -> None:
    first = _post(harness)
    second = _post(harness)

    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json()["data"]["outcome"] == OUTCOME_DUPLICATE
    assert len(harness.orchestrator.events) == 1


def test_request_id_header_is_echoed(harness: Harness) -> None:
    response = _post(harness)

    assert response.json()["request_id"] == response.headers["X-Request-Id"]


# --------------------------------------------------------------------------- #
# 验签与安全拒绝
# --------------------------------------------------------------------------- #
def test_missing_signature_header_is_rejected(harness: Harness) -> None:
    response = _post(harness, drop=(PROVIDER_SIGNATURE_HEADER_NAME,))

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "WEBHOOK_INVALID"
    assert harness.orchestrator.events == []


@pytest.mark.parametrize(
    "signature",
    ["", "deadbeef", "0" * 64],
)
def test_bad_signature_is_rejected(harness: Harness, signature: str) -> None:
    response = _post(harness, signature=signature)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "WEBHOOK_INVALID"
    assert harness.orchestrator.events == []


def test_tampered_body_is_rejected_even_with_an_intact_signature(harness: Harness) -> None:
    """签名覆盖的是原始字节：换了字节就等于没签名。"""

    original = json.dumps(_payload(harness.clock)).encode("utf-8")
    forged = json.dumps(
        {**json.loads(original), "provider_request_id": "ext-attacker"}
    ).encode("utf-8")

    response = _post(harness, raw=forged, signature=_sign(original))

    assert response.status_code == 401
    assert harness.orchestrator.events == []


def test_mismatched_event_id_header_is_rejected(harness: Harness) -> None:
    """事件 ID 只能以**已签名载荷**为准：未信任的头不得污染防重放键。"""

    response = _post(harness, event_id="evt-attacker")

    assert response.status_code == 401
    assert harness.orchestrator.events == []
    assert harness.redis.get("ai-avatar:v1:webhook:event:MOCK:evt-attacker") is None


def test_expired_timestamp_is_rejected(harness: Harness) -> None:
    stale = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()
    body = _payload(harness.clock, timestamp=stale)

    response = _post(harness, body=body, timestamp=stale)

    assert response.status_code == 401
    assert harness.orchestrator.events == []


def test_missing_identity_headers_are_rejected(harness: Harness) -> None:
    for name in (EVENT_ID_HEADER_NAME, TIMESTAMP_HEADER_NAME, "Idempotency-Key"):
        response = _post(harness, drop=(name,))

        assert response.status_code in (400, 401), (name, response.status_code)
        assert response.json()["error"]["code"] in {"WEBHOOK_INVALID", "IDEMPOTENCY_REQUIRED"}
    assert harness.orchestrator.events == []


def test_unknown_provider_path_is_rejected(harness: Harness) -> None:
    body = json.dumps(_payload(harness.clock)).encode("utf-8")

    response = harness.client.post(
        "/api/v1/internal/providers/SOMEREALPROVIDER/webhook",
        content=body,
        headers={
            PROVIDER_SIGNATURE_HEADER_NAME: _sign(body),
            EVENT_ID_HEADER_NAME: EVENT_ID,
            TIMESTAMP_HEADER_NAME: json.loads(body)["timestamp"],
            "Idempotency-Key": "wh-key-002",
        },
    )

    assert response.status_code == 401
    assert harness.orchestrator.events == []


def test_unlinkable_event_is_rejected_and_never_settles(harness: Harness) -> None:
    harness.app.dependency_overrides[get_webhook_ingress] = lambda: WebhookIngress(
        authenticator=MockHmacWebhookAuthenticator(secret=SECRET, provider_name="MOCK"),
        replay_guard=RedisReplayGuard(harness.redis),
        orchestrator=RecordingOrchestrator(error=ApiError(ErrorCode.WEBHOOK_INVALID)),
    )

    response = _post(harness)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "WEBHOOK_INVALID"


def test_idempotency_conflict_from_service_surfaces_as_409(harness: Harness) -> None:
    harness.app.dependency_overrides[get_webhook_ingress] = lambda: WebhookIngress(
        authenticator=MockHmacWebhookAuthenticator(secret=SECRET, provider_name="MOCK"),
        replay_guard=RedisReplayGuard(harness.redis),
        orchestrator=RecordingOrchestrator(
            error=ApiError(ErrorCode.IDEMPOTENCY_CONFLICT)
        ),
    )

    response = _post(harness)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


# --------------------------------------------------------------------------- #
# 脱敏与 fail-closed
# --------------------------------------------------------------------------- #
def test_logs_and_response_never_leak_secret_signature_or_bytes(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    body = json.dumps(_payload(harness.clock)).encode("utf-8")
    signature = _sign(body)

    with caplog.at_level("INFO"):
        response = harness.client.post(
            WEBHOOK_PATH,
            content=body,
            headers={
                PROVIDER_SIGNATURE_HEADER_NAME: signature,
                EVENT_ID_HEADER_NAME: EVENT_ID,
                TIMESTAMP_HEADER_NAME: json.loads(body)["timestamp"],
                "Idempotency-Key": "wh-key-003",
            },
        )

    assert response.status_code == 202
    dumped = f"{caplog.text}{response.text}"
    assert SECRET not in dumped
    assert signature not in dumped
    assert synthetic_bytes().hex() not in dumped
    assert PROVIDER_SIGNATURE_HEADER_NAME not in dumped


def test_authenticator_without_secret_fails_closed(harness: Harness) -> None:
    """密钥缺失时一律拒绝：绝不因为「看起来像本地测试」就放行未验签请求。"""

    no_secret = MockHmacWebhookAuthenticator(secret="", provider_name="MOCK")
    body = json.dumps(_payload(harness.clock)).encode("utf-8")

    with pytest.raises(ApiError) as excinfo:
        no_secret.authenticate(
            provider_name="MOCK",
            raw_body=body,
            signature=_sign(body),
            event_id=EVENT_ID,
            timestamp=json.loads(body)["timestamp"],
        )

    assert excinfo.value.code is ErrorCode.WEBHOOK_INVALID


def test_deps_wiring_uses_server_side_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """装配点必须从服务端配置取密钥与 Provider 名，不接受请求侧覆盖。"""

    from app.api import deps

    monkeypatch.setenv("AI_PROVIDER", "mock")
    monkeypatch.setenv("AI_API_KEY", "configured-secret")
    reset_settings_cache()

    authenticator = deps.get_webhook_authenticator()

    assert authenticator._provider_name == "MOCK"
    assert authenticator._secret == b"configured-secret"
    reset_settings_cache()


# --------------------------------------------------------------------------- #
# 修复批次 3：重复事件的三种成因必须给出三种后果
# --------------------------------------------------------------------------- #
def dedupe_name(event_id: str = EVENT_ID) -> str:
    return f"ai-avatar:v1:webhook:event:MOCK:{event_id}"


def test_in_flight_duplicate_is_a_conflict_not_an_acceptance(harness: Harness) -> None:
    """另一个 Worker 仍在对账中：给 202 会让供应商停止重投，一次竞态就变成永久丢失。"""

    harness.redis.set(dedupe_name(), "other-nonce|processing", ex=360, nx=True)

    response = _post(harness)

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "IDEMPOTENCY_IN_PROGRESS"
    assert harness.orchestrator.events == []


def test_reconciled_duplicate_is_an_acceptance_without_second_reconcile(
    harness: Harness,
) -> None:
    """先前那次已把结论持久落地：这才是可以回 202 的「重复」，且不再进入对账。"""

    harness.redis.set(dedupe_name(), "other-nonce|reconciled", ex=360, nx=True)

    response = _post(harness)

    assert response.status_code == 202, response.text
    assert response.json()["data"]["outcome"] == OUTCOME_DUPLICATE
    assert harness.orchestrator.events == []


def test_failed_reconciliation_leaves_the_event_id_reclaimable(harness: Harness) -> None:
    """对账失败必须释放占用：同一条事件重投时还要能再走一遍。"""

    failing = RecordingOrchestrator(error=ApiError(ErrorCode.PROVIDER_UNAVAILABLE))
    harness.app.dependency_overrides[get_webhook_ingress] = lambda: WebhookIngress(
        authenticator=MockHmacWebhookAuthenticator(secret=SECRET, provider_name="MOCK"),
        replay_guard=RedisReplayGuard(harness.redis),
        orchestrator=failing,
    )
    body = _payload(harness.clock, event_id="evt-retry")

    first = _post(harness, body=body, event_id="evt-retry")
    assert first.status_code == 503
    assert harness.redis.get(dedupe_name("evt-retry")) is None

    _patch_ingress(harness.app, harness.orchestrator, harness.redis)
    second = _post(harness, body=body, event_id="evt-retry")

    assert second.status_code == 202, second.text
    assert len(harness.orchestrator.events) == 1


def test_wiring_fails_closed_for_a_provider_without_webhooks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Quanzil 不提供 Webhook：装配点直接拒绝，绝不拿 API 凭据去验一个不存在的签名通路。"""

    from app.api import deps

    monkeypatch.setenv("AI_PROVIDER", "quanzil")
    monkeypatch.setenv("AI_API_KEY", "configured-relay-key")
    reset_settings_cache()

    with pytest.raises(ApiError) as raised:
        deps.get_webhook_authenticator()

    assert raised.value.code is ErrorCode.WEBHOOK_INVALID
    reset_settings_cache()


def test_generation_service_stamps_the_configured_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """任务行登记的 provider/model 必须与出网目标同源，否则事后关联会指向错误供应商。"""

    from app.api import deps
    from tests.services.fakes import GenerationUnitOfWork, MutableClock
    from app.services.points.service import PointsService

    clock = MutableClock()
    uow = GenerationUnitOfWork(clock=clock)
    monkeypatch.setenv("AI_PROVIDER", "quanzil")
    monkeypatch.setenv("AI_MODEL", "gpt-image-2.5-flare")
    monkeypatch.setenv("AI_API_KEY", "configured-relay-key")
    reset_settings_cache()

    service = deps.get_generation_service(
        uow=uow, points=PointsService(uow=uow, clock=clock)
    )

    assert service._provider_name == "QUANZIL"
    assert service._model_name == "gpt-image-2.5-flare"
    reset_settings_cache()
