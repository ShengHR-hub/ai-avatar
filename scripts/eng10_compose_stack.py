#!/usr/bin/env python
"""ENG-10 E2E 的 **Compose 模式** Worker 控制面（只作用于本轮的隔离项目）。

## 它解决什么问题

结算矩阵里有几条用例必须让**只有场景执行器**来处理任务（真实 Worker 会先用
`MockProvider(SUCCESS)` 把任务跑成 `SUCCEEDED`，场景执行器随后拿到 `NOT_CLAIMABLE`）。
原生形态下这是 `scripts/eng10_ui_stack.py stop-worker/start-worker` 的职责；
Compose 形态下 Worker 是**容器**，必须用 Compose 自己的 stop/start。

**与原生栈脚本严格分离**：本脚本不 import、不调用 `eng10_ui_stack.py`，
只通过 `docker compose -p <隔离项目>` 操作容器；反过来，E2E 在 Compose 模式下
也绝不会去调原生栈的启停脚本（见 `tests/e2e/conftest.py` 的模式分派）。

## 安全护栏

- 项目名必须显式给出（默认 `ai-avatar-eng10`），**并且**拒绝默认本地项目 `ai-avatar-v1`
  —— 那个项目承载用户的本地数据，不属于本轮资源；
- 每一步都带上 `-p <project>`，因此不可能影响到别的 Compose 项目；
- 操作前核对目标容器名以 `<project>-` 开头，名字不符直接非零退出（**停止与启动都核对**）。

## 停机 / 恢复的确凿性（返修要求逐条）

- `stop-worker`：停完**核对容器状态**（不再是 running）才返回 0；
- `start-worker`：**先核对容器归属**，再启动，然后以**本次启动的时间边界**
  （`docker inspect` 的 `State.StartedAt`）取日志，只接受这条边界**之后**出现的
  Celery `ready.` —— 历史日志不会被误当成「本次已就绪」；
- `ps` / `logs` 的**非零退出一律按失败处理**（绝不当成「空结果 = 成功」）；
- 任何一步失败都非零退出并打印实际状态 —— 调用方（E2E 的 `finally`）据此仍会尝试恢复。

用法：

```bash
python scripts/eng10_compose_stack.py stop-worker
python scripts/eng10_compose_stack.py start-worker
python scripts/eng10_compose_stack.py ps          # 逐行输出本项目 worker 容器状态（JSON）
```
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

#: 本轮隔离项目名；必须显式可覆盖，且拒绝默认本地项目
PROJECT = os.environ.get("ENG10_COMPOSE_PROJECT", "ai-avatar-eng10")
#: 承载用户本地数据的默认项目，绝不允许本脚本触碰
FORBIDDEN_PROJECTS = ("ai-avatar-v1",)

ENV_FILE = os.environ.get("ENG10_COMPOSE_ENV_FILE", str(REPO / ".env.eng10"))
COMPOSE_FILES = [
    part.strip()
    for part in os.environ.get(
        "ENG10_COMPOSE_FILES", "docker/compose.yml,docker/compose.e2e.yml"
    ).split(",")
    if part.strip()
]

#: 启动后等就绪的上限
READY_TIMEOUT_SECONDS = 90.0

__all__ = ["main", "compose_command"]


def log(message: str) -> None:
    print(f"[eng10-compose] {message}", flush=True)


def _guard_project() -> None:
    if PROJECT in FORBIDDEN_PROJECTS:
        raise SystemExit(
            f"拒绝操作 Compose 项目 {PROJECT!r}：它是默认本地环境，承载用户本地数据，"
            "不属于本轮隔离资源。"
        )
    if not PROJECT.startswith("ai-avatar-"):
        raise SystemExit(
            f"拒绝操作 Compose 项目 {PROJECT!r}：本轮只允许 ai-avatar-* 前缀的隔离项目。"
        )


def compose_command(*args: str) -> list[str]:
    """拼出带完整隔离参数的 compose 命令（项目名 + 环境文件 + 覆盖文件）。"""

    command = ["docker", "compose", "--env-file", ENV_FILE, "-p", PROJECT]
    for path in COMPOSE_FILES:
        command += ["-f", path]
    return command + list(args)


def run_compose(*args: str, timeout: float = 120.0) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603 - 参数全部来自本模块常量与环境变量
        compose_command(*args),
        cwd=str(REPO),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _require_ok(result: subprocess.CompletedProcess, what: str) -> None:
    """命令非零退出 = **失败**，绝不当成「空结果」。"""

    if result.returncode != 0:
        raise SystemExit(
            f"{what} 失败（退出码 {result.returncode}）："
            f"{(result.stdout or '')[-300:]}{(result.stderr or '')[-500:]}"
        )


def worker_containers() -> list[dict]:
    """本项目里 service=worker 的容器（`docker compose ps --format json` 逐行 JSON）。

    `ps` 非零退出直接失败：否则解析出空列表会被误读成「没有容器」，
    掩盖掉真正的编排故障。
    """

    result = run_compose("ps", "-a", "--format", "json")
    _require_ok(result, "compose ps")
    rows: list[dict] = []
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("Service") == "worker":
            rows.append(row)
    return rows


def _assert_owned(rows: list[dict]) -> None:
    if not rows:
        raise SystemExit(f"项目 {PROJECT} 里找不到 worker 容器，拒绝继续。")
    for row in rows:
        name = str(row.get("Name", ""))
        if not name.startswith(f"{PROJECT}-"):
            raise SystemExit(f"worker 容器名 {name!r} 不属于项目 {PROJECT}，拒绝继续。")


def _running(rows: list[dict]) -> list[dict]:
    return [row for row in rows if str(row.get("State", "")).lower() == "running"]


def _container_started_at(name: str) -> str:
    """容器的启动时刻（RFC3339）——**本次启动的时间边界**。

    就绪判据只认这条边界**之后**的日志：容器重建/重启后 `StartedAt` 会前进，
    因此历史 `ready.` 不会被误当成「本次已就绪」。
    """

    result = subprocess.run(  # noqa: S603 - 参数来自本模块常量
        ["docker", "inspect", "--format", "{{.State.StartedAt}}", name],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    _require_ok(result, f"docker inspect {name}")
    started_at = (result.stdout or "").strip()
    if not started_at:
        raise SystemExit(f"docker inspect {name} 没有返回 StartedAt，无法确定就绪边界。")
    return started_at


def command_ps(_args: argparse.Namespace) -> None:
    """逐行输出本项目 worker 容器的状态（与 `docker compose ps --format json` 同形）。

    刻意**不用** `json.dumps(list)` 打成数组：调用方（E2E 的身份核对）按行解析，
    数组形式会让每一行都以 `[` 开头而被跳过（实测踩到，整组用例报「没有 worker 容器」）。
    """

    _guard_project()
    rows = worker_containers()
    _assert_owned(rows)
    for row in rows:
        print(json.dumps(row, ensure_ascii=False))


def command_stop_worker(_args: argparse.Namespace) -> None:
    _guard_project()
    before = worker_containers()
    _assert_owned(before)
    log(f"停止 {PROJECT} 的 worker：{', '.join(str(r.get('Name')) for r in before)}")

    result = run_compose("stop", "worker")
    _require_ok(result, "compose stop worker")

    # 停完必须**核对容器状态**：调用方（E2E）随后会假设「队列上已经没有别的消费者」
    after = worker_containers()
    still = _running(after)
    if still:
        raise SystemExit("worker 仍在运行，停机未生效：" + json.dumps(still, ensure_ascii=False))
    log(f"worker 已停止（State={[r.get('State') for r in after]}）")


def command_start_worker(_args: argparse.Namespace) -> None:
    _guard_project()

    # 启动**之前**先核对归属：绝不因为「名字看起来对」就去动别人的容器
    before = worker_containers()
    _assert_owned(before)
    names = [str(row.get("Name")) for row in before]
    log(f"启动 {PROJECT} 的 worker：{', '.join(names)}")

    if not _running(before):
        result = run_compose("start", "worker")
        _require_ok(result, "compose start worker")

    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    last = ""
    boundary = ""
    while time.monotonic() < deadline:
        rows = worker_containers()
        running = _running(rows)
        if running:
            # 就绪边界 = 该容器的 StartedAt；只认这条边界之后的 ready.
            if not boundary:
                boundary = _container_started_at(str(running[0].get("Name")))
            logs = run_compose("logs", "--since", boundary, "worker")
            _require_ok(logs, "compose logs worker")
            last = logs.stdout or ""
            if "ready." in last:
                log(f"worker 已启动并**就绪**（StartedAt={boundary} 之后的日志出现 ready.）")
                return
        time.sleep(1.0)
    raise SystemExit(
        f"worker 启动后 {READY_TIMEOUT_SECONDS:.0f}s 内未就绪"
        f"（边界 {boundary or '未知'} 之后没有 ready.）：{last[-400:]}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ENG-10 Compose 模式的 Worker 控制面")
    sub = parser.add_subparsers(dest="command", required=True)

    ps = sub.add_parser("ps", help="输出本项目 worker 容器状态")
    ps.set_defaults(func=command_ps)

    stop = sub.add_parser("stop-worker", help="停 Worker 并核对容器状态")
    stop.set_defaults(func=command_stop_worker)

    start = sub.add_parser("start-worker", help="起 Worker 并等它就绪")
    start.set_defaults(func=command_start_worker)

    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
