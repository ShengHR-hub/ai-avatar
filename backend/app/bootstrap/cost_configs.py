"""生成点数成本的初始化与调价（部署期入口，可重复执行）。

为什么需要这个模块：`02-database-design.md` 第 3.2 节把用户点数价格放在
`generation_cost_configs` 里，**数据库迁移只建表、不填价格**。新环境迁移到 head 之后
这张表是空的，创建生成任务会走到「没有可用激活配置」的分支——表现为一次 5xx，
而根因是部署少了一步。这里把这一步变成显式、可重跑的命令。

口径：

- **没有默认价格**：每一项价格都必须由调用方显式给出。V1.0 的产品定价尚未确定，
  代码里的任何缺省值都会变成事实上的定价（静默定价等于凭空记账）；
- 重复执行安全：同一组价格第二次执行只报告 `unchanged`，不写库；
- **调价是显式动作**：同一 `(mode, model_name, output_count)` 已有一条开口中的激活行
  且价格不同时，必须带 `--allow-reprice` 才执行；执行方式是「关闭旧窗口 + 追加新行」，
  绝不改写历史行——历史任务的 `cost_snapshot`/`reserved_points` 必须仍能被读回；
- 只写这张配置表，不碰钱包、流水与任务行。

`REFERENCE_IMAGE` 是否要有价格取决于 `ENABLE_REFERENCE_IMAGE`（默认关闭），
见 `expected_cost_modes()`——健康检查与命令共用同一判据，避免「健康检查要求的价格
其实是关闭功能的价格」。
"""

from __future__ import annotations

import argparse
import sys
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from sqlalchemy.orm import Session

from app.db.models.generation import GenerationCostConfig
from app.repositories.generation import (
    GenerationCostConfigRepository,
    SqlAlchemyGenerationCostConfigRepository,
)

__all__ = [
    "COMMAND",
    "PRICE_EXAMPLE",
    "REQUIRED_OUTPUT_COUNT",
    "SUPPORTED_MODES",
    "BootstrapResult",
    "PlannedChange",
    "PriceInput",
    "apply_plan",
    "bootstrap_cost_configs",
    "expected_cost_modes",
    "main",
    "missing_cost_modes",
    "parse_price",
    "plan_prices",
]

#: 契约 `GenerationCreateRequest.output_count` 是 `const: 2`，因此价格只需覆盖 2 张
REQUIRED_OUTPUT_COUNT = 2

#: 可定价的模式集合（与 `app.schemas.generation.GenerationMode` 取值一致）
SUPPORTED_MODES: tuple[str, ...] = ("TEXT_PROMPT", "PHOTO_PROMPT", "REFERENCE_IMAGE")

#: 部署文档与错误提示里引用的命令名
COMMAND = "python -m app.bootstrap.cost_configs"

#: 帮助文本里的示例：刻意用 `<N>` 占位，不提供可直接照抄的价格
PRICE_EXAMPLE = f"{COMMAND} --price TEXT_PROMPT=<N> --price PHOTO_PROMPT=<N>"


@dataclass(frozen=True, slots=True)
class PriceInput:
    """一项显式价格输入（一个模式 + 一个出图数量 → 每次任务的点数）。"""

    mode: str
    points_cost: int
    output_count: int = REQUIRED_OUTPUT_COUNT
    model_name: str | None = None


@dataclass(frozen=True, slots=True)
class PlannedChange:
    """一条计划：`create` 新增、`unchanged` 已存在且价格一致、`reprice` 需要调价。"""

    kind: Literal["create", "unchanged", "reprice"]
    mode: str
    output_count: int
    points_cost: int
    model_name: str | None = None
    closes_config_id: uuid.UUID | None = None

    @property
    def needs_consent(self) -> bool:
        return self.kind == "reprice"


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    """一次执行的结果：写了什么、跳过了什么、被什么挡住。"""

    planned: tuple[PlannedChange, ...]
    created_ids: tuple[uuid.UUID, ...] = ()
    closed_ids: tuple[uuid.UUID, ...] = ()
    blocked: tuple[PlannedChange, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.created_ids)

    @property
    def ok(self) -> bool:
        return not self.blocked


def parse_price(raw: str) -> PriceInput:
    """解析 `MODE=点数`；任何歧义都直接拒绝，不做猜测或单位换算。"""

    mode, separator, value = raw.partition("=")
    mode = mode.strip().upper()
    if not separator or not value.strip():
        raise ValueError(f"--price 需要写成 MODE=<点数>，收到的是 {raw!r}。")
    if mode not in SUPPORTED_MODES:
        raise ValueError(
            f"未知模式 {mode!r}；可定价的模式是 {', '.join(SUPPORTED_MODES)}。"
        )
    try:
        points_cost = int(value.strip())
    except ValueError as exc:
        raise ValueError(f"{mode} 的点数必须是整数，收到的是 {value!r}。") from exc
    if points_cost <= 0:
        raise ValueError(f"{mode} 的点数必须为正整数（数据库 CHECK 同样拒绝非正数）。")
    return PriceInput(mode=mode, points_cost=points_cost)


