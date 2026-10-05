#!/usr/bin/env python
"""ENG-10 浏览器 E2E 的应用进程栈（原生进程 + **已存在的隔离基础设施**）。

## 为什么需要它：Docker Desktop 的预签名 URL 跨不过宿主防火墙

浏览器取图的地址是**后端进程签出来的**，它的 Host 来自 `STORAGE_ENDPOINT`
（`app/storage/minio_adapter.py::create_signed_url`）。在容器里跑后端时，
`STORAGE_ENDPOINT` 必须写成宿主的 MinIO 端口，于是预签名 URL 变成
`http://<宿主>:19000/...`。本机实测（ENG-10，2026-10-13）：

```text
宿主 -> 192.168.65.254:19000        ERR: 操作超时（Windows 无法路由到 Docker VM 网关）
宿主 -> 10.193.104.171:19000        ERR: 操作超时
容器 -> host.docker.internal:19000   OSError: [Errno 101] Network is unreachable
```

即：**Windows 宿主防火墙默认不接受来自 Docker VM 子网的入站连接**，因此
「后端在容器里、MinIO 必须经宿主端口访问」这条组合在本机走不通。这不是 compose
配置能修的东西（改成 `0.0.0.0` 也一样，错误从「超时」变成「网络不可达」）。

## 因此本脚本做什么

把**应用进程**放到宿主机上跑（它们能直连 Compose 已经起好的 PostgreSQL / Redis /
MinIO），只让**基础设施**留在容器里：

```text
PostgreSQL  容器 ai-avatar-eng10-postgres-1   → 宿主 127.0.0.1:15432
Redis       容器 ai-avatar-eng10-redis-1      → 宿主 127.0.0.1:16379
MinIO       容器 ai-avatar-eng10-minio-1      → 宿主 127.0.0.1:19000
FastAPI     原生 uvicorn tests.e2e.local_server:app   → 127.0.0.1:18001
Celery Worker 原生 celery ... worker           → Redis 队列 ai-avatar-v1
Celery Beat   原生 celery ... beat             → Redis
Next.js     原生 next start                    → 127.0.0.1:13000
```

这样 `STORAGE_ENDPOINT=http://127.0.0.1:19000` 对**后端进程**与**浏览器**是同一个
可解析地址，预签名 URL 两端都能用。**报告里必须如实写成「原生应用进程 + 容器化
基础设施」**，不能写成 Compose 七服务验收 —— 后者由
`docker compose ... up` 单独验证（七服务健康检查、同源代理、迁移）。

## 用法

```bash
python scripts/eng10_ui_stack.py up --hold    # 起全部并保持前台
python scripts/eng10_ui_stack.py status
python scripts/eng10_ui_stack.py down
```

只操作本脚本 `ENG10_UI_RUN_DIR`（默认 `D:/tmp/eng10-run/ui`）下记录过 PID 的进程。
"""

from __future__ import annotations

import argparse
import json
import os
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

RUN_DIR = Path(os.environ.get("ENG10_UI_RUN_DIR", "D:/tmp/eng10-run/ui"))
LOG_DIR = RUN_DIR / "logs"
PID_DIR = RUN_DIR / "pids"
MAIL_DIR = Path(os.environ.get("ENG10_MAIL_DIR", "D:/tmp/eng10-run/mail"))
SCENARIO_DIR = Path(os.environ.get("ENG10_SCENARIO_OUT", "D:/tmp/eng10-run/scenarios"))

PY = Path(
    os.environ.get("ENG10_PYTHON", str(REPO / ".venv-eng09-r2" / "Scripts" / "python.exe"))
)
NODE = Path(
    os.environ.get(
        "ENG10_NODE",
        "C:/Users/盛浩然/.workbuddy-ai/binaries/node/versions/22.22.2-3/node.exe",
    )
)
NEXT_CLI = FRONTEND / "node_modules" / "next" / "dist" / "bin" / "next"

PG_HOST = os.environ.get("ENG10_PG_HOST", "127.0.0.1")
PG_PORT = int(os.environ.get("ENG10_PG_PORT", "15432"))
REDIS_PORT = int(os.environ.get("ENG10_REDIS_PORT", "16379"))
MINIO_PORT = int(os.environ.get("ENG10_MINIO_PORT", "19000"))
API_PORT = int(os.environ.get("ENG10_API_PORT", "18001"))
#: 默认 13010：默认的 3000/13000 在本机被别人占着时，`next start` 会直接
#: `EADDRINUSE` 退出，而 `wait_http` 会因为那个前台服务同样返回 200 而**误判就绪**
#: —— 之后的浏览器请求会打到**别人的前端**上。实测踩到过：浏览器打的是 Compose 里的
#: Next.js，它连的是另一个数据库，于是兑换邀请码 422 INVITE_INVALID。
#: 除了避开常见端口，`start_frontend` 还会在就绪后核对进程仍然活着。
WEB_PORT = int(os.environ.get("ENG10_WEB_PORT", "13010"))
DB_NAME = os.environ.get("ENG10_UI_DB_NAME", "ai_avatar_eng10ui")

