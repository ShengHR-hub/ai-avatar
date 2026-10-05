"""用户点数成本解析与成本快照（ENG-06 第 3 步）。

唯一来源：`02-database-design.md` 第 3.2 节（`generation_cost_configs`）与
`LEAD-04` §1（预留金额是任务行 `reserved_points` 的事实来源，后续
`consume`/`release` 只能从它推导）。

口径：

- `reserved_points` **只取** `points_cost`：`cost_multiplier` 是内部成本倍率，
  绝不参与用户点数计算（`04-ai-provider.md` 第 5 节：Provider 成本不改变用户点数）；
- 命中哪一条配置必须留证：`cost_snapshot` 冻结配置身份与倍率，事后调价不影响历史任务；
- 没有可用的激活配置是**服务端未初始化**，不是用户可触发条件，也不是内部不变量破坏：
  返回 `NOT_READY`（503）并给出可执行的补救命令，让运维一眼看出「少跑了一步初始化」
  而不是把它当随机 5xx 排查；**绝不退回任何默认价格**（静默定价等于凭空记账）。
  初始化入口见 `app.bootstrap.cost_configs`，部署步骤见 `docker/README.md`。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.db.models.generation import GenerationCostConfig
from app.errors.codes import ErrorCode
from app.errors.exceptions import ApiError
from app.repositories.generation import GenerationCostConfigRepository

__all__ = ["ResolvedCost", "resolve_cost"]


@dataclass(frozen=True, slots=True)
class ResolvedCost:
    """一次创建请求解析出的成本与快照。"""

    cost_config_id: uuid.UUID
    reserved_points: int
    snapshot: dict[str, Any]


def _as_text(value: Decimal | int | None) -> str | None:
    """`Decimal` 以十进制字符串入快照。

    JSONB 没有十进制类型：转成 `float` 会把 `1.0000` 变成 `1.0` 并引入二进制误差，
    而成本倍率是审计字段，必须与配置值逐位一致（`04-ai-provider.md` 第 5 节：
    按既有精度保存十进制成本）。
    """

    return None if value is None else str(value)


def resolve_cost(
    costs: GenerationCostConfigRepository,
    *,
    mode: str,
    output_count: int,
    at: datetime,
    model_name: str | None = None,
) -> ResolvedCost:
    """按 `(mode, model_name, output_count, 生效窗口)` 取激活配置并冻结快照。"""

    config: GenerationCostConfig | None = costs.find_active(
        mode=mode, model_name=model_name, output_count=output_count, at=at
    )
    if config is None:
        raise ApiError(
            ErrorCode.NOT_READY,
            details={
                "reason": "cost_config_missing",
                "mode": mode,
                "output_count": output_count,
                "remediation": (
                    "python -m app.bootstrap.cost_configs "
                    f"--price {mode}=<点数>"
                ),
            },
        )
    if config.points_cost <= 0:
        # 数据库 CHECK 已禁止非正数；这里再挡住「配置读到 0 却照常记账」的路径
        raise ApiError(ErrorCode.INTERNAL_ERROR, details={"reason": "cost_config_invalid"})

    snapshot: dict[str, Any] = {
        "cost_config_id": str(config.id),
        "mode": config.mode,
        "model_name": config.model_name,
        "output_count": int(config.output_count),
        "points_cost": int(config.points_cost),
        "cost_multiplier": _as_text(config.cost_multiplier),
        "effective_from": config.effective_from.isoformat(),
        "pricing_source": "generation_cost_configs",
    }
    return ResolvedCost(
        cost_config_id=config.id,
        reserved_points=int(config.points_cost),
        snapshot=snapshot,
    )
