"""Generation Service：创建、重试与读取（ENG-06 第 3 步）。

规则来源：`LEAD-04` §1/§2/§3/§6/§7/§11、`03-api-contract.md` 第 4.3/9 节、
`docs/handoff/2026-09-27-current-handoff.md` §4。

本类只做**用户侧**编排（创建、重试、读取、内部取消）。
Worker 与回调的执行编排在 `orchestrator.py`，因为它们的边界不同：

- 本类的一个方法 = 一个事务，且 Provider 调用完全不出现；
- `GenerationOrchestrator` 必须是「先短事务领取 → 事务外调用 Provider →
  再一个事务落结果并结算」，因为 `LEAD-04` §6:121 明确禁止把网络调用放进事务。

账务边界：本类**只调用** Points Service，不写 `point_ledger`、不改钱包快照；
任务生命周期只通过 `uow.generation_tasks.transition` 这一个写入口。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.db.models.generation import GenerationTask
from app.errors.codes import ErrorCode
from app.errors.exceptions import ApiError
from app.repositories.generation import (
    DuplicateIdempotencyKey,
    GenerationInputDraft,
    GenerationTaskDraft,
)
from app.repositories.unit_of_work import UnitOfWork
from app.services.generation.commands import NewGenerationCommand
from app.services.generation.costs import resolve_cost
from app.services.generation.idempotency import (
    CREATE_CANONICAL_PATH,
    RETRY_CANONICAL_PATH,
    fingerprint_of,
    replay_decision,
)
from app.services.generation.mappers import GenerationOutputView, to_output_view
from app.services.generation.ports import GenerationQueuePort
from app.services.generation.state import (
    CREATED,
    FAILED,
    NEEDS_REVIEW,
    PROCESSING,
    QUEUED,
    TERMINAL_STATUSES,
)
from app.services.points.service import PointsService

__all__ = ["GenerationOutcome", "GenerationService", "Page"]

_LOGGER = logging.getLogger("ai_avatar.generation")

#: 用户取消的内部失败归类（`generation_tasks.failure_code` 是非枚举字符串列，
#: 不是新的对外错误码；`LEAD-04` §4 冻结该取值）
CANCEL_FAILURE_CODE = "CANCELLED_BY_USER"

#: 参与幂等指纹的 canonical 路径（`03-api-contract.md` 第 4.1、9 节）
CREATE_CANONICAL_PATH = "/api/v1/generations"
RETRY_CANONICAL_PATH = "/api/v1/generations/{generation_id}/retry"

#: 输入资源必须已 `READY`；`media_assets.status` 取值见 `02-database-design.md` 第 3.3 节
_READY_STATUS = "READY"

#: `generation_inputs.role` 只有 `SOURCE`/`REFERENCE`（`02-database-design.md` 第 3.2 节）；
#: 参考图数量上限沿用每次 2 张的结果数上限，超过即拒绝而不是静默截断（`LEAD-05`）
_MAX_REFERENCES = 2


def ledger_key(kind: str, generation_id: uuid.UUID) -> str:
    """账务幂等键。

    `point_ledger.idempotency_key` 是**全局唯一**列，因此不能直接用用户的 HTTP 幂等键
    （两个用户可能提交同样的字符串）；由任务身份派生既保证「每任务最多一条 RESERVE、
    最多一条终局流水」，也让重复投递自然回放。
    """

    return f"generation-{kind}:{generation_id}"


@dataclass(frozen=True, slots=True)
class GenerationOutcome:
    """创建/重试的结果。`replayed=True` 表示本次没有产生任何新副作用。"""

    task: GenerationTask
    replayed: bool


@dataclass(frozen=True, slots=True)
class Page:
    """键集分页结果。"""

    rows: tuple[GenerationTask, ...]
    next_cursor: str | None
    has_more: bool


class GenerationService:
    """生成任务的用户侧编排。"""

    def __init__(
        self,
        *,
        uow: UnitOfWork,
        points: PointsService,
        clock: Callable[[], datetime] | None = None,
        queue: GenerationQueuePort | None = None,
        enable_reference_image: bool = False,
        provider_name: str = "MOCK",
        model_name: str = "mock-image-v1",
        logger: logging.Logger | None = None,
    ) -> None:
        self._uow = uow
        self._points = points
        self._clock: Callable[[], datetime] = clock or (lambda: datetime.now(timezone.utc))
        self._queue = queue
        self._enable_reference_image = enable_reference_image
        self._provider_name = provider_name
        self._model_name = model_name
        self._logger = logger or _LOGGER

    # ------------------------------------------------------------------ 创建
    def create_generation(
        self,
        *,
        user_id: uuid.UUID,
        command: NewGenerationCommand,
        idempotency_key: str,
        canonical_path: str = CREATE_CANONICAL_PATH,
        parent_task_id: uuid.UUID | None = None,
    ) -> GenerationOutcome:
        """按 `LEAD-04` §6 的顺序创建任务并预留点数。

        一个事务内：锁钱包 → 校验余额 → 建任务 → 写 `RESERVE` → 更新钱包快照 → 提交；
        Celery 投递只在提交之后发生，投递丢失不回滚已提交账务（`LEAD-04` §6:119）。
        """

        self._require_verified_email(user_id)
        self._require_mode_allowed(command)
        self._require_command_key(idempotency_key)

        # 顺序即语义：幂等判定与资源校验都必须早于成本解析与钱包加锁。
        # 回放请求不得改动任何账务，非法输入也不该留下任何锁的痕迹（R1/R2/R10）
        fingerprint = fingerprint_of(
            "POST",
            canonical_path,
            self._request_payload(command, parent_task_id=parent_task_id),
        )
        decision = replay_decision(
            self._uow.generation_tasks.find_by_idempotency_key(
                user_id=user_id, idempotency_key=idempotency_key
            ),
            fingerprint=fingerprint,
            canonical_path=canonical_path,
            row_input_asset_ids=self._row_input_asset_ids,
        )
        if decision.replay_from is not None:
            # 同作用域同键同指纹且首次已完成：原样回放，不重复预留也不重复投递
            return GenerationOutcome(task=decision.replay_from, replayed=True)

        inputs = self._resolve_input_assets(user_id=user_id, command=command)

        requested_at = self._clock()
        cost = resolve_cost(
            self._uow.generation_costs,
            mode=command.mode,
            output_count=command.output_count,
            at=requested_at,
            # 必须带上本进程实际会用的型号：定价规则是「精确型号优先，NULL 兜底」，
            # 不传型号就只剩 NULL 一条路，于是「只给某个型号定价」的部署形态会在这里
            # 报 503，而 `/health/ready`（它传了型号）却是绿的 —— 两处口径必须一致。
            model_name=self._model_name,
        )

        wallet = self._uow.point_wallets.lock_by_user_id(user_id)
        available = wallet.available_points if wallet is not None else 0
        if available < cost.reserved_points:
            # 失败即结束本事务并释放刚拿到的钱包行锁；`LEAD-04` §6:126 要求
            # 事务失败整体回滚，绝不把锁留到请求结束、也不留下半截写入
            self._uow.rollback()
            raise ApiError(ErrorCode.INSUFFICIENT_POINTS, details={"available_points": available})

        try:
            task = self._write_task(
                user_id=user_id,
                command=command,
                idempotency_key=idempotency_key,
                cost=cost,
                fingerprint=fingerprint,
                inputs=inputs,
                parent_task_id=parent_task_id,
                at=requested_at,
            )
        except DuplicateIdempotencyKey:
            return self._resolve_lost_race(
                user_id=user_id,
                idempotency_key=idempotency_key,
                fingerprint=fingerprint,
                canonical_path=canonical_path,
            )

        self._dispatch(task.id)
        return GenerationOutcome(task=task, replayed=False)

    def _write_task(
        self,
        *,
        user_id: uuid.UUID,
        command: NewGenerationCommand,
        idempotency_key: str,
        cost: ResolvedCost,
        fingerprint: str,
        inputs: tuple[tuple[uuid.UUID, str, int], ...],
        parent_task_id: uuid.UUID | None,
        at: datetime,
    ) -> GenerationTask:
        """事务体：建任务 → 记输入资源 → 预留 → 置 `QUEUED` → 提交。"""

        task = self._uow.generation_tasks.create(
            GenerationTaskDraft(
                user_id=user_id,
                mode=command.mode,
                prompt=command.prompt,
                requested_count=command.output_count,
                cost_config_id=cost.cost_config_id,
                cost_snapshot={**cost.snapshot, "request_fingerprint": fingerprint},
                reserved_points=cost.reserved_points,
                idempotency_key=idempotency_key,
                status=CREATED,
                billing_status="RESERVED",
                style_id=command.style_id,
                prompt_template_id=command.prompt_template_id,
                parent_task_id=parent_task_id,
                # V1.0 固定单一 Provider：创建时冻结，重试与查询不得改判
                provider_name=self._provider_name,
                model_name=self._model_name,
            ),
            created_at=at,
        )

        for asset_id, role, sort_order in inputs:
            self._uow.generation_tasks.create_input(
                GenerationInputDraft(
                    generation_id=task.id, asset_id=asset_id, role=role, sort_order=sort_order
                )
            )

        self._points.reserve(
            user_id=user_id,
            generation_id=task.id,
            amount=cost.reserved_points,
            idempotency_key=ledger_key("reserve", task.id),
            reason="generation_reserve",
        )
        self._uow.generation_tasks.transition(task, status=QUEUED, changed_at=self._clock())
        self._uow.commit()
        return task

    def _resolve_lost_race(
        self,
        *,
        user_id: uuid.UUID,
        idempotency_key: str,
        fingerprint: str,
        canonical_path: str = CREATE_CANONICAL_PATH,
    ) -> GenerationOutcome:
        """唯一键裁决后重查：回滚未提交的一切，再按胜出行判定回放/冲突/处理中。"""

        self._uow.rollback()
        decision = replay_decision(
            self._uow.generation_tasks.find_by_idempotency_key(
                user_id=user_id, idempotency_key=idempotency_key
            ),
            fingerprint=fingerprint,
            canonical_path=canonical_path,
            row_input_asset_ids=self._row_input_asset_ids,
        )
        if decision.replay_from is not None:
            return GenerationOutcome(task=decision.replay_from, replayed=True)
        raise ApiError(ErrorCode.INTERNAL_ERROR, details={"reason": "idempotency_race_lost"})

    def _row_input_asset_ids(self, task: GenerationTask) -> tuple[uuid.UUID, ...]:
        """胜出行自己的输入资源（重算它的指纹时用，不看本次请求）。"""

        return tuple(row.asset_id for row in self._uow.generation_tasks.list_inputs(task.id))

    @staticmethod
    def _request_payload(
        command: NewGenerationCommand, *, parent_task_id: uuid.UUID | None
    ) -> dict[str, object]:
        """本次请求的规范化载荷：命令载荷 + 父任务，字段集合与 `task_payload` 同形。"""

        payload: dict[str, object] = dict(command.fingerprint_payload())
        payload["parent_task_id"] = str(parent_task_id) if parent_task_id is not None else None
        return payload

    def _dispatch(self, generation_id: uuid.UUID) -> None:
        """提交后投递；投递失败只记安全摘要，由恢复扫描补投（不回滚已提交账务）。"""

        if self._queue is None:
            return
        if not self._queue.enqueue_generation(generation_id):
            # 日志只含任务 ID 与事件名：不含 Prompt、载荷或任何凭据
            self._logger.warning("generation_dispatch_failed generation_id=%s", generation_id)

    # ------------------------------------------------------------------ 重试
    def retry_generation(
        self, *, user_id: uuid.UUID, generation_id: uuid.UUID, idempotency_key: str
    ) -> GenerationOutcome:
        """用户重试：只接受 `FAILED`，创建**新任务**并挂父任务。

        旧任务保持 `FAILED + RELEASED` 原样；`NEEDS_REVIEW` 必须先由人工结算，
        不得被重试绕过（`LEAD-04` §7:138）。
        """

        parent = self.get_generation(generation_id=generation_id, requester_user_id=user_id)
        if parent.status != FAILED:
            raise ApiError(ErrorCode.GENERATION_NOT_RETRYABLE)

        command = NewGenerationCommand(
            mode=parent.mode,
            prompt=parent.prompt,
            output_count=parent.requested_count,
            style_id=parent.style_id,
            prompt_template_id=parent.prompt_template_id,
            input_asset_ids=tuple(
                row.asset_id for row in self._uow.generation_tasks.list_inputs(parent.id)
            ),
        )
        return self.create_generation(
            user_id=user_id,
            command=command,
            idempotency_key=idempotency_key,
            canonical_path=RETRY_CANONICAL_PATH,
            parent_task_id=parent.id,
        )

    # ------------------------------------------------------------------ 取消
    def cancel_internally(self, *, generation_id: uuid.UUID) -> GenerationTask:
        """内部取消：只处理**尚未调用 Provider** 的任务。

        `CREATED/QUEUED` 可证明没有外部执行，因此按 `LEAD-04` §4 归为
        `FAILED + RELEASED`；`PROCESSING` 的取消结果不可由本方法判定，必须走
        `GenerationOrchestrator.cancel_processing()`，否则会凭空退款。
        """

        task = self._uow.generation_tasks.lock_by_id(generation_id)
        if task is None:
            raise ApiError(ErrorCode.GENERATION_NOT_FOUND)
        if task.status in TERMINAL_STATUSES:
            return task
        if task.status == PROCESSING:
            raise ApiError(
                ErrorCode.IDEMPOTENCY_CONFLICT,
                details={"reason": "processing_cancel_requires_orchestrator"},
            )

        self._points.release(
            user_id=task.user_id,
            generation_id=task.id,
            idempotency_key=ledger_key("release", task.id),
            reason="generation_cancelled",
            metadata={"cancelled_by": "internal", "from_status": task.status},
        )
        self._uow.generation_tasks.transition(
            task, status=FAILED, changed_at=self._clock(), failure_code=CANCEL_FAILURE_CODE
        )
        self._uow.commit()
        return task

    # ------------------------------------------------------------------ 读取
    def get_generation(
        self, *, generation_id: uuid.UUID, requester_user_id: uuid.UUID
    ) -> GenerationTask:
        """按所有者读取；不存在与越权同样返回 404，不泄露任务是否存在。"""

        task = self._uow.generation_tasks.get_by_id(generation_id)
        if task is None or task.user_id != requester_user_id:
            raise ApiError(ErrorCode.GENERATION_NOT_FOUND)
        return task

    def list_generations(
        self, *, user_id: uuid.UUID, cursor: str | None, limit: int
    ) -> Page:
        """我的任务列表（`(created_at, id)` 键集分页）。"""

        page = self._uow.generation_tasks.list_for_user(
            user_id=user_id, cursor=_decode_cursor(cursor), limit=limit
        )
        last = page.rows[-1] if page.rows else None
        return Page(
            rows=page.rows,
            next_cursor=(
                _encode_cursor(last.created_at, last.id)
                if last is not None and page.has_more
                else None
            ),
            has_more=page.has_more,
        )

    def list_generation_outputs(
        self, *, generation_id: uuid.UUID, requester_user_id: uuid.UUID
    ) -> tuple[GenerationOutputView, ...]:
        """结果资源列表；只返回元数据，签名 URL 由既有资产接口另行签发。"""

        task = self.get_generation(
            generation_id=generation_id, requester_user_id=requester_user_id
        )
        views: list[GenerationOutputView] = []
        for output in self._uow.generation_tasks.list_outputs(task.id):
            asset = self._uow.media.get(output.asset_id)
            if asset is not None:
                views.append(to_output_view(output, asset))
        return tuple(views)

    # ------------------------------------------------------------------ 校验
    def _require_verified_email(self, user_id: uuid.UUID) -> None:
        """生成消耗点数，因此沿用与上传一致的邮箱已验证门禁。"""

        if self._uow.emails.find_verified_for_user(user_id) is None:
            raise ApiError(ErrorCode.EMAIL_NOT_VERIFIED)

    def _require_mode_allowed(self, command: NewGenerationCommand) -> None:
        """参考图在**任何写入与投递之前**按 Feature Flag 拒绝，不静默忽略。"""

        if command.mode == "REFERENCE_IMAGE" and not self._enable_reference_image:
            raise ApiError(ErrorCode.FEATURE_NOT_AVAILABLE)

    @staticmethod
    def _require_command_key(idempotency_key: str) -> None:
        """空键在 HTTP 层已被拦下；Worker/脚本直调时同样不得静默放行。"""

        if not str(idempotency_key).strip():
            raise ApiError(
                ErrorCode.IDEMPOTENCY_INVALID, details={"fields": ["Idempotency-Key"]}
            )

    def _resolve_input_assets(
        self, *, user_id: uuid.UUID, command: NewGenerationCommand
    ) -> tuple[tuple[uuid.UUID, str, int], ...]:
        """把输入资源解析成 `(asset_id, role, sort_order)`。

        角色取自资源自身的 `purpose`，不按调用方声明猜测：声明与实际用途不一致时必须
        拒绝，否则一次参考图请求可以悄悄变成一次原图生成。`sort_order` 按角色分别编号，
        与 `uq_generation_inputs_generation_role_sort` 唯一键一一对应，重放顺序稳定。
        """

        assets = tuple(command.input_asset_ids)

        if command.mode == "TEXT_PROMPT":
            if assets:
                raise ApiError(
                    ErrorCode.VALIDATION_ERROR, details={"fields": ["input_asset_ids"]}
                )
            return ()

        rows: list[tuple[uuid.UUID, str, int]] = []
        counters: dict[str, int] = {"SOURCE": 0, "REFERENCE": 0}

        for asset_id in assets:
            asset = self._uow.media.get(asset_id)
            if asset is None:
                # 先判存在再判归属：不存在的 ID 不泄露任何资源信息，别人的 ID 才按越权处理
                raise ApiError(ErrorCode.ASSET_NOT_FOUND)
            if self._uow.media.find_owner(asset_id) != user_id:
                raise ApiError(ErrorCode.ASSET_FORBIDDEN)
            if asset.purpose not in counters:
                raise ApiError(
                    ErrorCode.VALIDATION_ERROR, details={"fields": ["input_asset_ids"]}
                )
            if asset.status != _READY_STATUS:
                raise ApiError(ErrorCode.ASSET_NOT_READY)
            role = asset.purpose
            rows.append((asset_id, role, counters[role]))
            counters[role] += 1

        if command.mode == "PHOTO_PROMPT" and (
            counters["SOURCE"] != 1 or counters["REFERENCE"] != 0
        ):
            raise ApiError(ErrorCode.VALIDATION_ERROR, details={"fields": ["input_asset_ids"]})
        if (
            command.mode == "REFERENCE_IMAGE"
            and not 1 <= counters["REFERENCE"] <= _MAX_REFERENCES
        ):
            raise ApiError(ErrorCode.VALIDATION_ERROR, details={"fields": ["input_asset_ids"]})

        return tuple(sorted(rows, key=lambda row: (row[1], row[2])))


def _encode_cursor(created_at: datetime, task_id: uuid.UUID) -> str:
    return f"{created_at.isoformat()}|{task_id}"


def _decode_cursor(raw: str | None) -> tuple[datetime, uuid.UUID] | None:
    """游标由服务端生成，客户端只能原样回传；被篡改时按非法输入拒绝。"""

    if raw is None:
        return None
    try:
        stamp, _, identifier = raw.partition("|")
        moment = datetime.fromisoformat(stamp)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment, uuid.UUID(identifier)
    except ValueError as exc:
        raise ApiError(ErrorCode.VALIDATION_ERROR, details={"fields": ["cursor"]}) from exc
