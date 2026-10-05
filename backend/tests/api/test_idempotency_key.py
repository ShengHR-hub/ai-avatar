"""`Idempotency-Key` 存在性与格式校验测试（ENG-02 第 2 步）。

对应 `03-api-contract.md` 第 4 节与第 7 节。
本步只校验存在性与格式，不涉及存储、TTL、fingerprint 或重复请求回放。
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.middleware.idempotency import (
    IDEMPOTENCY_KEY_HEADER,
    IDEMPOTENCY_KEY_MAX_LENGTH,
    is_valid_idempotency_key,
)


def test_missing_header_returns_idempotency_required(client: TestClient) -> None:
    response = client.post("/probe/idempotent")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "IDEMPOTENCY_REQUIRED"


def test_blank_header_returns_idempotency_invalid(client: TestClient) -> None:
    response = client.post("/probe/idempotent", headers={IDEMPOTENCY_KEY_HEADER: "   "})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "IDEMPOTENCY_INVALID"


def test_too_long_header_returns_idempotency_invalid(client: TestClient) -> None:
    response = client.post(
        "/probe/idempotent",
        headers={IDEMPOTENCY_KEY_HEADER: "a" * (IDEMPOTENCY_KEY_MAX_LENGTH + 1)},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "IDEMPOTENCY_INVALID"


def test_non_ascii_header_returns_idempotency_invalid(probe_app, call_raw) -> None:
    """非 ASCII 请求头无法经 httpx 发送，改在 ASGI 层构造原始字节头。"""

    provided = "café-idempotency-key"
    status, _, body = call_raw(
        probe_app,
        "/probe/idempotent",
        method="POST",
        headers=[(b"idempotency-key", provided.encode("latin-1"))],
    )

    assert status == 400
    assert json.loads(body.decode("utf-8"))["error"]["code"] == "IDEMPOTENCY_INVALID"


def test_valid_header_is_accepted_and_returned(client: TestClient) -> None:
    key = "client-generated-uuid-0123456789"
    response = client.post("/probe/idempotent", headers={IDEMPOTENCY_KEY_HEADER: key})

    assert response.status_code == 200
    assert response.json() == {"idempotency_key": key}


def test_max_length_header_is_accepted(client: TestClient) -> None:
    key = "b" * IDEMPOTENCY_KEY_MAX_LENGTH
    response = client.post("/probe/idempotent", headers={IDEMPOTENCY_KEY_HEADER: key})

    assert response.status_code == 200
    assert response.json() == {"idempotency_key": key}


def test_is_valid_idempotency_key_rules() -> None:
    assert is_valid_idempotency_key("a") is True
    assert is_valid_idempotency_key("a" * IDEMPOTENCY_KEY_MAX_LENGTH) is True
    assert is_valid_idempotency_key(None) is False
    assert is_valid_idempotency_key("") is False
    assert is_valid_idempotency_key("   ") is False
    assert is_valid_idempotency_key("a" * (IDEMPOTENCY_KEY_MAX_LENGTH + 1)) is False
    assert is_valid_idempotency_key("键") is False
    assert is_valid_idempotency_key("tab\tkey") is False


def test_idempotency_header_name_matches_contract() -> None:
    assert IDEMPOTENCY_KEY_HEADER == "Idempotency-Key"
    assert IDEMPOTENCY_KEY_MAX_LENGTH == 128
