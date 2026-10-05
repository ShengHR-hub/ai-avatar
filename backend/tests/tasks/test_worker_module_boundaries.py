"""任务体模块的装配边界测试（ENG-06 第 4 步）。

委托契约本身（解析参数 → 调用 Service → 回传结果码）由
`test_worker_task_delegation.py` 冻结，本模块只补它覆盖不到的**结构侧**两条：

- 依赖方向：`app.tasks.worker` 的**模块级**不得装配外部依赖（UoW/存储/Provider），
  否则 `celery -A app.tasks.celery_app worker` 一加载就连数据库——违反
  `05-system-architecture.md` 第 3 节的进程模型；
- 任务体的协作者只有编排层：函数体里不得出现会话、仓储、账务、HTTP 客户端或
  状态机原语（`LEAD-04` §11:188「Task 只解析参数、建立上下文、调用 Service」）。

不启动 Worker/Beat，不连 Redis、Broker、PostgreSQL 或 MinIO。
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from app.tasks import worker as worker_module

#: 任务体函数体里不得出现的依赖 token（出现即说明业务/账务/连接漏进了入口层）
FORBIDDEN_BODY_TOKENS = (
    "point_ledgers",
    "session",
    "engine",
    "repository",
    "httpx",
    "requests",
    "minio",
    "provider",
    "assert_transition",
    "reserve(",
    "consume(",
    "release(",
)

#: 允许在 `build_orchestrator` 内部惰性装配的外部依赖
DEFERRED_DEPENDENCY_MODULES = (
    "app.db.session",
    "app.repositories.unit_of_work",
    "app.storage",
    "app.services.generation.orchestrator",
    "app.services.generation.provider_factory",
    "app.services.points.service",
)


def _module_tree() -> ast.Module:
    source = Path(inspect.getfile(worker_module)).read_text(encoding="utf-8")
    return ast.parse(source)


def _module_level_imports(tree: ast.Module) -> list[str]:
    """顶层（不在任何函数体内）的 `from X import ...` 模块名。"""

    nested: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            nested.update(id(child) for child in ast.walk(node))

    return [
        node.module or ""
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and id(node) not in nested
    ]


def _function_tree(name: str) -> ast.FunctionDef:
    return next(
        node
        for node in ast.walk(_module_tree())
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


# --------------------------------------------------------------------------- #
# 装配时机
# --------------------------------------------------------------------------- #
def test_module_level_code_wires_no_external_dependency() -> None:
    """模块级只有 Celery 实例与常量：导入任务体不得连库、连存储或选 Provider。"""

    for module in _module_level_imports(_module_tree()):
        assert not module.startswith("app.services"), module
        assert not module.startswith("app.repositories"), module
        assert module not in {"app.db.session", "app.storage"}, module


@pytest.mark.parametrize("required", DEFERRED_DEPENDENCY_MODULES)
def test_build_orchestrator_defers_every_external_dependency(required: str) -> None:
    """反向证明上一条：这些依赖确实归 `build_orchestrator` 在调用时装配。"""

    deferred = {
        node.module or ""
        for node in ast.walk(_function_tree("build_orchestrator"))
        if isinstance(node, ast.ImportFrom)
    }
    assert required in deferred, f"装配缺少惰性导入：{required}"


# --------------------------------------------------------------------------- #
# 任务体的协作者
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "name",
    ["run_generation", "poll_generation_status", "recovery_scan", "worker_heartbeat"],
)
def test_task_bodies_depend_on_nothing_but_the_orchestration_layer(name: str) -> None:
    """任务体越界写业务/账务/连接，就等于把状态机的第二个写者请进了队列层。

    文档串里引用契约文件名（如 `04-ai-provider.md`）不算依赖，因此只扫描可执行语句。
    """

    function = _function_tree(name)
    statements = list(function.body)
    if isinstance(statements[0], ast.Expr) and isinstance(statements[0].value, ast.Constant):
        statements = statements[1:]

    lowered = ast.unparse(ast.Module(body=statements, type_ignores=())).lower()
    for token in FORBIDDEN_BODY_TOKENS:
        assert token not in lowered, f"{name} 里出现了越界依赖：{token}"
