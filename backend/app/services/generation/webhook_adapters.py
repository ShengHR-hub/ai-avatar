"""Webhook 入口的具体适配器：验签与重放去重（ENG-06 第 5 步）。

`04-ai-provider.md` 第 4 节要求：先对**原始请求字节**验签（恒定时间比较）、
校验签名时间戳窗口、按 event ID/nonce 原子去重、只按已存在的
`provider_request_id` 关联。第 4 节最后一句同时明确：**真实签名头、算法与
规范化形式必须等选定供应商后（ENG-07）验证**。

因此本模块只提供 V1.0 唯一允许的 `MOCK` 通道实现：

- 签名：`hex(hmac_sha256(secret, raw_body))`，`X-Provider-Signature` 头携带；
- 时间戳与 event ID 放在请求体里（冻结基线的 webhook requestBody 是
  `{"type":"object","additionalProperties":true}`，而 operation 的 `parameters`
  只有 `ProviderName` 与 `Idempotency-Key`：**不得新增对外参数**）；
- 未配置密钥时一律拒绝（fail closed），绝不放行「看起来像测试」的请求。

重放去重用 Redis 短期键，且**只用一次原子的 `SET key value NX EX ttl`**：冻结的
`RedisPort` 只暴露 `set/get/delete/expire`（没有 `nx`），因此本模块接收的是一个
独立连接客户端（由 API 层装配），而不是那个端口——去重不能靠「先读后写」拼，
两个 Worker 同时读空就会双双放行。按第 4 节第 4 条，数据库 attempt 行锁与终态判定
仍是权威：去重存储缺失或被逐出时，重复事件最多被重新对账一次并判为 no-op。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from app.errors.codes import ErrorCode
from app.errors.exceptions import ApiError
from app.services.generation.ports import WebhookEvent

__all__ = ["MockHmacWebhookAuthenticator", "RedisReplayGuard"]

#: 冻结基线的 webhook 签名头名（`components.securitySchemes.providerSignature`）
SIGNATURE_HEADER_NAME = "X-Provider-Signature"

#: MOCK 通道在请求体里携带的字段名（真实供应商字段名属 ENG-07）
EVENT_ID_FIELD = "event_id"
TIMESTAMP_FIELD = "timestamp"
PROVIDER_REQUEST_ID_FIELD = "provider_request_id"
STATE_FIELD = "state"
OUTPUTS_FIELD = "outputs"
ERROR_FIELD = "error"


class MockHmacWebhookAuthenticator:
    """`WebhookAuthenticator` 的 MOCK 实现：HMAC-SHA256 + 时间窗 + event ID。"""

    def __init__(
        self, *, secret: str, provider_name: str = "MOCK", acceptance_window_seconds: int = 300
    ) -> None:
        self._secret = (secret or "").encode("utf-8")
        self._provider_name = provider_name
        self._window_seconds = acceptance_window_seconds

    def verify_signature(self, raw_body: bytes, signature: str) -> bool:
        """对**解析前的原始字节**验签，恒定时间比较（第 4 节第 1 条）。"""

        if not self._secret or not signature:
            return False
        expected = hmac.new(self._secret, raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature.strip())

    def authenticate(
        self,
        *,
        provider_name: str,
        raw_body: bytes,
        signature: str,
        event_id: str,
        timestamp: str,
    ) -> WebhookEvent:
        """验签并解析为 `WebhookEvent`；任何一步失败都是 `WEBHOOK_INVALID`（401）。

        顺序即安全边界：先对**未解析的原始字节**验签，只有验签通过才读取载荷，
        因此伪造者无法用「畸形 JSON」探测解析器行为（第 4 节第 1 条）。

        `event_id` / `timestamp` 是路由从请求上取到的**未信任**值：验签通过后必须与
        已签名载荷里的同名字段完全一致才接受——否则回调方可以用任意事件 ID 污染
        防重放键（第 4 节第 2 条）。
        """

        if provider_name.strip().upper() != self._provider_name:
            raise ApiError(ErrorCode.WEBHOOK_INVALID)
        if not self.verify_signature(raw_body, signature):
            raise ApiError(ErrorCode.WEBHOOK_INVALID)

        payload = self._parse(raw_body)
        body_event_id = payload.get(EVENT_ID_FIELD)
        body_timestamp = payload.get(TIMESTAMP_FIELD)
        if not isinstance(body_event_id, str) or not body_event_id.strip():
            raise ApiError(ErrorCode.WEBHOOK_INVALID)
        if body_event_id != event_id or body_timestamp != timestamp:
            # 头部/查询给出的身份与签名内容不一致：拒绝，绝不以未信任值为准
            raise ApiError(ErrorCode.WEBHOOK_INVALID)
        if not self._within_window(body_timestamp):
            raise ApiError(ErrorCode.WEBHOOK_INVALID)

        external_id = payload.get(PROVIDER_REQUEST_ID_FIELD)
        if not isinstance(external_id, str) or not external_id.strip():
            raise ApiError(ErrorCode.WEBHOOK_INVALID)

        result = _result_from_payload(payload, client_request_id=body_event_id)
        if result is None:
            # 载荷无法归一成可信结果：按不可信输入拒绝，不进入对账
            raise ApiError(ErrorCode.WEBHOOK_INVALID)

        return WebhookEvent(
            provider_name=self._provider_name,
            provider_request_id=external_id,
            result=result,
        )

    def _within_window(self, timestamp: object) -> bool:
        if not isinstance(timestamp, str) or not timestamp.strip():
            return False
        try:
            sent_at = datetime.fromisoformat(timestamp)
        except (TypeError, ValueError):
            return False
        if sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=timezone.utc)
        skew = abs((datetime.now(timezone.utc) - sent_at).total_seconds())
        return skew <= self._window_seconds

    @staticmethod
    def _parse(raw_body: bytes) -> dict[str, object]:
        try:
            parsed = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(ErrorCode.WEBHOOK_INVALID) from exc
        if not isinstance(parsed, dict):
            raise ApiError(ErrorCode.WEBHOOK_INVALID)
        return parsed


def _result_from_payload(payload: dict[str, object], *, client_request_id: str) -> object:
    """把 MOCK 载荷归一成 `ProviderResult`；无法归一时返回 `None`。

    「已验签」只证明来源可信，不证明形状可用。这里把任何形状问题（缺字段、类型不对、
    hex 不可解、条目不是对象）都归成同一个 `None`，由调用方统一回 `WEBHOOK_INVALID`：
    绝不能让一条签名正确的畸形载荷以 `KeyError`/`TypeError` 的形式变成 500，
    更不能带着半截结果进入对账与结算。
    """

    from app.providers.ports import ProviderError, ProviderErrorCategory, ProviderOutput, ProviderResult, ProviderState

    state_raw = payload.get(STATE_FIELD)
    try:
        state = ProviderState(str(state_raw))
    except ValueError:
        return None

    try:
        outputs: tuple[ProviderOutput, ...] = ()
        error = None
        if state is ProviderState.SUCCEEDED:
            items = payload.get(OUTPUTS_FIELD)
            if items is not None and not isinstance(items, list):
                return None
            outputs = tuple(_output_from_item(item) for item in (items or []))
        elif state is ProviderState.FAILED:
            raw_error = payload.get(ERROR_FIELD)
            if not isinstance(raw_error, dict):
                return None
            category = ProviderErrorCategory(str(raw_error.get("category")))
            error = ProviderError(
                category=category,
                code=str(raw_error.get("code") or "webhook_failure"),
                safe_message=str(raw_error.get("safe_message") or "供应商明确失败"),
                retryable=bool(raw_error.get("retryable", False)),
                request_accepted=(
                    None
                    if raw_error.get("request_accepted") is None
                    else bool(raw_error.get("request_accepted"))
                ),
            )

        return ProviderResult(
            state=state,
            client_request_id=client_request_id,
            provider_request_id=str(payload.get(PROVIDER_REQUEST_ID_FIELD) or ""),
            outputs=outputs,
            usage=None,
            error=error,
            retry_after_seconds=None,
            completed_at=None,
        )
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


def _output_from_item(item: object) -> "ProviderOutput":
    """一条结果必须是带可解码 `content_hex` 的对象；形状不对直接抛给上层归一。"""

    from app.providers.mock import MOCK_IMAGE_MIME
    from app.providers.ports import ProviderOutput

    if not isinstance(item, dict):
        raise TypeError("结果条目不是对象")
    content_hex = item["content_hex"]
    if not isinstance(content_hex, str) or not content_hex:
        raise TypeError("content_hex 不是非空字符串")
    return ProviderOutput(
        content=bytes.fromhex(content_hex),
        mime_type=str(item.get("mime_type") or MOCK_IMAGE_MIME),
        provider_output_id=(
            str(item["provider_output_id"])
            if item.get("provider_output_id") is not None
            else None
        ),
    )


class RedisReplayGuard:
    """事件 ID 去重：用**一次原子 `SET key value NX EX ttl`** 占用。

    不能写成「先 `GET` 再 `SET`」：两个 Worker 同时处理同一条事件时都会读到「没有」，
    于是双双放行（`04-ai-provider.md` 第 4 节第 2 条）。这里只用 Redis 的原子命令，
    并把结论分成两态——`处理中` 与 `已对账`——因为「重复」有两种后果完全不同的成因：
    先前那次已经持久落地才可以按 no-op 成功回 202；仍在跑或崩在半路时必须让供应商重投。

    占用值里带上本次调用独有的 nonce：`mark_reconciled`/`release` 只动自己占到的那一次，
    竞态失败的一方不会把赢家的占用抹掉。

    需要的客户端形状是 `set(name, value, *, ex, nx)` / `get(name)` / `delete(name)`。
    `nx` 不在冻结 `RedisPort` 的表面上，因此注入的是**独立**连接，而不是那个端口。
    """

    #: 占用值后缀：仍在处理 / 已把结论持久写进数据库
    PROCESSING = "processing"
    RECONCILED = "reconciled"

    def __init__(
        self,
        client: Any,
        *,
        namespace: str = "ai-avatar:v1:webhook:event",
    ) -> None:
        self._client = client
        self._namespace = namespace
        # 本次请求占到的键 -> nonce。守卫实例按请求构造，因此这张表就是「本次的占用」
        self._owned: dict[str, str] = {}

    def _name(self, key: str) -> str:
        return f"{self._namespace}:{key}"

    def claim(self, key: str, *, ttl_seconds: int) -> str:
        """原子占用；返回 `CLAIM_ACQUIRED` / `CLAIM_IN_FLIGHT` / `CLAIM_RECONCILED`。"""

        from app.services.generation.ports import (
            CLAIM_ACQUIRED,
            CLAIM_IN_FLIGHT,
            CLAIM_RECONCILED,
        )

        name = self._name(key)
        for _ in range(2):
            # 最多再试一次：只可能是「恰好在本命令与读值之间 TTL 到期」
            nonce = uuid.uuid4().hex
            if self._client.set(
                name, f"{nonce}|{self.PROCESSING}", ex=ttl_seconds, nx=True
            ):
                self._owned[name] = nonce
                return CLAIM_ACQUIRED
            current = self._client.get(name)
            if current is None:
                continue
            return (
                CLAIM_RECONCILED
                if str(current).endswith(f"|{self.RECONCILED}")
                else CLAIM_IN_FLIGHT
            )
        return CLAIM_IN_FLIGHT

    def mark_reconciled(self, key: str, *, ttl_seconds: int) -> None:
        """把**自己占到的**那次占用改写为「已对账」；不是自己的占用则不动。"""

        name = self._name(key)
        nonce = self._owned_nonce(name)
        if nonce is None:
            return
        self._client.set(name, f"{nonce}|{self.RECONCILED}", ex=ttl_seconds)

    def release(self, key: str) -> None:
        """删除**自己占到的**那次占用，使供应商的重投可以再次进入对账。"""

        name = self._name(key)
        if self._owned_nonce(name) is None:
            return
        self._client.delete(name)

    def _owned_nonce(self, name: str) -> str | None:
        """仍属于本次占用的 nonce；本次没占用或已被替换时返回 `None`。

        读出 nonce 后即从表里移除：一次占用只允许一次状态推进，
        避免「先确认、再释放」把别人的键删掉。
        """

        nonce = self._owned.pop(name, None)
        if nonce is None:
            return None
        current = self._client.get(name)
        if current is None or not str(current).startswith(f"{nonce}|"):
            return None
        return nonce