STORAGE_ACCESS_KEY = "eng10-local-access"
STORAGE_SECRET_KEY = "eng10-local-secret"
SESSION_SECRET = "eng10-local-session-secret"

ENV = {
    "APP_ENV": "local",
    "APP_BASE_URL": f"http://127.0.0.1:{API_PORT}",
    "DATABASE_URL": f"postgresql+psycopg://ai_avatar:placeholder@{PG_HOST}:{PG_PORT}/{DB_NAME}",
    "REDIS_URL": f"redis://127.0.0.1:{REDIS_PORT}/0",
    "STORAGE_DRIVER": "minio",
    "STORAGE_ENDPOINT": f"http://127.0.0.1:{MINIO_PORT}",
    "STORAGE_BUCKET": "ai-avatar",
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
    "ENG10_MAIL_DIR": str(MAIL_DIR),
    "ENG10_SCENARIO_OUT": str(SCENARIO_DIR),
}


def log(message: str) -> None:
    print(f"[eng10ui] {message}", flush=True)


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    with socket.socket() as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def child_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(ENV)
    env["PYTHONPATH"] = "."
    env["PYTHONUNBUFFERED"] = "1"
    env.pop("PYTHONUTF8", None)
    env.pop("PYTHONIOENCODING", None)
    return env


def spawn(name: str, args: list[str], *, cwd: Path, extra_env: dict[str, str] | None = None) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    PID_DIR.mkdir(parents=True, exist_ok=True)
    env = child_env()
    env.update(extra_env or {})
    creation = 0
    if os.name == "nt":
        # **只用 `CREATE_NEW_PROCESS_GROUP`，不要 `DETACHED_PROCESS`。**
        #
        # 实测踩到（ENG-10）：两者一起用时，`Popen` 交出的 pid 并不是真正在跑
        # `python -m uvicorn` 的那个人，而这个「中间进程」是活的，所以
        #   - `record_pid` 记错了对象，
        #   - `down` 杀掉中间进程后**真正的 uvicorn 被留下**继续占着端口。
        # 后果非常隐蔽：下一次 `up` 以为端口空着，实际打的是**上一次那个**进程——
        # 它连着另一个数据库（`ai_avatar` 而不是 `ai_avatar_eng10ui`），于是
        # 创建生成任务返回 503 `cost_config_missing`，看起来像编排坏了。
        #
        # 现在实测：只带 `CREATE_NEW_PROCESS_GROUP` 时 `Popen.pid` 就是 uvicorn 本尊，
        # 而且父进程退出后它仍然活着（脚本结束不会带走服务）。
        creation = subprocess.CREATE_NEW_PROCESS_GROUP
    with open(LOG_DIR / f"{name}.log", "ab", buffering=0) as sink:
        process = subprocess.Popen(  # noqa: S603 - 参数全部来自本脚本常量
            args,
            cwd=str(cwd),
            env=env,
            stdout=sink,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=creation,
        )
    (PID_DIR / f"{name}.pid").write_text(str(process.pid), encoding="utf-8")
    log(f"启动 {name} pid={process.pid} → {LOG_DIR / f'{name}.log'}")


def port_owner(port: int) -> str:
    """`127.0.0.1:<port>` 上当前监听者的描述（用于「端口被谁占了」的诊断）。

    纯尽力而为：优先用 `psutil`（若已安装），否则退回一句通用说明。
    **不能依赖 `powershell.exe` 一定在 PATH 上**（本机实测 `FileNotFoundError`）。
    """

    try:
        import psutil  # type: ignore[import-not-found]
    except ImportError:
        return "unknown（未安装 psutil，无法列出占用者）"
    for connection in psutil.net_connections(kind="tcp"):
        if connection.laddr and connection.laddr.port == port and connection.status == "LISTEN":
            try:
                process = psutil.Process(connection.pid)
                return f"{connection.pid}/{process.name()}"
            except Exception:  # noqa: BLE001
                return f"{connection.pid}/unknown"
    return "unknown"


