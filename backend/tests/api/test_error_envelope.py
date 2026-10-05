"""统一错误 envelope 与异常映射测试（ENG-02 第 2 步）。

对应 `03-api-contract.md` 第 2.3 节、第 6 节、第 7 节。
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.errors.codes import ErrorCode
from app.errors.exceptions import ApiError
from app.middleware.access_log import AccessLogMiddleware
from app.middleware.request_id import REQUEST_ID_HEADER, RequestIdMiddleware


def test_api_error_uses_contract_envelope(client: TestClient) -> None:
    response = client.get("/probe/api-error")

    assert response.status_code == 409
    payload = response.json()
    assert set(payload) == {"error", "request_id"}
    assert set(payload["error"]) == {"code", "message", "details"}
    assert payload["error"]["code"] == "INSUFFICIENT_POINTS"
    assert payload["error"]["message"]
    assert payload["error"]["details"] == {"available_points": 0}
    assert response.headers[REQUEST_ID_HEADER] == payload["request_id"]


def test_api_error_can_carry_protocol_headers(client: TestClient) -> None:
    response = client.get("/probe/api-error-with-headers")

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "30"
    assert response.json()["error"]["code"] == "RATE_LIMITED"


def test_request_validation_error_maps_to_validation_error(client: TestClient) -> None:
    response = client.post("/probe/validated", json={"invite_code": "too-short"})

    assert response.status_code == 422
    payload = response.json()
    assert payload["error"]["code"] == "VALIDATION_ERROR"
    fields = payload["error"]["details"]["fields"]
    assert fields and fields[0]["loc"] == ["body", "invite_code"]
    assert fields[0]["type"]


def test_request_validation_error_does_not_echo_input_value(
    client: TestClient, sensitive_input_sentinel: str
) -> None:
    response = client.post(
        "/probe/validated", json={"invite_code": sensitive_input_sentinel}
    )

    assert response.status_code == 422
    # 原始输入值（可能含邀请码明文、邮箱等）不得回显到响应
    assert sensitive_input_sentinel not in response.text
    assert "input" not in response.text


def test_http_exception_with_code_uses_code_status(client: TestClient) -> None:
    """`detail` 携带契约错误码时，HTTP 状态由错误码决定，而不是异常自身的状态。"""

    response = client.get("/probe/http-exception-with-code")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "GENERATION_NOT_FOUND"


def test_http_exception_with_generic_status_maps_to_generic_code(
    client: TestClient,
) -> None:
    response = client.get("/probe/http-exception-401")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"


def test_http_exception_without_contract_code_keeps_status_without_fake_code(
    client: TestClient,
) -> None:
    """契约未提供通用码的状态（409）：保留原始状态，不伪造业务错误码。"""

    response = client.get("/probe/http-exception-plain")

    assert response.status_code == 409
    payload = response.json()
    assert "error" not in payload
    assert payload["detail"] == "plain framework detail"
    assert response.headers[REQUEST_ID_HEADER] == payload["request_id"]


def test_unhandled_exception_maps_to_internal_error_without_leaking(
    client: TestClient, sensitive_input_sentinel: str
) -> None:
    response = client.get("/probe/boom")

    assert response.status_code == 500
    payload = response.json()
    assert payload["error"]["code"] == "INTERNAL_ERROR"
    assert payload["error"]["details"] == {}
    # 异常文本、堆栈与内部细节不得出现在响应中
    assert sensitive_input_sentinel not in response.text
    assert "RuntimeError" not in response.text
    assert "Traceback" not in response.text


def test_unknown_path_keeps_404_and_request_id(client: TestClient) -> None:
    response = client.get("/probe/does-not-exist")

    assert response.status_code == 404
    assert REQUEST_ID_HEADER in response.headers
    assert response.json()["request_id"] == response.headers[REQUEST_ID_HEADER]


def test_production_app_registers_middleware_and_handlers() -> None:
    """真实装配检查：生产应用已注册中间件与异常处理器，且只暴露契约内已实现的路径。"""

    from app.main import app as production_app

    middleware = {middleware.cls for middleware in production_app.user_middleware}
    assert RequestIdMiddleware in middleware
    assert AccessLogMiddleware in middleware

    handlers = production_app.exception_handlers
    assert ApiError in handlers
    assert Exception in handlers
    assert any(
        isinstance(key, type) and issubclass(key, Exception) and key.__name__ == "HTTPException"
        for key in handlers
    )

    # 只允许契约内已实现路径：2 条健康检查 + ENG-03 的 6 条认证/用户路径
    # + ENG-05 的 4 条媒体资源路径 + 1 条参考图分析路径
    # + ENG-06 的 5 条生成任务路径与 1 条内部回调路径
    # + ENG-04 的 2 条点数读取路径
    # + ENG-09 的 2 条分享路径（含匿名读取）与 1 条反馈路径
    assert sorted(production_app.openapi()["paths"]) == [
        "/api/v1/analyses",
        "/api/v1/assets/uploads",
        "/api/v1/assets/{asset_id}",
        "/api/v1/assets/{asset_id}/complete",
        "/api/v1/assets/{asset_id}/download-url",
        "/api/v1/auth/email/request",
        "/api/v1/auth/email/verify",
        "/api/v1/auth/invites/redeem",
        "/api/v1/auth/logout",
        "/api/v1/auth/session",
        "/api/v1/feedback",
        "/api/v1/generations",
        "/api/v1/generations/{generation_id}",
        "/api/v1/generations/{generation_id}/outputs",
        "/api/v1/generations/{generation_id}/retry",
        "/api/v1/health/live",
        "/api/v1/health/ready",
        "/api/v1/internal/providers/{provider_name}/webhook",
        "/api/v1/me",
        "/api/v1/me/generations",
        "/api/v1/me/points",
        "/api/v1/me/points/ledger",
        "/api/v1/shares",
        "/api/v1/shares/{slug}",
    ]


def test_error_envelope_shape_matches_frozen_baseline() -> None:
    """第 4 步按裁决 ①④ 对齐基线：`code` 为 string、`details` 必填、无 `additionalProperties: false`。"""

    from app.schemas.common import ErrorDetail, ErrorResponse

    error_schema = ErrorDetail.model_json_schema()
    assert error_schema["properties"]["code"]["type"] == "string"
    assert "enum" not in error_schema["properties"]["code"]
    assert set(error_schema["required"]) == {"code", "message", "details"}
    details_schema = {
        key: value
        for key, value in error_schema["properties"]["details"].items()
        if key != "title"  # Pydantic 文档性注解，比较时按规范化忽略
    }
    assert details_schema == {"additionalProperties": True, "type": "object"}
    assert error_schema.get("additionalProperties") is None

    response_schema = ErrorResponse.model_json_schema()
    assert set(response_schema["required"]) == {"error", "request_id"}
    assert response_schema.get("additionalProperties") is None


def test_error_detail_requires_details_field() -> None:
    import pytest
    from pydantic import ValidationError

    from app.schemas.common import ErrorDetail

    with pytest.raises(ValidationError):
        ErrorDetail.model_validate({"code": "INTERNAL_ERROR", "message": "x"})


def test_error_code_serializes_enum_to_plain_string() -> None:
    """裁决 ④：内部保留 `ErrorCode` 枚举，响应序列化输出 `ErrorCode.value`。"""

    from app.schemas.common import ErrorDetail

    detail = ErrorDetail(
        code=ErrorCode.INSUFFICIENT_POINTS, message="x", details={}
    )

    assert isinstance(detail.code, str)
    assert detail.model_dump(mode="json")["code"] == "INSUFFICIENT_POINTS"


def test_error_code_status_mapping_is_used_by_handler(
    client: TestClient, probe_app: FastAPI
) -> None:
    """除 `INTERNAL_ERROR` 外的全部契约错误码都能通过 `ApiError` 产出其固定 HTTP 状态。

    `INTERNAL_ERROR`（500）由未处理异常路径覆盖，见
    `test_internal_error_covered_by_unhandled_exception_path`。
    """

    from app.errors.codes import ERROR_HTTP_STATUS

    checked = 0
    for code, expected_status in ERROR_HTTP_STATUS.items():
        if code is ErrorCode.INTERNAL_ERROR:
            continue
        path = f"/probe/status/{code.value}"

        def _raise(_code: ErrorCode = code) -> None:
            raise ApiError(_code)

        probe_app.add_api_route(path, _raise, methods=["GET"])
        response = client.get(path)
        assert response.status_code == expected_status, code
        assert response.json()["error"]["code"] == code.value
        assert isinstance(response.json()["error"]["code"], str)
        checked += 1

    assert checked == len(ERROR_HTTP_STATUS) - 1


def test_internal_error_covered_by_unhandled_exception_path(
    client: TestClient,
) -> None:
    """`INTERNAL_ERROR` 的 500 映射由未处理异常路径验证。"""

    from app.errors.codes import ERROR_HTTP_STATUS

    response = client.get("/probe/boom")

    assert response.status_code == ERROR_HTTP_STATUS[ErrorCode.INTERNAL_ERROR] == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"


def test_validation_error_mapping_is_stable_across_payloads(
    client: TestClient,
) -> None:
    """不同校验失败都必须落到同一个稳定错误码与状态码。"""

    for payload in ({}, {"invite_code": 123}, {"invite_code": "x"}, {"unknown": "y"}):
        response = client.post("/probe/validated", json=payload)

        assert response.status_code == 422, payload
        assert response.json()["error"]["code"] == "VALIDATION_ERROR", payload
        assert response.json()["error"]["details"]["fields"], payload
