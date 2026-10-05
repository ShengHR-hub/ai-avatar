"""Celery 层边界测试的源码扫描助手（非测试模块，便于测试模块直接导入）。

先例：`tests/db/test_constraint_alignment.py` 用读取迁移脚本文本的方式做纠正性约束
对齐；本文件把同一手法推广到 `app/tasks/*.py`：

- `code_only()` 剥掉模块/类/函数文档串，只留可执行代码与注释，
  这样「文档里解释为什么不用 `autoretry`」不会被误判成实现；
- `imported_modules()` 用 AST 收集**全部** `import` / `from-import` 目标，
  包括函数体内的惰性导入（本层的执行上下文装配恰恰是惰性导入）。

只读文件，不导入被测模块，也不触网。
"""

from __future__ import annotations

import ast
from pathlib import Path

#: `backend/app/tasks/`
TASKS_DIR = Path(__file__).resolve().parents[2] / "app" / "tasks"

#: `docs/architecture/05-system-architecture.md` 第 3 节冻结的本层模块清单
FROZEN_MODULE_NAMES: tuple[str, ...] = (
    "__init__.py",
    "celery_app.py",
    "queue.py",
    "worker.py",
    "heartbeat.py",
)

_DOCUMENTED_NODE_TYPES = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def task_sources() -> dict[str, str]:
    """`{模块文件名: 源码文本}`；缺文件即失败，避免扫描面被静默缩小。"""

    found = {path.name: path.read_text(encoding="utf-8") for path in TASKS_DIR.glob("*.py")}
    missing = set(FROZEN_MODULE_NAMES) - set(found)
    assert not missing, f"app/tasks 缺少被冻结的模块：{sorted(missing)}"
    return found


def _documented_statements(tree: ast.AST) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, _DOCUMENTED_NODE_TYPES):
            continue
        body = node.body
        statement = body[0] if body else None
        if (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
        ):
            ranges.append((statement.lineno, statement.end_lineno or statement.lineno))
    return ranges


def code_only(source: str) -> str:
    """剥掉文档串所在行后的源码（保留注释与全部可执行代码）。"""

    ranges = _documented_statements(ast.parse(source))
    kept = [
        line
        for index, line in enumerate(source.splitlines(), start=1)
        if not any(start <= index <= end for start, end in ranges)
    ]
    return "\n".join(kept)


def imported_modules(source: str) -> tuple[str, ...]:
    """全部导入的**来源模块**（含函数体内的惰性导入），不含被导入的成员名。

    `from a.b import c` 记作 `a.b`；调用方按包前缀判断即可。
    """

    return tuple(_import_roots(ast.parse(source), top_level_only=False))


def module_level_imports(source: str) -> tuple[str, ...]:
    """只取模块顶层（非函数体内）的导入来源模块。"""

    return tuple(_import_roots(ast.parse(source), top_level_only=True))


def _import_roots(tree: ast.AST, *, top_level_only: bool) -> list[str]:
    nodes: list[ast.stmt] = (
        list(tree.body) if top_level_only else [n for n in ast.walk(tree)]  # type: ignore[assignment]
    )
    roots: list[str] = []
    for node in nodes:
        if isinstance(node, ast.Import):
            roots.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.append(node.module)
    return sorted(set(roots))


def attribute_assignment_targets(source: str) -> tuple[str, ...]:
    """所有 `obj.<attr> = ...` 形式的属性名（含增强赋值）。"""

    targets: list[str] = []
    for node in ast.walk(ast.parse(source)):
        nodes: list[ast.stmt] = []
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            if isinstance(node, ast.Assign):
                nodes = list(node.targets)
            else:
                nodes = [node.target]
        for statement in nodes:
            for attribute in ast.walk(statement):
                if isinstance(attribute, ast.Attribute):
                    targets.append(attribute.attr)
    return tuple(sorted(set(targets)))