def _process_alive(pid: int) -> bool:
    """进程是否仍然存在（只用标准库，不依赖外部命令）。"""

    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(  # type: ignore[attr-defined]
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]
        return True
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _children_of(pid: int) -> list[int]:
    """直接子进程的 pid 列表。

    本机实测 `taskkill`、`wmic`、`powershell` **都不在 PATH 上**
    （`FileNotFoundError`），所以这里用 `CreateToolhelp32Snapshot` 走 Win32 工具
    帮助 API（`ctypes` + 标准库，无需外部命令）。非 Windows 上退回 `psutil`（若有）。
    """

    if os.name != "nt":
        try:
            import psutil  # type: ignore[import-not-found]

            return [child.pid for child in psutil.Process(pid).children()]
        except Exception:  # noqa: BLE001
            return []

    import ctypes
    import ctypes.wintypes as wintypes

    TH32CS_SNAPPROCESS = 0x00000002
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class PROCESSENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_char * 260),
        ]

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == INVALID_HANDLE_VALUE or not snapshot:
        return []
    children: list[int] = []
    try:
        entry = PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
        ok = kernel32.Process32First(snapshot, ctypes.byref(entry))
        while ok:
            if entry.th32ParentProcessID == pid:
                children.append(int(entry.th32ProcessID))
            ok = kernel32.Process32Next(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return children


def _terminate(pid: int) -> None:
    """**只用标准库**强制结束一个进程（不依赖 `taskkill`）。"""

    if os.name != "nt":
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        return

    import ctypes

    PROCESS_TERMINATE = 0x0001
    handle = ctypes.windll.kernel32.OpenProcess(  # type: ignore[attr-defined]
        PROCESS_TERMINATE, False, pid
    )
    if not handle:
        return
    try:
        ctypes.windll.kernel32.TerminateProcess(handle, 1)  # type: ignore[attr-defined]
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]


def _graceful_terminate(pid: int, *, timeout: float = 20.0) -> bool:
    """先请进程**自己收尾**（Celery 收到 SIGTERM 会把在跑的任务做完），超时才强杀。

    为什么重要：`acks_late` 下强杀正在处理任务的 Worker，消息不会被确认，
    要等 broker 的 visibility timeout 才重投（本仓库是 600 秒）。实测踩到：
    测试为了独占任务把 Worker 强杀，正在跑的成功任务就一直停在 `QUEUED/RESERVED`，
    看起来像「结算坏了」。优雅退出把在跑任务做完，语义干净得多。
    """

    if os.name != "nt":
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return True
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not _process_alive(pid):
                return True
            time.sleep(0.2)
        return False

    # Windows 没有真正的 SIGTERM；`CTRL_BREAK_EVENT` 是 Celery 认得的优雅信号，
    # 但要求进程以 CREATE_NEW_PROCESS_GROUP 启动（本脚本正是这样起的）。
    try:
        os.kill(pid, signal.CTRL_BREAK_EVENT)
    except (OSError, AttributeError, ValueError):
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _process_alive(pid):
            return True
        time.sleep(0.2)
    return False


def stop_process_tree(pid: int) -> None:
    """结束进程**及其所有后代**。

    为什么不能只杀 `Popen.pid`：Windows 上 `python -m uvicorn` 会再派生一个子进程，
    **真正监听端口的是子进程**。只杀父进程会留下一个占着端口的孤儿（实测踩到：
    下一次 `up` 以为端口空着，实际打的是上一次那个连着**另一个数据库**的进程，
    于是创建生成任务 503 `cost_config_missing`）。

    先走 `_graceful_terminate`（让 Celery 把在跑的任务做完），超时才递归强杀；
    不依赖 `taskkill`（本机不在 PATH 上）。
    """

    if not _graceful_terminate(pid):
        for child in _children_of(pid):
            stop_process_tree(child)
        _terminate(pid)


def assert_still_alive(name: str, port: int) -> None:
    """就绪后再确认端口是**我们自己这条进程链**在服务。

    只看 `/` 返回 200 是不够的：端口被别的进程占着时，`wait_http` 会误判成功，
    而浏览器随后打到的是别人的前端（实测踩到：浏览器打的是 Compose 里的 Next.js，
    它连的是另一个数据库，于是兑换邀请码 422 INVITE_INVALID）。

    注意「我们自己这条链」而不是「Popen.pid 本人」：`python -m uvicorn` 会再派生
    一个子进程，监听端口的是子进程（见 `stop_process_tree`）。
    """

    pid = int((PID_DIR / f"{name}.pid").read_text(encoding="utf-8").strip())
    if not _process_alive(pid):
        raise SystemExit(
            f"{name}（pid={pid}）已退出，但 127.0.0.1:{port} 仍有服务在响应 —— "
            f"该端口被别的进程占用（{port_owner(port)}）。见 {LOG_DIR / f'{name}.log'}。"
        )

    descendant = _descendant_holding_port(pid, port)
    if descendant is None:
        raise SystemExit(
            f"{name}（pid={pid}）还活着，但 127.0.0.1:{port} **不是它这条链**在监听"
            f"（占用者：{port_owner(port)}）—— 说明端口被抢占，`wait_http` 的 200 来自别人。"
            f"见 {LOG_DIR / f'{name}.log'}。"
        )


