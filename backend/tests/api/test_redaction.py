"""日志脱敏测试（ENG-02 第 2 步）。

对应 `09-repository-boundary.md` 第 7 节与 `06-development-standard.md` 第 6 节。
"""

from __future__ import annotations

import logging

from fastapi.testclient import TestClient

from app.errors.handlers import ERROR_LOGGER_NAME
from app.middleware.access_log import ACCESS_LOGGER_NAME
from app.middleware.redaction import (
    REDACTED,
    redact_dsn,
    redact_headers,
    redact_mapping,
    redact_url,
)

SENTINEL_AUTH = "Bearer SENTINEL-AUTH-TOKEN"
SENTINEL_COOKIE = "session=SENTINEL-COOKIE-VALUE"
SENTINEL_QUERY = "SENTINEL-SIGNATURE-QUERY"


def test_redact_headers_from_mapping() -> None:
    redacted = redact_headers(
        {
            "Authorization": SENTINEL_AUTH,
            "Cookie": SENTINEL_COOKIE,
            "Set-Cookie": SENTINEL_COOKIE,
            "X-Api-Key": "SENTINEL-API-KEY",
            "Content-Type": "application/json",
            "X-Request-Id": "req_1",
        }
    )

    assert redacted["Authorization"] == REDACTED
    assert redacted["Cookie"] == REDACTED
    assert redacted["Set-Cookie"] == REDACTED
    assert redacted["X-Api-Key"] == REDACTED
    assert redacted["Content-Type"] == "application/json"
    assert redacted["X-Request-Id"] == "req_1"


def test_redact_headers_from_pairs() -> None:
    redacted = redact_headers([("authorization", SENTINEL_AUTH), ("accept", "*/*")])

    assert redacted == {"authorization": REDACTED, "accept": "*/*"}


def test_redact_mapping_recurses_and_matches_key_patterns() -> None:
    redacted = redact_mapping(
        {
            "user_id": "u1",
            "session_secret": "SENTINEL-SECRET",
            "database_url": "postgresql://u:p@h/db",
            "nested": {"api_key": "SENTINEL-API-KEY", "safe": 1},
            "items": [{"token": "SENTINEL-TOKEN"}, {"safe": 2}],
        }
    )

    assert redacted["user_id"] == "u1"
    assert redacted["session_secret"] == REDACTED
    assert redacted["database_url"] == REDACTED
    assert redacted["nested"]["api_key"] == REDACTED
    assert redacted["nested"]["safe"] == 1
    assert redacted["items"][0]["token"] == REDACTED
    assert redacted["items"][1]["safe"] == 2


def test_redact_url_strips_query_and_fragment() -> None:
    assert (
        redact_url(f"/api/v1/assets/a/download-url?signature={SENTINEL_QUERY}#frag")
        == "/api/v1/assets/a/download-url"
    )
    assert redact_url("/api/v1/me") == "/api/v1/me"


def test_redact_dsn_strips_credentials() -> None:
    assert redact_dsn("postgresql+psycopg://ai_avatar:placeholder@postgres:5432/ai_avatar") == (
        "postgresql+psycopg://***@postgres:5432/ai_avatar"
    )
    assert redact_dsn("redis://redis:6379/0") == "redis://redis:6379/0"


def test_access_log_records_safe_structured_fields(
    client: TestClient, caplog
) -> None:
    with caplog.at_level(logging.INFO):
        response = client.get("/probe/echo-request-id")

    records = [r for r in caplog.records if r.name == ACCESS_LOGGER_NAME]
    assert len(records) == 1
    record = records[0]
    assert record.event == "http_request"
    assert record.method == "GET"
    assert record.path == "/probe/echo-request-id"
    assert record.status == 200
    assert isinstance(record.duration_ms, float)
    assert record.request_id == response.json()["request_id"]


def test_access_log_never_contains_headers_query_or_body(
    client: TestClient, caplog
) -> None:
    with caplog.at_level(logging.INFO):
        client.get(
            f"/probe/echo-request-id?signature={SENTINEL_QUERY}",
            headers={
                "Authorization": SENTINEL_AUTH,
                "Cookie": SENTINEL_COOKIE,
                "X-Api-Key": "SENTINEL-API-KEY",
            },
        )

    records = [r for r in caplog.records if r.name == ACCESS_LOGGER_NAME]
    assert records
    dumped = "\n".join(
        f"{record.getMessage()} {record.__dict__}" for record in records
    )
    assert SENTINEL_AUTH not in dumped
    assert SENTINEL_COOKIE not in dumped
    assert "SENTINEL-API-KEY" not in dumped
    assert SENTINEL_QUERY not in dumped
    assert "?" not in dumped


def test_access_log_records_error_code(client: TestClient, caplog) -> None:
    with caplog.at_level(logging.INFO):
        client.get("/probe/api-error")

    records = [r for r in caplog.records if r.name == ACCESS_LOGGER_NAME]
    assert records
    assert records[-1].error_code == "INSUFFICIENT_POINTS"
    assert records[-1].status == 409


def test_access_log_records_unhandled_error_without_stack(client: TestClient, caplog) -> None:
    """未处理异常路径：访问日志记录 `http_request_failed`，不含状态码、堆栈或内部细节。

    500 响应由最外层 ServerErrorMiddleware 产出，位于访问日志中间件之外，
    因此该路径无法得知最终状态码；错误码与异常类型由 `app.errors.handlers`
    的 `unhandled_exception` 诊断日志记录。
    """

    with caplog.at_level(logging.INFO):
        response = client.get("/probe/boom")

    assert response.status_code == 500

    failed_records = [
        r
        for r in caplog.records
        if r.name == ACCESS_LOGGER_NAME and r.event == "http_request_failed"
    ]
    assert len(failed_records) == 1
    record = failed_records[0]
    assert record.method == "GET"
    assert record.path == "/probe/boom"
    assert isinstance(record.duration_ms, float)
    assert not hasattr(record, "status")

    # 访问日志必须干净：不含堆栈、异常文本或请求内容
    access_dump = "\n".join(
        f"{r.getMessage()} {r.__dict__}"
        for r in caplog.records
        if r.name == ACCESS_LOGGER_NAME
    )
    assert "Traceback" not in access_dump
    assert "RuntimeError" not in access_dump
    assert "SENTINEL-INVITE-CODE" not in access_dump

    # 诊断日志（受控服务端日志）必须保留异常堆栈，用于排障
    diagnostic_records = [
        r
        for r in caplog.records
        if r.name == ERROR_LOGGER_NAME and r.event == "unhandled_exception"
    ]
    assert len(diagnostic_records) == 1
    diagnostic = diagnostic_records[0]
    assert diagnostic.exception_type == "RuntimeError"
    assert diagnostic.exc_info is not None
