"""Provider 回调入口：验签 → 防重放 → 统一对账（ENG-06 第 3 步）。

规则来源：`04-ai-provider.md` 第 4 节（Webhook 安全 5 条）、`03-api-contract.md`
第 4.5 节与第 9 节、`LEAD-04` §7。

顺序不可调换：

1. 先校验**身份要素齐备**（供应商、签名、事件 ID、时间戳、幂等键），缺失即
   `WEBHOOK_INVALID`（401）——不区分「不存在」与「非法」，避免给探测者反馈；
2. 时间戳必须落在接受窗口内（第 4 节第 2 条），未来偏移只容忍
   `CLOCK_SKEW_ALLOWANCE_SECONDS`；
3. 再对**解析前的原始字节**验签（`WebhookAuthenticator` 负责，恒定时间比较），
   验签通过之前绝不解析载荷（第 4 节第 1 条）；
4. 事件 ID 进入短期去重存储（**一次原子的 `SET NX`**，TTL 至少覆盖「接受窗口 +
   时钟偏移」），占用结果分三态：
   - 首次占用成功 → 进入对账；
   - 已登记「已对账」→ 按 **no-op 成功**返回（第 4 节第 2 条），不再进入对账；
   - 仍在处理中 → 用既有 `IDEMPOTENCY_IN_PROGRESS`（409）让供应商重投：给 202 会让它
     停止重投，一次纯粹的时间竞态就变成永久丢失的结果；
   对账抛错则**释放**本次占用，同一条事件可以安全重投；只有持久落地过的结论才会被
   登记为「已对账」。去重存储真正不可用时按既有 `PROVIDER_UNAVAILABLE`（503）让供应商
   重投——这是本入口唯一允许的 503 映射；
5. 对账只按 `provider_name + provider_request_id` 关联，回调里携带的任何
   用户/任务标识都不作为授权依据（第 4 节第 3 条）；本模块绝不结算点数、
   绝不写结果、绝不改任务状态，一律交 `GenerationOrchestrator`，
   权威判定是数据库里的 attempt 行锁与终态检查（第 4 节第 4 条）。

日志纪律（第 4 节第 5 条、`06-development-standard.md` 第 6 节）：只记事件身份与
结论标签，绝不记签名头、密钥、完整载荷、图片 URL 与 Prompt。
"""

from __future__ import annotations

import hmac
import inspect
import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Protocol

from app.errors.codes import ErrorCode
from app.errors.exceptions import ApiError
from app.services.generation.ports import (
    CLAIM_ACQUIRED,
    CLAIM_IN_FLIGHT,
    CLAIM_RECONCILED,
    ReplayGuard,
    WebhookAuthenticator,
    WebhookEvent,
)

__all__ = [
    "CLOCK_SKEW_ALLOWANCE_SECONDS",
    "WebhookIngress",
    "WebhookReconciler",
    "constant_time_equals",
    "event_key",
    "parse_event_timestamp",
]

_LOGGER = logging.getLogger("ai_avatar.generation.webhook")

#: 允许的时钟偏移：事件 ID 的 TTL 至少要覆盖「接受窗口 + 本偏移」（第 4 节第 2 条）
CLOCK_SKEW_ALLOWANCE_SECONDS = 60

#: 重复事件的结论标签：与编排器的「终态已提交，只做幂等重放」同一语义
OUTCOME_DUPLICATE = "REPLAYED"


def event_key(provider_name: str, event_id: str) -> str:
    """去重键：作用域是 `provider_name + provider_event_id`（`03-api-contract.md` 第 9 节）。

    `Idempotency-Key` Header 不参与：否则回调方可以用不同 Header 绕过防重放。
    """

    return f"{provider_name}:{event_id}"


def constant_time_equals(expected: str, provided: str) -> bool:
    """恒定时间比较两个字符串（`hmac.compare_digest`）。

    缺失、空值与长度差异都只返回 `False`，不提前返回可比对的字节数信息。
    供 `WebhookAuthenticator` 的实现复用，避免每个供应商适配器各写一遍比较。
    """

    if not expected or not provided:
        return False
    try:
        return hmac.compare_digest(expected.encode("utf-8"), provided.encode("utf-8"))
    except (AttributeError, TypeError):
        return False


def parse_event_timestamp(raw: str) -> datetime:
    """解析回调时间戳（RFC 3339 / ISO 8601）；无法解析即 `WEBHOOK_INVALID`。"""

    try:
        moment = datetime.fromisoformat(raw.strip())
    except (AttributeError, TypeError, ValueError) as exc:
        raise ApiError(ErrorCode.WEBHOOK_INVALID) from exc
    if moment.tzinfo is None:
        # 未带偏移量的时间戳按 UTC 解释：不猜本地时区，否则窗口判断随部署地漂移
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


