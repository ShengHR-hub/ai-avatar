"""DB 行到契约 DTO 的映射（ENG-06 第 3 步）。

唯一来源：`03-api-contract.md` 第 5 节 `Generation`。

只映射契约字段：`cost_snapshot`、`cost_config_id`、`idempotency_key`、
`retry_count`、阶段时间戳与服务端失败文案都是服务端事实，
**不得进入响应**（`09-repository-boundary.md` 第 7 节）。
数据库列 `parent_task_id` 对外固定叫 `parent_generation_id`。
"""

from __future__ import annotations

from app.db.models.generation import GenerationOutput, GenerationTask
from app.db.models.media import MediaAsset
from app.schemas.generation import Generation

__all__ = ["GenerationOutputView", "to_api_generation", "to_output_view"]

from dataclasses import dataclass
import uuid


@dataclass(frozen=True, slots=True)
class GenerationOutputView:
    """结果资源视图（`GET /generations/{id}/outputs` 的一行）。"""

    output_index: int
    asset_id: uuid.UUID
    mime_type: str
    width: int | None
    height: int | None
    size_bytes: int


def to_api_generation(task: GenerationTask) -> Generation:
    """任务行 -> 契约 `Generation`。"""

    return Generation(
        id=task.id,
        parent_generation_id=task.parent_task_id,
        mode=task.mode,  # type: ignore[arg-type]
        status=task.status,  # type: ignore[arg-type]
        billing_status=task.billing_status,  # type: ignore[arg-type]
        prompt=task.prompt,
        requested_count=task.requested_count,  # type: ignore[arg-type]
        reserved_points=int(task.reserved_points),
        failure_code=task.failure_code,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


def to_output_view(output: GenerationOutput, asset: MediaAsset) -> GenerationOutputView:
    """`generation_outputs` + 对应媒体资源 -> 结果视图。"""

    return GenerationOutputView(
        output_index=output.output_index,
        asset_id=output.asset_id,
        mime_type=asset.mime_type,
        width=asset.width,
        height=asset.height,
        size_bytes=int(asset.size_bytes),
    )
