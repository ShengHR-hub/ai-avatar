"""请求 ID 测试（ENG-02 第 2 步）。

对应 `03-api-contract.md` 第 2.1 节。
"""

from __future__ import annotations

import json
import re

from fastapi.testclient import TestClient

from app.middleware.request_id import (
    REQUEST_ID_HEADER,
    REQUEST_ID_MAX_LENGTH,
    is_valid_request_id,
    resolve_request_id,
)

GENERATED_REQUEST_ID_PATTERN = re.compile(r"^req_[0-9a-f]{32}$")


def test_generates_request_id_when_header_absent(client: TestClient) -> None:
    response = client.get("/probe/echo-request-id")

    assert response.status_code == 200
    request_id = response.json()["request_id"]
    assert GENERATED_REQUEST_ID_PATTERN.match(request_id)
    assert response.headers[REQUEST_ID_HEADER] == request_id


def test_keeps_valid_client_request_id(client: TestClient) -> None:
    provided = "client-supplied-request-id-001"
    response = client.get(
        "/probe/echo-request-id", headers={REQUEST_ID_HEADER: provided}
    )

    assert response.json()["request_id"] == provided
    assert response.headers[REQUEST_ID_HEADER] == provided


def test_replaces_non_ascii_request_id(
    probe_app, call_raw
) -> None:
    """非 ASCII 请求头无法经 httpx 发送，改在 ASGI 层构造原始字节头。"""

    provided = "café-request-id-01"
    status, headers, body = call_raw(
        probe_app,
        "/probe/echo-request-id",
        headers=[(b"x-request-id", provided.encode("latin-1"))],
    )

    assert status == 200
    request_id = json.loads(body.decode("utf-8"))["request_id"]
    assert request_id != provided
    assert GENERATED_REQUEST_ID_PATTERN.match(request_id)
    assert headers["x-request-id"] == request_id


def test_replaces_too_long_request_id(client: TestClient) -> None:
    provided = "a" * (REQUEST_ID_MAX_LENGTH + 1)
    response = client.get(
        "/probe/echo-request-id", headers={REQUEST_ID_HEADER: provided}
    )

    assert response.json()["request_id"] != provided


def test_accepts_max_length_request_id(client: TestClient) -> None:
    provided = "b" * REQUEST_ID_MAX_LENGTH
    response = client.get(
        "/probe/echo-request-id", headers={REQUEST_ID_HEADER: provided}
    )

    assert response.json()["request_id"] == provided


def test_request_id_is_unique_per_request(client: TestClient) -> None:
    first = client.get("/probe/echo-request-id").json()["request_id"]
    second = client.get("/probe/echo-request-id").json()["request_id"]

    assert first != second


def test_request_id_returned_on_error_responses(client: TestClient) -> None:
    response = client.get("/probe/api-error")

    assert response.status_code == 409
    assert response.headers[REQUEST_ID_HEADER] == response.json()["request_id"]


def test_request_id_present_on_unhandled_error(client: TestClient) -> None:
    response = client.get("/probe/boom")

    assert response.status_code == 500
    assert response.headers[REQUEST_ID_HEADER] == response.json()["request_id"]


def test_is_valid_request_id_rules() -> None:
    assert is_valid_request_id("a") is True
    assert is_valid_request_id("a" * REQUEST_ID_MAX_LENGTH) is True
    assert is_valid_request_id(None) is False
    assert is_valid_request_id("") is False
    assert is_valid_request_id("a" * (REQUEST_ID_MAX_LENGTH + 1)) is False
    assert is_valid_request_id("请求") is False
    assert is_valid_request_id("with\ncontrol") is False


def test_resolve_request_id_prefers_valid_client_value() -> None:
    assert resolve_request_id("abc") == "abc"
    assert GENERATED_REQUEST_ID_PATTERN.match(resolve_request_id(""))
    assert GENERATED_REQUEST_ID_PATTERN.match(resolve_request_id(None))
