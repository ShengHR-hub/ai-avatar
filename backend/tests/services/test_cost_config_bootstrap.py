"""点数成本初始化命令的单元测试（不依赖 PostgreSQL）。

这里证明的是**命令的判断力**，不是 SQL：

- 价格只能显式输入，格式有歧义就拒绝（没有默认价格）；
- 空表 → 全部 `create`；同价重跑 → 全部 `unchanged`，一条都不写；
- 改价是显式动作：未带 `--allow-reprice` 时**整批**挡下，不出现半套配置；
- 获授权后改价走「关闭旧窗口 + 追加新行」，历史行不被改写；
- 健康检查用的模式集合与 `ENABLE_REFERENCE_IMAGE` 一致。

真库上的「空库 → 初始化 → 用 Mock 走完一次生成」在 `tests/db/test_cost_config_bootstrap.py`。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.bootstrap.cost_configs import (
    REQUIRED_OUTPUT_COUNT,
    PriceInput,
    apply_plan,
    expected_cost_modes,
    missing_cost_modes,
    parse_price,
    plan_prices,
)
from tests.services.fakes import (
    InMemoryGenerationCostConfigRepository,
    make_generation_cost_config,
)

AT = datetime(2026, 5, 1, tzinfo=timezone.utc)


def _text(points: int = 30, **overrides: object) -> PriceInput:
    return PriceInput(mode="TEXT_PROMPT", points_cost=points, **overrides)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# 显式输入
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("raw", "mode", "points"),
    [
        ("TEXT_PROMPT=30", "TEXT_PROMPT", 30),
        (" text_prompt = 7 ", "TEXT_PROMPT", 7),
        ("REFERENCE_IMAGE=1", "REFERENCE_IMAGE", 1),
    ],
)
def test_parse_price_accepts_only_well_formed_pairs(
    raw: str, mode: str, points: int
) -> None:
    parsed = parse_price(raw)

    assert (parsed.mode, parsed.points_cost) == (mode, points)
    assert parsed.output_count == REQUIRED_OUTPUT_COUNT
    assert parsed.model_name is None


@pytest.mark.parametrize(
    "raw",
    [
        "TEXT_PROMPT",  # 没有等号
        "TEXT_PROMPT=",  # 有点数却没值
        "=30",  # 有价却没模式
        "TEXT_PROMPT=abc",  # 非整数
        "TEXT_PROMPT=0",  # 数据库 CHECK 也会拒绝
        "TEXT_PROMPT=-5",
        "STYLE_PROMPT=5",  # 契约里没有的模式
    ],
)
def test_parse_price_rejects_ambiguous_input(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_price(raw)


def test_command_has_no_default_price() -> None:
    """价格没有产品依据时不允许「先跑起来」：命令必须拒绝空输入。"""

    import os
    import subprocess
    import sys
    from pathlib import Path

    completed = subprocess.run(
        [sys.executable, "-m", "app.bootstrap.cost_configs"],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[2],
        env={"PYTHONPATH": ".", "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")},
    )

    assert completed.returncode == 2
    assert "--price" in completed.stderr


# --------------------------------------------------------------------------- #
# 计划
# --------------------------------------------------------------------------- #
def test_plan_on_empty_table_creates_every_mode() -> None:
    costs = InMemoryGenerationCostConfigRepository()

    plan = plan_prices(
        costs,
        [_text(30), PriceInput(mode="PHOTO_PROMPT", points_cost=40)],
        at=AT,
    )

    assert [change.kind for change in plan] == ["create", "create"]
    assert all(change.closes_config_id is None for change in plan)


def test_same_price_replan_reports_unchanged() -> None:
    costs = InMemoryGenerationCostConfigRepository()
    costs.seed(make_generation_cost_config(mode="TEXT_PROMPT", points_cost=30))

    plan = plan_prices(costs, [_text(30)], at=AT)

    assert [change.kind for change in plan] == ["unchanged"]


def test_different_price_plans_a_reprice_that_names_the_row_to_close() -> None:
    costs = InMemoryGenerationCostConfigRepository()
    current = costs.seed(make_generation_cost_config(mode="TEXT_PROMPT", points_cost=30))

    plan = plan_prices(costs, [_text(45)], at=AT)

    assert plan[0].kind == "reprice"
    assert plan[0].closes_config_id == current.id


def test_plan_treats_model_specific_price_as_a_different_key() -> None:
    """精确型号价与通配（NULL）价是两条独立的价格，互不覆盖。"""

    costs = InMemoryGenerationCostConfigRepository()
    costs.seed(make_generation_cost_config(mode="TEXT_PROMPT", points_cost=30))
    costs.seed(
        make_generation_cost_config(
            mode="TEXT_PROMPT", points_cost=50, model_name="gpt-image-x"
        )
    )

    plan = plan_prices(costs, [_text(30)], at=AT)

    assert plan[0].kind == "unchanged"
    assert plan[0].model_name is None
    assert plan[0].closes_config_id is None


# --------------------------------------------------------------------------- #
# 执行
# --------------------------------------------------------------------------- #
def test_reprice_without_consent_blocks_the_whole_batch() -> None:
    costs = InMemoryGenerationCostConfigRepository()
    seeded = costs.seed(make_generation_cost_config(mode="TEXT_PROMPT", points_cost=30))
    plan = plan_prices(
        costs,
        [_text(45), PriceInput(mode="PHOTO_PROMPT", points_cost=40)],
        at=AT,
    )

    result = apply_plan(costs, plan, at=AT)

    assert result.ok is False
    assert [change.mode for change in result.blocked] == ["TEXT_PROMPT"]
    # 关键：被挡下的同时，**新模式也没写进去**，不存在半套配置
    assert result.created_ids == ()
    assert [row.points_cost for row in costs._rows] == [30]
    assert costs._rows[0].status == "ACTIVE"
    assert seeded.status == "ACTIVE"


def test_reprice_closes_old_window_and_appends_new_row() -> None:
    costs = InMemoryGenerationCostConfigRepository()
    old = costs.seed(make_generation_cost_config(mode="TEXT_PROMPT", points_cost=30))
    plan = plan_prices(costs, [_text(45)], at=AT)

    result = apply_plan(costs, plan, at=AT, allow_reprice=True)

    assert result.ok is True
    assert result.closed_ids == (old.id,)
    assert len(result.created_ids) == 1
    assert old.status == "INACTIVE"
    assert old.effective_to == AT
    # 历史行仍在：只被关窗，价格没被改写
    assert old.points_cost == 30

    resolved = costs.find_active(
        mode="TEXT_PROMPT", model_name=None, output_count=2, at=AT + timedelta(seconds=1)
    )
    assert resolved is not None
    assert resolved.points_cost == 45
    assert resolved.id == result.created_ids[0]


def test_second_run_writes_nothing() -> None:
    costs = InMemoryGenerationCostConfigRepository()
    prices = [_text(30), PriceInput(mode="PHOTO_PROMPT", points_cost=40)]

    first = apply_plan(costs, plan_prices(costs, prices, at=AT), at=AT)
    rows_after_first = list(costs._rows)
    second = apply_plan(costs, plan_prices(costs, prices, at=AT), at=AT)

    assert first.created_ids and len(first.created_ids) == 2
    assert second.created_ids == ()
    assert second.closed_ids == ()
    assert costs._rows == rows_after_first


def test_reprice_refuses_zero_length_window() -> None:
    """同一瞬间「先建后关」会让 `effective_to > effective_from` 的 CHECK 失败，
    这里提前给出人能读懂的原因，而不是抛数据库完整性错误。"""

    costs = InMemoryGenerationCostConfigRepository()
    apply_plan(costs, plan_prices(costs, [_text(30)], at=AT), at=AT)

    plan = plan_prices(costs, [_text(60)], at=AT)
    with pytest.raises(RuntimeError, match="稍后重跑"):
        apply_plan(costs, plan, at=AT, allow_reprice=True)


# --------------------------------------------------------------------------- #
# 健康检查的取数口径
# --------------------------------------------------------------------------- #
def test_expected_modes_follow_reference_image_switch() -> None:
    assert expected_cost_modes(enable_reference_image=False) == ("TEXT_PROMPT", "PHOTO_PROMPT")
    assert expected_cost_modes(enable_reference_image=True) == (
        "TEXT_PROMPT",
        "PHOTO_PROMPT",
        "REFERENCE_IMAGE",
    )


def test_missing_modes_reports_only_unpriced_modes() -> None:
    costs = InMemoryGenerationCostConfigRepository()
    costs.seed(make_generation_cost_config(mode="TEXT_PROMPT", points_cost=30))

    missing = missing_cost_modes(
        costs, modes=expected_cost_modes(enable_reference_image=False), model_name="m1", at=AT
    )

    assert missing == ("PHOTO_PROMPT",)


def test_missing_modes_accepts_wildcard_model_price() -> None:
    """兜底价（`model_name IS NULL`）对任何型号都算已定价，与 `find_active` 一致。"""

    costs = InMemoryGenerationCostConfigRepository()
    for mode in ("TEXT_PROMPT", "PHOTO_PROMPT"):
        costs.seed(make_generation_cost_config(mode=mode, points_cost=30))

    missing = missing_cost_modes(
        costs,
        modes=expected_cost_modes(enable_reference_image=False),
        model_name="some-new-model",
        at=AT,
    )

    assert missing == ()


def test_missing_modes_ignores_expired_and_inactive_rows() -> None:
    costs = InMemoryGenerationCostConfigRepository()
    costs.seed(
        make_generation_cost_config(mode="TEXT_PROMPT", points_cost=30, status="INACTIVE")
    )
    costs.seed(
        make_generation_cost_config(
            mode="PHOTO_PROMPT", points_cost=30, effective_to=AT - timedelta(seconds=1)
        )
    )

    missing = missing_cost_modes(
        costs, modes=expected_cost_modes(enable_reference_image=False), model_name=None, at=AT
    )

    assert missing == ("TEXT_PROMPT", "PHOTO_PROMPT")


# --------------------------------------------------------------------------- #
# 型号定价规则：**精确型号优先，NULL 兜底**
#
# 创建任务、`/health/ready` 自检、定价初始化三处必须给出同一个答案。
# 口径不一致会制造两种最难排查的故障：
#   ① 「健康检查绿了、创建任务却 503」（只给某个型号定价时，创建任务原来只找 NULL）；
#   ② 「后来补的兜底价把已有专价顶掉」（原来按 `effective_from` 谁新谁赢）。
# --------------------------------------------------------------------------- #
EXACT_MODEL = "mock-image-v1"


def test_find_active_prefers_the_exact_model_price() -> None:
    """仅精确型号：只有它自己的那条价格可用。"""

    costs = InMemoryGenerationCostConfigRepository()
    exact = costs.seed(
        make_generation_cost_config(
            mode="TEXT_PROMPT", model_name=EXACT_MODEL, points_cost=13
        )
    )

    hit = costs.find_active(
        mode="TEXT_PROMPT",
        model_name=EXACT_MODEL,
        output_count=REQUIRED_OUTPUT_COUNT,
        at=AT,
    )

    assert hit is exact
    assert hit.points_cost == 13


def test_find_active_falls_back_to_the_wildcard_price() -> None:
    """仅 NULL（兜底价）：任何型号都按它定价。"""

    costs = InMemoryGenerationCostConfigRepository()
    wildcard = costs.seed(make_generation_cost_config(mode="TEXT_PROMPT", points_cost=30))

    for model in (EXACT_MODEL, "another-model"):
        hit = costs.find_active(
            mode="TEXT_PROMPT",
            model_name=model,
            output_count=REQUIRED_OUTPUT_COUNT,
            at=AT,
        )
        assert hit is wildcard, model


def test_exact_model_price_survives_a_later_wildcard_update() -> None:
    """两者并存、且 NULL 行**更晚生效**：精确型号仍然优先，兜底价不得顶掉专价。"""

    costs = InMemoryGenerationCostConfigRepository()
    exact = costs.seed(
        make_generation_cost_config(
            mode="TEXT_PROMPT",
            model_name=EXACT_MODEL,
            points_cost=13,
            effective_from=AT - timedelta(days=2),
        )
    )
    wildcard = costs.seed(
        make_generation_cost_config(
            mode="TEXT_PROMPT", points_cost=99, effective_from=AT - timedelta(days=1)
        )
    )

    assert (
        costs.find_active(
            mode="TEXT_PROMPT",
            model_name=EXACT_MODEL,
            output_count=REQUIRED_OUTPUT_COUNT,
            at=AT,
        )
        is exact
    )
    assert (
        costs.find_active(
            mode="TEXT_PROMPT", model_name=None, output_count=REQUIRED_OUTPUT_COUNT, at=AT
        )
        is wildcard
    )
    # 别的型号只能拿兜底价
    assert (
        costs.find_active(
            mode="TEXT_PROMPT",
            model_name="another-model",
            output_count=REQUIRED_OUTPUT_COUNT,
            at=AT,
        )
        is wildcard
    )


def test_missing_modes_is_ready_when_only_the_exact_model_is_priced() -> None:
    """readiness 与创建任务同口径：只给精确型号定价也算「能定价」。"""

    costs = InMemoryGenerationCostConfigRepository()
    for mode in ("TEXT_PROMPT", "PHOTO_PROMPT"):
        costs.seed(
            make_generation_cost_config(mode=mode, model_name=EXACT_MODEL, points_cost=13)
        )

    assert (
        missing_cost_modes(
            costs,
            modes=expected_cost_modes(enable_reference_image=False),
            model_name=EXACT_MODEL,
            at=AT,
        )
        == ()
    )
    # 换成别的型号就只剩兜底，而兜底并不存在 → 如实报缺口（不返回任何默认价）
    assert missing_cost_modes(
        costs, modes=("TEXT_PROMPT",), model_name="another-model", at=AT
    ) == ("TEXT_PROMPT",)


def test_missing_modes_reports_every_mode_when_nothing_is_priced() -> None:
    """缺配置：三种取数口径（不指定型号 / 精确型号 / 其他型号）都报同一个缺口。"""

    costs = InMemoryGenerationCostConfigRepository()
    modes = expected_cost_modes(enable_reference_image=False)

    for model in (None, EXACT_MODEL, "another-model"):
        assert missing_cost_modes(costs, modes=modes, model_name=model, at=AT) == modes
