"""Generation Orchestrator：Worker 与回调的执行编排（ENG-06 第 3 步）。

规则来源：`LEAD-04` §2/§3/§6/§7/§11、`04-ai-provider.md` 第 2/3/4/5/6 节。

事务边界是本模块存在的理由：

```text
短事务：QUEUED -> PROCESSING + provider_attempts(STARTED)          -> commit
事务外：provider.generate / check_status / cancel（网络调用）
一个事务：结果写入 + 任务终态 + Points 结算 + 钱包快照 + attempt 终态 -> commit
```

- `LEAD-04` §6:121：**Provider 网络调用绝不放进数据库事务**；
- §6:123：成功结果与 `CONSUME` 同事务、明确失败与 `RELEASE` 同事务，
  `UNKNOWN`/结果缺失只转 `NEEDS_REVIEW` 并保持 `RESERVED`；
- §6:126：该事务失败即整体回滚，重投安全；
- §7:136：终态任务上的重投是 no-op，`PROCESSING` 只由持有当前 attempt 的 Worker 继续。

轮询与 Webhook 都汇入同一个 `_apply`，因此两条传输路径的判定不可能漂移
（`04-ai-provider.md` 第 4 节）。
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.db.models.generation import GenerationTask
from app.db.models.provider import ProviderAttempt
from app.errors.codes import ErrorCode
from app.errors.exceptions import ApiError
from app.providers.ports import (
    AIProvider,
    GenerateRequest,
    ImageInput,
    ProviderCallError,
    ProviderError,
    ProviderErrorCategory,
    ProviderResult,
    ProviderState,
    classify,
)
from app.repositories.provider_attempt import ProviderAttemptDraft
from app.repositories.unit_of_work import UnitOfWork
from app.services.generation.outputs import persist_provider_outputs
from app.services.generation.ports import GenerationQueuePort, ProviderFactory, UowFactory
from app.services.generation.service import CANCEL_FAILURE_CODE, ledger_key
from app.services.generation.state import (
    CREATED,
    FAILED,
    NEEDS_REVIEW,
    PROCESSING,
    QUEUED,
    SUCCEEDED,
    TERMINAL_STATUSES,
    assert_transition,
)
from app.services.points.service import PointsService
from app.storage.ports import ObjectNotFound, StorageAdapter, StorageError

__all__ = [
    "GenerationOrchestrator",
    "OUTCOME_FAILED",
    "OUTCOME_NEEDS_REVIEW",
    "OUTCOME_NOT_CLAIMABLE",
    "OUTCOME_PENDING",
    "OUTCOME_REPLAYED",
    "OUTCOME_RETRY_SCHEDULED",
    "OUTCOME_SUCCEEDED",
]

_LOGGER = logging.getLogger("ai_avatar.generation.orchestrator")

#: 编排结果标签（Celery 任务只把它写进日志，不据此改变账务）
OUTCOME_SUCCEEDED = "SUCCEEDED"
OUTCOME_FAILED = "FAILED"
OUTCOME_NEEDS_REVIEW = "NEEDS_REVIEW"
OUTCOME_PENDING = "PENDING"
OUTCOME_REPLAYED = "REPLAYED"
OUTCOME_NOT_CLAIMABLE = "NOT_CLAIMABLE"
OUTCOME_RETRY_SCHEDULED = "RETRY_SCHEDULED"

#: `provider_attempts.status` 取值（`02-database-design.md` 第 3.4 节 CHECK）
_ATTEMPT_STARTED = "STARTED"
_ATTEMPT_SUBMITTED = "SUBMITTED"
_ATTEMPT_SUCCEEDED = "SUCCEEDED"
_ATTEMPT_FAILED = "FAILED"
_ATTEMPT_TIMEOUT = "TIMEOUT"
_ATTEMPT_UNKNOWN = "UNKNOWN"
_OPEN_ATTEMPT_STATUSES = (_ATTEMPT_STARTED, _ATTEMPT_SUBMITTED)

#: 输入资源不可读时的失败归类：写进 `generation_tasks.failure_code`（非枚举字符串列），
#: **不是**新的对外错误码。此时请求根本没交给供应商，可证明没有外部执行
_INPUT_FAILURE_CODE = "INPUT_UNAVAILABLE"


class _InputUnavailable(RuntimeError):
    """生成输入资源读不到：只用于内部分流，绝不带对象键以外的信息进日志。"""

    def __init__(self, asset_id: str) -> None:
        super().__init__("生成输入资源不可读")
        self.asset_id = asset_id


@dataclass(frozen=True, slots=True)
class _Claim:
    """一次逻辑 attempt 的执行上下文（跨事务传递，不含任何账务判断）。

    `resumed=True` 表示这**不是**新的外部请求：任务已在 `PROCESSING` 且仍有未终结的
    attempt（典型场景是 `acks_late` 下 Worker 崩溃后同一条消息被重投）。此时调用方
    只能向权威源查询当前 attempt，绝不能再提交一次 `generate`。
    """

    generation_id: uuid.UUID
    user_id: uuid.UUID
    attempt_no: int
    client_request_id: str
    provider_name: str
    model_name: str
    mode: str
    prompt: str
    output_count: int
    resumed: bool = False
    external_id: str | None = None


class GenerationOrchestrator:
    """Worker / 回调侧的编排入口。"""

    def __init__(
        self,
        *,
        uow_factory: UowFactory,
        points_factory: Callable[[UnitOfWork], PointsService],
        provider_factory: ProviderFactory,
        storage: StorageAdapter,
        clock: Callable[[], datetime] | None = None,
        queue: GenerationQueuePort | None = None,
        storage_provider: str = "MINIO",
        max_transport_retries: int = 3,
        backoff_seconds: tuple[int, ...] = (5, 30, 120),
        poll_interval_seconds: int = 20,
        overall_deadline_seconds: int = 240,
        logger: logging.Logger | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._points_factory = points_factory
        self._provider_factory = provider_factory
        self._storage = storage
        self._clock: Callable[[], datetime] = clock or (lambda: datetime.now(timezone.utc))
        self._queue = queue
        self._storage_provider = storage_provider
        self._max_transport_retries = max_transport_retries
        self._backoff_seconds = backoff_seconds
        self._poll_interval_seconds = poll_interval_seconds
        self._overall_deadline_seconds = overall_deadline_seconds
        self._logger = logger or _LOGGER

    # ------------------------------------------------------------------ 入口
    def submit(self, generation_id: uuid.UUID) -> str:
        """领取并执行一次生成。"""

        claim = self._claim(generation_id)
        if claim is None:
            return OUTCOME_NOT_CLAIMABLE
        if claim.resumed:
            return self._resume(claim)

        provider = self._provider_factory()
        try:
            request = self._build_request(claim)
        except _InputUnavailable:
            # 输入图读不到时请求根本没有交给供应商：可证明没有外部执行，
            # 因此按「明确失败」走同一条结算路径（一次 `RELEASE`），不伪造结果
            return self._apply(
                claim.generation_id,
                claim.attempt_no,
                self._definite_failure_result(
                    _INPUT_FAILURE_CODE,
                    "生成输入资源不可读，请求未提交给供应商。",
                ),
            )

        try:
            result = provider.generate(request)
        except ProviderCallError as exc:
            return self._handle_call_error(claim.generation_id, claim.attempt_no, exc)

        return self._apply(claim.generation_id, claim.attempt_no, result)

    def _resume(self, claim: _Claim) -> str:
        """重投落在「本 Worker 已在跑的 attempt」上：只观察，绝不重复提交。

        再调一次 `generate` 会在供应商侧产生第二个外部请求——两份结果、两次消费，
        而账务只认一条 `RESERVE`。因此这里只有两条出路（`04-ai-provider.md` 第 2 节）：

        - 已知外部 ID：向权威源查一次状态，汇入同一条对账路径；
        - 未知是否被接受（`STARTED` 且没有外部 ID）：既不能证明未接受（所以不能重试
          也不能放款），只能收敛为 `UNKNOWN` + `NEEDS_REVIEW` 且保持 `RESERVED`。
        """

        if claim.external_id is None:
            return self._to_needs_review(
                claim.generation_id,
                claim.attempt_no,
                reason="resumed_without_external_id",
                attempt_status=_ATTEMPT_UNKNOWN,
            )

        try:
            result = self._provider_factory().check_status(claim.external_id)
        except ProviderCallError as exc:
            return self._handle_call_error(claim.generation_id, claim.attempt_no, exc)
        return self._apply(claim.generation_id, claim.attempt_no, result)

    def _build_request(self, claim: _Claim) -> GenerateRequest:
        """组装归一请求：输入图字节在**事务外**读取（`04-ai-provider.md` 第 2、8 节）。

        资源行与 `storage_key` 在一个只读事务里取出，退出事务后才读对象，
        因此数据库事务不跨网络调用；角色直接来自 `generation_inputs.role`，
        与创建时校验过的 `purpose` 一致。
        """

        with self._uow_factory() as uow:
            refs: list[tuple[str, str, str]] = []
            for row in uow.generation_tasks.list_inputs(claim.generation_id):
                asset = uow.media.get(row.asset_id)
                if asset is None:
                    raise _InputUnavailable(str(row.asset_id))
                refs.append((row.role, str(asset.storage_key), str(asset.mime_type)))

        source_images: list[ImageInput] = []
        reference_images: list[ImageInput] = []
        for role, storage_key, mime_type in refs:
            try:
                content, _metadata = self._storage.read(storage_key)
            except (ObjectNotFound, StorageError) as error:
                # 日志只含标识符：不含对象键之外的任何信息
                self._logger.warning(
                    "generation_input_unavailable generation_id=%s error=%s",
                    claim.generation_id,
                    type(error).__name__,
                )
                raise _InputUnavailable(storage_key) from error
            image = ImageInput(
                mime_type=mime_type,
                content=bytes(content),
                sha256=hashlib.sha256(bytes(content)).hexdigest(),
            )
            (reference_images if role == "REFERENCE" else source_images).append(image)

        return GenerateRequest(
            mode=claim.mode,
            prompt=claim.prompt,
            source_images=tuple(source_images),
            reference_images=tuple(reference_images),
            style=None,
            output_count=claim.output_count,
            model=claim.model_name,
            client_request_id=claim.client_request_id,
        )

    @staticmethod
    def _definite_failure_result(code: str, safe_message: str) -> ProviderResult:
        """服务端侧的「明确失败」归一结果：可证明未接受、未产生外部执行。"""

        return ProviderResult(
            state=ProviderState.FAILED,
            client_request_id="server-side",
            provider_request_id=None,
            error=ProviderError(
                category=ProviderErrorCategory.VALIDATION,
                code=code,
                safe_message=safe_message,
                retryable=False,
                request_accepted=False,
            ),
        )

    def reconcile(
        self, *, generation_id: uuid.UUID, provider_request_id: str | None = None
    ) -> str:
        """轮询入口：向权威源取一次状态，然后汇入同一条对账路径。

        已终结任务上的重复轮询/重投是 no-op 回放（`LEAD-04` §7:136、第 4 节终态单调）：
        不查外部状态、不改 attempt、不再写任何流水。
        """

        if self._status_of(generation_id) in TERMINAL_STATUSES:
            return OUTCOME_REPLAYED

        target = self._poll_target(generation_id, provider_request_id)
        if target is None:
            return OUTCOME_NOT_CLAIMABLE
        attempt_no, external_id = target

        try:
            result = self._provider_factory().check_status(external_id)
        except ProviderCallError as exc:
            # 轮询本身也可能超时/无法解析：与 `generate` 走同一条分类路径，
            # 绝不把未处理的异常抛回 Worker（第 3、4 节）
            return self._handle_call_error(generation_id, attempt_no, exc)

        return self._apply(generation_id, attempt_no, result)

    def reconcile_event(self, event: object) -> str:
        """Webhook 入口：事件已由认证器验签并归一，这里只按外部 ID 关联。"""

        provider_name = str(getattr(event, "provider_name", ""))
        external_id = str(getattr(event, "provider_request_id", ""))
        result = getattr(event, "result")

        with self._uow_factory() as uow:
            attempt = uow.provider_attempts.find_by_request_id(
                provider_name=provider_name, request_id=external_id
            )
            if attempt is None:
                # 第 4 节第 3 条：关联不到已知 attempt 的事件一律拒绝
                raise ApiError(ErrorCode.WEBHOOK_INVALID)
            generation_id, attempt_no = attempt.generation_id, attempt.attempt_no

        return self._apply(generation_id, attempt_no, result)

    def cancel_processing(self, generation_id: uuid.UUID) -> str:
        """`PROCESSING` 任务的内部取消：接受取消不等于取消成功（第 2 节）。

        顺序刻意是「只读定位 → 事务外调用 `cancel` → 按观察到的终局结算」：
        `_poll_target` 在自己的短事务里只取 `attempt_no` 与外部 ID，退出即结束事务，
        因此 Provider 调用绝不落在数据库事务内（`LEAD-04` §6:121）。

        观察结果是唯一依据：确认取消（明确失败）才 `FAILED + RELEASED` 并记
        `CANCELLED_BY_USER`；仍 `PENDING`、取消请求本身超时或无法解析都归
        `NEEDS_REVIEW + RESERVED`；已经成功的结果照常按成功结算——取消晚于完成
        时点数本就该被消费。
        """

        target = self._poll_target(generation_id, None)
        if target is None:
            return OUTCOME_NOT_CLAIMABLE
        attempt_no, external_id = target

        try:
            observed = self._provider_factory().cancel(external_id)
        except ProviderCallError:
            return self._to_needs_review(generation_id, attempt_no, reason="cancel_unconfirmed")

        if observed.state is ProviderState.PENDING:
            return self._to_needs_review(generation_id, attempt_no, reason="cancel_unconfirmed")
        if observed.state is ProviderState.FAILED and observed.error is not None:
            if classify(observed.error).value == "FAILURE":
                return self._apply(
                    generation_id, attempt_no, observed, failure_code=CANCEL_FAILURE_CODE
                )
        return self._apply(generation_id, attempt_no, observed)

    # ------------------------------------------------------------------ 扫描
    def resume_stale(self, *, older_than_seconds: int, limit: int = 50) -> int:
        """补投「已提交但投递丢失」的任务；不动任何账务。"""

        threshold = self._clock() - timedelta(seconds=older_than_seconds)
        with self._uow_factory() as uow:
            stale = uow.generation_tasks.list_stale(
                statuses=(CREATED, QUEUED), older_than=threshold, limit=limit
            )
            ids = [task.id for task in stale]

        resumed = 0
        for generation_id in ids:
            if self._queue is not None and self._queue.enqueue_generation(generation_id):
                resumed += 1
        return resumed

    def enforce_deadlines(self, *, limit: int = 50, deadline_seconds: int | None = None) -> int:
        """把超过整体时限仍无结论的处理归为 attempt `UNKNOWN` + `NEEDS_REVIEW`。

        `deadline_seconds=None` 用装配时的整体时限；Beat 可以按运行时配置覆盖它，
        否则调用方传进来的时限会被静默丢弃。
        """

        budget = (
            self._overall_deadline_seconds
            if deadline_seconds is None
            else deadline_seconds
        )
        threshold = self._clock() - timedelta(seconds=budget)
        with self._uow_factory() as uow:
            timed_out = uow.generation_tasks.list_timed_out(
                status=PROCESSING, processing_before=threshold, limit=limit
            )
            ids = [task.id for task in timed_out]

        return sum(
            1
            for generation_id in ids
            if self.mark_deadline_unknown(generation_id) == OUTCOME_NEEDS_REVIEW
        )

    def run_recovery_scan(
        self, *, stale_after_seconds: int, deadline_seconds: int, limit: int = 50
    ) -> dict[str, int]:
        """Beat 的单一恢复入口：先补投丢失投递，再判定超时。"""

        return {
            "reenqueued": self.resume_stale(
                older_than_seconds=stale_after_seconds, limit=limit
            ),
            "needs_review": self.enforce_deadlines(
                limit=limit, deadline_seconds=deadline_seconds
            ),
        }

    def mark_deadline_unknown(self, generation_id: uuid.UUID) -> str:
        """单个任务的超时归一：绝不伪造终态，只转人工复核。"""

        with self._uow_factory() as uow:
            task = uow.generation_tasks.lock_by_id(generation_id)
            if task is None or task.status != PROCESSING:
                return OUTCOME_NOT_CLAIMABLE
            attempt = self._open_attempt(uow, generation_id)
            if attempt is None:
                return OUTCOME_NOT_CLAIMABLE
            self._finish_attempt(
                uow,
                attempt,
                status=_ATTEMPT_TIMEOUT,
                error_code="deadline_exceeded",
                error_message="超过整体处理时限仍无法确认外部状态。",
            )
            self._transition(uow, task, NEEDS_REVIEW)
            uow.commit()
        return OUTCOME_NEEDS_REVIEW

    # ------------------------------------------------------------------ 事务
    def _claim(self, generation_id: uuid.UUID) -> _Claim | None:
        """短事务：领取 `QUEUED` 任务并登记一行 `STARTED` attempt。

        `PROCESSING` 的两种情形分开处理（`LEAD-04` §7:136）：

        - 仍有未终结的 attempt → 说明当前 attempt 的持有者还在跑，重投只复用它的
          attempt 序号继续查询，绝不并发再开一次外部请求；
        - 已经没有未终结的 attempt → 这是传输层重试的补投消息，按 R7 递增一条**新的**
          attempt 行继续（每次重试都必须留下独立记录）。
        """

        with self._uow_factory() as uow:
            task = uow.generation_tasks.lock_by_id(generation_id)
            if task is None:
                return None

            if task.status == PROCESSING:
                open_attempt = self._open_attempt(uow, generation_id)
                if open_attempt is not None:
                    # 重投：复用当前 attempt，只做观察。`resumed` 让 `submit` 不再提交外部请求
                    return _Claim(
                        generation_id=task.id,
                        user_id=task.user_id,
                        attempt_no=open_attempt.attempt_no,
                        client_request_id=self._client_request_id(
                            task.id, open_attempt.attempt_no
                        ),
                        provider_name=open_attempt.provider_name,
                        model_name=open_attempt.model_name,
                        mode=task.mode,
                        prompt=task.prompt,
                        output_count=task.requested_count,
                        resumed=True,
                        external_id=open_attempt.request_id,
                    )
            elif task.status != QUEUED:
                return None

            attempt_no = max(
                (
                    attempt.attempt_no
                    for attempt in uow.provider_attempts.list_by_generation_id(generation_id)
                ),
                default=0,
            ) + 1
            now = self._clock()
            provider_name = str(task.provider_name or "")
            model_name = str(task.model_name or "")

            uow.provider_attempts.create(
                ProviderAttemptDraft(
                    generation_id=task.id,
                    attempt_no=attempt_no,
                    provider_name=provider_name,
                    model_name=model_name,
                    status=_ATTEMPT_STARTED,
                    started_at=now,
                    # 只放允许白名单化的字段：Prompt、图片字节与凭据一律不进元数据
                    request_metadata={
                        "client_request_id": self._client_request_id(task.id, attempt_no),
                        "mode": task.mode,
                        "model": model_name,
                    },
                )
            )
            if task.status == QUEUED:
                self._transition(uow, task, PROCESSING)
            uow.commit()

        return _Claim(
            generation_id=generation_id,
            user_id=task.user_id,
            attempt_no=attempt_no,
            client_request_id=self._client_request_id(generation_id, attempt_no),
            provider_name=provider_name,
            model_name=model_name,
            mode=task.mode,
            prompt=task.prompt,
            output_count=task.requested_count,
        )

    def _apply(
        self,
        generation_id: uuid.UUID,
        attempt_no: int,
        result: ProviderResult,
        *,
        failure_code: str | None = None,
    ) -> str:
        """把一次归一结果落库并结算（同一事务）。

        `failure_code` 只用于取消路径把 `FAILED` 的失败归类改写成
        `CANCELLED_BY_USER`：结算判定仍然完全来自 `classify()`，改写的是记账原因，
        不是账务规则。
        """

        if result.state is ProviderState.PENDING:
            return self._mark_pending(generation_id, attempt_no, result)

        try:
            return self._settle(generation_id, attempt_no, result, failure_code=failure_code)
        except ApiError as exc:
            # 结果不可用（校验失败、资源缺失）：整体回滚后只转人工复核
            return self._to_needs_review(
                generation_id, attempt_no, reason=str(exc.details.get("reason") or exc.code.value)
            )

    def _mark_pending(
        self, generation_id: uuid.UUID, attempt_no: int, result: ProviderResult
    ) -> str:
        """已接受但未出结果：attempt `SUBMITTED` + 外部 ID，任务保持 `PROCESSING`。"""

        if result.provider_request_id is None:
            # `PENDING` 不带外部 ID 就无法轮询/关联，属于不可确认状态
            return self._to_needs_review(
                generation_id, attempt_no, reason="pending_without_external_id"
            )

        with self._uow_factory() as uow:
            task = uow.generation_tasks.lock_by_id(generation_id)
            attempt = uow.provider_attempts.get_by_generation_and_attempt(
                generation_id, attempt_no
            )
            if task is None or attempt is None or task.status in TERMINAL_STATUSES:
                return OUTCOME_REPLAYED
            if not self._is_current_open_attempt(uow, generation_id, attempt):
                return OUTCOME_REPLAYED
            uow.provider_attempts.update(
                attempt,
                status=_ATTEMPT_SUBMITTED,
                request_id=str(result.provider_request_id),
            )
            uow.commit()

        if self._queue is not None:
            self._queue.enqueue_status_poll(
                generation_id,
                attempt_no=attempt_no,
                countdown_seconds=self._poll_interval_seconds,
            )
        return OUTCOME_PENDING

    def _settle(
        self,
        generation_id: uuid.UUID,
        attempt_no: int,
        result: ProviderResult,
        *,
        failure_code: str | None = None,
    ) -> str:
        """终局结算：成功消费、明确失败释放、不可确认转人工复核。"""

        with self._uow_factory() as uow:
            task = uow.generation_tasks.lock_by_id(generation_id)
            attempt = uow.provider_attempts.get_by_generation_and_attempt(
                generation_id, attempt_no
            )
            if task is None or attempt is None:
                return OUTCOME_NOT_CLAIMABLE
            if task.status in TERMINAL_STATUSES:
                # 终态单调：重复或迟到事件在首次已提交结算后是 no-op
                return OUTCOME_REPLAYED
            if not self._is_current_open_attempt(uow, generation_id, attempt):
                # 迟到的旧 attempt 结果：既不写结果也不结算当前任务
                return OUTCOME_REPLAYED

            points = self._points_factory(uow)

            if result.state is ProviderState.SUCCEEDED:
                if not result.outputs:
                    # 第 2 节：零输出不得按成功计费
                    return self._needs_review(uow, task, attempt, reason="empty_result")
                persist_provider_outputs(
                    uow,
                    storage=self._storage,
                    storage_provider=self._storage_provider,
                    task=task,
                    outputs=tuple(result.outputs),
                    now=self._clock,
                )
                persisted = len(uow.generation_tasks.list_outputs(generation_id))
                if persisted < task.requested_count:
                    return self._needs_review(
                        uow, task, attempt, reason="incomplete_result"
                    )
                points.consume(
                    user_id=task.user_id,
                    generation_id=task.id,
                    idempotency_key=ledger_key("consume", task.id),
                    reason="generation_succeeded",
                    metadata={"attempt_no": attempt_no},
                )
                self._finish_attempt(
                    uow, attempt, status=_ATTEMPT_SUCCEEDED, result=result
                )
                self._transition(uow, task, SUCCEEDED)
                uow.commit()
                return OUTCOME_SUCCEEDED

            if result.error is None:
                return self._needs_review(uow, task, attempt, reason="unclassified_failure")

            disposition = classify(result.error)
            if disposition.value == "RETRY":
                return self._schedule_transport_retry(uow, task, attempt, result)
            if disposition.value == "FAILURE":
                points.release(
                    user_id=task.user_id,
                    generation_id=task.id,
                    idempotency_key=ledger_key("release", task.id),
                    reason="generation_failed",
                    metadata={"failure_category": result.error.category.value},
                )
                self._finish_attempt(
                    uow,
                    attempt,
                    status=_ATTEMPT_FAILED,
                    result=result,
                    error_code=result.error.code,
                    error_message=result.error.safe_message,
                )
                self._transition(
                    uow,
                    task,
                    FAILED,
                    failure_code=failure_code or result.error.category.value,
                    failure_message=result.error.safe_message,
                )
                uow.commit()
                return OUTCOME_FAILED
            return self._needs_review(
                uow,
                task,
                attempt,
                reason="provider_outcome_unknown",
                attempt_status=(
                    _ATTEMPT_TIMEOUT
                    if result.error.category.value == "TIMEOUT"
                    else _ATTEMPT_UNKNOWN
                ),
                result=result,
            )

    def _schedule_transport_retry(
        self,
        uow: UnitOfWork,
        task: GenerationTask,
        attempt: ProviderAttempt,
        result: ProviderResult,
    ) -> str:
        """传输层重试：留下独立 attempt 行，绝不改写已提交的历史。"""

        error = result.error
        assert error is not None
        if task.retry_count >= self._max_transport_retries:
            # 重试预算耗尽：可证明未接受，因此按明确失败放款一次
            self._points_factory(uow).release(
                user_id=task.user_id,
                generation_id=task.id,
                idempotency_key=ledger_key("release", task.id),
                reason="generation_retry_exhausted",
                metadata={"failure_category": error.category.value},
            )
            self._finish_attempt(
                uow, attempt, status=_ATTEMPT_FAILED, result=result,
                error_code=error.code, error_message=error.safe_message,
            )
            self._transition(
                uow, task, FAILED, failure_code=error.category.value,
                failure_message=error.safe_message,
            )
            uow.commit()
            return OUTCOME_FAILED

        retry_no = task.retry_count
        delay = self._backoff_seconds[min(retry_no, len(self._backoff_seconds) - 1)]
        self._finish_attempt(
            uow,
            attempt,
            status=_ATTEMPT_TIMEOUT if error.category.value == "TIMEOUT" else _ATTEMPT_FAILED,
            result=result,
            error_code=error.code,
            error_message=error.safe_message,
        )
        uow.generation_tasks.record_retry(task, changed_at=self._clock())
        uow.commit()

        if self._queue is not None:
            self._queue.enqueue_generation(task.id, countdown_seconds=delay)
        return OUTCOME_RETRY_SCHEDULED

    def _handle_call_error(
        self,
        generation_id: uuid.UUID,
        attempt_no: int,
        error: ProviderCallError,
    ) -> str:
        """`ProviderCallError`：只有可证明未接受的提交前超时可重试。

        `generate` 与 `check_status` 两条入口共用本方法：轮询同样可能超时或无法解析，
        绝不能把未处理异常抛回 Worker（`04-ai-provider.md` 第 3 节）。
        """

        disposition = classify(error)
        with self._uow_factory() as uow:
            task = uow.generation_tasks.lock_by_id(generation_id)
            attempt = uow.provider_attempts.get_by_generation_and_attempt(
                generation_id, attempt_no
            )
            if task is None or attempt is None:
                return OUTCOME_NOT_CLAIMABLE
            if task.status in TERMINAL_STATUSES:
                return OUTCOME_REPLAYED
            if not self._is_current_open_attempt(uow, generation_id, attempt):
                return OUTCOME_REPLAYED

            if disposition.value == "RETRY":
                if task.retry_count >= self._max_transport_retries:
                    # 预算耗尽。`RETRY` 归类的含义正是「可证明没有外部执行」，
                    # 因此这是**明确失败**：按 `LEAD-04` §1 放款一次并留 `FAILED` 终态，
                    # 不能拖成人工复核——那会把一次确定没发生的调用挂成待定。
                    points = self._points_factory(uow)
                    points.release(
                        user_id=task.user_id,
                        generation_id=task.id,
                        idempotency_key=ledger_key("release", task.id),
                        reason="generation_retry_exhausted",
                        metadata={"failure_category": error.category.value},
                    )
                    self._finish_attempt(
                        uow,
                        attempt,
                        status=_ATTEMPT_TIMEOUT,
                        error_code=error.code,
                        error_message=error.safe_message,
                    )
                    self._transition(
                        uow,
                        task,
                        FAILED,
                        failure_code=error.category.value,
                        failure_message=error.safe_message,
                    )
                    uow.commit()
                    return OUTCOME_FAILED

                delay = self._backoff_seconds[
                    min(task.retry_count, len(self._backoff_seconds) - 1)
                ]
                self._finish_attempt(
                    uow,
                    attempt,
                    status=_ATTEMPT_TIMEOUT,
                    error_code=error.code,
                    error_message=error.safe_message,
                )
                uow.generation_tasks.record_retry(task, changed_at=self._clock())
                uow.commit()
                if self._queue is not None:
                    self._queue.enqueue_generation(task.id, countdown_seconds=delay)
                return OUTCOME_RETRY_SCHEDULED

            self._finish_attempt(
                uow,
                attempt,
                status=_ATTEMPT_UNKNOWN,
                error_code=error.code,
                error_message=error.safe_message,
            )
            self._transition(uow, task, NEEDS_REVIEW)
            uow.commit()
        return OUTCOME_NEEDS_REVIEW

    def _to_needs_review(
        self,
        generation_id: uuid.UUID,
        attempt_no: int,
        *,
        reason: str,
        attempt_status: str = _ATTEMPT_UNKNOWN,
    ) -> str:
        """新开一个事务转人工复核（前一个事务已因异常回滚）。

        核对与结算共用同一把任务锁：只有**当前仍未终结的 attempt** 才能被转成
        `NEEDS_REVIEW`，已被后续 attempt 取代的迟到结论什么都不写。
        """

        with self._uow_factory() as uow:
            task = uow.generation_tasks.lock_by_id(generation_id)
            attempt = uow.provider_attempts.get_by_generation_and_attempt(
                generation_id, attempt_no
            )
            if task is None or attempt is None:
                return OUTCOME_NOT_CLAIMABLE
            if task.status in TERMINAL_STATUSES or attempt.status not in _OPEN_ATTEMPT_STATUSES:
                return OUTCOME_REPLAYED
            if not self._is_current_open_attempt(uow, generation_id, attempt):
                # 已被更新的 attempt 取代：迟到的结果既不结算也不转人工复核
                return OUTCOME_REPLAYED
            return self._needs_review(
                uow, task, attempt, reason=reason, attempt_status=attempt_status
            )

    def _needs_review(
        self,
        uow: UnitOfWork,
        task: GenerationTask,
        attempt: ProviderAttempt,
        *,
        reason: str,
        attempt_status: str = _ATTEMPT_UNKNOWN,
        result: ProviderResult | None = None,
    ) -> str:
        """`NEEDS_REVIEW + RESERVED`：不自动消费、不自动退款（`LEAD-04` §1）。"""

        self._finish_attempt(
            uow,
            attempt,
            status=attempt_status,
            result=result,
            error_code=reason,
            error_message="外部结果无法确认，转人工复核。",
        )
        self._transition(uow, task, NEEDS_REVIEW)
        uow.commit()
        return OUTCOME_NEEDS_REVIEW

    # ------------------------------------------------------------------ 内部
    def _status_of(self, generation_id: uuid.UUID) -> str | None:
        """只读取任务状态：用于「终态任务上的重复投递是 no-op」判定，不开写事务。"""

        with self._uow_factory() as uow:
            task = uow.generation_tasks.get_by_id(generation_id)
            return None if task is None else task.status

    def _poll_target(
        self, generation_id: uuid.UUID, provider_request_id: str | None
    ) -> tuple[int, str] | None:
        """取**当前开放 attempt** 的外部 ID；对不上就不给轮询。

        轮询与 Webhook 一样只能在任务锁下核对当前 attempt：拿旧 attempt 的外部 ID 来
        查询会把别人的结论当成当前请求的结果来结算。
        """

        with self._uow_factory() as uow:
            task = uow.generation_tasks.lock_by_id(generation_id)
            if task is None or task.status != PROCESSING:
                return None
            attempt = self._open_attempt(uow, generation_id)
            if attempt is None or attempt.request_id is None:
                return None
            if provider_request_id is not None and str(provider_request_id) != str(
                attempt.request_id
            ):
                return None
            return attempt.attempt_no, str(attempt.request_id)

    @staticmethod
    def _open_attempt(uow: UnitOfWork, generation_id: uuid.UUID) -> ProviderAttempt | None:
        """最新一条仍未终结的 attempt。"""

        candidates = [
            attempt
            for attempt in uow.provider_attempts.list_by_generation_id(generation_id)
            if attempt.status in _OPEN_ATTEMPT_STATUSES
        ]
        return max(candidates, key=lambda attempt: attempt.attempt_no) if candidates else None

    @classmethod
    def _is_current_open_attempt(
        cls, uow: UnitOfWork, generation_id: uuid.UUID, attempt: ProviderAttempt
    ) -> bool:
        """给定的 attempt 是否就是当前仍未终结的那一条（调用方必须已持有任务锁）。

        「终态单调」只挡住已结算的任务；没挡住的是**同一个任务上更晚开始的 attempt**：
        旧请求的迟到回调若被接受，就会用别人的结果给当前请求记账——消费一次、
        结果却来自另一条外部请求。因此轮询、Webhook、`PENDING` 与终局结算都必须
        在任务锁下核对当前开放 attempt（`04-ai-provider.md` 第 4 节第 4 条）。
        """

        if attempt.status not in _OPEN_ATTEMPT_STATUSES:
            return False
        current = cls._open_attempt(uow, generation_id)
        return current is not None and current.attempt_no == attempt.attempt_no

    def _finish_attempt(
        self,
        uow: UnitOfWork,
        attempt: ProviderAttempt,
        *,
        status: str,
        result: ProviderResult | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        """终结 attempt：用量与成本未知时保持 `NULL`，绝不伪造成 0（第 5 节）。"""

        changes: dict[str, object] = {"status": status, "finished_at": self._clock()}
        usage = getattr(result, "usage", None)
        if usage is not None:
            changes.update(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                image_count=usage.image_count,
                estimated_cost=usage.estimated_cost,
                currency=usage.currency,
            )
        response_metadata: dict[str, object] = {}
        if result is not None:
            response_metadata = {"state": result.state.value}
            if result.provider_request_id is not None:
                response_metadata["provider_request_id"] = str(result.provider_request_id)
                changes.setdefault("request_id", str(result.provider_request_id))
            response_metadata["output_count"] = len(tuple(result.outputs or ()))
        if response_metadata:
            merged = dict(attempt.response_metadata or {})
            merged.update(response_metadata)
            changes["response_metadata"] = merged
        if error_code is not None:
            changes["error_code"] = error_code
        if error_message is not None:
            changes["error_message"] = error_message
        uow.provider_attempts.update(attempt, **changes)

    def _transition(
        self,
        uow: UnitOfWork,
        task: GenerationTask,
        target: str,
        *,
        failure_code: str | None = None,
        failure_message: str | None = None,
    ) -> None:
        """唯一的生命周期写入口，非法转换在此拦住且不写任何流水。"""

        assert_transition(task.status, target)
        uow.generation_tasks.transition(
            task,
            status=target,
            changed_at=self._clock(),
            failure_code=failure_code,
            failure_message=failure_message,
        )

    @staticmethod
    def _client_request_id(generation_id: uuid.UUID, attempt_no: int) -> str:
        """一次逻辑 attempt 的相关性键：传输层重试与状态查询期间保持稳定。"""

        return f"{generation_id}:{attempt_no}"