def _open_ended(
    costs: GenerationCostConfigRepository, *, price: PriceInput, at: datetime
) -> GenerationCostConfig | None:
    """找出该键下**唯一开口中**的激活行（`effective_to IS NULL`）。

    `list_active` 只按生效窗口过滤，这里再收一次键：`find_active` 允许「精确型号优先、
    NULL 兜底」两条并存，而初始化必须知道同一 `(mode, model, output_count)` 上
    是否已经有一条正在生效的价格，否则会写出互相覆盖的重复配置。
    """

    for config in costs.list_active(at=at):
        if (
            config.effective_to is None
            and config.mode == price.mode
            and config.output_count == price.output_count
            and config.model_name == price.model_name
        ):
            return config
    return None


def plan_prices(
    costs: GenerationCostConfigRepository, prices: Sequence[PriceInput], *, at: datetime
) -> tuple[PlannedChange, ...]:
    """把显式价格输入映射成计划（纯读，不写库）。"""

    planned: list[PlannedChange] = []
    for price in prices:
        current = _open_ended(costs, price=price, at=at)
        if current is None:
            planned.append(
                PlannedChange(
                    kind="create",
                    mode=price.mode,
                    output_count=price.output_count,
                    points_cost=price.points_cost,
                    model_name=price.model_name,
                )
            )
            continue
        if current.points_cost == price.points_cost:
            planned.append(
                PlannedChange(
                    kind="unchanged",
                    mode=price.mode,
                    output_count=price.output_count,
                    points_cost=price.points_cost,
                    model_name=price.model_name,
                )
            )
            continue
        planned.append(
            PlannedChange(
                kind="reprice",
                mode=price.mode,
                output_count=price.output_count,
                points_cost=price.points_cost,
                model_name=price.model_name,
                closes_config_id=current.id,
            )
        )
    return tuple(planned)


def apply_plan(
    costs: GenerationCostConfigRepository,
    plan: Sequence[PlannedChange],
    *,
    at: datetime,
    allow_reprice: bool = False,
) -> BootstrapResult:
    """执行计划；未获 `allow_reprice` 时把调价项整批挡下，**不产生半套写入**。

    整批先判定再写入：一组价格里只要有一项是未经授权的调价，就一条都不写，
    避免「TEXT_PROMPT 已改、PHOTO_PROMPT 被拒」这种半成品配置。
    """

    blocked = tuple(
        change for change in plan if change.needs_consent and not allow_reprice
    )
    if blocked:
        return BootstrapResult(planned=tuple(plan), blocked=blocked)

    created: list[uuid.UUID] = []
    closed: list[uuid.UUID] = []
    for change in plan:
        if change.kind == "unchanged":
            continue
        if change.kind == "reprice":
            previous = _previous_for_close(costs, change=change, at=at)
            if previous is None:  # pragma: no cover - 计划与执行之间有人改了配置
                raise RuntimeError(
                    f"{change.mode} 的调价目标行 {change.closes_config_id} 已不存在，"
                    "请重新执行本命令。"
                )
            closed.append(costs.close(previous, closed_at=at).id)
        created.append(
            costs.add(
                mode=change.mode,
                output_count=change.output_count,
                points_cost=change.points_cost,
                model_name=change.model_name,
                effective_from=at,
            ).id
        )
    return BootstrapResult(
        planned=tuple(plan), created_ids=tuple(created), closed_ids=tuple(closed)
    )


def _previous_for_close(
    costs: GenerationCostConfigRepository, *, change: PlannedChange, at: datetime
) -> GenerationCostConfig | None:
    """按 `closes_config_id` 找回要关窗的行（经 Repository 读，不绕过端口）。

    `at` 用它做生效窗口过滤：只有在当前仍生效的行才允许被关闭，已过期或被别人关掉的
    行返回 `None`，由调用方以明确错误终止，而不是静默跳过留下两条 ACTIVE。
    """

    config_id = change.closes_config_id
    if config_id is None:
        return None
    for config in costs.list_active(at=at):
        if config.id == config_id:
            if at <= config.effective_from:
                raise RuntimeError(
                    f"{change.mode} 的现行价格生效时间不早于本次调价时间，"
                    "数据库 CHECK 会拒绝零长度窗口；请稍后重跑本命令。"
                )
            return config
    return None


def expected_cost_modes(*, enable_reference_image: bool) -> tuple[str, ...]:
    """当前配置下**必须**有点数价格的模式集合（健康检查与部署自检共用）。

    `REFERENCE_IMAGE` 由 `ENABLE_REFERENCE_IMAGE` 把关：功能关着却要求它为一条
    永不生效的价格定价，健康检查就会永久红着。
    """

    modes = ("TEXT_PROMPT", "PHOTO_PROMPT")
    return modes + (("REFERENCE_IMAGE",) if enable_reference_image else ())


