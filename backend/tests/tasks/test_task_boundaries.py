"""Celery 层的边界守卫与导入面隔离测试（ENG-06 第 4 步）。

冻结依据：

- `docs/architecture/05-system-architecture.md` 第 3 节：依赖方向固定为
  `API → Service → Repository → PostgreSQL` / `Beat → Redis → Worker → Service`，
  并明文规定「Beat 不直接写 PostgreSQL、MinIO 或点数账务；Worker 不绕过 Service
  直接修改账务表」；
- `docs/task-cards/LEAD-04-points-and-state-machine.md` 第 6 节（锁与结算只存在于
  Service 事务里）、第 11 节第 1/2 条（Task 只解析参数、建立上下文、调用 Service）；
- `docs/architecture/09-repository-boundary.md` 第 7 节：敏感值不进日志/响应。

手法沿用 `tests/db/test_constraint_alignment.py`：直接扫描 `app/tasks/*.py` 的文本与
AST，而不是靠导入时的偶然行为。第 8 项（导入面隔离）必须用**子进程**验证：
同进程里其他测试早已导入 SQLAlchemy/FastAPI，`sys.modules` 会被污染。
子进程只导入被测模块并打印 `sys.modules` 快照，不连接任何网络或数据库。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from app.config import get_settings
from app.tasks.celery_app import (
    TASK_LIFECYCLE_CLEANUP,
    TASK_POLL_STATUS,
    TASK_RECOVERY_SCAN,
    TASK_RUN_GENERATION,
    TASK_WORKER_HEARTBEAT,
)
from tests.tasks.source_scan import (
    FROZEN_MODULE_NAMES,
    attribute_assignment_targets,
    code_only,
    imported_modules,
    module_level_imports,
    task_sources,
)

BACKEND_ROOT = Path(__file__).resolve().parents[2]

#: 与 `tests/api/conftest.py` 同样的占位连接串：`app/config.py` 是 fail-fast 的
PROBE_DATABASE_URL = "postgresql+psycopg://ai_avatar:placeholder@postgres:5432/ai_avatar"

#: 原生 SQL / ORM 语句形状（词边界避免误伤 `selector` 之类的普通标识符）
RAW_SQL_PATTERNS: tuple[str, ...] = (
    r"\bSELECT\b",
    r"\bINSERT\s+INTO\b",
    r"\bUPDATE\s+\w+\s+SET\b",
    r"\bDELETE\s+FROM\b",
    r"\bUPSERT\b",
    r"\bTRUNCATE\b",
    r"\bCREATE\s+(TABLE|INDEX)\b",
    r"\bDROP\s+(TABLE|INDEX)\b",
)

#: 账务/流水/结算面：Worker 只能通过 Service 触达
ACCOUNTING_TOKENS: tuple[str, ...] = (
    "point_ledger",
    "PointLedger",
    "point_wallet",
    "PointWallet",
    "billing_status",
    "session.commit",
    ".commit(",
    ".rollback(",
    ".execute(",
    "text(",
    "apply_delta",
    "assert_transition",
)

#: 行锁 API：锁顺序由 Service 的事务统一持有（`LEAD-04` 第 6 节）
ROW_LOCK_TOKENS: tuple[str, ...] = (
    "with_for_update",
    "FOR UPDATE",
    "lock_by_id",
    "lock_by_hash",
    "lock_by_user_id",
    "SELECT 1",
)

#: `provider_attempts` 的模型与字段写入面（Worker 不得自己写 attempt 行）
PROVIDER_ATTEMPT_TOKENS: tuple[str, ...] = (
    "ProviderAttempt",
    "ProviderAttemptDraft",
    "provider_attempts",
    ".status =",
    ".error_code =",
    ".request_id =",
    ".finished_at =",
    "setattr(",
)

#: Provider SDK / HTTP 客户端：外部调用一律留在 Provider 适配器里
FORBIDDEN_IMPORT_ROOTS: tuple[str, ...] = (
    "sqlalchemy",
    "alembic",
    "psycopg",
    "fastapi",
    "starlette",
    "pydantic",
    "httpx",
    "requests",
    "aiohttp",
    "urllib3",
    "urllib",
    "http",
    "boto3",
    "minio",
    "oss2",
    "cos",
    "openai",
    "anthropic",
    "socket",
)

#: 本层允许出现的 `app.*` 依赖（第 3 节：Worker 走 Service；装配上下文允许 UoW/Provider 工厂）
ALLOWED_APP_IMPORTS: frozenset[str] = frozenset(
    {
        "app.config",
        "app.db.session",
        "app.repositories.unit_of_work",
        "app.services.generation.orchestrator",
        "app.services.generation.provider_factory",
        "app.services.points.service",
        # ENG-09：生命周期清理的装配入口（Task 只调用 Service）
        "app.services.shares.lifecycle",
        "app.services.ports",
        "app.storage",
    }
)

#: 每个模块**顶层**允许的导入来源：执行上下文的装配必须留在函数体里
EXPECTED_MODULE_LEVEL_IMPORTS: dict[str, tuple[str, ...]] = {
    "__init__.py": ("__future__", "app.tasks", "app.tasks.celery_app", "app.tasks.queue"),
    "celery_app.py": ("__future__", "celery"),
    "heartbeat.py": ("__future__", "app.services.ports", "app.tasks.celery_app", "datetime"),
    "queue.py": ("__future__", "app.tasks.celery_app", "logging", "typing", "uuid"),
    "worker.py": ("__future__", "app.tasks.celery_app", "logging", "typing", "uuid"),
}

#: 导入 `app.tasks.celery_app` 时不得出现在 `sys.modules` 里的包前缀
FORBIDDEN_LOADED_PREFIXES: tuple[str, ...] = (
    "sqlalchemy",
    "alembic",
    "psycopg",
    "fastapi",
    "starlette",
    "pydantic",
    "redis",
    "minio",
    "PIL",
    "httpx",
    "requests",
    "urllib3",
    "boto3",
    "app.services",
    "app.repositories",
    "app.db",
    "app.api",
    "app.main",
    "app.providers",
    "app.storage",
    "app.schemas",
    "app.middleware",
    "app.errors",
)

#: 导入 `app.main`（Web 进程）时不得出现在 `sys.modules` 里的包前缀
FORBIDDEN_WEB_LOADED_PREFIXES: tuple[str, ...] = ("app.tasks", "celery", "kombu")

#: 子进程探针：只导入目标模块，打印 `sys.modules` 快照（不触网、不连库）
IMPORT_PROBE = """
import importlib
import json
import sys

