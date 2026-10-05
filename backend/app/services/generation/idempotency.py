"""写请求的幂等判定（ENG-06 第 3 步）。

契约（`03-api-contract.md` 第 9 节、第 9.1 节）：

- `request_fingerprint = SHA-256(method + canonical_path + normalized_json_body)`，
  原始请求体不入库、不入日志；
- 创建类写入的作用域是 `user_id + method + path + key`；本卡把它落到
  `generation_tasks` 上既有的 `(user_id, idempotency_key)` 唯一索引，
  **不新增表、不新增列、不扩展 Redis 端口**（总控 2026-09-28 裁决）；
- 同作用域同键同指纹且首次已完成 → 回放原任务（不重复写入、不重复预留、不重复投递）；
- 同键不同指纹 → `IDEMPOTENCY_CONFLICT`（409）；
- 首次请求仍在处理中 → `IDEMPOTENCY_IN_PROGRESS`（409）。

「处理中」的可观测事实就是任务行还停在 `CREATED`：`CREATED -> QUEUED` 与
`RESERVE` 在同一事务内提交，因此提交后不可能再停留在 `CREATED`。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from app.db.models.generation import GenerationTask
from app.errors.codes import ErrorCode
from app.errors.exceptions import ApiError

__all__ = [
    "CREATE_CANONICAL_PATH",
    "IdempotencyDecision",
    "RETRY_CANONICAL_PATH",
    "fingerprint_of",
    "replay_decision",
    "task_payload",
]

#: 参与指纹的 canonical 路径（`03-api-contract.md` 第 4.1 节）
CREATE_CANONICAL_PATH = "/api/v1/generations"
RETRY_CANONICAL_PATH = "/api/v1/generations/{generation_id}/retry"

#: 仍被视为「首次请求在处理中」的任务状态（见模块 docstring）
_IN_FLIGHT_STATUS = "CREATED"


@dataclass(frozen=True, slots=True)
class IdempotencyDecision:
    """幂等判定结果：`replay_from` 非空表示必须回放而不是重新执行。"""

    replay_from: GenerationTask | None
    fingerprint: str


def fingerprint_of(*parts: Any) -> str:
    """按「方法 + canonical 路径 + 规范化 JSON 载荷」计算 SHA-256 指纹。

    规范化只依赖 `json.dumps(sort_keys=True)`：调用方传入的键顺序与
    `uuid`/`int` 混用都不会改变指纹，因此重放判定稳定。
    """

    payload = [part for part in parts]
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def task_payload(
    task: GenerationTask, input_asset_ids: Sequence[UUID]
) -> dict[str, Any]:
    """从任务行重算与命令侧同形的规范化载荷。

    `input_asset_ids` 排序后参与比较：提交顺序不影响业务结果（角色与 `sort_order`
    由服务端按稳定顺序落库），因此顺序不同不算「不同请求」。
    """

    return {
        "mode": task.mode,
        "prompt": task.prompt,
        "output_count": int(task.requested_count),
        "style_id": str(task.style_id) if task.style_id is not None else None,
        "prompt_template_id": (
            str(task.prompt_template_id) if task.prompt_template_id is not None else None
        ),
        "input_asset_ids": sorted(str(asset_id).lower() for asset_id in input_asset_ids),
        "parent_task_id": (
            str(task.parent_task_id) if task.parent_task_id is not None else None
        ),
    }


def replay_decision(
    existing: GenerationTask | None,
    *,
    fingerprint: str,
    method: str = "POST",
    canonical_path: str = CREATE_CANONICAL_PATH,
    row_input_asset_ids: Callable[[GenerationTask], Sequence[UUID]] = lambda task: (),
) -> IdempotencyDecision:
    """给出回放判定；「处理中」与冲突直接抛既有错误码。

    判定顺序刻意把「处理中」放在指纹比较之前：首次请求尚未落定终局时，比较载荷
    谁对谁错都没有意义，契约要求的答案就是 `IDEMPOTENCY_IN_PROGRESS`。

    胜出行的指纹由它自己的列加 `generation_inputs` 重算，**不落新列**：
    只比 `prompt` 不够——同一把键下换模式、换输出数量、换父任务都必须是冲突。
    """

    if existing is None:
        return IdempotencyDecision(replay_from=None, fingerprint=fingerprint)

    if existing.status == _IN_FLIGHT_STATUS:
        raise ApiError(
            ErrorCode.IDEMPOTENCY_IN_PROGRESS,
            details={"generation_id": str(existing.id)},
        )

    row_path = (
        RETRY_CANONICAL_PATH if existing.parent_task_id is not None else CREATE_CANONICAL_PATH
    )
    row_fingerprint = fingerprint_of(
        method, row_path, task_payload(existing, row_input_asset_ids(existing))
    )
    if row_fingerprint != fingerprint:
        raise ApiError(
            ErrorCode.IDEMPOTENCY_CONFLICT,
            details={"reason": "idempotency_key_reused_with_different_body"},
        )
    return IdempotencyDecision(replay_from=existing, fingerprint=fingerprint)


def stored_fingerprint_of(task: GenerationTask) -> str:
    """读取任务行上记录的请求指纹（随任务一起提交，不另建存储）。"""

    return _stored_fingerprint(task)


def _stored_fingerprint(task: GenerationTask) -> str:
    snapshot = task.cost_snapshot if isinstance(task.cost_snapshot, dict) else {}
    value = snapshot.get("request_fingerprint")
    return str(value) if value is not None else ""