def missing_cost_modes(
    costs: GenerationCostConfigRepository,
    *,
    modes: Sequence[str],
    output_count: int = REQUIRED_OUTPUT_COUNT,
    model_name: str | None = None,
    at: datetime,
) -> tuple[str, ...]:
    """逐个模式复用 `find_active` 判定「能不能定价」，与创建任务时同一套取值规则。"""

    return tuple(
        mode
        for mode in modes
        if costs.find_active(
            mode=mode, model_name=model_name, output_count=output_count, at=at
        )
        is None
    )


def bootstrap_cost_configs(
    session_factory: Callable[[], Session],
    prices: Sequence[PriceInput],
    *,
    at: datetime | None = None,
    allow_reprice: bool = False,
    dry_run: bool = False,
) -> BootstrapResult:
    """一次完整的「计划 → （提交前）确认 → 写入」。

    事务边界在这里：dry-run 与整批被拒都不提交，因此绝不留下半个窗口。
    """

    moment = at or datetime.now(timezone.utc)
    with session_factory() as session:
        costs = SqlAlchemyGenerationCostConfigRepository(session)
        plan = plan_prices(costs, prices, at=moment)
        if dry_run:
            session.rollback()
            return BootstrapResult(planned=plan)
        result = apply_plan(costs, plan, at=moment, allow_reprice=allow_reprice)
        if not result.ok:
            session.rollback()
            return result
        session.commit()
        return result


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=COMMAND,
        description=(
            "写入 generation_cost_configs 的用户点数价格。"
            "价格必须逐项显式给出；本命令不提供任何默认价格。"
        ),
    )
    parser.add_argument(
        "--price",
        action="append",
        default=[],
        metavar="MODE=<点数>",
        help=f"可重复；模式取值 {'/'.join(SUPPORTED_MODES)}。示例 {PRICE_EXAMPLE}",
    )
    parser.add_argument(
        "--model",
        default=None,
        metavar="<型号>",
        help="只对该型号定价；缺省为 NULL，即对所有型号生效的兜底价格。",
    )
    parser.add_argument(
        "--output-count",
        type=int,
        default=REQUIRED_OUTPUT_COUNT,
        help=f"每次生成的出图数量（契约固定为 {REQUIRED_OUTPUT_COUNT}）。",
    )
    parser.add_argument(
        "--allow-reprice",
        action="store_true",
        help="允许关闭现行价格窗口并追加新价格（默认拒绝，避免误改在售价）。",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印计划，不写入。",
    )
    return parser


def _report(changes: Iterable[PlannedChange]) -> str:
    lines: list[str] = []
    for change in changes:
        label = {
            "create": "新增",
            "unchanged": "已存在，价格一致",
            "reprice": "调价（关闭旧窗口 + 追加新行）",
        }[change.kind]
        scope = change.model_name or "全部型号"
        lines.append(
            f"{label}: {change.mode} × {change.output_count} 张 = "
            f"{change.points_cost} 点（{scope}）"
        )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not args.price:
        parser.error(
            "至少要给一项 --price MODE=<点数>。价格没有产品依据时不要执行本命令，"
            "先确定定价再来。"
        )
    if args.output_count <= 0:
        parser.error("--output-count 必须是正整数。")

    try:
        prices = [
            PriceInput(
                mode=parsed.mode,
                points_cost=parsed.points_cost,
                output_count=args.output_count,
                model_name=args.model,
            )
            for parsed in (parse_price(raw) for raw in args.price)
        ]
    except ValueError as error:
        print(f"参数错误：{error}", file=sys.stderr)
        return 2

    modes = [price.mode for price in prices]
    duplicates = {mode for mode in modes if modes.count(mode) > 1}
    if duplicates:
        print(
            f"参数错误：同一模式重复给出（{', '.join(sorted(duplicates))}）；"
            "一次执行里每个模式只能有一个价格。",
            file=sys.stderr,
        )
        return 2

    from app.db.session import get_session_factory

    result = bootstrap_cost_configs(
        get_session_factory(),
        prices,
        allow_reprice=args.allow_reprice,
        dry_run=args.dry_run,
    )

    if args.dry_run:
        print(f"[dry-run]\n{_report(result.planned)}")
        return 0
    if not result.ok:
        print(
            "已终止，未写入任何配置。以下模式已有不同的在售价，"
            f"确认要改价再加 --allow-reprice：\n{_report(result.blocked)}",
            file=sys.stderr,
        )
        return 1

    print(_report(result.planned) or "没有需要写入的价格。")
    print(
        f"已写入 {len(result.created_ids)} 条配置"
        + (f"，关闭 {len(result.closed_ids)} 条旧窗口" if result.closed_ids else "")
        + "。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
