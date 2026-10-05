"""结算矩阵的**独立进程**执行器：真实任务函数 + 指定 Mock 场景。

## 它做什么

对给定 `generation_id`，在一个**新进程**里：

1. 用 `tests/e2e/scenario_provider.patch_provider_factory(scenario)` 只替换
   `build_ai_provider` 一个函数；
2. 调用**已注册的真实任务函数** `app.tasks.worker.run_generation`；
3. 把结果以**一行 JSON** 打到 stdout。

因此这条路径里的编排、账务、事务边界、Provider 端口、存储适配器、PostgreSQL 与
MinIO 全部是生产实现；**唯一**的差别是 Provider 场景由参数决定，以及投递不走
Redis broker（见模块 `scenario_provider.py` 的分工表）。

## 用法

```bash
python -m tests.e2e.scenario_runner --generation-id <uuid> --scenario failure --prefetch
python -m tests.e2e.scenario_runner --generation-id <uuid> --scenario unknown --prefetch
```

`--prefetch` 会先为 `unknown` 场景把真实图片字节写到 `ENG10_SCENARIO_OUT`。
"""

from __future__ import annotations

import argparse
import json
import sys

__all__ = ["main"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ENG-10 结算矩阵的独立进程执行器")
    parser.add_argument("--generation-id", required=True)
    parser.add_argument(
        "--scenario",
        required=True,
        choices=(
            "success",
            "failure",
            "unknown",
            "timeout",
            "success_without_image",
            "duplicate_webhook",
        ),
    )
    parser.add_argument(
        "--prefetch",
        action="store_true",
        help="执行前为 unknown 场景预生成图片字节",
    )
    args = parser.parse_args(argv)

    from tests.e2e.scenario_provider import patch_provider_factory, prefetch_unknown_outputs

    patch_provider_factory(args.scenario)
    if args.prefetch:
        prefetch_unknown_outputs()

    from app.tasks.worker import run_generation

    outcome = run_generation.run(args.generation_id)
    print(
        json.dumps(
            {"generation_id": args.generation_id, "scenario": args.scenario, "outcome": outcome},
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
