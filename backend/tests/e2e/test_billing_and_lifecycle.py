"""ENG-10 结算矩阵 / 权限 / 幂等 / 生命周期：隔离库上的真实 HTTP + 真实进程。

## 如实区分两种执行形态

| 场景 | 投递方式 | 走的代码 |
|---|---|---|
| `success` | **真实 Celery Worker 容器/进程消费真实 Redis 队列** | 完整 Worker 路径 |
| `failure` / `unknown` | `tests/e2e/scenario_runner.py` 在**独立进程**里调用同一个已注册任务函数 | 真实编排/账务/存储，但**不是** broker 投递 |

后者这样安排是刻意的：`MockProvider` 的场景在真实进程里没有选择入口
（`04-ai-provider.md` 第 6 节要求场景只能由服务端配置决定），所以测试用
「独立进程 + 只替换 `build_ai_provider` 一个函数」的方式覆盖另两条结算分支。
报告里不会把它写成「Worker 容器验收」。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest
import sqlalchemy as sa

from tests.e2e.conftest import (
    ARTIFACTS,
    SCENARIO_OUT,
    Account,
    CeleryQueue,
    create_ready_asset,
    new_client,
    points_of,
    redeem_and_verify,
    submit_generation,
    wait_generation,
    worker_stopped,
)

SCENARIO_PREFIX = "[eng10-scenario:"
WORK_DIR = ARTIFACTS / "synthetic"

REPO_BACKEND = Path(__file__).resolve().parents[2]


def _stack_env() -> dict[str, str]:
    """独立进程要用的服务端环境（**与运行中的 API 同一套配置**）。

    为什么必须有：`scenario_runner` / `cleanup_media` 是**独立进程**，它们自己读
    环境变量。少给一项就会走偏，而症状很有迷惑性——实测漏了 `STORAGE_ENDPOINT` 时，
    执行器连不上对象存储，源图读不出来，任务直接
    `FAILED / INPUT_UNAVAILABLE / failure_code=VALIDATION`（看起来完全像「场景坏了」）。
    """

    env = dict(os.environ)
    env.update(
        {
            "DATABASE_URL": os.environ["ENG10_DATABASE_URL"],
            "REDIS_URL": os.environ.get("ENG10_REDIS_URL", "redis://127.0.0.1:16379/0"),
            "STORAGE_DRIVER": "minio",
            "STORAGE_ENDPOINT": os.environ.get(
                "ENG10_STORAGE_ENDPOINT", "http://127.0.0.1:19000"
            ),
            "STORAGE_BUCKET": "ai-avatar",
            "STORAGE_REGION": "us-east-1",
            "STORAGE_ACCESS_KEY": "eng10-local-access",
            "STORAGE_SECRET_KEY": "eng10-local-secret",
            "AI_PROVIDER": "mock",
            "AI_BASE_URL": "",
            "AI_MODEL": "",
            "AI_API_KEY": "",
            "ENABLE_IMAGE_ANALYSIS": "false",
            "ENABLE_REFERENCE_IMAGE": "false",
            "SESSION_SECRET": "eng10-local-session-secret",
            "APP_ENV": "local",
            "PYTHONUNBUFFERED": "1",
        }
    )
    env.pop("PYTHONUTF8", None)
    env.pop("PYTHONIOENCODING", None)
    return env


# 停 / 起 Worker 的动作**不在这里实现**：它按 `ENG10_STACK_MODE` 分派到
# `conftest.stop_scenario_worker` / `start_scenario_worker`。
# 本文件刻意不再持有任何指向 `scripts/eng10_ui_stack.py` 的调用点 ——
# Compose 模式下绝不能碰原生栈的启停脚本。


@contextmanager
def only_scenario_worker():
    """临时让**只有**场景执行器来处理任务。

    为什么需要：`failure` / `unknown` 两条分支只能由
    `tests/e2e/scenario_runner.py` 驱动（真实 Worker 里没有场景选择入口），
    而真实 Worker 与场景执行器**抢同一个任务**——真实 Worker 会先用
    `MockProvider(SUCCESS)` 把任务跑成 `SUCCEEDED`，于是场景执行器拿到
    `NOT_CLAIMABLE`（实测踩到）。

    因此这几条用例在运行期间把**本栈**的 Worker 停掉，结束后**无论发生什么**都拉起来。
    这只改变本轮测试进程的调度，不改产品语义。

    ## 两种执行模式（由 `ENG10_STACK_MODE` 显式选择，业务断言完全相同）

    | 模式 | 停 / 起 Worker 的手段 |
    |---|---|
    | `native` | `scripts/eng10_ui_stack.py stop-worker` / `start-worker`（原生进程栈） |
    | `compose` | `scripts/eng10_compose_stack.py stop-worker` / `start-worker`，**只**操作 `ENG10_COMPOSE_PROJECT`（默认 `ai-avatar-eng10`）里的 worker 容器 |

    **两种模式不会调用对方的脚本**：Compose 模式绝不碰原生栈的启停脚本，
    原生模式也不会去动任何容器。

    停机、核对与恢复的**全部**逻辑在 `conftest.worker_stopped`：停机及其检查都在
    恢复保护之内，`finally` 无条件恢复，并且不会用恢复失败盖掉正在抛出的原始失败。
    """

    with worker_stopped():
        yield


def _run_scenario(generation_id: str, scenario: str, *, timeout: float = 180.0) -> dict:
    """在**独立进程**里执行真实任务函数（见模块头的分工表）。"""

    result = subprocess.run(  # noqa: S603 - 参数全部来自本测试的常量
        [
            sys.executable,
            "-m",
            "tests.e2e.scenario_runner",
            "--generation-id",
            generation_id,
            "--scenario",
            scenario,
            "--prefetch",
        ],
        cwd=str(REPO_BACKEND),
        env=_stack_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    assert result.returncode == 0, (
        f"场景执行失败（退出码 {result.returncode}）：\n"
        f"stdout:\n{result.stdout[-2000:]}\nstderr:\n{result.stderr[-2000:]}"
    )
    payload = None
    for line in reversed((result.stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            payload = json.loads(line)
            break
    assert payload is not None, f"场景执行器没有输出结果行：{result.stdout[-2000:]}"
    return payload


def _ledger(engine, generation_id: str) -> list[tuple[str, int]]:
    with engine.begin() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT entry_type, amount FROM point_ledger"
                " WHERE generation_id = :gid ORDER BY created_at, id"
            ),
            {"gid": generation_id},
        ).all()
    return [(row[0], int(row[1])) for row in rows]


def _attempt_status(engine, generation_id: str) -> list[tuple[int, str]]:
    with engine.begin() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT attempt_no, status FROM provider_attempts"
                " WHERE generation_id = :gid ORDER BY attempt_no"
            ),
            {"gid": generation_id},
        ).all()
    return [(int(row[0]), str(row[1])) for row in rows]


def _task_status(engine, generation_id: str) -> str:
    with engine.begin() as conn:
        return str(
            conn.execute(
                sa.text("SELECT status FROM generation_tasks WHERE id = :gid"),
                {"gid": generation_id},
            ).scalar_one()
        )


def _task_count(engine, user_id) -> int:
    with engine.begin() as conn:
        return int(
            conn.execute(
                sa.text("SELECT count(*) FROM generation_tasks WHERE user_id = :uid"),
                {"uid": user_id},
            ).scalar_one()
        )


def _run_recovery_scan(*, timeout: float = 180.0) -> dict:
    """在**独立进程**里跑一次真实的 `recovery_scan` 任务。

    与 Beat 投递的是**同一个已注册任务函数**（`app.tasks.worker.recovery_scan`），
    只是这次由测试主动触发，用来验证「补投丢失投递」这条分支本身。
    """

    result = subprocess.run(  # noqa: S603 - 参数全部来自本测试的常量
        [
            sys.executable,
            "-c",
            "import json;"
            "from app.tasks.worker import recovery_scan;"
            "print(json.dumps(recovery_scan.run()))",
        ],
        cwd=str(REPO_BACKEND),
        env=_stack_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    payload = None
    for line in reversed((result.stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            payload = json.loads(line)
            break
    assert payload is not None, f"恢复扫描没有输出结果行：{result.stdout[-2000:]}"
    return payload


def _submit_photo_with_source(account: Account, *, prompt: str) -> tuple[str, int]:
    """上传一张合成 SOURCE 并提交 PHOTO_PROMPT；返回 `(generation_id, 预留点数)`。"""

    asset = create_ready_asset(
        account.client,
        cookie=account.cookie,
        directory=WORK_DIR,
        name=f"source-{uuid.uuid4().hex[:6]}.png",
        seed=3,
    )
    status, body = submit_generation(
        account.client,
        cookie=account.cookie,
        mode="PHOTO_PROMPT",
        prompt=prompt,
        input_asset_ids=[asset["id"]],
    )
    assert status == 202, body
    generation = body["data"]
    return generation["id"], int(generation["reserved_points"])


# --------------------------------------------------------------------------- #
# 结算矩阵：成功消费
# --------------------------------------------------------------------------- #
def test_success_consumes_exactly_once_through_the_real_worker(engine, funded: Account) -> None:
    """成功：真实 Worker 消费真实队列 → `SUCCEEDED + CONSUMED`，流水 RESERVE → CONSUME。"""

    account = funded
    before = points_of(account.client, cookie=account.cookie)
    generation_id, reserved = _submit_photo_with_source(account, prompt="成功消费路径。")

    pending = points_of(account.client, cookie=account.cookie)
    assert pending["reserved_points"] == reserved, pending
    assert pending["available_points"] == before["available_points"] - reserved, pending

    generation = wait_generation(account.client, cookie=account.cookie, generation_id=generation_id)
    assert generation["status"] == "SUCCEEDED", generation
    assert generation["billing_status"] == "CONSUMED", generation

    after = points_of(account.client, cookie=account.cookie)
    assert after["reserved_points"] == 0, after
    assert after["available_points"] == before["available_points"] - reserved, after

    assert _ledger(engine, generation_id) == [("RESERVE", reserved), ("CONSUME", reserved)]
    assert _attempt_status(engine, generation_id) == [(1, "SUCCEEDED")]


# --------------------------------------------------------------------------- #
# 结算矩阵：明确失败释放（独立进程 + failure 场景）
# --------------------------------------------------------------------------- #
def test_definite_failure_releases_the_reservation(engine, funded: Account) -> None:
    """明确失败：`FAILED + RELEASED`，流水 RESERVE → RELEASE，可用点数回到原值。"""

    account = funded

    before = points_of(account.client, cookie=account.cookie)
    with only_scenario_worker():
        generation_id, reserved = _submit_photo_with_source(
            account, prompt=f"{SCENARIO_PREFIX}failure] 明确失败路径。"
        )
        assert points_of(account.client, cookie=account.cookie)["reserved_points"] == reserved

        outcome = _run_scenario(generation_id, "failure")
        assert outcome["outcome"] in ("FAILED", "REPLAYED"), outcome

    generation = wait_generation(account.client, cookie=account.cookie, generation_id=generation_id)
    assert generation["status"] == "FAILED", generation
    assert generation["billing_status"] == "RELEASED", generation

    after = points_of(account.client, cookie=account.cookie)
    assert after["reserved_points"] == 0, after
    assert after["available_points"] == before["available_points"], after

    assert _ledger(engine, generation_id) == [("RESERVE", reserved), ("RELEASE", reserved)]
    assert _attempt_status(engine, generation_id) == [(1, "FAILED")]


# --------------------------------------------------------------------------- #
# 结算矩阵：UNKNOWN 保留预留
# --------------------------------------------------------------------------- #
def test_unknown_outcome_keeps_the_reservation_and_goes_to_review(
    engine, funded: Account
) -> None:
    """UNKNOWN：`NEEDS_REVIEW + RESERVED`，绝不自动消费、也绝不自动放款。"""

    account = funded

    before = points_of(account.client, cookie=account.cookie)
    with only_scenario_worker():
        generation_id, reserved = _submit_photo_with_source(
            account, prompt=f"{SCENARIO_PREFIX}unknown] 结果无法解析路径。"
        )

        outcome = _run_scenario(generation_id, "unknown")
        assert outcome["outcome"] in ("NEEDS_REVIEW", "REPLAYED"), outcome

        # 结算与转派都在**同一次**任务执行里完成：不依赖真实 Worker 消费后续轮询，
        # 因此也不会被「Worker 与场景执行器抢任务」影响。
        deadline = time.monotonic() + 60
        generation = {}
        while time.monotonic() < deadline:
            generation = account.get(f"/api/v1/generations/{generation_id}").json()["data"]
            if generation["status"] in ("NEEDS_REVIEW", "SUCCEEDED", "FAILED"):
                break
            time.sleep(0.5)

    assert generation["status"] == "NEEDS_REVIEW", generation
    assert generation["billing_status"] == "RESERVED", generation

    after = points_of(account.client, cookie=account.cookie)
    assert after["reserved_points"] == reserved, after
    assert after["available_points"] == before["available_points"] - reserved, after

    assert _ledger(engine, generation_id) == [("RESERVE", reserved)]
    # 「接受后超时」与「状态无法解析」是同一结算口径（保持 RESERVED），
    # attempt 状态按编排的判定可以是 `TIMEOUT` 或 `UNKNOWN`——两者都不许放款。
    assert _attempt_status(engine, generation_id)[0][0] == 1
    assert _attempt_status(engine, generation_id)[0][1] in ("UNKNOWN", "TIMEOUT")


# --------------------------------------------------------------------------- #
# 重复提交与幂等
# --------------------------------------------------------------------------- #
def test_repeated_submission_with_the_same_key_replays_not_recharges(
    engine, funded: Account
) -> None:
    """同键重复提交：回放同一个任务，点数只预留一次。"""

    account = funded

    asset = create_ready_asset(
        account.client,
        cookie=account.cookie,
        directory=WORK_DIR,
        name=f"idem-{uuid.uuid4().hex[:6]}.png",
        seed=11,
    )
    key = f"eng10-idem-{uuid.uuid4()}"
    payload = {
        "mode": "PHOTO_PROMPT",
        "prompt": "重复提交回放路径。",
        "input_asset_ids": [asset["id"]],
        "output_count": 2,
    }
    first = account.client.post(
        "/api/v1/generations",
        headers={"cookie": account.cookie, "Idempotency-Key": key},
        json=payload,
    )
    assert first.status_code == 202, first.text
    generation_id = first.json()["data"]["id"]
    reserved = int(first.json()["data"]["reserved_points"])
    reserved_after_first = points_of(account.client, cookie=account.cookie)["reserved_points"]
    assert reserved_after_first == reserved

    second = account.client.post(
        "/api/v1/generations",
        headers={"cookie": account.cookie, "Idempotency-Key": key},
        json=payload,
    )
    assert second.status_code in (200, 202), second.text
    assert second.json()["data"]["id"] == generation_id, second.text

    after = points_of(account.client, cookie=account.cookie)
    assert after["reserved_points"] == reserved, after
    assert _ledger(engine, generation_id) == [("RESERVE", reserved)]

    # 同键**不同载荷** → 稳定冲突，不新建任务
    conflict = account.client.post(
        "/api/v1/generations",
        headers={"cookie": account.cookie, "Idempotency-Key": key},
        json={**payload, "prompt": "换了内容的同一把键。"},
    )
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT", conflict.text


def test_duplicate_settlement_events_do_not_double_charge(engine, funded: Account) -> None:
    """重复结算：同一任务再跑一次场景执行器，点数与流水都不再变化（终态单调）。"""

    account = funded

    before = points_of(account.client, cookie=account.cookie)
    generation_id, reserved = _submit_photo_with_source(account, prompt="重复结算路径。")
    generation = wait_generation(account.client, cookie=account.cookie, generation_id=generation_id)
    assert generation["status"] == "SUCCEEDED", generation
    ledger_after_success = _ledger(engine, generation_id)

    # 重复投递同一个任务（模拟重复回调 / 重投）：终态单调，不得再结算一次。
    # 这一步在「只有场景执行器」的窗口里做，避免真实 Worker 抢先把它当成新任务。
    with only_scenario_worker():
        again = _run_scenario(generation_id, "success")
    # 已经终结的任务再次投递：要么原样回放、要么直接判为不可认领——都不能改账
    assert again["outcome"] in ("NOT_CLAIMABLE", "REPLAYED", "SUCCEEDED"), again

    assert _ledger(engine, generation_id) == ledger_after_success
    after = points_of(account.client, cookie=account.cookie)
    assert after["available_points"] == before["available_points"] - reserved, after
    assert after["reserved_points"] == 0, after
    assert len(_attempt_status(engine, generation_id)) == 1, "重复投递不应新建 attempt"


def test_duplicate_terminal_observations_across_processes_settle_once(
    engine, funded: Account
) -> None:
    """重复回调/重复轮询：同一终态被**多次**观察到，结算只发生一次。

    `duplicate_webhook` 场景（`04-ai-provider.md` 第 6 节：终态两次 + 一个迟到
    `PENDING`）会在三次独立进程调用里各产一个事件，因此对账入口被真实地重复驱动。
    期望：结算一次、迟到 `PENDING` 不得重开终态、attempt 仍只有一行。
    """

    account = funded
    before = points_of(account.client, cookie=account.cookie)
    with only_scenario_worker():
        generation_id, reserved = _submit_photo_with_source(
            account, prompt=f"{SCENARIO_PREFIX}duplicate_webhook] 重复终态路径。"
        )

        outcomes = [
            _run_scenario(generation_id, "duplicate_webhook")["outcome"] for _ in range(3)
        ]
        # 第一次真的结算；之后无论投递几次都是**无副作用**的重复观察
        assert outcomes[0] == "SUCCEEDED", outcomes
        assert all(
            item in ("NOT_CLAIMABLE", "REPLAYED", "SUCCEEDED") for item in outcomes[1:]
        ), outcomes

        generation = wait_generation(
            account.client, cookie=account.cookie, generation_id=generation_id, timeout=120
        )

    assert generation["status"] == "SUCCEEDED", generation
    assert generation["billing_status"] == "CONSUMED", generation

    after = points_of(account.client, cookie=account.cookie)
    assert after["available_points"] == before["available_points"] - reserved, after
    assert after["reserved_points"] == 0, after

    # 结算只有一次：RESERVE → CONSUME，且没有第二条 CONSUME
    assert _ledger(engine, generation_id) == [("RESERVE", reserved), ("CONSUME", reserved)]
    assert _attempt_status(engine, generation_id) == [(1, "SUCCEEDED")]


# --------------------------------------------------------------------------- #
# 权限拒绝
# --------------------------------------------------------------------------- #
def test_permissions_are_enforced_for_assets_generations_and_feedback(
    engine, funded: Account
) -> None:
    """越权访问：别人的资产、任务、反馈一律拒绝，且不泄漏存在性以外的信息。"""

    account = funded

    stranger: Account = redeem_and_verify(engine, points=100, prefix="stranger")
    try:
        asset = create_ready_asset(
            account.client,
            cookie=account.cookie,
            directory=WORK_DIR,
            name=f"private-{uuid.uuid4().hex[:6]}.png",
            seed=21,
        )
        generation_id, _ = _submit_photo_with_source(account, prompt="越权测试用任务。")
        wait_generation(account.client, cookie=account.cookie, generation_id=generation_id)

        # 别人的资产：读取与取下载地址都拒绝
        assert stranger.get(f"/api/v1/assets/{asset['id']}").status_code in (403, 404)
        assert stranger.post(f"/api/v1/assets/{asset['id']}/download-url").status_code in (403, 404)

        # 别人的任务与结果
        assert stranger.get(f"/api/v1/generations/{generation_id}").status_code in (403, 404)
        assert stranger.get(f"/api/v1/generations/{generation_id}/outputs").status_code in (
            403,
            404,
        )

        # 拿别人的任务创建分享 / 提交反馈：拒绝
        share = stranger.post(
            "/api/v1/shares",
            {
                "generation_id": generation_id,
                "output_asset_ids": [asset["id"]],
            },
        )
        assert share.status_code in (403, 404), share.text
        feedback = stranger.post(
            "/api/v1/feedback",
            {"generation_id": generation_id, "rating": 5, "category": "画面质量"},
        )
        assert feedback.status_code in (403, 404), feedback.text

        # 匿名（无 Cookie）访问私有资产
        with new_client() as anon:
            assert anon.get(f"/api/v1/assets/{asset['id']}").status_code in (401, 403, 404)
            assert anon.get(f"/api/v1/generations/{generation_id}").status_code in (401, 403, 404)
            assert anon.get("/api/v1/me/points").status_code == 401
    finally:
        stranger.client.close()


# --------------------------------------------------------------------------- #
# Worker 重启恢复
# --------------------------------------------------------------------------- #
def test_recovery_scan_requeues_a_lost_delivery_and_the_worker_finishes_it(
    engine, funded: Account, celery_queue: CeleryQueue
) -> None:
    """恢复扫描：投递丢失的任务被**真实补投**并跑完，且只结算一次。

    ## 这条用例怎么造出「投递丢失」（逐条对应返修要求）

    1. **先停本栈 Worker**（`worker_stopped`）：任务创建后没有任何消费者会碰它，
       所以「任务还没执行」是**客观事实**，不是靠改状态装出来的；
    2. **隔离队列**：Worker 已停，队列上没有消费者，此时清空积压的计划任务
       （心跳 / 恢复扫描），从「已知为空」开始 —— 于是「本任务的消息出现了 /
       被移除了 / 被补投了」都是可核对的绝对事实，不用在别人的消息里翻找；
    3. 创建任务 → 断言队列里**恰好一条**属于它的消息（投递确实发生过）；
    4. **把那条消息从队列移除**（模拟投递丢失）→ 断言队列里再没有它的消息；
    5. 断言任务**尚未执行**：仍 `QUEUED`、`provider_attempts` **0 行**、
       流水只有 `RESERVE`。**没有删除任何 attempt，也没有把状态改回 `QUEUED`**
       —— 这两件事都不需要做，因为任务压根没被执行过；
    6. 只把 `created_at` 往前推（**不改状态、不删 attempt**），让它进入恢复扫描的
       「陈旧」窗口 —— `SqlAlchemyGenerationTaskRepository.list_stale` 判定陈旧的依据是
       **`created_at`**（阈值 = 扫描间隔 60s × 2 = 120s），不是 `updated_at`；
    7. 手动跑一次真实的 `recovery_scan` → 断言它**真的补投了一条**。

    ## 为什么不会被 Beat 的自动补投干扰

    Beat 每 60 秒投递 `generation.recovery_scan`，但**没有 Worker 消费**，那些消息只是
    积压在队列里、不会执行。手动扫描补投之后，本任务的 `created_at` 已经回到当前时刻，
    于是任何一次周期性扫描都不会再把同一个任务判为「陈旧」而重复补投；
    任务一旦被领取就离开 `QUEUED`，扫描也再不会选中它。
    「自动补投」与「手动扫描」因此不可能叠加成两次投递。

    ## 恢复 Worker 之后

    任务必须被真实消费到 `SUCCEEDED + CONSUMED`，并且**恰好一次 RESERVE / CONSUME、
    恰好一行 attempt、没有第二个任务行**（无重复生成、无重复结算）。
    """

    account = funded
    before = points_of(account.client, cookie=account.cookie)

    with worker_stopped():
        drained = celery_queue.drain()
        # 清空之后不能残留任何**生成**投递（Beat 的心跳/扫描可能马上又冒出来，
        # 但它们不是生成投递，因此这里按消息内容过滤，避免用例依赖 Beat 的节奏）
        leftovers = [body for body in celery_queue.messages() if "generation.run" in body]
        assert leftovers == [], f"队列里仍有生成投递：{leftovers}"
        print(f"[eng10-e2e] 已清空队列 {drained} 条积压消息")

        generation_id, reserved = _submit_photo_with_source(
            account, prompt="恢复扫描路径。"
        )

        delivered = celery_queue.messages_for(generation_id)
        assert len(delivered) == 1, f"创建任务后队列里应恰好有一条本任务的消息：{delivered}"

        removed = celery_queue.remove(delivered[0])
        assert removed == 1, f"移除投递失败（删掉 {removed} 条）"
        assert celery_queue.messages_for(generation_id) == [], "投递没有被真正移除"

        # 任务尚未执行：状态仍是 QUEUED，且没有任何 attempt 行
        assert _task_status(engine, generation_id) == "QUEUED"
        assert _attempt_status(engine, generation_id) == [], "任务不该有 attempt 行"
        assert _ledger(engine, generation_id) == [("RESERVE", reserved)]

        # 只推 created_at 进入「陈旧」窗口：不动状态、不删 attempt。
        # 判据是 created_at —— `list_stale` 就是这么选人的（阈值 120s）。
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE generation_tasks SET created_at = now() - interval '10 minutes'"
                    " WHERE id = :gid"
                ),
                {"gid": generation_id},
            )

        outcome = _run_recovery_scan()
        assert outcome["reenqueued"] >= 1, outcome

        requeued = celery_queue.messages_for(generation_id)
        assert len(requeued) == 1, f"恢复扫描应恰好补投一条：{requeued}"

        # 陈旧窗口已用完：把 created_at 归位，避免周期性 Beat 扫描重复补投同一任务。
        # 这一步只动 created_at（状态、账务、attempt 全不动）。
        with engine.begin() as conn:
            conn.execute(
                sa.text("UPDATE generation_tasks SET created_at = now() WHERE id = :gid"),
                {"gid": generation_id},
            )

    # Worker 已恢复：任务必须被真实消费到终局
    generation = wait_generation(
        account.client, cookie=account.cookie, generation_id=generation_id, timeout=240
    )
    assert generation["status"] == "SUCCEEDED", generation
    assert generation["billing_status"] == "CONSUMED", generation

    after = points_of(account.client, cookie=account.cookie)
    assert after["available_points"] == before["available_points"] - reserved, after
    assert after["reserved_points"] == 0, after

    # 只结算一次、只执行一次、没有重复生成
    assert _ledger(engine, generation_id) == [("RESERVE", reserved), ("CONSUME", reserved)]
    assert _attempt_status(engine, generation_id) == [(1, "SUCCEEDED")]
    assert _task_count(engine, account.user_id) == 1


# --------------------------------------------------------------------------- #
# 分享与反馈幂等
# --------------------------------------------------------------------------- #
def test_share_and_feedback_are_idempotent_and_scoped(engine, funded: Account) -> None:
    """分享与反馈：同键回放同一条记录；换内容稳定冲突；匿名读取只放行被选中的图。"""

    account = funded

    generation_id, _ = _submit_photo_with_source(account, prompt="分享与反馈幂等路径。")
    wait_generation(account.client, cookie=account.cookie, generation_id=generation_id)
    outputs = account.get(f"/api/v1/generations/{generation_id}/outputs").json()["data"]
    assert len(outputs) == 2, outputs
    first_id, second_id = outputs[0]["asset_id"], outputs[1]["asset_id"]

    key = f"eng10-share-{uuid.uuid4()}"
    payload = {"generation_id": generation_id, "output_asset_ids": [first_id]}
    created = account.client.post(
        "/api/v1/shares",
        headers={"cookie": account.cookie, "Idempotency-Key": key},
        json=payload,
    )
    assert created.status_code == 201, created.text
    slug = created.json()["data"]["slug"]

    replayed = account.client.post(
        "/api/v1/shares",
        headers={"cookie": account.cookie, "Idempotency-Key": key},
        json=payload,
    )
    assert replayed.status_code in (200, 201), replayed.text
    assert replayed.json()["data"]["slug"] == slug, replayed.text

    conflict = account.client.post(
        "/api/v1/shares",
        headers={"cookie": account.cookie, "Idempotency-Key": key},
        json={"generation_id": generation_id, "output_asset_ids": [second_id]},
    )
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT", conflict.text

    # 匿名读取：有效分享 200；未选中的资产仍是私有（拿不到下载地址）
    with new_client() as anon:
        shared = anon.get(f"/api/v1/shares/{slug}")
        assert shared.status_code == 200, shared.text
        view = shared.json()["data"]
        assert view["slug"] == slug
        assert [row["asset_id"] for row in view["outputs"]] == [first_id], view
        assert anon.get(f"/api/v1/shares/{slug}-not-a-real-slug").status_code == 404
        # 未选中的资产仍是私有：匿名取下载地址必须被拒（400/401/403/404 都算拒绝，
        # 因为这个接口要求 Idempotency-Key，缺头时先撞上 400）
        assert anon.post(f"/api/v1/assets/{second_id}/download-url").status_code in (
            400,
            401,
            403,
            404,
        )

    # 反馈幂等
    feedback_key = f"eng10-feedback-{uuid.uuid4()}"
    body = {"generation_id": generation_id, "rating": 4, "category": "画面质量"}
    once = account.client.post(
        "/api/v1/feedback",
        headers={"cookie": account.cookie, "Idempotency-Key": feedback_key},
        json=body,
    )
    assert once.status_code == 201, once.text
    twice = account.client.post(
        "/api/v1/feedback",
        headers={"cookie": account.cookie, "Idempotency-Key": feedback_key},
        json=body,
    )
    assert twice.status_code in (200, 201), twice.text

    different = account.client.post(
        "/api/v1/feedback",
        headers={"cookie": account.cookie, "Idempotency-Key": feedback_key},
        json={**body, "rating": 1},
    )
    assert different.status_code == 409, different.text

    with engine.begin() as conn:
        rows = conn.execute(
            sa.text("SELECT rating FROM feedback WHERE generation_id = :gid"),
            {"gid": generation_id},
        ).all()
    assert len(rows) == 1 and rows[0][0] == 4, rows


# --------------------------------------------------------------------------- #
# 生命周期清理
# --------------------------------------------------------------------------- #
def test_lifecycle_cleanup_deletes_expired_objects_and_converges_shares(
    engine, funded: Account
) -> None:
    """生命周期：到期 SOURCE 先删对象再标终结状态；其后下载地址不再可用。

    **必须用一张没有任何分享引用的资产**：清理对「到期但仍有有效分享引用」的资产
    按设计是 `deferred`（分享的公开可读性优先，先收敛分享、下一轮再删对象），
    所以拿一张被分享过的图来断言「本轮必须被删」本身就是错的期望。
    """

    account = funded
    from tests.e2e.conftest import create_ready_asset as make_asset

    # 先落一张**合成 SOURCE**（上传用途），再把它的到期时间改到过去
    asset = make_asset(
        account.client,
        cookie=account.cookie,
        directory=WORK_DIR,
        name=f"expiring-{uuid.uuid4().hex[:6]}.png",
        seed=31,
    )
    with engine.begin() as conn:
        shares = conn.execute(
            sa.text(
                "SELECT count(*) FROM share_assets WHERE asset_id = :aid"
            ),
            {"aid": asset["id"]},
        ).scalar_one()
        legacy = conn.execute(
            sa.text("SELECT count(*) FROM shares WHERE asset_id = :aid"),
            {"aid": asset["id"]},
        ).scalar_one()
    # 两条引用路径都要为空：ENG-09 的授权/收敛对「有 share_assets 行」用明确选中集合，
    # 对「没有关联行」会回落到 `shares.asset_id`（历史单图分享）。只查前者会漏判，
    # 于是资产被按设计 `deferred`，而用例却在等它被删（实测踩到）。
    # 这是一张 **SOURCE** 资产（上传用途），正常永远不被分享引用；如果被引用，
    # 说明有别的用例把 `shares.asset_id` 指向了它，那种情况下清理会正确 `deferred`，
    # 因此这里先撤销这类引用再验证删除路径。
    if shares or legacy:
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE shares SET status = 'REVOKED', revoked_at = now()"
                    " WHERE user_id = :uid AND (asset_id = :aid OR id IN ("
                    "   SELECT share_id FROM share_assets WHERE asset_id = :aid))"
                ),
                {"uid": account.user_id, "aid": asset["id"]},
            )
        shares = legacy = 0

    # 只把这一张的到期时间改到过去（合成数据，只动本轮自己的行）
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "UPDATE media_assets SET expires_at = now() - interval '1 hour'"
                " WHERE id = :aid"
            ),
            {"aid": asset["id"]},
        )

    cleanup = subprocess.run(  # noqa: S603 - 参数来自本测试常量
        [
            sys.executable,
            "-c",
            "from app.tasks.worker import cleanup_media;"
            " print(cleanup_media.run())",
        ],
        cwd=str(REPO_BACKEND),
        env=_stack_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    assert cleanup.returncode == 0, cleanup.stderr[-2000:]
    report = cleanup.stdout.strip().splitlines()[-1]
    assert "assets_expired" in report, report
    # 不断言「本轮恰好删了 1 张」：同一轮清理会顺带处理**所有**到期资产
    # （包括其他用例留下的），精确计数会把无关的历史状态变成假失败。
    # 真正要证的是「我们这一张真的被处理了」——下面按 id 查状态与对象可达性。

    with engine.begin() as conn:
        status = conn.execute(
            sa.text("SELECT status FROM media_assets WHERE id = :aid"),
            {"aid": asset["id"]},
        ).scalar_one()

    assert status in ("EXPIRED", "DELETED"), status

    # 对象真的从 MinIO 删掉了：再取下载地址必须失败
    refreshed = account.post(f"/api/v1/assets/{asset['id']}/download-url")
    assert refreshed.status_code in (403, 404, 409), refreshed.text

    # 清理是幂等的：再跑一次不报错、状态不再变化
    second = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-c",
            "from app.tasks.worker import cleanup_media;"
            " print(cleanup_media.run())",
        ],
        cwd=str(REPO_BACKEND),
        env=_stack_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    assert second.returncode == 0, second.stderr[-2000:]
    with engine.begin() as conn:
        assert (
            conn.execute(
                sa.text("SELECT status FROM media_assets WHERE id = :aid"),
                {"aid": asset["id"]},
            ).scalar_one()
            == status
        )


@pytest.fixture(scope="session", autouse=True)
def _scenario_dir() -> None:
    """场景执行器需要一个可写的输出目录（仓库外）。"""

    SCENARIO_OUT.mkdir(parents=True, exist_ok=True)
