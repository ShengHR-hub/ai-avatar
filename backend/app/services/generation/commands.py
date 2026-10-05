"""生成任务的创建命令（ENG-06 第 3 步）。

`NewGenerationCommand` 是 Service 的领域入参，与 HTTP 请求模型解耦：API 层把冻结的
`GenerationCreateRequest` 转成它，Worker、脚本与测试因此可以同样直接调用
（`06-development-standard.md` 第 2 节分层约定）。

命令自身只做**形状**校验（模式取值、Prompt 非空、数量为正），越权资源、Feature Flag、
余额与幂等等判断都在 Service 内完成：命令不是策略层。
校验失败统一按契约的 `VALIDATION_ERROR`（422）返回，不新增错误码。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from app.errors.codes import ErrorCode
from app.errors.exceptions import ApiError

__all__ = ["GENERATION_MODES", "NewGenerationCommand"]

#: `generation_tasks.mode` 取值（`02-database-design.md` 第 3.2 节 CHECK）
GENERATION_MODES = ("PHOTO_PROMPT", "TEXT_PROMPT", "REFERENCE_IMAGE")


@dataclass(frozen=True, slots=True)
class NewGenerationCommand:
    """一次创建请求的领域参数。

    `input_asset_ids` 的顺序参与幂等指纹，而 `generation_inputs.sort_order` 按角色
    分别编号——两种顺序都由本命令的输入唯一决定，重放时不会漂移。
    """

    mode: str
    prompt: str
    output_count: int
    style_id: uuid.UUID | None = None
    prompt_template_id: uuid.UUID | None = None
    input_asset_ids: tuple[uuid.UUID, ...] = ()

    def __post_init__(self) -> None:
        # 列表 -> 元组：命令是不可变值对象，`input_asset_ids` 因此可比较、可哈希
        object.__setattr__(self, "input_asset_ids", tuple(self.input_asset_ids))

        if self.mode not in GENERATION_MODES:
            raise ApiError(ErrorCode.VALIDATION_ERROR, details={"fields": ["mode"]})
        if not str(self.prompt).strip():
            raise ApiError(ErrorCode.VALIDATION_ERROR, details={"fields": ["prompt"]})
        if int(self.output_count) <= 0:
            raise ApiError(ErrorCode.VALIDATION_ERROR, details={"fields": ["output_count"]})

    def fingerprint_payload(self) -> dict[str, Any]:
        """幂等比较用的规范化载荷（`03-api-contract.md` 第 9 节）。

        只取影响业务结果的字段；`uuid` 一律转小写字符串、输入资源**排序**后参与：
        提交顺序不影响业务结果（角色与 `sort_order` 由服务端稳定落库），因此顺序
        不同不算「不同请求」。字段集合与 `idempotency.task_payload` 严格同形，
        才能与从任务行重算出来的指纹相比对。
        """

        return {
            "mode": self.mode,
            "prompt": self.prompt,
            "output_count": int(self.output_count),
            "style_id": str(self.style_id) if self.style_id is not None else None,
            "prompt_template_id": (
                str(self.prompt_template_id) if self.prompt_template_id is not None else None
            ),
            "input_asset_ids": sorted(
                str(asset_id).lower() for asset_id in self.input_asset_ids
            ),
        }
