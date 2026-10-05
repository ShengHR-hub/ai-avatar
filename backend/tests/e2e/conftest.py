"""ENG-10 浏览器/HTTP E2E 的夹具与工具（设备与场景无关的公共部分）。

## 依赖什么

只依赖**已经在跑的真实栈**（端口由环境变量给出）：

```text
ENG10_WEB_URL        默认 http://127.0.0.1:13000   Next.js
ENG10_API_URL        默认 http://127.0.0.1:18001   FastAPI（tests/e2e/local_server.py 邮件替身入口）
ENG10_DATABASE_URL   默认 $DATABASE_URL            用于合成数据与状态断言
ENG10_MAIL_DIR       默认 D:/tmp/eng10-run/mail    测试邮件落盘目录
ENG10_ARTIFACTS      默认 D:/tmp/eng10-run/artifacts 截图等取证
ENG10_SCENARIO_OUT   默认 D:/tmp/eng10-run/scenarios 场景执行器的预生成输出
```

栈没起来时用例**直接失败**并给出可执行的启动命令，不做任何静默跳过 ——
「跳过」会让一条没跑过的 E2E 看起来像通过（`LEAD-06` 的 E2E 门禁要求）。

## 本轮**没有** Provider 替身

真实 Worker（容器）跑 `AI_PROVIDER=mock` 的 `SUCCESS` 场景。结算矩阵里的
`failure` / `unknown` 由 `tests/e2e/scenario_runner.py` 在**独立进程**里驱动真实的
`app.tasks.worker.run_generation` 覆盖，本文件不做任何 Provider 替身。

## 两个必须如实记录的宿主环境事实（都不是产品缺陷，但会改变测试写法）

1. **会话 Cookie 的 Secure 属性**：会话 Cookie 是 `HttpOnly + Secure + SameSite=Lax`
   （契约第 3 节）。本机 shell 里有一个 HTTP 代理（DSH 沙箱注入的
   `HTTP_PROXY`），`httpx` 的 cookie jar **不会**把 `Secure` cookie 放进明文 HTTP
   请求，于是走 jar 会得到 401，而显式 `cookie:` 头可以。真实浏览器把 loopback
   当可信来源，会正常发送 —— 所以本文件的 HTTP 客户端统一：
   `trust_env=False` + 显式 `cookie` 头。
2. **CDP 的 `--remote-debugging-port`** 必须写成 `=` 连接的单参数
   （见 `cdp.py` 的说明）。
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
import redis as redis_lib
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from tests.e2e.cdp import Chrome, SyncPage, open_page
from tests.e2e.images import sha256_hex, synthetic_image_file, synthetic_png

WEB_URL = os.environ.get("ENG10_WEB_URL", "http://127.0.0.1:13000")
API_URL = os.environ.get("ENG10_API_URL", "http://127.0.0.1:18001")
MAIL_DIR = Path(os.environ.get("ENG10_MAIL_DIR", "D:/tmp/eng10-run/mail"))
ARTIFACTS = Path(os.environ.get("ENG10_ARTIFACTS", "D:/tmp/eng10-run/artifacts"))
SCENARIO_OUT = Path(os.environ.get("ENG10_SCENARIO_OUT", "D:/tmp/eng10-run/scenarios"))

#: 每个会话独立的合成邮箱后缀，避免与上一轮的合成用户混淆
_RUN_TAG = os.environ.get("ENG10_RUN_TAG") or uuid.uuid4().hex[:10]

__all__ = [
    "API_URL",
    "ARTIFACTS",
    "MAIL_DIR",
    "QUEUE_NAME",
    "SCENARIO_OUT",
    "STACK_MODE",
    "WEB_URL",
    "Account",
    "CeleryQueue",
    "api_json",
    "complete_upload",
    "create_ready_asset",
    "hash_invite_code",
    "new_email",
    "new_invite_code",
    "new_mail_token",
    "points_of",
    "read_mail_tokens",
    "redeem_and_verify",
    "seed_invite_code",
    "sha256_hex",
    "start_scenario_worker",
    "stop_scenario_worker",
    "submit_generation",
    "synthetic_image_file",
    "synthetic_png",
    "wait_generation",
    "worker_stopped",
]


# --------------------------------------------------------------------------- #
# 合成数据（不使用任何真实用户数据）
# --------------------------------------------------------------------------- #
def hash_invite_code(raw: str) -> str:
    """与 `app.services.auth.invite_code.hash_invite_code` 同一算法（明文不落库）。"""

    return hashlib.sha256(raw.strip().upper().encode("utf-8")).hexdigest()


def new_email(prefix: str = "eng10") -> str:
    """一枚本会话专属的合成邮箱地址。"""

    return f"{prefix}+{_RUN_TAG}-{secrets.token_hex(4)}@example.com"


def new_invite_code() -> str:
    """一枚合成邀请码明文（只在用例里出现，库里只存哈希）。"""

    return f"ENG10{_RUN_TAG.upper()}{secrets.token_hex(3).upper()}"


# --------------------------------------------------------------------------- #
# 执行模式：native（原生进程栈） | compose（Compose 容器栈）
#
# 两种形态的**业务断言完全相同**，差别只有两处：
#   ① 怎么停/起 Worker（原生进程 vs 本项目隔离容器）；
#   ② 怎么核对「被测的 API / 浏览器入口 / DB / Redis / MinIO 属于同一个栈」。
# 模式必须**显式声明**，绝不猜测：猜错会让 Worker 控制打到别的栈上。
# --------------------------------------------------------------------------- #
STACK_MODE = os.environ.get("ENG10_STACK_MODE", "native").strip().lower()
VALID_STACK_MODES = ("native", "compose")
#: Compose 模式下要操作的隔离项目（脚本内还有一层护栏，拒绝默认本地项目）
COMPOSE_PROJECT = os.environ.get("ENG10_COMPOSE_PROJECT", "ai-avatar-eng10")
REPO_ROOT = Path(__file__).resolve().parents[3]

_START_HINTS = {
    "native": (
        "  docker compose --env-file .env.eng10 -p ai-avatar-eng10 "
        "-f docker/compose.yml up -d postgres redis minio\n"
        "  python scripts/eng10_ui_stack.py up"
    ),
    "compose": (
        "  docker compose --env-file .env.eng10 -p ai-avatar-eng10 "
        "-f docker/compose.yml -f docker/compose.e2e.yml up -d --build"
    ),
}


# --------------------------------------------------------------------------- #
# 栈可达性：**不做静默跳过**
# --------------------------------------------------------------------------- #
def _probe(label: str, url: str, timeout: float = 10.0) -> None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            if response.status >= 500:
                raise RuntimeError(f"{label} 返回 {response.status}")
    except Exception as error:  # noqa: BLE001
        hint = _START_HINTS.get(STACK_MODE, "（未知 ENG10_STACK_MODE）")
        raise RuntimeError(
            f"{label} 不可达（{url}）：{error}\n"
            f"当前 ENG10_STACK_MODE={STACK_MODE!r}，先起对应的栈：\n{hint}"
        ) from error


def _run_script(script: str, *args: str, timeout: float = 180.0) -> subprocess.CompletedProcess:
    """调 `scripts/` 下的栈控制脚本（两种模式各用各的，互不调用）。"""

    return subprocess.run(  # noqa: S603 - 参数来自本模块常量
        [sys.executable, str(REPO_ROOT / "scripts" / script), *args],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def stop_scenario_worker() -> subprocess.CompletedProcess:
    """停掉本栈的 Worker，让**只有场景执行器**能处理任务。

    - `native`：`scripts/eng10_ui_stack.py stop-worker`（原生进程栈）
    - `compose`：`scripts/eng10_compose_stack.py stop-worker`（**只**操作隔离项目的容器）

    两种模式**不会调用对方的脚本**：Compose 模式绝不碰原生栈的启停脚本。
    """

    if STACK_MODE == "compose":
        return _run_script("eng10_compose_stack.py", "stop-worker")
    return _run_script("eng10_ui_stack.py", "stop-worker")


def start_scenario_worker() -> subprocess.CompletedProcess:
    """把本栈的 Worker 拉回来，并**等它就绪**才返回（两边的脚本各自负责）。"""

    if STACK_MODE == "compose":
        return _run_script("eng10_compose_stack.py", "start-worker")
    return _run_script("eng10_ui_stack.py", "start-worker")


def _restore_worker(
    start_call: Callable[[], subprocess.CompletedProcess],
) -> str | None:
    """恢复本栈 Worker：成功返回 `None`，失败返回**可读的失败说明**。

    **这里绝不抛异常**。恢复动作的失败必须由调用方（`worker_stopped`）决定怎么呈现：
    有原始异常时保留原始异常，没有时让测试失败。失败有两种形态，都要接住：

    - 脚本**非零退出**（`returncode != 0`）；
    - **调用本身抛异常**：`subprocess.TimeoutExpired`（脚本卡住）、`OSError`
      （解释器或脚本起不来）等 —— 这类异常如果直接往外抛，会把正在传播的
      原始失败**顶掉**，于是「用例为什么失败」这条信息就丢了。
    """

    try:
        started = start_call()
    except Exception as error:  # noqa: BLE001 - 恢复路径绝不允许把异常抛出去盖掉原始失败
        return f"恢复 Worker 失败（调用异常）：{type(error).__name__}: {error}"
    if started.returncode != 0:
        return (
            f"恢复 Worker 失败（退出码 {started.returncode}）："
            f"{started.stdout}{started.stderr}"
        )
    return None


@contextmanager
def worker_stopped(
    *,
    stop: Callable[[], subprocess.CompletedProcess] | None = None,
    start: Callable[[], subprocess.CompletedProcess] | None = None,
) -> Iterator[None]:
    """停掉**本栈**的 Worker，退出时**无条件**恢复。

    返修要求逐条落实：

    1. **停机及其检查都在恢复保护之内**：`stop_call()` 的调用与它的 `returncode` 断言
       都放在 `try` 里 —— 「停止部分成功」「停止后核对失败」「用例本身失败」
       三种情况下 `finally` 都会尝试恢复；
    2. **进入 `finally` 时先把原始异常状态存下来**（`pending = sys.exc_info()`），
       后面所有恢复动作都不得改变「谁是主异常」；
    3. **恢复失败必须可见**：有原始异常时保留它并**明确打印**恢复失败；
       没有原始异常时**让测试失败**（绝不吞掉）；
    4. 就绪判据由各模式的脚本负责（原生：日志字节偏移；Compose：容器 `StartedAt`
       这条时间边界），都不接受历史 `ready.` 日志。

    `stop` / `start` 只是为**不启动真实资源**的故障注入测试留的注入口，
    默认值就是本栈的真实控制面，正常路径的行为一字未变。
    """

    stop_call = stop or stop_scenario_worker
    start_call = start or start_scenario_worker

    try:
        stopped = stop_call()
        if stopped.returncode != 0:
            raise AssertionError(
                f"停止 Worker 失败（退出码 {stopped.returncode}）："
                f"{stopped.stdout}{stopped.stderr}"
            )
        yield
    finally:
        # 进入 finally 的第一件事：把原始异常状态存下来，之后再判断恢复结果。
        pending = sys.exc_info()
        failure = _restore_worker(start_call)
        if failure is not None:
            if pending[0] is not None:
                print(f"[eng10-e2e] {failure}（原始失败优先，请先看上面的报错）", flush=True)
            else:
                raise AssertionError(failure)


# --------------------------------------------------------------------------- #
# Celery 队列：直接读写本栈的 Redis 列表
#
# 为什么需要它：恢复用例必须**确定原始投递真的没了**（而不是「希望」它没了），
# 并在手动扫描后**确定补投真的发生了**。只操作 Redis 队列，不碰任何业务表。
# --------------------------------------------------------------------------- #
#: Celery 队列名（与 `app.tasks.celery_app` 的 `task_default_queue` 一致；可覆盖）
QUEUE_NAME = os.environ.get("ENG10_QUEUE_NAME", "ai-avatar-v1")


class CeleryQueue:
    """本栈 Celery 队列的读写句柄（Redis list；按前缀扫描以覆盖优先级分片）。"""

    def __init__(self, url: str | None = None, name: str = QUEUE_NAME) -> None:
        self._client = redis_lib.Redis.from_url(
            url or os.environ.get("ENG10_REDIS_URL", "redis://127.0.0.1:16379/0"),
            decode_responses=True,
        )
        self._name = name

    def keys(self) -> list[str]:
        found = [key for key in self._client.scan_iter(match=f"{self._name}*")]
        return sorted(key for key in found if self._client.type(key) == "list")

    def messages(self) -> list[str]:
        """队列里的全部消息体（FIFO 顺序）。"""

        bodies: list[str] = []
        for key in self.keys():
            bodies.extend(self._client.lrange(key, 0, -1))
        return bodies

    def messages_for(self, generation_id: str) -> list[str]:
        """属于某个任务的消息（Celery 消息体里带 `generation_id`）。"""

        return [body for body in self.messages() if generation_id in body]

    def remove(self, body: str) -> int:
        """按内容删除一条消息（模拟「投递丢失」），返回删除条数。"""

        removed = 0
        for key in self.keys():
            removed += int(self._client.lrem(key, 1, body))
        return removed

    def drain(self) -> int:
        """清空队列，返回清掉的消息条数。

        **只在本栈 Worker 已停时调用**：此时队列上没有消费者，清掉的是积压的
        计划任务（心跳 / 恢复扫描），不会丢任何正在处理的消息。
        """

        total = 0
        for key in self.keys():
            total += int(self._client.llen(key))
            self._client.delete(key)
        return total


@pytest.fixture()
def celery_queue() -> Iterator[CeleryQueue]:
    queue = CeleryQueue()
    try:
        yield queue
    finally:
        queue._client.close()  # noqa: SLF001 - 只关连接，不做任何清理动作


@pytest.fixture(scope="session", autouse=True)
def stack_reachable() -> None:
    """栈没起来就明确报错（附启动命令），不静默跳过。"""

    assert STACK_MODE in VALID_STACK_MODES, (
        f"ENG10_STACK_MODE={STACK_MODE!r} 不是合法取值，必须是 {VALID_STACK_MODES} 之一。"
    )
    _probe("Next.js", WEB_URL)
    _probe("FastAPI", f"{API_URL}/api/v1/health/live")


@pytest.fixture(scope="session", autouse=True)
def stack_identity(engine: Engine) -> None:
    """核对**同一个隔离栈**：浏览器入口 / 测试入口 / DB / Redis / MinIO 必须同源。

    三条可核验的事实，而不是靠环境变量自证：

    1. **浏览器入口经 Next 代理打到同一个库**：在断言用的那个库里种一枚新邀请码，
       再经 `WEB_URL/api/v1/**`（Next 同源代理）兑换 —— 若代理指向别的栈，
       那枚码在对面根本不存在，会得到 `INVITE_INVALID`；
    2. **测试入口（邮件替身）打到同一个库、并把邮件写进我们读的目录**：
       经 `API_URL` 兑换另一枚码，再 `POST /auth/email/request`，
       断言 `ENG10_MAIL_DIR` 里出现该收件人的新邮件 —— 这是「浏览器/宿主看到的
       就是被断言的这套 DB + Redis + MinIO」的直接证据（`/health/ready` 的
       redis 与 object_storage 探针同时为绿）；
    3. **Compose 模式下再核对容器归属**：本项目必须存在 worker 容器，
       且容器名以 `<项目名>-` 开头（`scripts/eng10_compose_stack.py` 内还有一层护栏）。

    任一条不成立都**直接失败**，不做静默跳过。
    """

    # 1) 两个入口的 readiness：Redis / MinIO / DB 五项探针全绿
    for label, base in (("测试入口", API_URL), ("浏览器入口（Next 同源代理）", WEB_URL)):
        with urllib.request.urlopen(f"{base}/api/v1/health/ready", timeout=25) as response:
            assert response.status == 200, f"{label} /health/ready 未就绪：{response.status}"
            payload = json.loads(response.read().decode("utf-8"))
        assert (payload.get("data") or {}).get("status") == "ok", (label, payload)

    # 2) 浏览器入口 → Next 代理 → 同一个库
    proxy_code = new_invite_code()
    proxy_email = new_email("proxysame")
    seed_invite_code(engine, proxy_code, points=10, max_redemptions=1)
    with httpx.Client(base_url=WEB_URL, timeout=30.0, follow_redirects=False, trust_env=False) as client:
        through_proxy = client.post(
            "/api/v1/auth/invites/redeem",
            headers={"Idempotency-Key": f"eng10-identity-{uuid.uuid4()}"},
            json={"invite_code": proxy_code, "email": proxy_email},
        )
    assert through_proxy.status_code == 201, (
        "经 Next 同源代理兑换失败 —— 浏览器入口与断言用的数据库不是同一个栈："
        f"{through_proxy.status_code} {through_proxy.text}"
    )

    # 3) 测试入口 → 同一个库，且邮件真的落到我们读的目录
    api_code = new_invite_code()
    api_email = new_email("apisame")
    seed_invite_code(engine, api_code, points=10, max_redemptions=1)
    since = time.time()
    with new_client() as client:
        redeemed = client.post(
            "/api/v1/auth/invites/redeem",
            headers={"Idempotency-Key": f"eng10-identity-{uuid.uuid4()}"},
            json={"invite_code": api_code, "email": api_email},
        )
        assert redeemed.status_code == 201, (
            "测试入口兑换失败 —— 它和断言用的数据库不是同一个栈："
            f"{redeemed.status_code} {redeemed.text}"
        )
        cookie = cookie_of(redeemed)
        requested = client.post(
            "/api/v1/auth/email/request",
            headers=auth(cookie),
            json={"email": api_email},
        )
        assert requested.status_code == 202, (
            "测试入口没能发出验证邮件 —— 它可能不是邮件替身入口："
            f"{requested.status_code} {requested.text}"
        )
    mail = new_mail_token(since=since, email=api_email, timeout=30.0)
    assert mail["to_email"] == api_email, mail

    # 4) Compose 模式：容器归属核对
    if STACK_MODE == "compose":
        result = _run_script("eng10_compose_stack.py", "ps")
        assert result.returncode == 0, (result.stdout, result.stderr)
        rows = [
            json.loads(line)
            for line in (result.stdout or "").splitlines()
            if line.strip().startswith("{")
        ]
        assert rows, f"项目 {COMPOSE_PROJECT} 里没有 worker 容器：{result.stdout}"
        for row in rows:
            assert str(row.get("Name", "")).startswith(f"{COMPOSE_PROJECT}-"), row

    print(
        f"\n[eng10-e2e] 栈身份核对通过：mode={STACK_MODE}"
        + (f" project={COMPOSE_PROJECT}" if STACK_MODE == "compose" else "")
        + f" web={WEB_URL} api={API_URL}"
    )


@pytest.fixture(scope="session", autouse=True)
def only_mock_provider() -> None:
    """**硬断言**：本轮 E2E 只允许 `AI_PROVIDER=mock`，绝不触碰真实供应商。

    判据是可核验的事实而不是本机环境变量：`/health/ready` 必须 200，而它的
    `generation_costs` 探针用 `provider_identity()` 推导当前 provider 的型号并核对该
    型号在**每个必需模式**上都有生效价格。真实供应商缺少 `AI_BASE_URL`/`AI_API_KEY`
    时会在装配期响亮失败，`/ready` 因此不可能绿。
    """

    with urllib.request.urlopen(f"{API_URL}/api/v1/health/ready", timeout=20) as response:
        assert response.status == 200, f"/health/ready 未就绪：{response.status}"
        payload = json.loads(response.read().decode("utf-8"))
    assert (payload.get("data") or {}).get("status") == "ok", payload

    os.environ.setdefault(
        "DATABASE_URL", "postgresql+psycopg://placeholder@127.0.0.1:1/none"
    )
    from app.services.generation.provider_factory import provider_identity

    provider, model = provider_identity(ai_provider="mock", ai_model="")
    assert provider == "MOCK", f"本轮 E2E 只允许 Mock Provider，实测 {provider}"
    assert model == "mock-image-v1", model


# --------------------------------------------------------------------------- #
# 数据库
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="session")
def database_url() -> str:
    url = os.environ.get("ENG10_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""
    if not url:
        raise RuntimeError(
            "缺少 ENG10_DATABASE_URL / DATABASE_URL：浏览器 E2E 需要直连本地隔离库做合成数据与断言。"
        )
    return url


@pytest.fixture(scope="session")
def engine(database_url: str) -> Iterator[Engine]:
    eng = create_engine(database_url, poolclass=None)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture(scope="session", autouse=True)
def generation_costs_seeded(database_url: str) -> None:
    """确保**目标库**里有生效价格（缺就按规范命令补），否则整组硬失败。

    为什么要有这条：`generation_cost_configs` 迁移只建表、**不填价格**
    （`docker/README.md`「为什么点数价格必须显式传入」）。少跑那一步时
    `POST /generations` 会返回 `503 NOT_READY / cost_config_missing`，看起来像
    「编排坏了」——实测踩到过两次，一次整组 E2E 全红。

    ## 为什么这里按 `model_name=None`（通配价）判定与补齐

    定价规则是**精确型号优先、`NULL` 兜底**（`SqlAlchemyGenerationCostConfigRepository
    .find_active`），创建任务会带上本进程实际使用的型号（`provider_identity()` 推出的
    `mock-image-v1`）。因此「一条通配价」对任何型号都成立，正是 `docker/README.md`
    里那条部署命令（`--price MODE=<点数>`，不带 `--model`）的效果 —— 本夹具与它逐字一致。

    「只给精确型号定价」这条路径由单元测试与真库测试覆盖
    （`tests/services/test_cost_config_bootstrap.py`、
    `tests/db/test_cost_config_bootstrap.py`），不在这里重复。
    """

    from datetime import datetime, timezone

    from sqlalchemy.orm import Session

    os.environ.setdefault("DATABASE_URL", database_url)
    from app.bootstrap.cost_configs import (
        PriceInput,
        bootstrap_cost_configs,
        expected_cost_modes,
        missing_cost_modes,
    )
    from app.repositories.generation import SqlAlchemyGenerationCostConfigRepository

    modes = expected_cost_modes(enable_reference_image=False)
    engines = [create_engine(database_url, poolclass=None)]

    def session_factory() -> Session:
        return Session(engines[0])

    try:
        with session_factory() as session:
            missing = missing_cost_modes(
                SqlAlchemyGenerationCostConfigRepository(session),
                modes=modes,
                model_name=None,
                at=datetime.now(timezone.utc),
            )
        if not missing:
            return

        prices = [
            PriceInput(
                mode=mode,
                points_cost=30 if mode == "TEXT_PROMPT" else 40,
                model_name=None,
            )
            for mode in missing
        ]
        result = bootstrap_cost_configs(session_factory, prices)
        assert result.ok, f"补价格失败：{result}"
        print(f"\n[eng10-e2e] 目标库缺少生效价格，已按规范命令补：{missing}")
    finally:
        for engine in engines:
            engine.dispose()


# --------------------------------------------------------------------------- #
# HTTP：真实服务端
# --------------------------------------------------------------------------- #
def new_client() -> httpx.Client:
    """真实 HTTP 客户端：**不读环境里的 HTTP 代理**、Cookie 由调用方显式给出。

    见模块头「宿主环境事实 1」：走 cookie jar 会因为 `Secure` 属性在明文 HTTP 上被
    丢掉，从而得到 401。显式头与真实浏览器在这一点的行为一致。
    """

    return httpx.Client(
        base_url=API_URL, timeout=30.0, follow_redirects=False, trust_env=False
    )


def cookie_of(response: httpx.Response) -> str:
    """把响应的 `Set-Cookie` 摘成一行 `name=value; ...`。"""

    return "; ".join(f"{name}={value}" for name, value in response.cookies.items())


def auth(cookie: str) -> dict[str, str]:
    return {"cookie": cookie, "Idempotency-Key": f"eng10-{uuid.uuid4()}"}


@pytest.fixture()
def api() -> Iterator[httpx.Client]:
    """**匿名**客户端（不持有任何 Cookie），用于断言匿名/越权行为。"""

    with new_client() as client:
        yield client


def api_json(
    method: str,
    path: str,
    *,
    body: dict | None = None,
    headers: dict[str, str] | None = None,
    cookie: str | None = None,
    timeout: float = 30.0,
    base: str = API_URL,
) -> tuple[int, dict]:
    """用 urllib 打一次真实 HTTP（便于 session 级夹具与「匿名」断言）。"""

    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(f"{base}{path}", data=data, method=method)
    request.add_header("content-type", "application/json")
    if cookie:
        request.add_header("cookie", cookie)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return response.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8")
        try:
            return error.code, (json.loads(raw) if raw else {})
        except json.JSONDecodeError:
            return error.code, {"raw": raw}


# --------------------------------------------------------------------------- #
# 邀请码 / 邮件 / 会话
# --------------------------------------------------------------------------- #
def seed_invite_code(
    engine: Engine,
    code: str,
    *,
    points: int = 100,
    max_redemptions: int = 5,
) -> uuid.UUID:
    """插入一枚合成邀请码（只存哈希，明文只在用例里）。"""

    with engine.begin() as conn:
        existing = conn.execute(
            text("SELECT id FROM invite_codes WHERE code_hash = :h"),
            {"h": hash_invite_code(code)},
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        return conn.execute(
            text(
                """
                INSERT INTO invite_codes (code_hash, status, initial_points, max_redemptions)
                VALUES (:h, 'ACTIVE', :points, :max)
                RETURNING id
                """
            ),
            {"h": hash_invite_code(code), "points": points, "max": max_redemptions},
        ).scalar_one()


def read_mail_tokens(*, since: float) -> list[dict]:
    """返回 `sent_at >= since` 的全部验证邮件（按时间升序）。"""

    rows: list[dict] = []
    for path in sorted(MAIL_DIR.glob("*.json")) if MAIL_DIR.exists() else []:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if float(payload.get("sent_at", 0)) >= since:
            rows.append(payload)
    rows.sort(key=lambda item: float(item.get("sent_at", 0)))
    return rows


def new_mail_token(
    *, since: float = 0.0, email: str | None = None, timeout: float = 30.0
) -> dict:
    """等一封**新的**验证邮件（由 E2E 邮件替身落盘）并返回它的内容。"""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for payload in reversed(read_mail_tokens(since=since)):
            if email is None or payload.get("to_email") == email:
                return payload
        time.sleep(0.2)
    target = f"（收件人 {email}）" if email else ""
    raise TimeoutError(f"{timeout}s 内没有新的验证邮件{target}（目录 {MAIL_DIR}）")


class Account:
    """一条真实走完「邀请码 → 邮箱验证」的账号。

    `cookie` 是**已验证之后**重新取得的会话（见 `redeem_and_verify`），
    因此 `session.email_verified` 为 true。
    """

    def __init__(
        self,
        *,
        email: str,
        invite_code: str,
        user_id: uuid.UUID,
        cookie: str,
        client: httpx.Client,
        initial_points: int,
    ) -> None:
        self.email = email
        self.invite_code = invite_code
        self.user_id = user_id
        self.cookie = cookie
        self.client = client
        self.initial_points = initial_points

    def get(self, path: str) -> httpx.Response:
        return self.client.get(path, headers={"cookie": self.cookie})

    def post(self, path: str, body: dict | None = None) -> httpx.Response:
        return self.client.post(path, json=body, headers=auth(self.cookie))

    def session_token(self) -> str:
        """`session=<token>` 里的那个 token（给浏览器装 Cookie 用）。"""

        for part in self.cookie.split(";"):
            name, _, value = part.strip().partition("=")
            if name == "session":
                return value
        raise AssertionError(f"会话 Cookie 里没有 session：{self.cookie!r}")


def _user_id_of(engine: Engine, email: str) -> uuid.UUID:
    with engine.begin() as conn:
        return conn.execute(
            text(
                """
                SELECT u.id
                  FROM users u
                  JOIN user_emails e ON e.user_id = u.id
                 WHERE lower(e.email) = lower(:email)
                """
            ),
            {"email": email},
        ).scalar_one()


def _session_flag(client: httpx.Client, cookie: str) -> bool:
    response = client.get("/api/v1/auth/session", headers={"cookie": cookie})
    assert response.status_code == 200, response.text
    return bool(response.json()["data"]["email_verified"])


def top_up(engine: Engine, account: Account, *, points: int = 400) -> Account:
    """给**同一个**账号再加一笔点数（真实路径：兑换一枚新邀请码，同邮箱恢复同一用户）。

    加的是**新的**邀请码，不改钱包、不改流水，也不伪造余额。

    **不要**用它给结算矩阵的用例铺底：那些用例要断言 `reserved_points` 的**绝对值**，
    而共享账号会被别的用例按产品语义留下的预留污染（见 `funded` 的说明）。
    保留它是为了「确实需要在同一用户身上累加点数」的场景。
    """

    code = new_invite_code()
    seed_invite_code(engine, code, points=points, max_redemptions=1)
    response = account.client.post(
        "/api/v1/auth/invites/redeem",
        headers={"Idempotency-Key": f"eng10-topup-{uuid.uuid4()}"},
        json={"invite_code": code, "email": account.email},
    )
    assert response.status_code == 201, f"加点数失败：{response.status_code} {response.text}"
    account.cookie = cookie_of(response)
    assert _session_flag(account.client, account.cookie), "加点数后的会话必须仍是已验证"
    return account


@pytest.fixture()
def funded(engine: Engine) -> Iterator[Account]:
    """结算/生命周期用例用它：**每个用例一条全新的已验证账号**（200 点、0 预留）。

    ## 为什么不能复用 session 级的 `account` 再补点（这是本轮 4 条用例失败的真实原因）

    `test_unknown_outcome_keeps_the_reservation_and_goes_to_review` 按产品语义
    **故意保留预留**（`NEEDS_REVIEW + RESERVED`，等人工判定，绝不自动放款）。
    共享账号时，那笔预留会一直挂在该用户身上，于是后续用例里所有
    `assert after["reserved_points"] == 0` 都会读到残留值 —— 本轮实测：

    ```text
    AssertionError: {'available_points': 2040, ..., 'reserved_points': 40, ...}
    assert 40 == 0
    ```

    4 条用例（重复提交、重复结算、跨进程重复终态、恢复扫描）同时报 `40 != 0`，
    数字**恰好等于** UNKNOWN 用例留下的那一笔预留，与 Worker/Beat 的时序无关。
    共享账号还会引入第二种假失败：上一条用例刚提交、真实 Worker 还没结算完时，
    下一条用例读到的 `reserved_points` 也是非零。

    ## 为什么是「每个用例独立账号」而不是「把断言改成相对差值」

    改成相对差值等于放宽业务条件（无法再证明「这笔预留确实被释放了」）。
    独立账号之后，断言保持**绝对值**这一最强形式，只是不再被别的用例的历史状态污染。
    """

    acc = redeem_and_verify(engine, points=200, prefix="funded")
    try:
        yield acc
    finally:
        acc.client.close()


def redeem_and_verify(
    engine: Engine,
    *,
    points: int = 100,
    prefix: str = "eng10",
) -> Account:
    """跑完真实注册链路并返回**已验证**的会话（**只用一枚邀请码**）。

    步骤（全部是真实接口）：

    1. 种子一枚一次性邀请码；
    2. `POST /auth/invites/redeem`（码，邮箱）→ 201 + 会话 Cookie；
    3. `POST /auth/email/request`（真实邮件替身落盘）→ 读令牌 →
       `POST /auth/email/verify` → 数据库里的邮箱变成已验证；
    4. **同一个会话**（同一枚 Cookie）再查 `GET /auth/session`，必须已经
       `email_verified=true`。

    ## 第 4 步是 ENG-03 缺陷的回归（以前这里靠「第二枚邀请码」绕过）

    修复前：`SessionRecord.email_verified` 是**会话创建时的快照**，只在
    `RedisSessionStore.create()`（邀请兑换）里写入；`/auth/email/verify`
    只改数据库、**不续签会话**。于是「先兑换、再点验证链接」的用户拿到的会话里
    `email_verified` 永远是 false，而前端 `useSession` 正是用它决定
    `signed-in` / `needs-verification`，上传与提交入口因此一直锁着——尽管服务端授权
    读的是数据库（`app/api/deps.py::get_verified_current_user`），所以**接口能过、界面锁死**。

    修复后：`GET /auth/session` 的 `email_verified` 以 `user_emails` 的**当前事实**为准
    （不重签 Cookie、不延长 TTL、不复活已撤销会话、不改 Cookie 属性）。
    因此本函数**不再需要第二枚邀请码**，这里直接把第 4 步断言下来：主闭环跑的就是
    用户真实会走的那条路。
    """

    code = new_invite_code()
    seed_invite_code(engine, code, points=points, max_redemptions=1)
    email = new_email(prefix)

    client = new_client()
    since = time.time()

    redeemed = client.post(
        "/api/v1/auth/invites/redeem",
        headers={"Idempotency-Key": f"eng10-{uuid.uuid4()}"},
        json={"invite_code": code, "email": email},
    )
    assert redeemed.status_code == 201, f"兑换邀请码失败：{redeemed.status_code} {redeemed.text}"
    cookie = cookie_of(redeemed)
    assert not _session_flag(client, cookie), (
        "兑换后邮箱还没验证，会话查询不该说已验证：本文件关于「事实以数据库为准」的说明需要复核"
    )

    request = client.post(
        "/api/v1/auth/email/request",
        headers=auth(cookie),
        json={"email": email},
    )
    assert request.status_code == 202, request.text

    mail = new_mail_token(since=since, email=email)
    verify = client.post(
        "/api/v1/auth/email/verify",
        headers={"Idempotency-Key": f"eng10-{uuid.uuid4()}"},
        json={"token": mail["token"]},
    )
    assert verify.status_code == 200, verify.text
    assert verify.json()["data"]["email_verified"] is True, verify.text

    # 同一枚 Cookie：验证事实必须已经反映在会话查询上（缺陷回归）
    assert _session_flag(client, cookie), (
        "邮箱验证后同一会话仍报未验证 —— 会话事实一致性回归失败（ENG-03 缺陷复现）"
    )

    return Account(
        email=email,
        invite_code=code,
        user_id=_user_id_of(engine, email),
        cookie=cookie,
        client=client,
        initial_points=points,
    )


@pytest.fixture(scope="session")
def account(engine: Engine) -> Iterator[Account]:
    """**已验证邮箱**的合成账号（整组用例共用的同一个用户）。

    ⚠️ 这是**跨用例共享**的账号：任何「按产品语义故意保留预留」的用例
    （`NEEDS_REVIEW`）都会把 `reserved_points` 留在它身上，污染后续所有
    `reserved_points == 0` 的绝对断言（见 `funded` 的说明）。
    结算/生命周期用例请用 `funded`；只有确实需要跨用例共享同一用户
    （且不断言预留绝对值）的场景才用它。
    """

    acc = redeem_and_verify(engine, points=100)
    try:
        yield acc
    finally:
        acc.client.close()


# --------------------------------------------------------------------------- #
# 资产：真实上传（签名 URL + 合成 PNG）
# --------------------------------------------------------------------------- #
def create_upload_intent(
    client: httpx.Client, *, cookie: str, directory: Path, name: str = "source.png",
    seed: int = 0, purpose: str = "SOURCE",
) -> tuple[dict, bytes]:
    """`POST /assets/uploads` → 返回 `(intent, 原始字节)`。"""

    path = synthetic_image_file(directory, name, seed=seed)
    payload = path.read_bytes()
    response = client.post(
        "/api/v1/assets/uploads",
        headers=auth(cookie),
        json={
            "purpose": purpose,
            "filename": path.name,
            "mime_type": "image/png",
            "size_bytes": len(payload),
            "sha256": sha256_hex(payload),
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["data"], payload


def signed_put(intent: dict, payload: bytes) -> int:
    """按签名 URL 真送字节（用独立客户端，避免把 MinIO 的响应带进会话）。"""

    with new_client() as client:
        response = client.put(
            intent["upload_url"], content=payload,
            headers={"content-type": "image/png"},
        )
    return response.status_code


def complete_upload(client: httpx.Client, *, cookie: str, asset_id: str) -> dict:
    response = client.post(
        f"/api/v1/assets/{asset_id}/complete", headers=auth(cookie)
    )
    assert response.status_code == 200, response.text
    asset = response.json()["data"]
    assert asset["status"] == "READY", asset
    return asset


def create_ready_asset(
    client: httpx.Client,
    *,
    cookie: str,
    directory: Path,
    name: str = "source.png",
    seed: int = 0,
    purpose: str = "SOURCE",
) -> dict:
    """走真实的「上传意图 → 签名 PUT → complete」三步，返回 `Asset` 字典。"""

    intent, payload = create_upload_intent(
        client, cookie=cookie, directory=directory, name=name, seed=seed, purpose=purpose
    )
    status = signed_put(intent, payload)
    assert status in (200, 204), f"签名上传失败：{status}"
    return complete_upload(client, cookie=cookie, asset_id=intent["asset_id"])


def list_outputs(client: httpx.Client, *, cookie: str, generation_id: str) -> list[dict]:
    response = client.get(
        f"/api/v1/generations/{generation_id}/outputs", headers={"cookie": cookie}
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def submit_generation(
    client: httpx.Client,
    *,
    cookie: str,
    mode: str,
    prompt: str,
    input_asset_ids: list[str] | None = None,
    idempotency_key: str | None = None,
) -> tuple[int, dict]:
    """`POST /generations`（契约固定 `output_count=2`）。返回 `(status, body)`。"""

    response = client.post(
        "/api/v1/generations",
        headers={
            "cookie": cookie,
            "Idempotency-Key": idempotency_key or f"eng10-generation-{uuid.uuid4()}",
        },
        json={
            "mode": mode,
            "prompt": prompt,
            "input_asset_ids": input_asset_ids or [],
            "output_count": 2,
        },
    )
    try:
        body = response.json()
    except Exception:  # noqa: BLE001
        body = {"raw": response.text}
    return response.status_code, body


def wait_generation(
    client: httpx.Client, *, cookie: str, generation_id: str, timeout: float = 180.0
) -> dict:
    """轮询任务直到终局（`SUCCEEDED`/`FAILED`/`NEEDS_REVIEW`）。"""

    terminal = {"SUCCEEDED", "FAILED", "NEEDS_REVIEW"}
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        response = client.get(
            f"/api/v1/generations/{generation_id}", headers={"cookie": cookie}
        )
        assert response.status_code == 200, response.text
        last = response.json()["data"]
        if last["status"] in terminal:
            return last
        time.sleep(0.5)
    raise TimeoutError(f"{timeout}s 内任务未终局：{last}")


def points_of(client: httpx.Client, *, cookie: str) -> dict:
    response = client.get("/api/v1/me/points", headers={"cookie": cookie})
    assert response.status_code == 200, response.text
    return response.json()["data"]


# --------------------------------------------------------------------------- #
# 真实浏览器（CDP）
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="session")
def chrome() -> Iterator[Chrome]:
    """一个受控的真实 Chrome（headless=new，桌面 1440x900）。

    session 级：启动一次、多个用例各自开新标签；每页在用例结束时关闭。
    """

    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    with Chrome(window="1440,900") as instance:
        yield instance


@pytest.fixture()
def page(chrome: Chrome) -> Iterator[SyncPage]:
    """一个桌面视口的同步页面。

    **每个用例先清一次 Cookie**：这些页面共享同一个 Chrome profile，会话 Cookie
    是浏览器级状态，不清就会让上一个用例的登录态泄漏到下一个用例里
    （实测踩到：兑换过邀请码的用例之后，「去开通」按钮就不再出现了）。
    """

    with open_page(chrome, "about:blank") as instance:
        instance.clear_cookies()
        yield instance


@pytest.fixture()
def mobile_page(chrome: Chrome) -> Iterator[SyncPage]:
    """一个移动端视口的同步页面（375x812，`mobile=true` + 触摸仿真）。"""

    with open_page(chrome, "about:blank") as instance:
        instance.clear_cookies()
        instance.set_viewport(375, 812)
        yield instance


def goto_studio(page: SyncPage) -> None:
    """打开创作台并等到**真正挂载完成**（不是 Suspense 回退）。

    `/studio` 外面包了 `Suspense`，先用正文判断会读到「正在准备创作台…」这个回退；
    必须等到「创作台」这个标题与三种入口都在。
    """

    page.goto(f"{WEB_URL}/studio")
    page.wait_for_text("创作台", timeout=30)
    page.wait_for(
        "return document.body.innerText.includes('创作台')"
        " && document.body.innerText.includes('改造我的图片')"
        " && document.body.innerText.includes('入口 03');",
        timeout=30,
        raw=True,
    )
    # 会话是异步读的：等它离开「正在确认登录状态」，否则断言会读到中间态
    page.wait_for("!document.body.innerText.includes('正在确认登录状态')", timeout=30)


def studio_generation_id(page: SyncPage) -> str:
    """当前任务的 id。

    **不能只看地址栏**：前端在提交成功后把 id 存进 `sessionStorage`
    （`services/generation.ts` 的 `ACTIVE_KEY = "ai-avatar:active-generation"`，
    由 `useStudio.ts:510` 的 `rememberActiveGeneration` 写入），
    并没有把 `?generation=` 写回 URL。这里按前端真实使用的位置读，
    同时兼容「从首页带着 id 进来」那条路径。
    """

    return str(
        page.evaluate(
            "const fromUrl = new URL(location.href).searchParams.get('generation');"
            " if (fromUrl) return fromUrl;"
            " return sessionStorage.getItem('ai-avatar:active-generation') || '';"
        )
        or ""
    )


def browser_login(page: SyncPage, account: Account) -> None:
    """把已验证的会话 Cookie 装进真实浏览器，然后打开创作台。

    **必须**走 CDP 的 `Network.setCookie`：会话 Cookie 是 `HttpOnly + Secure`
    （契约第 3 节），`document.cookie = ...` 既写不进 `HttpOnly`，在非 HTTPS 来源上
    也会被浏览器直接丢掉 `Secure` 那条。
    """

    page.goto(f"{WEB_URL}/")
    page.wait_for("document.readyState === 'complete'", timeout=30)
    page.set_cookie("session", account.session_token())
    goto_studio(page)
    page.wait_for("document.body.innerText.includes('可用点数')", timeout=40)