class WebhookReconciler(Protocol):
    """对账入口的最小依赖面（便于测试注入）。"""

    def reconcile_event(self, event: WebhookEvent) -> str: ...


class WebhookIngress:
    """把一次 HTTP 回调归一为「已对账 / 已去重 / 拒绝」。"""

    def __init__(
        self,
        *,
        authenticator: WebhookAuthenticator,
        replay_guard: ReplayGuard,
        orchestrator: WebhookReconciler,
        replay_ttl_seconds: int = 300,
        acceptance_window_seconds: int = 300,
        clock: Callable[[], datetime] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._authenticator = authenticator
        self._replay_guard = replay_guard
        self._orchestrator = orchestrator
        self._replay_ttl_seconds = replay_ttl_seconds
        self._acceptance_window_seconds = acceptance_window_seconds
        self._clock: Callable[[], datetime] = clock or (lambda: datetime.now(timezone.utc))
        self._logger = logger or _LOGGER
        self._authenticate = _authenticate_binding(authenticator)

    def ingest(
        self,
        *,
        provider_name: str,
        raw_body: bytes,
        signature: str,
        event_id: str,
        timestamp: str,
        idempotency_key: str,
    ) -> str:
        """处理一次回调；返回值是编排器的结论标签。

        `idempotency_key` 由路由按契约校验存在性（`03-api-contract.md` 第 9 节：
        Webhook 的幂等作用域是 `provider_name + provider_event_id`），因此重放
        判定只以事件 ID 为准，Header 值不得成为绕过防重放的第二个键。
        """

        self._require_identity(
            provider_name=provider_name,
            signature=signature,
            event_id=event_id,
            timestamp=timestamp,
            idempotency_key=idempotency_key,
        )
        self._require_within_window(timestamp)

        try:
            event = self._authenticate(
                provider_name=provider_name,
                raw_body=raw_body,
                signature=signature,
                event_id=event_id,
                timestamp=timestamp,
            )
        except ApiError as exc:
            # 只记事件身份：载荷、签名与密钥一律不落日志
            self._logger.warning(
                "webhook_rejected provider=%s code=%s event_id=%s",
                provider_name,
                exc.code.value,
                event_id,
            )
            raise

        claimed = self._claim(provider_name=event.provider_name, event_id=event_id)
        if claimed == CLAIM_RECONCILED:
            # 先前那次已经把结论持久写进数据库：这才是可以回 202 的「重复」
            self._logger.info(
                "webhook_duplicate provider=%s event_id=%s", event.provider_name, event_id
            )
            return OUTCOME_DUPLICATE
        if claimed == CLAIM_IN_FLIGHT:
            # 先前那次仍在跑：给它 202 会让供应商不再重投，一次竞态就变成永久丢失
            self._logger.info(
                "webhook_in_flight provider=%s event_id=%s", event.provider_name, event_id
            )
            raise ApiError(ErrorCode.IDEMPOTENCY_IN_PROGRESS)

        try:
            outcome = self._orchestrator.reconcile_event(event)
        except Exception as error:
            # 对账没落地就不算处理过：释放自己那次占用，允许供应商安全重投
            if isinstance(error, ApiError) and error.code is ErrorCode.WEBHOOK_INVALID:
                # 第 4 节第 3 条：关联不到的事件必须安全留痕，但不带载荷
                self._logger.warning(
                    "webhook_unlinkable provider=%s event_id=%s", event.provider_name, event_id
                )
            self._release(provider_name=event.provider_name, event_id=event_id)
            raise

        self._confirm(provider_name=event.provider_name, event_id=event_id)
        self._logger.info(
            "webhook_reconciled provider=%s event_id=%s outcome=%s",
            event.provider_name,
            event_id,
            outcome,
        )
        return outcome

    # ---------------------------------------------------------------- 校验
    @staticmethod
    def _require_identity(
        *,
        provider_name: str,
        signature: str,
        event_id: str,
        timestamp: str,
        idempotency_key: str,
    ) -> None:
        """身份要素缺失统一 `WEBHOOK_INVALID`（401），不给探测者区分依据。"""

        for value in (provider_name, signature, event_id, timestamp, idempotency_key):
            if not str(value or "").strip():
                raise ApiError(ErrorCode.WEBHOOK_INVALID)

    def _require_within_window(self, raw: str) -> None:
        """时间窗判断：过期或过于未来的时间戳都拒绝（第 4 节第 2 条）。"""

        moment = parse_event_timestamp(raw)
        now = self._clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        if moment > now + timedelta(seconds=CLOCK_SKEW_ALLOWANCE_SECONDS):
            raise ApiError(ErrorCode.WEBHOOK_INVALID)
        if moment < now - timedelta(seconds=self._acceptance_window_seconds):
            raise ApiError(ErrorCode.WEBHOOK_INVALID)

    def _claim(self, *, provider_name: str, event_id: str) -> str:
        """原子占用事件 ID；存储故障映射为既有 `PROVIDER_UNAVAILABLE`（503）。"""

        try:
            return self._replay_guard.claim(
                event_key(provider_name, event_id), ttl_seconds=self._ttl_seconds()
            )
        except Exception as exc:  # noqa: BLE001 - 依赖故障不得外泄成 500 或载荷
            self._logger.warning(
                "webhook_replay_guard_unavailable provider=%s error=%s",
                provider_name,
                type(exc).__name__,
            )
            raise ApiError(ErrorCode.PROVIDER_UNAVAILABLE) from exc

    def _confirm(self, *, provider_name: str, event_id: str) -> None:
        """把**已持久**的结论登记为「已对账」；此后同 ID 事件才可按 no-op 成功回 202。

        这一步失败不改变本次结果：数据库里已是终局，重复事件最多多做一次 no-op 对账
        （`04-ai-provider.md` 第 4 节第 4 条：Redis 只是快速路径，权威在数据库）。
        """

        try:
            self._replay_guard.mark_reconciled(
                event_key(provider_name, event_id), ttl_seconds=self._ttl_seconds()
            )
        except Exception as exc:  # noqa: BLE001 - 已落地的结论不能因缓存故障被改判
            self._logger.warning(
                "webhook_replay_mark_failed provider=%s event_id=%s error=%s",
                provider_name,
                event_id,
                type(exc).__name__,
            )

    def _release(self, *, provider_name: str, event_id: str) -> None:
        """对账失败即释放占用；释放本身失败只记日志（TTL 会兜底）。"""

        try:
            self._replay_guard.release(event_key(provider_name, event_id))
        except Exception as exc:  # noqa: BLE001
            self._logger.warning(
                "webhook_replay_release_failed provider=%s event_id=%s error=%s",
                provider_name,
                event_id,
                type(exc).__name__,
            )

    def _ttl_seconds(self) -> int:
        """事件 ID 至少活过「接受窗口 + 时钟偏移」，否则过期后的重放会二次生效。"""

        return max(
            self._replay_ttl_seconds,
            self._acceptance_window_seconds + CLOCK_SKEW_ALLOWANCE_SECONDS,
        )


def _authenticate_binding(authenticator: object) -> Callable[..., WebhookEvent]:
    """按实现方的形参名把端口调用绑定到具体适配器上。

    冻结端口（`ports.WebhookAuthenticator`）声明的是长名
    `event_id_header` / `timestamp_header`；已落地的适配器实现有两种签名：

    - 长名（端口原样）；
    - 短名 `event_id` / `timestamp`；
    - 只收 `provider_name` / `raw_body` / `signature`：事件 ID 与时间戳从**已验签的
      载荷**里取出（冻结基线的 webhook operation 除 `Idempotency-Key` 外没有其它
      对外参数，`03-api-contract.md` 第 4.5 节），因此不接收未信任的头部值。

    构造期绑定一次（而不是 `except TypeError` 重试），既不留隐式吞异常的路径，
    也不会因为实现方形参名不同而 500。
    """

    try:
        parameters = inspect.signature(authenticator.authenticate).parameters  # type: ignore[attr-defined]
    except (AttributeError, TypeError, ValueError) as error:
        raise TypeError("WebhookAuthenticator 实现缺少 authenticate() 方法。") from error

    def _long(**values: object) -> WebhookEvent:
        return authenticator.authenticate(  # type: ignore[attr-defined,no-any-return]
            provider_name=values["provider_name"],
            raw_body=values["raw_body"],
            signature=values["signature"],
            event_id_header=values["event_id"],
            timestamp_header=values["timestamp"],
        )

    def _short(**values: object) -> WebhookEvent:
        return authenticator.authenticate(  # type: ignore[attr-defined,no-any-return]
            provider_name=values["provider_name"],
            raw_body=values["raw_body"],
            signature=values["signature"],
            event_id=values["event_id"],
            timestamp=values["timestamp"],
        )

    def _payload_only(**values: object) -> WebhookEvent:
        return authenticator.authenticate(  # type: ignore[attr-defined,no-any-return]
            provider_name=values["provider_name"],
            raw_body=values["raw_body"],
            signature=values["signature"],
        )

    if "event_id_header" in parameters:
        return _long
    if "event_id" in parameters:
        return _short
    return _payload_only
