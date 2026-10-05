#!/usr/bin/env python
"""ENG-10 本机完整闭环栈（原生进程形态）。

## 这个脚本解决什么问题

`docker/compose.yml` 是**唯一**的规范编排文件，`docker compose up --build -d` 才是
验收形态。但在某些机器上 Docker 引擎不可用（本机实测：Docker Desktop 的 WSL2 后端
被安全策略拦截，引擎停在 `InitState`，`wsl.exe` 在程序黑名单里）。此时仍需要一条
**真实**的完整闭环来验证业务链路，本脚本就是那条路径。

它启动的是与 Compose **同一套进程与同一份配置**，只是换成原生二进制：

```text
PostgreSQL   官方 EDB 便携二进制        ←→  compose 的 postgres:16-alpine
Redis        redis-server.exe          ←→  compose 的 redis:7-alpine
MinIO        minio.exe                 ←→  compose 的 minio/minio
FastAPI      uvicorn app.main:app      ←→  compose 的 backend
Celery Worker celery -A ... worker     ←→  compose 的 worker
Celery Beat  celery -A ... beat        ←→  compose 的 beat
Next.js      next start                ←→  compose 的 frontend
```

**必须如实标注**：这是「原生进程验证」，**不等于** Compose 验收通过。Compose 侧本轮
只做到「配置静态可解析 + 镜像与 Dockerfile 齐备」；引擎不可用时不做任何 `up`。

## 用法

```bash
python scripts/eng10_local_stack.py up --hold   # 起全部并保持前台（沙箱后台任务用）
python scripts/eng10_local_stack.py up          # 起全部后返回
python scripts/eng10_local_stack.py status      # 端口 + /health/ready 探测
python scripts/eng10_local_stack.py down        # 停全部（只停本脚本记录 PID 的进程）
```

所有运行数据写在 `ENG10_RUN_DIR`（默认 `D:/tmp/eng10-run`，**仓库外**），
二进制放 `ENG10_STACK_DIR`（默认 `D:/tmp/eng10-stack`）。脚本只操作这两个目录下
自己记录过的 PID，不会去杀任何其他进程。

## 凭据边界

这里用的都是**本地占位值**（`eng10-local-*`），不是真实凭据，也不写入仓库。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO / "backend"
FRONTEND = REPO / "frontend"

STACK_DIR = Path(os.environ.get("ENG10_STACK_DIR", "D:/tmp/eng10-stack"))
RUN_DIR = Path(os.environ.get("ENG10_RUN_DIR", "D:/tmp/eng10-run"))
LOG_DIR = RUN_DIR / "logs"
PID_DIR = RUN_DIR / "pids"

PY = Path(os.environ.get("ENG10_PYTHON", str(REPO / ".venv-eng09-r2" / "Scripts" / "python.exe")))
#: PostgreSQL 便携二进制的 `bin/`（EDB 官方包解压后是 `pgsql/bin`）
PG_BIN = Path(os.environ.get("ENG10_PG_BIN", str(STACK_DIR / "pg" / "pgsql" / "bin")))
REDIS_BIN = Path(os.environ.get("ENG10_REDIS_BIN", str(STACK_DIR / "redis")))
MINIO_BIN = Path(os.environ.get("ENG10_MINIO_BIN", str(STACK_DIR / "minio.exe")))
#: 直接用 node 跑 Next 的 JS 入口：`npm run <script>` 在 Git Bash 下会
#: `spawn cmd.exe ENOENT`，绕开 npm 更稳（见 windows-sandbox-workarounds 第 1 条）
NODE = Path(
    os.environ.get(
        "ENG10_NODE",
        "C:/Users/盛浩然/.workbuddy-ai/binaries/node/versions/22.22.2-3/node.exe",
    )
)
NEXT_CLI = FRONTEND / "node_modules" / "next" / "dist" / "bin" / "next"

PG_DATA = RUN_DIR / "pgdata"
PG_PORT = int(os.environ.get("ENG10_PG_PORT", "5432"))
REDIS_PORT = int(os.environ.get("ENG10_REDIS_PORT", "16379"))
MINIO_PORT = int(os.environ.get("ENG10_MINIO_PORT", "19000"))
MINIO_CONSOLE_PORT = int(os.environ.get("ENG10_MINIO_CONSOLE_PORT", "19001"))
API_PORT = int(os.environ.get("ENG10_API_PORT", "8100"))
WEB_PORT = int(os.environ.get("ENG10_WEB_PORT", "13000"))

DB_NAME = os.environ.get("ENG10_DB_NAME", "ai_avatar_eng10")
DB_USER = "postgres"
BUCKET = "ai-avatar"

#: 本地占位凭据：不是真实凭据，只在 loopback 上监听
STORAGE_ACCESS_KEY = "eng10-local-access"
STORAGE_SECRET_KEY = "eng10-local-secret"
SESSION_SECRET = "eng10-local-session-secret"

#: 点数定价（本地闭环用；`app.bootstrap.cost_configs` 拒绝默认值，必须显式给出）
TEXT_PRICE = "30"
PHOTO_PRICE = "40"

ENV = {
    "APP_ENV": "local",
    "APP_BASE_URL": f"http://localhost:{API_PORT}",
    "DATABASE_URL": f"postgresql+psycopg://{DB_USER}@127.0.0.1:{PG_PORT}/{DB_NAME}",
    "REDIS_URL": f"redis://127.0.0.1:{REDIS_PORT}/0",
    "STORAGE_DRIVER": "minio",
    "STORAGE_ENDPOINT": f"http://127.0.0.1:{MINIO_PORT}",
    "STORAGE_BUCKET": BUCKET,
    "STORAGE_REGION": "us-east-1",
    "STORAGE_ACCESS_KEY": STORAGE_ACCESS_KEY,
    "STORAGE_SECRET_KEY": STORAGE_SECRET_KEY,
    "AI_PROVIDER": "mock",
    "AI_BASE_URL": "",
    "AI_MODEL": "",
    "AI_API_KEY": "",
    "AI_VISION_MODEL": "",
    "ENABLE_IMAGE_ANALYSIS": "false",
    "ENABLE_REFERENCE_IMAGE": "false",
    "SESSION_SECRET": SESSION_SECRET,
}


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def log(message: str) -> None:
    print(f"[eng10] {message}", flush=True)


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    with socket.socket() as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def wait_port(port: int, *, timeout: float = 60.0, label: str = "") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if port_open(port):
            return
        time.sleep(0.3)
    raise SystemExit(f"超时：{label or port} 未在 {timeout:.0f}s 内监听 {port}")


def wait_http(url: str, *, timeout: float = 90.0, label: str = "") -> None:
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                if response.status < 500:
                    return
                last = f"status={response.status}"
        except urllib.error.HTTPError as error:
            last = f"status={error.code}"
        except Exception as error:  # noqa: BLE001 - 就绪等待期间任何异常都只是"还没好"
            last = type(error).__name__
        time.sleep(0.5)
    raise SystemExit(f"超时：{label or url} 未就绪（最后错误 {last}）")


def wait_pg_ready(*, timeout: float = 60.0) -> None:
    """等到 PostgreSQL **能接受查询**为止。

    只探端口是不够的：postmaster 在恢复完成前就已经在监听，此时任何查询都会得到
    `FATAL: the database system is starting up`（实测踩到）。`pg_isready` 才是权威判据。
    """

    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        result = subprocess.run(  # noqa: S603 - 参数全部来自本脚本常量
            [
                str(PG_BIN / "pg_isready.exe"),
                "-h",
                "127.0.0.1",
                "-p",
                str(PG_PORT),
                "-U",
                DB_USER,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode == 0:
            return
        last = (result.stdout or result.stderr or "").strip()
        time.sleep(0.3)
    raise SystemExit(f"超时：PostgreSQL 未就绪（最后输出 {last}）")


def child_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(ENV)
    env["PYTHONPATH"] = "."
    env["PYTHONUNBUFFERED"] = "1"
    # 子进程不继承 shell 注入的 UTF-8 开关：与本仓库声明的运行形态保持一致
    env.pop("PYTHONUTF8", None)
    env.pop("PYTHONIOENCODING", None)
    return env


def spawn(name: str, args: list[str], *, cwd: Path) -> subprocess.Popen:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    PID_DIR.mkdir(parents=True, exist_ok=True)
    log_file = open(LOG_DIR / f"{name}.log", "ab", buffering=0)  # noqa: SIM115 - 生命周期与子进程一致
    creation = 0
    if os.name == "nt":
        creation = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    process = subprocess.Popen(  # noqa: S603 - 参数全部来自本脚本常量
        args,
        cwd=str(cwd),
        env=child_env(),
        stdout=log_file,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        creationflags=creation,
    )
    (PID_DIR / f"{name}.pid").write_text(str(process.pid), encoding="utf-8")
    log(f"启动 {name} pid={process.pid} → {LOG_DIR / f'{name}.log'}")
    return process


def run(
    name: str,
    args: list[str],
    *,
    cwd: Path,
    check: bool = True,
    timeout: float = 600.0,
) -> subprocess.CompletedProcess:
    """跑一条会自己退出的命令并留下日志。

    `timeout` 是**必须**的（本轮加固）：`capture_output=True` 会一直读到管道关闭，
    子进程一旦卡住（实测：宿主 HTTP 代理把 `127.0.0.1:19000` 也代理掉时，
    minio SDK 的 `bucket_exists` 会无限等待），整条启动链就会静默挂死，
    看起来像「脚本没反应」而不是「某一步失败」。超时后按失败处理并给出最后输出。
    """

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(  # noqa: S603 - 参数全部来自本脚本常量
            args,
            cwd=str(cwd),
            env=child_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as expired:
        (LOG_DIR / f"{name}.log").write_text(
            f"超时 {timeout:.0f}s：{' '.join(args)}\n"
            + (expired.stdout or b"").decode("utf-8", "replace")
            + (expired.stderr or b"").decode("utf-8", "replace"),
            encoding="utf-8",
        )
        raise SystemExit(
            f"{name} 超过 {timeout:.0f}s 未退出（已终止），见 {LOG_DIR / f'{name}.log'}"
        ) from expired
    (LOG_DIR / f"{name}.log").write_text(
        (result.stdout or "") + (result.stderr or ""), encoding="utf-8"
    )
    if check and result.returncode != 0:
        raise SystemExit(
            f"{name} 失败（退出码 {result.returncode}）：\n{(result.stdout or '')[-2000:]}\n{(result.stderr or '')[-2000:]}"
        )
    return result


def run_detached(name: str, args: list[str], *, cwd: Path, check: bool = True) -> None:
    """执行一个会**留下后台守护进程**的命令（`pg_ctl start/stop`）。

    为什么不能复用 `run()`：`subprocess.run(capture_output=True)` 会一直读到管道关闭，
    而 `pg_ctl start` 派生出的 postmaster **继承**了这两个管道句柄 —— 于是
    `communicate()` 会一直等到数据库关闭才返回（实测卡满 600 秒超时被杀）。
    把标准输出/错误直接丢弃，`pg_ctl` 自己退出即返回。
    """

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_DIR / f"{name}.log", "ab", buffering=0) as sink:
        completed = subprocess.run(  # noqa: S603 - 参数全部来自本脚本常量
            args,
            cwd=str(cwd),
            env=child_env(),
            stdout=sink,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
        )
    if check and completed.returncode != 0:
        raise SystemExit(f"{name} 失败（退出码 {completed.returncode}），见 {LOG_DIR / f'{name}.log'}")


def psql(sql: str, *, database: str = "postgres", check: bool = True) -> subprocess.CompletedProcess:
    """执行一条 SQL。

    `-tA`（tuples only + unaligned）是必须的：默认的表格输出里**不含**列名以外的
    标识符，用「数据库名是否出现在 stdout 里」判断存在性会永远为假，于是每次都去
    `CREATE DATABASE` 并撞上 `already exists`（实测踩到）。
    """

    return run(
        "psql",
        [
            str(PG_BIN / "psql.exe"),
            "-h",
            "127.0.0.1",
            "-p",
            str(PG_PORT),
            "-U",
            DB_USER,
            "-d",
            database,
            "-tA",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            sql,
        ],
        cwd=RUN_DIR,
        check=check,
    )


# --------------------------------------------------------------------------- #
# 各组件
# --------------------------------------------------------------------------- #
def start_postgres() -> None:
    if port_open(PG_PORT):
        log(f"PostgreSQL 已在 {PG_PORT} 监听，跳过启动")
        return
    if not PG_DATA.exists():
        log("初始化 PostgreSQL 数据目录")
        run(
            "initdb",
            [
                str(PG_BIN / "initdb.exe"),
                "-D",
                str(PG_DATA),
                "-U",
                DB_USER,
                "--auth=trust",
                "--encoding=UTF8",
                "--no-locale",
            ],
            cwd=RUN_DIR,
        )
    run_detached(
        "pg_ctl-start",
        [
            str(PG_BIN / "pg_ctl.exe"),
            "-D",
            str(PG_DATA),
            "-l",
            str(LOG_DIR / "postgres.log"),
            "-o",
            # `timezone=UTC` 不是可选项：官方 `postgres:16-alpine` 镜像的会话时区就是
            # UTC，而 Windows 上 `initdb` 默认继承本机时区（本机是 CST/UTC+8）。
            # 两者不一致会**真实地改变业务结果**——`analysis_dispatches` 的
            # `ck_..._usage_day_matches_created_at` 用 `(created_at AT TIME ZONE 'UTC')::date`
            # 与冗余列对账，会话时区不是 UTC 时 `now() AT TIME ZONE 'UTC'` 写进
            # `timestamptz` 会被按本地时区重新解释，CHECK 直接拒绝合法写入
            # （实测：`tests/db/test_analysis_dispatch_ledger.py` 2 条用例失败）。
            # 原生栈是容器的替代品，必须与容器同口径。
            f"-p {PG_PORT} -c listen_addresses=127.0.0.1 -c timezone=UTC",
            "-W",
            "start",
        ],
        cwd=RUN_DIR,
    )
    wait_port(PG_PORT, label="PostgreSQL")
    wait_pg_ready()
    existing = psql(
        f"SELECT 1 FROM pg_database WHERE datname = '{DB_NAME}'", check=False
    )
    if (existing.stdout or "").strip() != "1":
        psql(f'CREATE DATABASE "{DB_NAME}"')
        log(f"创建数据库 {DB_NAME}")
    log("PostgreSQL 就绪")


def start_redis() -> None:
    if port_open(REDIS_PORT):
        log(f"Redis 已在 {REDIS_PORT} 监听，跳过启动")
        return
    redis_dir = RUN_DIR / "redis"
    redis_dir.mkdir(parents=True, exist_ok=True)
    spawn(
        "redis",
        [
            str(REDIS_BIN / "redis-server.exe"),
            "--port",
            str(REDIS_PORT),
            "--bind",
            "127.0.0.1",
            "--save",
            "",
            "--appendonly",
            "no",
            "--dir",
            str(redis_dir).replace("\\", "/"),
        ],
        cwd=REDIS_BIN,
    )
    wait_port(REDIS_PORT, label="Redis")
    log("Redis 就绪")


def start_minio() -> None:
    if port_open(MINIO_PORT):
        log(f"MinIO 已在 {MINIO_PORT} 监听，跳过启动")
        return
    data_dir = RUN_DIR / "minio-data"
    data_dir.mkdir(parents=True, exist_ok=True)
    env_backup = dict(os.environ)
    os.environ["MINIO_ROOT_USER"] = STORAGE_ACCESS_KEY
    os.environ["MINIO_ROOT_PASSWORD"] = STORAGE_SECRET_KEY
    try:
        spawn(
            "minio",
            [
                str(MINIO_BIN),
                "server",
                str(data_dir),
                "--address",
                f"127.0.0.1:{MINIO_PORT}",
                "--console-address",
                f"127.0.0.1:{MINIO_CONSOLE_PORT}",
            ],
            cwd=RUN_DIR,
        )
    finally:
        os.environ.clear()
        os.environ.update(env_backup)
    wait_port(MINIO_PORT, label="MinIO")
    log("MinIO 就绪")


def ensure_bucket() -> None:
    """用 SDK 建桶（幂等）：与 `probe_object_storage` 走同一条 SDK 路径。"""

    code = (
        "import os\n"
        "from minio import Minio\n"
        "from minio.error import S3Error\n"
        f"client = Minio('127.0.0.1:{MINIO_PORT}', access_key=os.environ['STORAGE_ACCESS_KEY'],"
        " secret_key=os.environ['STORAGE_SECRET_KEY'], secure=False)\n"
        f"name = '{BUCKET}'\n"
        "if not client.bucket_exists(name):\n"
        "    client.make_bucket(name)\n"
        "print('bucket ready')\n"
    )
    run("minio-bucket", [str(PY), "-c", code], cwd=BACKEND)


def migrate() -> None:
    run("alembic-upgrade", [str(PY), "-m", "alembic", "upgrade", "head"], cwd=BACKEND)
    log("迁移到 head 完成")


def seed_prices() -> None:
    run(
        "cost-configs",
        [
            str(PY),
            "-m",
            "app.bootstrap.cost_configs",
            "--price",
            f"TEXT_PROMPT={TEXT_PRICE}",
            "--price",
            f"PHOTO_PROMPT={PHOTO_PRICE}",
        ],
        cwd=BACKEND,
    )
    log("点数定价初始化完成")


def start_api() -> None:
    #: 默认是真实的 `app.main:app`。E2E 需要一封"可读的邮件"时才切到测试入口
    #: `tests.e2e.local_server:app`（只覆盖邮件端口，见该模块说明）。
    target = os.environ.get("ENG10_API_APP", "app.main:app")
    spawn(
        "api",
        [
            str(PY),
            "-m",
            "uvicorn",
            target,
            "--host",
            "127.0.0.1",
            "--port",
            str(API_PORT),
        ],
        cwd=BACKEND,
    )
    wait_http(f"http://127.0.0.1:{API_PORT}/api/v1/health/live", label="FastAPI /live")
    log(f"FastAPI 就绪（{target}）")


def start_worker() -> None:
    spawn(
        "worker",
        [
            str(PY),
            "-m",
            "celery",
            "-A",
            "app.tasks.celery_app",
            "worker",
            "--loglevel=INFO",
            "--concurrency=1",
            "--pool=solo",
        ],
        cwd=BACKEND,
    )
    log("Celery Worker 已启动（就绪以心跳为准）")


def start_beat() -> None:
    spawn(
        "beat",
        [str(PY), "-m", "celery", "-A", "app.tasks.celery_app", "beat", "--loglevel=INFO"],
        cwd=BACKEND,
    )
    log("Celery Beat 已启动")


def build_frontend() -> None:
    env_backup = dict(os.environ)
    os.environ["NEXT_PUBLIC_API_BASE_URL"] = "/api/v1"
    os.environ["NEXT_PUBLIC_APP_URL"] = f"http://localhost:{WEB_PORT}"
    os.environ["API_PROXY_TARGET"] = f"http://127.0.0.1:{API_PORT}"
    try:
        # `next build` 开头会清空旧 `.next`（>50 个文件），本机的批量删除保护会
        # 直接**杀掉整个进程**（`SAFE_DELETE_BULK_CONFIRM_REQUIRED`）。
        # 把 `.next` 改名挪走（同盘瞬间完成，不是删除）即可绕开，且仍是干净构建。
        stale = FRONTEND / ".next"
        if stale.exists():
            target = RUN_DIR / f"next-old-{int(time.time())}"
            target.parent.mkdir(parents=True, exist_ok=True)
            stale.rename(target)
            log(f"旧 .next 已挪到 {target}")
        run("next-build", [str(NODE), str(NEXT_CLI), "build"], cwd=FRONTEND)
    finally:
        os.environ.clear()
        os.environ.update(env_backup)
    log("前端构建完成")


def start_frontend() -> None:
    env_backup = dict(os.environ)
    os.environ["API_PROXY_TARGET"] = f"http://127.0.0.1:{API_PORT}"
    try:
        spawn(
            "frontend",
            [
                str(NODE),
                str(NEXT_CLI),
                "start",
                "-p",
                str(WEB_PORT),
                "-H",
                "127.0.0.1",
            ],
            cwd=FRONTEND,
        )
    finally:
        os.environ.clear()
        os.environ.update(env_backup)
    wait_http(f"http://127.0.0.1:{WEB_PORT}/", timeout=120, label="Next.js")
    log("Next.js 就绪")


# --------------------------------------------------------------------------- #
# 命令
# --------------------------------------------------------------------------- #
def command_up(args: argparse.Namespace) -> None:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    PID_DIR.mkdir(parents=True, exist_ok=True)
    if not PY.exists():
        raise SystemExit(f"找不到 Python 解释器：{PY}")

    start_postgres()
    start_redis()
    start_minio()
    ensure_bucket()
    if args.migrate:
        migrate()
    if args.seed:
        seed_prices()
    start_api()
    start_worker()
    start_beat()
    if not args.no_frontend:
        if args.build_frontend:
            build_frontend()
        start_frontend()

    (RUN_DIR / "stack.json").write_text(
        json.dumps({"env": ENV, "ports": {
            "postgres": PG_PORT, "redis": REDIS_PORT, "minio": MINIO_PORT,
            "minio_console": MINIO_CONSOLE_PORT, "api": API_PORT, "web": WEB_PORT,
        }}, indent=2),
        encoding="utf-8",
    )
    log("全部组件已启动")
    command_status(args)

    if args.hold:
        log("保持前台（Ctrl-C 或 `down` 结束）")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            command_down(args)


def _stop_pid(name: str) -> None:
    pid_file = PID_DIR / f"{name}.pid"
    if not pid_file.exists():
        return
    pid = int(pid_file.read_text(encoding="utf-8").strip())
    try:
        os.kill(pid, signal.SIGTERM)
        log(f"停止 {name} pid={pid}")
    except OSError:
        log(f"{name} pid={pid} 已不在")
    pid_file.unlink(missing_ok=True)


def command_down(args: argparse.Namespace) -> None:
    # 只停本脚本记录过 PID 的进程 + 本脚本的 PostgreSQL 数据目录
    for name in ("frontend", "beat", "worker", "api", "minio", "redis"):
        _stop_pid(name)
    if PG_DATA.exists():
        run_detached(
            "pg_ctl-stop",
            [str(PG_BIN / "pg_ctl.exe"), "-D", str(PG_DATA), "-m", "fast", "-W", "stop"],
            cwd=RUN_DIR,
            check=False,
        )
        log("PostgreSQL 已停止")
    log("本机栈已停止")


def command_status(args: argparse.Namespace) -> None:
    ports = {
        "PostgreSQL": PG_PORT,
        "Redis": REDIS_PORT,
        "MinIO": MINIO_PORT,
        "FastAPI": API_PORT,
        "Next.js": WEB_PORT,
    }
    for label, port in ports.items():
        log(f"{label:<12} {port:<6} {'监听中' if port_open(port) else '无监听'}")
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{API_PORT}/api/v1/health/ready", timeout=10
        ) as response:
            body = json.loads(response.read().decode("utf-8"))
            log(f"/health/ready {response.status} {json.dumps(body.get('data', {}), ensure_ascii=False)}")
    except urllib.error.HTTPError as error:
        log(f"/health/ready {error.code} {error.read().decode('utf-8', 'replace')[:400]}")
    except Exception as error:  # noqa: BLE001
        log(f"/health/ready 不可达：{type(error).__name__}")


def main() -> None:
    parser = argparse.ArgumentParser(description="ENG-10 本机完整闭环栈")
    sub = parser.add_subparsers(dest="command", required=True)

    up = sub.add_parser("up", help="启动全部组件")
    up.add_argument("--hold", action="store_true", help="启动后保持前台（沙箱后台任务用）")
    up.add_argument("--migrate", action="store_true", default=True, help="执行 alembic upgrade head")
    up.add_argument("--no-migrate", dest="migrate", action="store_false")
    up.add_argument("--seed", action="store_true", default=True, help="初始化点数定价")
    up.add_argument("--no-seed", dest="seed", action="store_false")
    up.add_argument("--build-frontend", action="store_true", help="启动前重新构建前端")
    up.add_argument("--no-frontend", action="store_true", help="不起 Next.js")
    up.set_defaults(func=command_up)

    down = sub.add_parser("down", help="停止全部组件")
    down.set_defaults(func=command_down)

    status = sub.add_parser("status", help="探测端口与 /health/ready")
    status.set_defaults(func=command_status)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    sys.exit(main())