def _descendant_holding_port(root_pid: int, port: int) -> int | None:
    """在 `root_pid` 及其后代里找监听 `port` 的进程；找不到返回 `None`。

    本机没有 `psutil`，因此「谁在监听」用「尝试连一次 + 进程树归属」的近似判据：
    只要端口上有连接，且这条链里存在任意活跃进程，就认为归属成立。
    """

    pending = [root_pid]
    seen: set[int] = set()
    alive = False
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        if not _process_alive(current):
            continue
        alive = True
        pending.extend(_children_of(current))

    if not alive:
        return None
    if not port_open(port):
        return None
    return root_pid


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
        except Exception as error:  # noqa: BLE001
            last = type(error).__name__
        time.sleep(0.5)
    raise SystemExit(f"超时：{label or url} 未就绪（最后错误 {last}）")


def command_up(args: argparse.Namespace) -> None:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    MAIL_DIR.mkdir(parents=True, exist_ok=True)
    SCENARIO_DIR.mkdir(parents=True, exist_ok=True)
    if not PY.exists():
        raise SystemExit(f"找不到 Python 解释器：{PY}")

    for label, port in (("PostgreSQL", PG_PORT), ("Redis", REDIS_PORT), ("MinIO", MINIO_PORT)):
        if not port_open(port):
            raise SystemExit(
                f"{label} 未在 127.0.0.1:{port} 监听。先起隔离基础设施：\n"
                "  docker compose --env-file .env.eng10 -p ai-avatar-eng10 "
                "-f docker/compose.yml up -d postgres redis minio"
            )

    if not args.no_api:
        spawn(
            "api",
            [
                str(PY),
                "-m",
                "uvicorn",
                "tests.e2e.local_server:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(API_PORT),
            ],
            cwd=BACKEND,
        )
        wait_http(f"http://127.0.0.1:{API_PORT}/api/v1/health/live", label="FastAPI /live")
        log(f"FastAPI（邮件替身入口）就绪 → 127.0.0.1:{API_PORT}")

    if not args.no_worker:
        _start_worker_only()

    if not args.no_beat:
        # `--schedule` 必须指到仓库外：Celery Beat 默认把调度状态写在**当前工作目录**
        # 的 `celerybeat-schedule`（外加 `-shm`/`-wal`）。在 `backend/` 里直接跑
        # 会在仓库根留下三个未跟踪文件（实测踩到），因此显式改到 RUN_DIR。
        spawn(
            "beat",
            [
                str(PY),
                "-m",
                "celery",
                "-A",
                "app.tasks.celery_app",
                "beat",
                "--loglevel=INFO",
                "--schedule",
                str(RUN_DIR / "celerybeat-schedule"),
            ],
            cwd=BACKEND,
        )
        log("Celery Beat 已启动")

    if not args.no_frontend:
        spawn(
            "frontend",
            [str(NODE), str(NEXT_CLI), "start", "-p", str(WEB_PORT), "-H", "127.0.0.1"],
            cwd=FRONTEND,
            extra_env={"API_PROXY_TARGET": f"http://127.0.0.1:{API_PORT}"},
        )
        wait_http(f"http://127.0.0.1:{WEB_PORT}/", timeout=120, label="Next.js")
        assert_still_alive("frontend", WEB_PORT)
        log(f"Next.js 就绪 → 127.0.0.1:{WEB_PORT}")

    (RUN_DIR / "stack.json").write_text(
        json.dumps(
            {
                "env": ENV,
                "ports": {
                    "postgres": PG_PORT,
                    "redis": REDIS_PORT,
                    "minio": MINIO_PORT,
                    "api": API_PORT,
                    "web": WEB_PORT,
                },
            },
            indent=2,
        ),
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


def _start_worker_only() -> None:
    """只启动 Worker（`command_up` 与 `start-worker` 子命令共用）。

    `DATABASE_URL` 等由 `child_env()` 注入，与 API 完全同源 —— 这一点是硬要求：
    如果 Worker 连的是**另一个库**，它会消费到队列里的任务却在那个库里找不到对应行，
    表现成「任务停在 QUEUED」（实测踩到，排查了很久）。

    ## 启动后必须等到**真的就绪**才返回（本轮新增）

    E2E 里 `only_scenario_worker()` 的 `finally` 会调 `start-worker`，随后用例立刻
    继续断言。Celery 从进程起来到「连上 broker、开始 prefetch」有几秒窗口；
    这个窗口里投递的消息没人消费，任务会停在 `QUEUED` —— 看起来像「结算坏了」。
    因此这里等 Worker 日志出现 Celery 的 `ready.` 行才返回，失败就响亮退出。
    """

    log_path = LOG_DIR / "worker.log"
    offset = log_path.stat().st_size if log_path.exists() else 0
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
    _wait_log_contains("worker", "ready.", since=offset, timeout=90)
    log("Celery Worker 已启动并就绪（真实消费 Redis 队列 ai-avatar-v1）")


def _wait_log_contains(name: str, needle: str, *, since: int = 0, timeout: float = 60.0) -> None:
    """等某个组件的日志（从 `since` 字节起）出现 `needle`；超时抛 `SystemExit`。

    只读日志、不猜进程状态：`_process_alive` 只能证明进程活着，证明不了它**已经就绪**。
    """

    path = LOG_DIR / f"{name}.log"
    deadline = time.monotonic() + timeout
    tail = ""
    while time.monotonic() < deadline:
        try:
            with open(path, "rb") as sink:
                sink.seek(since)
                tail = sink.read().decode("utf-8", "replace")
        except OSError:
            tail = ""
        if needle in tail:
            return
        time.sleep(0.3)
    raise SystemExit(
        f"超时：{name} 的日志在 {timeout:.0f}s 内没有出现 {needle!r}"
        f"（末尾 {tail[-300:]!r}），见 {path}"
    )


def _stop_pid(name: str) -> None:
    pid_file = PID_DIR / f"{name}.pid"
    if not pid_file.exists():
        return
    pid = int(pid_file.read_text(encoding="utf-8").strip())
    if not _process_alive(pid):
        log(f"{name} pid={pid} 已不在")
        pid_file.unlink(missing_ok=True)
        return
    # 必须整棵树一起停：真正监听端口的是 `python -m uvicorn` 派生出来的子进程
    # （见 `stop_process_tree` 的说明）。
    stop_process_tree(pid)
    # 停完必须**核对真的死了**：`stop-worker` 的调用方（E2E 的场景执行器）会立刻
    # 假设「队列上已经没有别的消费者」。如果这里静默失败，就会同时存在两个 Worker，
    # 场景执行器与真实 Worker 抢同一个任务，症状是随机的 `NOT_CLAIMABLE`。
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and _process_alive(pid):
        time.sleep(0.2)
    if _process_alive(pid):
        raise SystemExit(
            f"{name}（pid={pid}）停止失败：进程仍在。见 {LOG_DIR / f'{name}.log'}。"
        )
    log(f"停止 {name} pid={pid}（含子进程，已核对进程退出）")
    pid_file.unlink(missing_ok=True)


def command_down(args: argparse.Namespace) -> None:
    for name in ("frontend", "beat", "worker", "api"):
        _stop_pid(name)
    log("本机应用进程栈已停止（容器化基础设施未动）")


def command_status(args: argparse.Namespace) -> None:
    for label, port in (
        ("PostgreSQL", PG_PORT),
        ("Redis", REDIS_PORT),
        ("MinIO", MINIO_PORT),
        ("FastAPI", API_PORT),
        ("Next.js", WEB_PORT),
    ):
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
    parser = argparse.ArgumentParser(description="ENG-10 浏览器 E2E 应用进程栈")
    sub = parser.add_subparsers(dest="command", required=True)

    up = sub.add_parser("up", help="启动应用进程")
    up.add_argument("--hold", action="store_true")
    up.add_argument("--no-api", action="store_true")
    up.add_argument("--no-worker", action="store_true")
    up.add_argument("--no-beat", action="store_true")
    up.add_argument("--no-frontend", action="store_true")
    up.set_defaults(func=command_up)

    down = sub.add_parser("down", help="停止应用进程")
    down.set_defaults(func=command_down)

    stop_worker = sub.add_parser(
        "stop-worker", help="只停 Worker（供 E2E 让场景执行器独占任务）"
    )
    stop_worker.set_defaults(func=lambda args: _stop_pid("worker"))

    start_worker = sub.add_parser("start-worker", help="只起 Worker")
    start_worker.set_defaults(func=lambda args: _start_worker_only())

    status = sub.add_parser("status", help="探测端口与 /health/ready")
    status.set_defaults(func=command_status)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    sys.exit(main())
