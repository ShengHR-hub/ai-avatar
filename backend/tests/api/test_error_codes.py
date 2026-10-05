"""错误码与 HTTP 映射测试（ENG-02 第 2 步）。

把 `03-api-contract.md` 第 7 节的表逐项抄成期望值，用于防止实现侧无意增删或改动。
"""

from __future__ import annotations

from app.errors.codes import (
    ERROR_CODES,
    ERROR_HTTP_STATUS,
    GENERIC_CODE_BY_STATUS,
    ErrorCode,
    default_message,
    http_status_for,
)

#: 契约第 7 节冻结的错误码 → HTTP 状态（逐项抄录）
CONTRACT_MAPPING: dict[str, int] = {
    "AUTH_REQUIRED": 401,
    "SESSION_INVALID": 401,
    "EMAIL_NOT_VERIFIED": 403,
    "ACCOUNT_SUSPENDED": 403,
    "FORBIDDEN": 403,
    "INVITE_INVALID": 422,
    "INVITE_ALREADY_USED": 409,
    "EMAIL_TOKEN_INVALID": 422,
    "EMAIL_ALREADY_VERIFIED": 409,
    "EMAIL_IN_USE": 409,
    "VALIDATION_ERROR": 422,
    "ASSET_NOT_FOUND": 404,
    "ASSET_FORBIDDEN": 403,
    "ASSET_INVALID": 422,
    "ASSET_NOT_READY": 409,
    "UPLOAD_TOO_LARGE": 413,
    "MEDIA_TYPE_UNSUPPORTED": 415,
    "GENERATION_NOT_FOUND": 404,
    "GENERATION_NOT_RETRYABLE": 409,
    "GENERATION_NOT_SHAREABLE": 409,
    "INSUFFICIENT_POINTS": 409,
    "FEATURE_NOT_AVAILABLE": 422,
    "IDEMPOTENCY_REQUIRED": 400,
    "IDEMPOTENCY_INVALID": 400,
    "IDEMPOTENCY_CONFLICT": 409,
    "IDEMPOTENCY_IN_PROGRESS": 409,
    "RATE_LIMITED": 429,
    "PROVIDER_UNAVAILABLE": 503,
    "PROVIDER_BAD_GATEWAY": 502,
    "GENERATION_FAILED": 502,
    "WEBHOOK_INVALID": 401,
    "NOT_READY": 503,
    "INTERNAL_ERROR": 500,
}

#: 契约第 6 节列出的 HTTP 状态码
CONTRACT_STATUS_CODES = frozenset(
    {200, 201, 202, 204, 400, 401, 403, 404, 409, 413, 415, 422, 429, 500, 502, 503}
)


def test_error_code_set_matches_contract() -> None:
    assert {code.value for code in ERROR_CODES} == set(CONTRACT_MAPPING)
    assert len(ERROR_CODES) == 33


def test_http_mapping_matches_contract() -> None:
    actual = {code.value: status for code, status in ERROR_HTTP_STATUS.items()}
    assert actual == CONTRACT_MAPPING


def test_every_status_is_declared_in_contract() -> None:
    assert set(ERROR_HTTP_STATUS.values()) <= CONTRACT_STATUS_CODES


def test_every_code_has_display_message() -> None:
    for code in ERROR_CODES:
        message = default_message(code)
        assert message and isinstance(message, str)


def test_http_status_for_is_consistent() -> None:
    for code in ERROR_CODES:
        assert http_status_for(code) == ERROR_HTTP_STATUS[code]


def test_generic_code_by_status_uses_contract_codes_only() -> None:
    assert set(GENERIC_CODE_BY_STATUS.values()) <= set(ERROR_CODES)


def test_generic_code_by_status_matches_code_status() -> None:
    """通用码的状态必须与状态键一致，避免同一错误码出现两种 HTTP 映射。"""

    for status, code in GENERIC_CODE_BY_STATUS.items():
        assert ERROR_HTTP_STATUS[code] == status, (status, code)


def test_contract_has_no_generic_code_for_some_statuses() -> None:
    """记录契约缺口：400 / 404 / 405 / 409 没有语义通用的错误码。

    该断言是**有意**的——如果技术负责人在契约第 7 节补充通用码，
    这个测试会失败并提醒同步更新 `GENERIC_CODE_BY_STATUS` 与处理说明。
    """

    for status in (400, 404, 405, 409):
        assert status not in GENERIC_CODE_BY_STATUS


def test_error_codes_are_string_enums() -> None:
    for code in ERROR_CODES:
        assert isinstance(code, str)
        assert code.value == str(code)


# --------------------------------------------------------------------------- #
# 错误映射矩阵（第 5 步收敛）
# --------------------------------------------------------------------------- #
def test_every_code_builds_api_error_with_contract_status() -> None:
    """33 个冻结错误码都能构造 `ApiError`，并给出契约规定的 HTTP 状态。"""

    from app.errors.exceptions import ApiError

    for code in ERROR_CODES:
        error = ApiError(code)

        assert error.code is code
        assert error.status_code == ERROR_HTTP_STATUS[code]
        assert error.message == default_message(code)
        assert error.details == {}
        assert error.headers == {}


def test_api_error_carries_custom_message_details_and_headers() -> None:
    from app.errors.exceptions import ApiError

    error = ApiError(
        ErrorCode.RATE_LIMITED,
        message="自定义文案",
        details={"retry_after_seconds": 30},
        headers={"Retry-After": "30"},
    )

    assert error.message == "自定义文案"
    assert error.details == {"retry_after_seconds": 30}
    assert error.headers == {"Retry-After": "30"}
    assert error.status_code == 429


def test_idempotency_error_subclasses_use_contract_codes() -> None:
    from app.errors.exceptions import IdempotencyKeyInvalid, IdempotencyKeyRequired

    required = IdempotencyKeyRequired()
    invalid = IdempotencyKeyInvalid()

    assert required.code is ErrorCode.IDEMPOTENCY_REQUIRED
    assert required.status_code == 400
    assert invalid.code is ErrorCode.IDEMPOTENCY_INVALID
    assert invalid.status_code == 400


def test_default_messages_are_safe_and_bounded() -> None:
    """展示文案必须非空、简短，且不含堆栈、SQL、连接串或凭据痕迹。"""

    forbidden = (
        "traceback",
        "select ",
        "postgresql://",
        "redis://",
        "secret",
        "api_key",
        "password",
        "token",
    )

    for code in ERROR_CODES:
        message = default_message(code)

        assert message.strip(), code
        assert len(message) <= 120, code
        lowered = message.lower()
        for token in forbidden:
            assert token not in lowered, (code, token)