target = sys.argv[1]
importlib.import_module(target)
module = sys.modules[target]

payload = {"target": target, "modules": sorted(sys.modules)}
if "celery" in sys.modules:
    from celery import Celery

    instances = [value for value in vars(module).values() if isinstance(value, Celery)]
    payload["celery_instances"] = len(instances)
    if instances:
        app = instances[0]
        payload["app_main"] = app.main
        payload["broker_url"] = app.conf.broker_url
        payload["result_backend"] = repr(app.conf.result_backend)
        payload["backend_class"] = type(app.backend).__name__
        payload["tasks"] = sorted(
            name for name in app.tasks if not name.startswith("celery.")
        )
sys.stdout.write(json.dumps(payload))
"""


class ImportProbeResult:
    """子进程导入探针的结果（只暴露断言需要的字段）。"""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    @property
    def modules(self) -> list[str]:
        return [str(name) for name in self._payload["modules"]]

    @property
    def tasks(self) -> list[str]:
        return [str(name) for name in self._payload["tasks"]]

    def value(self, key: str) -> Any:
        assert key in self._payload, f"探针未返回 {key}：{sorted(self._payload)}"
        return self._payload[key]


def run_import_probe(target: str) -> ImportProbeResult:
    """在独立子进程里导入 `target` 并返回其 `sys.modules` 快照与 Celery 侧观察值。

    子进程只做导入：不启动 Worker/Beat，不建连接，不执行任务。
    """

    environment = dict(os.environ)
    environment["DATABASE_URL"] = PROBE_DATABASE_URL
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(BACKEND_ROOT), environment.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"

    completed = subprocess.run(
        [sys.executable, "-c", IMPORT_PROBE, target],
        cwd=str(BACKEND_ROOT),
        env=environment,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )

    assert completed.returncode == 0, (
        f"子进程导入 {target} 失败：\n{completed.stdout}\n{completed.stderr}"
    )
    payload: dict[str, Any] = json.loads(completed.stdout)
    return ImportProbeResult(payload)


def leaked_modules(modules: list[str], prefixes: tuple[str, ...]) -> list[str]:
    """落在禁用前缀里的已导入模块名。"""

    return sorted(
        name
        for name in modules
        if any(name == prefix or name.startswith(f"{prefix}.") for prefix in prefixes)
    )


# --------------------------------------------------------------------------- #
# 7. 任务层不得绕过 Service
# --------------------------------------------------------------------------- #
#: 每个模块必须留在扫描面里的一条代表性语句（防止「剥文档串剥过头」让否定断言空转）
REPRESENTATIVE_STATEMENTS: dict[str, str] = {
    "__init__.py": "from app.tasks import worker",
    "celery_app.py": "beat_schedule=BEAT_SCHEDULE",
    "heartbeat.py": "redis.set(WORKER_HEARTBEAT_KEY",
    "queue.py": "self._app.send_task(task_name",
    "worker.py": "build_orchestrator().submit(",
}


def test_the_frozen_task_modules_are_all_scanned() -> None:
    """扫描面本身要成立：`app/tasks/` 下五个被冻结模块都在。"""

    sources = task_sources()

    assert set(FROZEN_MODULE_NAMES) <= set(sources)
    assert all(text.strip() for text in sources.values())


@pytest.mark.parametrize("module_name", FROZEN_MODULE_NAMES)
def test_docstring_stripping_keeps_the_scanned_surface_meaningful(module_name: str) -> None:
    """`code_only()` 只剥文档串：可执行代码必须仍在，否则下面的否定断言全是空转。"""

    raw = task_sources()[module_name]
    code = code_only(raw)

    assert code.strip()
    assert len(code) >= len(raw) * 0.4, module_name
    assert REPRESENTATIVE_STATEMENTS[module_name] in code, module_name


@pytest.mark.parametrize("module_name", FROZEN_MODULE_NAMES)
def test_no_raw_sql_in_the_task_layer(module_name: str) -> None:
    """任务层不出现任何原生 SQL（第 3 节：SQL 属于 Repository，事务属于 Service）。"""

    code = code_only(task_sources()[module_name])

    for pattern in RAW_SQL_PATTERNS:
        assert re.search(pattern, code, re.IGNORECASE) is None, (module_name, pattern)


@pytest.mark.parametrize("module_name", FROZEN_MODULE_NAMES)
def test_no_accounting_or_ledger_touchpoints_in_the_task_layer(module_name: str) -> None:
    """点数流水/钱包/`billing_status`/事务提交一律不得出现在本层。"""

    code = code_only(task_sources()[module_name])

    for token in ACCOUNTING_TOKENS:
        assert token not in code, (module_name, token)


@pytest.mark.parametrize("module_name", FROZEN_MODULE_NAMES)
def test_no_row_lock_apis_in_the_task_layer(module_name: str) -> None:
    """`FOR UPDATE` / `with_for_update` / 仓储锁方法不得出现在本层（固定锁顺序在 Service）。"""

    code = code_only(task_sources()[module_name])

    for token in ROW_LOCK_TOKENS:
        assert token.upper() not in code.upper(), (module_name, token)


@pytest.mark.parametrize("module_name", FROZEN_MODULE_NAMES)
def test_no_provider_attempt_field_writes_in_the_task_layer(module_name: str) -> None:
    """本层不认识 `provider_attempts` 行：既不建模也不改字段。"""

    code = code_only(task_sources()[module_name])

    for token in PROVIDER_ATTEMPT_TOKENS:
        assert token not in code, (module_name, token)


@pytest.mark.parametrize("module_name", FROZEN_MODULE_NAMES)
def test_the_layer_performs_no_object_attribute_writes_except_its_own_port(
    module_name: str,
) -> None:
    """整个任务层只有一处属性赋值：`CeleryGenerationQueue.__init__` 保存注入的 app。

    出现任何其它 `obj.field = ...` 就意味着有人在任务里直接改实体字段。
    """

    code = code_only(task_sources()[module_name])

    assert attribute_assignment_targets(code) == (("_app",) if module_name == "queue.py" else ())


@pytest.mark.parametrize("module_name", FROZEN_MODULE_NAMES)
def test_no_provider_sdk_or_http_client_is_imported(module_name: str) -> None:
    """Provider SDK / HTTP 客户端不得进入本层：外部调用留在 Provider 适配器内。"""

    roots = set(imported_modules(code_only(task_sources()[module_name])))

    offenders = {
        root
        for root in roots
        if any(root == prefix or root.startswith(f"{prefix}.") for prefix in FORBIDDEN_IMPORT_ROOTS)
    }
    assert offenders == set(), (module_name, sorted(offenders))


def test_the_web_framework_is_not_imported_anywhere_in_the_layer() -> None:
    """`app/tasks` 不导入 `fastapi`/`starlette`/`pydantic`：Worker 进程不加载 Web 栈。"""

    roots = {
        root
        for name, text in task_sources().items()
        for root in imported_modules(code_only(text))
    }

    for framework in ("fastapi", "starlette", "pydantic"):
        assert not any(
            root == framework or root.startswith(f"{framework}.") for root in roots
        ), sorted(roots)


def test_app_layer_dependencies_stay_inside_the_allow_list() -> None:
    """本层触达的 `app.*` 依赖必须逐项确认过（新增一条就要过一次评审）。"""

    roots = {
        root
        for name, text in task_sources().items()
        for root in imported_modules(code_only(text))
        if root.startswith("app.")
    }
    outside = {
        root
        for root in roots
        if not root.startswith("app.tasks") and root not in ALLOWED_APP_IMPORTS
    }

    assert outside == set(), sorted(outside)
    assert "app.repositories.generation" not in roots, "任务层不得直接用生成仓储"
    assert not [root for root in roots if root.startswith("app.api")], sorted(roots)


@pytest.mark.parametrize("module_name", FROZEN_MODULE_NAMES)
def test_execution_context_is_only_wired_lazily(module_name: str) -> None:
    """顶层导入面冻结：数据库/存储/Provider/Service 装配只能藏在函数体里。

    `celery -A app.tasks.celery_app` 加载任务模块时不应建引擎、不应连对象存储；
    真正执行任务时才由 `build_orchestrator()` 装配。
    """

    text = task_sources()[module_name]

    assert module_level_imports(code_only(text)) == EXPECTED_MODULE_LEVEL_IMPORTS[module_name]
    deferred = set(imported_modules(code_only(text))) - set(
        module_level_imports(code_only(text))
    )
    if module_name == "worker.py":
        assert {
            "app.db.session",
            "app.repositories.unit_of_work",
            "app.services.generation.orchestrator",
            "app.services.generation.provider_factory",
            "app.services.points.service",
            "app.storage",
            "app.config",
        } <= deferred, sorted(deferred)


# --------------------------------------------------------------------------- #
# 8. 导入面隔离（子进程，避免同进程内其他测试污染 sys.modules）
# --------------------------------------------------------------------------- #
def test_importing_the_celery_app_does_not_load_the_execution_stack() -> None:
    """导入 `app.tasks.celery_app` 不得拉起 SQLAlchemy/Service/Repository/FastAPI。

    Web 进程只按任务名投递、不加载 Worker 侧执行栈；Worker 也不加载 FastAPI 栈。
    子进程只导入模块，不建任何连接。
    """

    probe = run_import_probe("app.tasks.celery_app")

    assert probe.value("celery_instances") == 1
    assert probe.value("app_main") == "ai_avatar"
    assert set(probe.tasks) == {
        TASK_RUN_GENERATION,
        TASK_POLL_STATUS,
        TASK_RECOVERY_SCAN,
        TASK_WORKER_HEARTBEAT,
        TASK_LIFECYCLE_CLEANUP,
    }
    assert probe.value("result_backend") == "None"
    assert probe.value("backend_class") == "DisabledBackend"
    assert leaked_modules(probe.modules, FORBIDDEN_LOADED_PREFIXES) == []


def test_importing_the_web_app_does_not_load_the_worker_stack() -> None:
    """镜像方向也必须成立：`uvicorn app.main:app` 不加载 Celery/Worker 栈。"""

    probe = run_import_probe("app.main")

    assert "app.main" in probe.modules
    assert leaked_modules(probe.modules, FORBIDDEN_WEB_LOADED_PREFIXES) == []


def test_import_probe_needs_no_real_broker_or_redis_client() -> None:
    """探针自身的前提：Broker URL 只是字符串，导入阶段既不加载 Redis 驱动也不解析主机名。"""

    probe = run_import_probe("app.tasks.celery_app")

    assert probe.value("broker_url") == get_settings().redis_url
    assert str(probe.value("broker_url")).startswith("redis://")
    assert "redis" not in probe.modules
    assert "psycopg" not in probe.modules
