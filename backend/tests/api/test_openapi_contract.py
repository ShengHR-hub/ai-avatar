"""ENG-02 第 4 步：OpenAPI 导出与阶段性规范化比较。

比较口径（技术负责人 2026-09-26 裁决 ①③）：

```text
运行时路径集合 ⊆ shared/api-contract/openapi.json 路径集合
```

- `shared/api-contract/openapi.json` 是 LEAD-03 冻结基线，**不得被运行时结果覆盖**；
- 运行时导出只写入 pytest 临时目录（`tmp_path`），测试结束由 pytest 清理，不作为契约文件提交；
- 采用**内容级规范化对齐**：允许 FastAPI 内联响应、允许使用不同的组件名称，
  **不要求**运行时复刻基线的 `components.responses` 命名、组件拓扑或 `$ref` 位置；
- 但路径、方法、`operationId`、`security`、状态码与**最终解引用后的响应内容**必须一致。

规范化步骤（只做等价变形，不丢弃契约语义）：

1. 递归解引用 `$ref`；
2. 丢弃文档性注解键：`title`（Pydantic 自动生成）、Schema 级 `description`、
   operation 级 `summary` / `description`（FastAPI 强制生成或取自端点 docstring）、
   `$defs`（Pydantic 的定义容器，引用内联后无语义）——均不属于契约语义；
3. 折叠可空联合：`anyOf: [X, {"type": "null"}]` → `{**X, "type": [X.type, "null"]}`。
   基线写联合 `type` 数组，运行时必须写 `anyOf`（FastAPI 0.115 的 OpenAPI 模型把
   `Schema.type` 校验为字符串，联合数组会抛 `pydantic_core.ValidationError`，已复现），
   两者语义等价；
4. 展平 `allOf`：把全部分支为对象 Schema 的 `allOf` 合并为单一对象 Schema
   （基线用 `allOf` 表达「基类 + 收窄」，Pydantic 输出扁平结构，二者语义等价）；
5. 折叠整数化的数值约束：`minimum: 0.0` → `minimum: 0`（仅五个被 FastAPI 声明为
   `Optional[float]` 的键，见 `_fold_integral_floats` 的说明）；
6. 排序对象键后比较。

响应级 `description` 在规范化**之前**单独严格比较（基线：`Health response` / `Stable API error`）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.main import create_app

BASELINE_PATH = (
    Path(__file__).resolve().parents[3] / "shared" / "api-contract" / "openapi.json"
)

#: ENG-02 已实现、且必须与基线一致的两条健康路径
IMPLEMENTED_HEALTH_PATHS = ("/api/v1/health/live", "/api/v1/health/ready")

#: ENG-03 第 3 步已实现、且必须与基线一致的认证与用户路径
IMPLEMENTED_AUTH_PATHS = (
    "/api/v1/auth/invites/redeem",
    "/api/v1/auth/email/request",
    "/api/v1/auth/email/verify",
    "/api/v1/auth/session",
    "/api/v1/auth/logout",
    "/api/v1/me",
)

#: ENG-05 第 4 步已实现、且必须与基线一致的媒体资源路径
IMPLEMENTED_ASSET_PATHS = (
    "/api/v1/assets/uploads",
    "/api/v1/assets/{asset_id}",
    "/api/v1/assets/{asset_id}/complete",
    "/api/v1/assets/{asset_id}/download-url",
)

#: 「参考图片找灵感」已实现、且必须与基线一致的图片理解路径
IMPLEMENTED_ANALYSIS_PATHS = ("/api/v1/analyses",)

#: ENG-04 已实现、且必须与基线一致的点数读取路径（只读，写入仍走账务 Service）
IMPLEMENTED_POINTS_PATHS = ("/api/v1/me/points", "/api/v1/me/points/ledger")

#: ENG-06 第 5 步已实现、且必须与基线一致的生成任务与内部回调路径
IMPLEMENTED_GENERATION_PATHS = (
    "/api/v1/generations",
    "/api/v1/generations/{generation_id}",
    "/api/v1/generations/{generation_id}/outputs",
    "/api/v1/generations/{generation_id}/retry",
    "/api/v1/me/generations",
    "/api/v1/internal/providers/{provider_name}/webhook",
)

#: ENG-09 已实现、且必须与基线一致的分享与反馈路径
#: （`GET /shares/{slug}` 是匿名读取：`security` 为 `[]`，与基线一致）
IMPLEMENTED_ENGAGEMENT_PATHS = (
    "/api/v1/shares",
    "/api/v1/shares/{slug}",
    "/api/v1/feedback",
)

#: 全部已实现路径（必须与基线逐字段一致）
IMPLEMENTED_PATHS = (
    IMPLEMENTED_HEALTH_PATHS
    + IMPLEMENTED_AUTH_PATHS
    + IMPLEMENTED_ASSET_PATHS
    + IMPLEMENTED_ANALYSIS_PATHS
    + IMPLEMENTED_POINTS_PATHS
    + IMPLEMENTED_GENERATION_PATHS
    + IMPLEMENTED_ENGAGEMENT_PATHS
)

#: 基线路径总数（用于证明基线未被运行时导出覆盖）
BASELINE_PATH_COUNT = 29

#: operation 级严格比较字段
_OPERATION_KEYS = ("operationId", "tags", "security", "parameters", "x-idempotent")

#: 被 FastAPI 0.115 的 OpenAPI 模型声明为 `Optional[float]` 的数值约束键
#: （`fastapi.openapi.models.Schema`：`multipleOf`/`maximum`/`exclusiveMaximum`/
#: `minimum`/`exclusiveMinimum`；其余 `maxLength`/`minItems` 等仍是 `Optional[int]`）。
_FLOAT_TYPED_NUMERIC_KEYS = frozenset(
    {"multipleOf", "maximum", "exclusiveMaximum", "minimum", "exclusiveMinimum"}
)

#: operation 级允许存在的文档性注解字段（FastAPI 强制生成）
_DOC_ONLY_OPERATION_KEYS = frozenset({"summary", "description"})

#: 「名字 → 子 Schema」映射容器：遍历 schema 时必须跳过它们的**键**
#: （键是属性名/组件名，不是 JSON Schema 关键字）
_NAME_TO_SUBSCHEMA_KEYS = frozenset({"properties", "$defs", "definitions", "patternProperties"})

#: Schema 级文档性注解键。
#: `$defs` 是 Pydantic 的定义容器：引用全部内联后它只是打包产物、不承载契约语义
#: （技术负责人 2026-09-26 追认）。
_DOC_ONLY_SCHEMA_KEYS = frozenset({"title", "description", "$defs"})


# --------------------------------------------------------------------------- #
# 导出
# --------------------------------------------------------------------------- #
def load_baseline() -> dict[str, Any]:
    """读取 LEAD-03 冻结基线（只读）。"""

    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


def runtime_openapi() -> dict[str, Any]:
    """导出运行时 OpenAPI（不落盘、不覆盖基线）。"""

    return create_app().openapi()


def export_runtime_openapi(target: Path) -> Path:
    """把运行时 OpenAPI 导出到给定临时路径。"""

    target.write_text(
        json.dumps(runtime_openapi(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return target


# --------------------------------------------------------------------------- #
# 规范化
# --------------------------------------------------------------------------- #
def dereference(document: dict[str, Any], node: Any) -> Any:
    """递归解析 `$ref`。"""

    if isinstance(node, dict):
        if set(node) == {"$ref"}:
            pointer = node["$ref"]
            assert pointer.startswith("#/"), pointer
            target: Any = document
            for part in pointer.lstrip("#/").split("/"):
                target = target[part]
            return dereference(document, target)
        return {key: dereference(document, value) for key, value in node.items()}
    if isinstance(node, list):
        return [dereference(document, item) for item in node]
    return node


def _strip_doc_keys(node: Any) -> Any:
    if isinstance(node, dict):
        return {
            key: _strip_doc_keys(value)
            for key, value in node.items()
            if key not in _DOC_ONLY_SCHEMA_KEYS
        }
    if isinstance(node, list):
        return [_strip_doc_keys(item) for item in node]
    return node


def _merge_object_branch(target: dict[str, Any], branch: dict[str, Any]) -> dict[str, Any]:
    merged = dict(target)
    for key, value in branch.items():
        if key == "properties":
            merged["properties"] = {**merged.get("properties", {}), **value}
        elif key == "required":
            merged["required"] = sorted(set(merged.get("required", [])) | set(value))
        else:
            merged[key] = value
    return merged


def _flatten_all_of(node: Any) -> Any:
    if isinstance(node, dict):
        node = {key: _flatten_all_of(value) for key, value in node.items()}
        branches = node.get("allOf")
        if isinstance(branches, list) and branches:
            merged = {key: value for key, value in node.items() if key != "allOf"}
            for branch in branches:
                if not isinstance(branch, dict):
                    return node
                merged = _merge_object_branch(merged, branch)
            return _flatten_all_of(merged)
        return node
    if isinstance(node, list):
        return [_flatten_all_of(item) for item in node]
    return node


def _fold_null_unions(node: Any) -> Any:
    """把 `anyOf: [X, {"type": "null"}]` 折叠为 `{**X, "type": [X.type, "null"]}`。

    基线的可空字段写作联合 `type` 数组，而运行时必须用 `anyOf` 表达（FastAPI 0.115 的
    OpenAPI 模型把 `Schema.type` 校验为字符串，联合数组会抛
    `pydantic_core.ValidationError`）。两者语义等价，属规范化步骤。
    """

    if isinstance(node, dict):
        node = {key: _fold_null_unions(value) for key, value in node.items()}
        branches = node.get("anyOf")
        if isinstance(branches, list) and len(branches) == 2:
            null_branch = next(
                (
                    branch
                    for branch in branches
                    if isinstance(branch, dict) and branch.get("type") == "null"
                ),
                None,
            )
            other = next(
                (branch for branch in branches if branch is not null_branch), None
            )
            if (
                null_branch is not None
                and isinstance(other, dict)
                and isinstance(other.get("type"), str)
            ):
                merged = {key: value for key, value in node.items() if key != "anyOf"}
                merged.update(other)
                merged["type"] = [other["type"], "null"]
                return _fold_null_unions(merged)
        return node
    if isinstance(node, list):
        return [_fold_null_unions(item) for item in node]
    return node


def _fold_integral_floats(node: Any) -> Any:
    """把整数值的浮点数值约束折叠回整数（`minimum: 0.0` → `minimum: 0`）。

    根因：FastAPI 0.115 的 `openapi.models.Schema` 把 `multipleOf`/`maximum`/
    `exclusiveMaximum`/`minimum`/`exclusiveMinimum` 声明为 `Optional[float]`，
    Pydantic 校验时把整数 `0` 提升为 `0.0`，导出后是浮点字面量；而 LEAD-03 冻结
    基线按 OpenAPI 3.1 习惯写成整数 `0`/`1`。

    JSON 中 `0` 与 `0.0` 是同一个数（RFC 8259 数字无整/浮点之分），约束语义完全相同，
    因此这是**等价变形**而非放宽比较：

    - 只作用于上表五个键，其它键（含 `enum` 里的浮点值）一律保持原样；
    - 只折叠 `is_integer()` 的浮点，`minimum: 0.5` 仍是 `0.5`，非整数差异照样暴露；
    - 约束缺失、约束取值不同（`0` vs `1`）仍会判为差异。

    运行时导出无法在不改 `app/main.py`（`custom_openapi`）的前提下输出整数，
    故在规范化层对**两侧对称**应用本折叠。
    """

    if isinstance(node, dict):
        return {
            key: (
                int(value)
                if key in _FLOAT_TYPED_NUMERIC_KEYS
                and isinstance(value, float)
                and value.is_integer()
                else _fold_integral_floats(value)
            )
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [_fold_integral_floats(item) for item in node]
    return node


def canonical_schema(document: dict[str, Any], node: Any) -> Any:
    """把 Schema 规范化为可比较形式。

    解引用 `$ref` → 去文档注解（`title`/`description`/`$defs`）→ 折叠可空联合 →
    展平 `allOf` → 整数化数值约束。
    """

    return _fold_integral_floats(
        _flatten_all_of(_fold_null_unions(_strip_doc_keys(dereference(document, node))))
    )


def canonical_content(document: dict[str, Any], response: Any) -> dict[str, Any]:
    """规范化响应体：媒体类型 → Schema。"""

    resolved = dereference(document, response)
    content = resolved.get("content", {}) if isinstance(resolved, dict) else {}
    return {
        media_type: {"schema": canonical_schema(document, spec.get("schema", {}))}
        for media_type, spec in content.items()
    }


def normalize(node: Any) -> str:
    """排序对象键 + 统一格式（不丢弃任何字段）。"""

    return json.dumps(node, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


# --------------------------------------------------------------------------- #
# 比较
# --------------------------------------------------------------------------- #
def operation_problems(
    path: str,
    method: str,
    runtime_op: dict[str, Any],
    baseline_op: dict[str, Any],
    runtime_doc: dict[str, Any],
    baseline_doc: dict[str, Any],
) -> list[str]:
    """返回单个 operation 的规范化差异清单（空列表表示一致）。"""

    problems: list[str] = []

    for key in _OPERATION_KEYS:
        runtime_value = runtime_op.get(key)
        baseline_value = baseline_op.get(key)
        if key == "parameters":
            runtime_value = (
                _strip_doc_keys(dereference(runtime_doc, runtime_value))
                if runtime_value
                else runtime_value
            )
            baseline_value = (
                _strip_doc_keys(dereference(baseline_doc, baseline_value))
                if baseline_value
                else baseline_value
            )
        if runtime_value != baseline_value:
            problems.append(
                f"{path} [{method}] {key}: 运行时={runtime_value!r} 基线={baseline_value!r}"
            )

    extra_keys = sorted(set(runtime_op) - set(baseline_op) - _DOC_ONLY_OPERATION_KEYS)
    if extra_keys:
        problems.append(f"{path} [{method}] 运行时多出契约字段: {extra_keys}")
    missing_keys = sorted(set(baseline_op) - set(runtime_op))
    if missing_keys:
        problems.append(f"{path} [{method}] 运行时缺少字段: {missing_keys}")

    runtime_body = (
        dereference(runtime_doc, runtime_op["requestBody"])
        if runtime_op.get("requestBody")
        else None
    )
    baseline_body = (
        dereference(baseline_doc, baseline_op["requestBody"])
        if baseline_op.get("requestBody")
        else None
    )
    if (runtime_body or {}).get("required") != (baseline_body or {}).get("required"):
        problems.append(
            f"{path} [{method}] requestBody required: "
            f"运行时={(runtime_body or {}).get('required')!r} "
            f"基线={(baseline_body or {}).get('required')!r}"
        )
    if normalize(canonical_content(runtime_doc, runtime_body or {})) != normalize(
        canonical_content(baseline_doc, baseline_body or {})
    ):
        problems.append(f"{path} [{method}] requestBody 与基线不一致")

    runtime_codes = set(runtime_op.get("responses", {}))
    baseline_codes = set(baseline_op.get("responses", {}))
    if runtime_codes != baseline_codes:
        problems.append(
            f"{path} [{method}] 响应状态码: 运行时={sorted(runtime_codes)} "
            f"基线={sorted(baseline_codes)}"
        )

    for code in sorted(baseline_codes & runtime_codes):
        runtime_response = dereference(runtime_doc, runtime_op["responses"][code])
        baseline_response = dereference(baseline_doc, baseline_op["responses"][code])

        runtime_description = runtime_response.get("description")
        baseline_description = baseline_response.get("description")
        if runtime_description != baseline_description:
            problems.append(
                f"{path} [{method}] {code} 响应 description: "
                f"运行时={runtime_description!r} 基线={baseline_description!r}"
            )

        runtime_content = canonical_content(runtime_doc, runtime_response)
        baseline_content = canonical_content(baseline_doc, baseline_response)
        if normalize(runtime_content) != normalize(baseline_content):
            problems.append(
                f"{path} [{method}] {code} 解引用响应内容与基线不一致\n"
                f"      运行时: {normalize(runtime_content)}\n"
                f"      基线:   {normalize(baseline_content)}"
            )

        runtime_headers = runtime_response.get("headers")
        baseline_headers = baseline_response.get("headers")
        if (runtime_headers is None) != (baseline_headers is None):
            problems.append(
                f"{path} [{method}] {code} 响应头: 运行时={runtime_headers is not None} "
                f"基线={baseline_headers is not None}"
            )

    return problems


# --------------------------------------------------------------------------- #
# 规范化工具自检
# --------------------------------------------------------------------------- #
def test_normalize_sorts_keys_and_preserves_values() -> None:
    assert normalize({"b": 1, "a": {"d": 2, "c": [1, 2]}}) == normalize(
        {"a": {"c": [1, 2], "d": 2}, "b": 1}
    )
    assert normalize({"a": 1}) != normalize({"a": 2})
    assert normalize({"a": 1}) != normalize({"a": 1, "b": 2})


def test_dereference_resolves_nested_refs() -> None:
    document = {
        "components": {
            "schemas": {
                "Inner": {"type": "object"},
                "Outer": {"allOf": [{"$ref": "#/components/schemas/Inner"}]},
            }
        }
    }

    assert dereference(document, {"$ref": "#/components/schemas/Outer"}) == {
        "allOf": [{"type": "object"}]
    }


def test_strip_doc_keys_removes_only_annotations() -> None:
    node = {
        "title": "X",
        "description": "doc",
        "$defs": {"Inner": {"type": "string"}},
        "type": "object",
        "properties": {"a": {"title": "A", "type": "string"}},
    }

    assert _strip_doc_keys(node) == {
        "type": "object",
        "properties": {"a": {"type": "string"}},
    }


def test_flatten_all_of_merges_object_branches() -> None:
    node = {
        "allOf": [
            {
                "type": "object",
                "required": ["data", "request_id"],
                "properties": {"data": {}, "request_id": {"type": "string"}},
            },
            {
                "type": "object",
                "properties": {"data": {"type": "object", "properties": {"status": {"type": "string"}}}},
            },
        ]
    }

    assert _flatten_all_of(node) == {
        "type": "object",
        "required": ["data", "request_id"],
        "properties": {
            "data": {"type": "object", "properties": {"status": {"type": "string"}}},
            "request_id": {"type": "string"},
        },
    }


def test_fold_null_unions_matches_baseline_type_array() -> None:
    node = {"anyOf": [{"type": "string", "maxLength": 80}, {"type": "null"}]}

    assert _fold_null_unions(node) == {"type": ["string", "null"], "maxLength": 80}


def test_fold_null_unions_keeps_non_nullable_unions() -> None:
    node = {"anyOf": [{"type": "string"}, {"type": "integer"}]}

    assert _fold_null_unions(node) == node


def test_fold_integral_floats_normalizes_fastapi_numeric_bounds() -> None:
    """FastAPI 提升为浮点的整数约束折叠回整数（含嵌套层级）。"""

    assert _fold_integral_floats({"type": "integer", "minimum": 0.0}) == {
        "type": "integer",
        "minimum": 0,
    }
    assert _fold_integral_floats({"properties": {"size_bytes": {"minimum": 1.0}}}) == {
        "properties": {"size_bytes": {"minimum": 1}},
    }


def test_fold_integral_floats_keeps_real_differences() -> None:
    """非整数值、非约束键、约束缺失都必须原样保留，不得被折叠掩盖。"""

    assert _fold_integral_floats({"maximum": 0.5}) == {"maximum": 0.5}
    assert _fold_integral_floats({"maxLength": 255.0}) == {"maxLength": 255.0}
    assert _fold_integral_floats({"enum": [1.0, 2.0]}) == {"enum": [1.0, 2.0]}
    assert _fold_integral_floats({"type": "integer"}) == {"type": "integer"}
    assert normalize(_fold_integral_floats({"minimum": 0})) != normalize(
        _fold_integral_floats({"minimum": 1})
    )
    assert normalize(_fold_integral_floats({"minimum": 0.0})) != normalize(
        _fold_integral_floats({})
    )


def _body_schema(document: dict[str, Any], operation: dict[str, Any], code: str | None) -> Any:
    """取请求体（`code is None`）或指定状态码响应体的原始 `application/json` Schema 节点。"""

    node = (
        dereference(document, operation["requestBody"])
        if code is None
        else dereference(document, operation["responses"][code])
    )
    return node["content"]["application/json"]["schema"]


@pytest.mark.parametrize(
    ("path", "method", "code", "envelope_data_key", "expected_minimum"),
    [
        ("/api/v1/assets/uploads", "post", None, None, 1),
        ("/api/v1/assets/{asset_id}", "get", "200", "data", 0),
        ("/api/v1/assets/{asset_id}/complete", "post", "200", "data", 0),
    ],
)
def test_numeric_constraints_survive_normalization(
    path: str,
    method: str,
    code: str | None,
    envelope_data_key: str | None,
    expected_minimum: int,
) -> None:
    """折叠只统一序列化形式：`minimum` 必须两侧都真实存在且取值一致，不能被折叠掉。

    按位置（请求体 / 响应体）取 Schema，不依赖组件名——运行时组件名由 Pydantic
    模型类名决定（`UploadIntentRequest`），基线用的是契约名（`UploadIntent`）。
    """

    for document in (runtime_openapi(), load_baseline()):
        operation = document["paths"][path][method]
        schema = canonical_schema(document, _body_schema(document, operation, code))
        if envelope_data_key is not None:
            schema = schema["properties"][envelope_data_key]

        size_bytes = schema["properties"]["size_bytes"]
        assert size_bytes["type"] == "integer"
        assert size_bytes["minimum"] == expected_minimum
        assert isinstance(size_bytes["minimum"], int)


def test_canonical_schema_resolves_ref_strips_docs_and_flattens() -> None:
    document = {
        "components": {
            "schemas": {
                "Base": {
                    "title": "Base",
                    "description": "doc",
                    "type": "object",
                    "required": ["data"],
                    "properties": {"data": {}},
                },
                "Child": {
                    "allOf": [
                        {"$ref": "#/components/schemas/Base"},
                        {"type": "object", "properties": {"data": {"type": "object"}}},
                    ]
                },
            }
        }
    }

    assert canonical_schema(document, {"$ref": "#/components/schemas/Child"}) == {
        "type": "object",
        "required": ["data"],
        "properties": {"data": {"type": "object"}},
    }


# --------------------------------------------------------------------------- #
# 导出行为
# --------------------------------------------------------------------------- #
def test_runtime_openapi_can_be_exported_to_temp_file(tmp_path: Path) -> None:
    """运行时导出只落临时目录，不写基线。"""

    target = export_runtime_openapi(tmp_path / "runtime-openapi.json")

    assert target.exists()
    assert target.parent == tmp_path
    assert BASELINE_PATH.parent != tmp_path
    assert json.loads(target.read_text(encoding="utf-8"))["openapi"] == "3.1.0"


def test_runtime_openapi_generates_without_errors() -> None:
    """`create_app().openapi()` 必须能成功生成（ENG-03 第 3 步曾因联合 type 失败）。"""

    document = runtime_openapi()

    assert document["openapi"] == "3.1.0"
    assert len(document["paths"]) == len(IMPLEMENTED_PATHS)


def test_no_schema_declares_a_union_type_array() -> None:
    """任何 `type` 都必须是字符串。

    FastAPI 0.115 的 OpenAPI 模型把 `Schema.type` 校验为字符串，联合数组（`["string","null"]`）
    会在生成阶段抛 `pydantic_core.ValidationError`（已在系统解释器上复现）。
    因此运行时可空字段一律用 `anyOf` 表达。
    """

    offenders: list[str] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            if "type" in node and not isinstance(node["type"], str):
                offenders.append(f"{path}.type={node['type']!r}")
            for key, value in node.items():
                # `properties` / `$defs` 是「名字 → 子 Schema」的映射：
                # 其中的键是**属性名**而不是 Schema 关键字。FastAPI 自动生成的
                # `ValidationError` 恰好有名为 `type` 的属性，键写成 `properties.type`
                # 时必须跳过，否则会把一个普通属性误判成联合 `type` 数组。
                if key in _NAME_TO_SUBSCHEMA_KEYS:
                    continue
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, f"{path}[{index}]")

    walk(runtime_openapi(), "$")
    assert offenders == [], f"存在联合 type 数组: {offenders}"


def test_nullable_fields_use_anyof_with_null_branch() -> None:
    """可空语义必须保留：`anyOf` 中带 `{"type": "null"}` 分支。"""

    user_schema = runtime_openapi()["components"]["schemas"]["User"]

    for field in ("display_name", "avatar_asset_id", "primary_email"):
        branches = user_schema["properties"][field]["anyOf"]
        assert {"type": "null"} in branches, field
        assert any(branch.get("type") == "string" for branch in branches), field


def test_baseline_is_not_overwritten_by_runtime_export() -> None:

    baseline = load_baseline()

    assert len(baseline["paths"]) == BASELINE_PATH_COUNT
    # 冻结基线仍多于运行时：未挂载的只剩 Admin/内部路径（不是前端业务 API）
    assert len(runtime_openapi()["paths"]) == len(IMPLEMENTED_PATHS) < BASELINE_PATH_COUNT


def test_engagement_paths_are_declared_as_implemented() -> None:
    """ENG-09 的三条分享/反馈路径必须在比较清单内且已挂载。

    这三条曾被登记为「尚未挂载」，随实现一并移出未挂载集合；这里把它钉住，
    避免清单被无声删掉后逐字段比较静默跳过。
    """

    runtime_paths = set(runtime_openapi()["paths"])

    assert set(IMPLEMENTED_ENGAGEMENT_PATHS) == {
        "/api/v1/shares",
        "/api/v1/shares/{slug}",
        "/api/v1/feedback",
    }
    assert set(IMPLEMENTED_ENGAGEMENT_PATHS) <= set(IMPLEMENTED_PATHS)
    assert set(IMPLEMENTED_ENGAGEMENT_PATHS) <= runtime_paths


def test_runtime_no_longer_exposes_unmounted_engagement_paths() -> None:
    """基线里剩下未挂载的只能是 Admin/内部路径。"""

    baseline_paths = set(load_baseline()["paths"])
    runtime_paths = set(runtime_openapi()["paths"])
    unmounted = baseline_paths - runtime_paths

    assert not [path for path in unmounted if "/admin/" not in path], sorted(unmounted)


# --------------------------------------------------------------------------- #
# 阶段性路径集合比较
# --------------------------------------------------------------------------- #
def test_runtime_paths_are_subset_of_frozen_baseline() -> None:
    runtime_paths = set(runtime_openapi()["paths"])
    baseline_paths = set(load_baseline()["paths"])

    assert runtime_paths <= baseline_paths, sorted(runtime_paths - baseline_paths)


def test_runtime_exposes_only_implemented_paths() -> None:
    assert sorted(runtime_openapi()["paths"]) == sorted(IMPLEMENTED_PATHS)


def test_runtime_points_paths_are_declared_as_implemented() -> None:
    """ENG-04 的点数读取口已实现，必须出现在比较清单里。

    曾经的反向断言（「本卡不得实现」）随实现一并作废：路径是否越界由
    `test_runtime_exposes_only_implemented_paths` 与逐字段比较负责，这里只钉住
    「点数两个读取口确实在已实现清单内」，避免清单被无声删掉后比较静默跳过。
    """

    runtime_paths = set(runtime_openapi()["paths"])

    assert set(IMPLEMENTED_POINTS_PATHS) == {
        "/api/v1/me/points",
        "/api/v1/me/points/ledger",
    }
    assert set(IMPLEMENTED_POINTS_PATHS) <= set(IMPLEMENTED_PATHS)
    assert set(IMPLEMENTED_POINTS_PATHS) <= runtime_paths


def test_runtime_does_not_expose_auth_me_alias() -> None:
    """契约只冻结 `/auth/session`，不得创建 `/auth/me` 别名。"""

    assert "/api/v1/auth/me" not in set(runtime_openapi()["paths"])


# --------------------------------------------------------------------------- #
# 两条已实现路径的规范化内容比较
# --------------------------------------------------------------------------- #
def test_implemented_paths_match_baseline_exactly() -> None:
    """全部已实现路径的 operation、状态码与解引用响应内容必须与基线一致。"""

    runtime_doc = runtime_openapi()
    baseline_doc = load_baseline()

    problems: list[str] = []

    for path in IMPLEMENTED_PATHS:
        runtime_item = runtime_doc["paths"].get(path, {})
        baseline_item = baseline_doc["paths"][path]

        if sorted(runtime_item) != sorted(baseline_item):
            problems.append(
                f"{path} 方法集合: 运行时={sorted(runtime_item)} 基线={sorted(baseline_item)}"
            )

        for method in sorted(set(baseline_item) & set(runtime_item)):
            problems.extend(
                operation_problems(
                    path,
                    method,
                    runtime_item[method],
                    baseline_item[method],
                    runtime_doc,
                    baseline_doc,
                )
            )

    assert problems == [], "OpenAPI 与冻结基线存在差异:\n" + "\n".join(
        f"  - {problem}" for problem in problems
    )


@pytest.mark.parametrize("path", IMPLEMENTED_PATHS)
def test_path_declares_operation_id_and_security(path: str) -> None:
    """显式断言两个最易漂移的字段，便于定位差异来源。"""

    runtime_doc = runtime_openapi()
    baseline_doc = load_baseline()

    for method in sorted(baseline_doc["paths"][path]):
        runtime_op = runtime_doc["paths"][path][method]
        baseline_op = baseline_doc["paths"][path][method]
        assert runtime_op.get("operationId") == baseline_op["operationId"]
        assert runtime_op.get("security") == baseline_op["security"]


def test_error_code_is_plain_string_without_enum() -> None:
    """裁决 ④：OpenAPI 中 `Error.code` 必须是 `type: string`，不得生成 enum。"""

    runtime_doc = runtime_openapi()
    error_detail = runtime_doc["components"]["schemas"]["ErrorDetail"]

    code_schema = error_detail["properties"]["code"]
    assert code_schema["type"] == "string"
    assert "enum" not in code_schema


def test_response_schema_has_no_enum_and_matches_baseline_error_shape() -> None:
    """503 的解引用响应内容必须与基线 `ErrorResponse` 内容一致。"""

    runtime_doc = runtime_openapi()
    baseline_doc = load_baseline()

    runtime_503 = runtime_doc["paths"]["/api/v1/health/ready"]["get"]["responses"]["503"]
    baseline_503 = baseline_doc["paths"]["/api/v1/health/ready"]["get"]["responses"]["503"]

    assert normalize(canonical_content(runtime_doc, runtime_503)) == normalize(
        canonical_content(baseline_doc, baseline_503)
    )
