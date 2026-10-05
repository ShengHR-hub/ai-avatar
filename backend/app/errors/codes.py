"""稳定错误码与 HTTP 状态映射（ENG-02 第 2 步）。

唯一来源：`docs/architecture/03-api-contract.md` 第 7 节「稳定错误码及映射」
与第 6 节「HTTP 状态码规范」。

约束：

- 错误码集合与 HTTP 映射**逐项复制契约**，不得自行增删或改动；
- 同一错误码在所有路由保持相同 HTTP 映射；
- `message` 仅用于展示，前端只能依赖 `code`；
- 异常文本、数据库错误和 Provider 原始码不得进入响应。
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "ERROR_CODES",
    "ERROR_HTTP_STATUS",
    "GENERIC_CODE_BY_STATUS",
    "ErrorCode",
    "default_message",
    "http_status_for",
]


class ErrorCode(StrEnum):
    """契约第 7 节冻结的稳定错误码。"""

    AUTH_REQUIRED = "AUTH_REQUIRED"
    SESSION_INVALID = "SESSION_INVALID"
    EMAIL_NOT_VERIFIED = "EMAIL_NOT_VERIFIED"
    ACCOUNT_SUSPENDED = "ACCOUNT_SUSPENDED"
    FORBIDDEN = "FORBIDDEN"
    INVITE_INVALID = "INVITE_INVALID"
    INVITE_ALREADY_USED = "INVITE_ALREADY_USED"
    EMAIL_TOKEN_INVALID = "EMAIL_TOKEN_INVALID"
    EMAIL_ALREADY_VERIFIED = "EMAIL_ALREADY_VERIFIED"
    EMAIL_IN_USE = "EMAIL_IN_USE"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    ASSET_NOT_FOUND = "ASSET_NOT_FOUND"
    ASSET_FORBIDDEN = "ASSET_FORBIDDEN"
    ASSET_INVALID = "ASSET_INVALID"
    ASSET_NOT_READY = "ASSET_NOT_READY"
    UPLOAD_TOO_LARGE = "UPLOAD_TOO_LARGE"
    MEDIA_TYPE_UNSUPPORTED = "MEDIA_TYPE_UNSUPPORTED"
    GENERATION_NOT_FOUND = "GENERATION_NOT_FOUND"
    GENERATION_NOT_RETRYABLE = "GENERATION_NOT_RETRYABLE"
    GENERATION_NOT_SHAREABLE = "GENERATION_NOT_SHAREABLE"
    INSUFFICIENT_POINTS = "INSUFFICIENT_POINTS"
    FEATURE_NOT_AVAILABLE = "FEATURE_NOT_AVAILABLE"
    IDEMPOTENCY_REQUIRED = "IDEMPOTENCY_REQUIRED"
    IDEMPOTENCY_INVALID = "IDEMPOTENCY_INVALID"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    IDEMPOTENCY_IN_PROGRESS = "IDEMPOTENCY_IN_PROGRESS"
    RATE_LIMITED = "RATE_LIMITED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    PROVIDER_BAD_GATEWAY = "PROVIDER_BAD_GATEWAY"
    GENERATION_FAILED = "GENERATION_FAILED"
    WEBHOOK_INVALID = "WEBHOOK_INVALID"
    NOT_READY = "NOT_READY"
    INTERNAL_ERROR = "INTERNAL_ERROR"


#: 错误码 → HTTP 状态（`03-api-contract.md` 第 7 节，逐项复制）
ERROR_HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.AUTH_REQUIRED: 401,
    ErrorCode.SESSION_INVALID: 401,
    ErrorCode.EMAIL_NOT_VERIFIED: 403,
    ErrorCode.ACCOUNT_SUSPENDED: 403,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.INVITE_INVALID: 422,
    ErrorCode.INVITE_ALREADY_USED: 409,
    ErrorCode.EMAIL_TOKEN_INVALID: 422,
    ErrorCode.EMAIL_ALREADY_VERIFIED: 409,
    ErrorCode.EMAIL_IN_USE: 409,
    ErrorCode.VALIDATION_ERROR: 422,
    ErrorCode.ASSET_NOT_FOUND: 404,
    ErrorCode.ASSET_FORBIDDEN: 403,
    ErrorCode.ASSET_INVALID: 422,
    ErrorCode.ASSET_NOT_READY: 409,
    ErrorCode.UPLOAD_TOO_LARGE: 413,
    ErrorCode.MEDIA_TYPE_UNSUPPORTED: 415,
    ErrorCode.GENERATION_NOT_FOUND: 404,
    ErrorCode.GENERATION_NOT_RETRYABLE: 409,
    ErrorCode.GENERATION_NOT_SHAREABLE: 409,
    ErrorCode.INSUFFICIENT_POINTS: 409,
    ErrorCode.FEATURE_NOT_AVAILABLE: 422,
    ErrorCode.IDEMPOTENCY_REQUIRED: 400,
    ErrorCode.IDEMPOTENCY_INVALID: 400,
    ErrorCode.IDEMPOTENCY_CONFLICT: 409,
    ErrorCode.IDEMPOTENCY_IN_PROGRESS: 409,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.PROVIDER_UNAVAILABLE: 503,
    ErrorCode.PROVIDER_BAD_GATEWAY: 502,
    ErrorCode.GENERATION_FAILED: 502,
    ErrorCode.WEBHOOK_INVALID: 401,
    ErrorCode.NOT_READY: 503,
    ErrorCode.INTERNAL_ERROR: 500,
}

#: 契约第 7 节冻结的错误码全集（用于自检，防止无意增删）
ERROR_CODES: frozenset[ErrorCode] = frozenset(ERROR_HTTP_STATUS)

#: 展示用默认文案（`message` 仅用于展示；前端只依赖 `code`）
_DEFAULT_MESSAGES: dict[ErrorCode, str] = {
    ErrorCode.AUTH_REQUIRED: "请先登录",
    ErrorCode.SESSION_INVALID: "登录状态已失效，请重新登录",
    ErrorCode.EMAIL_NOT_VERIFIED: "请先完成邮箱验证",
    ErrorCode.ACCOUNT_SUSPENDED: "账号已被暂停",
    ErrorCode.FORBIDDEN: "没有访问该资源的权限",
    ErrorCode.INVITE_INVALID: "邀请码无效或已过期",
    ErrorCode.INVITE_ALREADY_USED: "邀请码已被使用或兑换次数已满",
    ErrorCode.EMAIL_TOKEN_INVALID: "邮箱验证链接无效或已过期",
    ErrorCode.EMAIL_ALREADY_VERIFIED: "该邮箱已完成验证",
    ErrorCode.EMAIL_IN_USE: "该邮箱已绑定其他账号",
    ErrorCode.VALIDATION_ERROR: "请求参数不符合要求",
    ErrorCode.ASSET_NOT_FOUND: "资源不存在",
    ErrorCode.ASSET_FORBIDDEN: "没有操作该资源的权限",
    ErrorCode.ASSET_INVALID: "文件校验未通过",
    ErrorCode.ASSET_NOT_READY: "文件尚未上传完成",
    ErrorCode.UPLOAD_TOO_LARGE: "文件超过大小上限",
    ErrorCode.MEDIA_TYPE_UNSUPPORTED: "不支持该文件类型",
    ErrorCode.GENERATION_NOT_FOUND: "任务不存在",
    ErrorCode.GENERATION_NOT_RETRYABLE: "当前任务不可重试",
    ErrorCode.GENERATION_NOT_SHAREABLE: "当前任务不可分享",
    ErrorCode.INSUFFICIENT_POINTS: "可用点数不足",
    ErrorCode.FEATURE_NOT_AVAILABLE: "该功能暂未开放",
    ErrorCode.IDEMPOTENCY_REQUIRED: "缺少 Idempotency-Key 请求头",
    ErrorCode.IDEMPOTENCY_INVALID: "Idempotency-Key 格式不合法",
    ErrorCode.IDEMPOTENCY_CONFLICT: "相同幂等键对应了不同的请求内容",
    ErrorCode.IDEMPOTENCY_IN_PROGRESS: "相同请求正在处理中，请稍后重试",
    ErrorCode.RATE_LIMITED: "请求过于频繁，请稍后重试",
    ErrorCode.PROVIDER_UNAVAILABLE: "生成服务暂时不可用",
    ErrorCode.PROVIDER_BAD_GATEWAY: "生成服务返回异常结果",
    ErrorCode.GENERATION_FAILED: "生成任务失败",
    ErrorCode.WEBHOOK_INVALID: "回调签名校验失败",
    ErrorCode.NOT_READY: "服务尚未就绪",
    ErrorCode.INTERNAL_ERROR: "服务内部错误",
}

#: 框架级 HTTP 状态 → 契约中语义通用的错误码。
#:
#: 只登记**语义确实通用**的状态，避免把框架级错误伪装成业务错误。
#: 契约第 7 节没有为 `400`、`404`、`405`、`409` 提供通用错误码，
#: 因此这些状态不在此表中——见 `handlers.py` 中的处理说明。
GENERIC_CODE_BY_STATUS: dict[int, ErrorCode] = {
    401: ErrorCode.AUTH_REQUIRED,
    403: ErrorCode.FORBIDDEN,
    422: ErrorCode.VALIDATION_ERROR,
    429: ErrorCode.RATE_LIMITED,
    500: ErrorCode.INTERNAL_ERROR,
    502: ErrorCode.PROVIDER_BAD_GATEWAY,
    503: ErrorCode.NOT_READY,
}


def http_status_for(code: ErrorCode) -> int:
    """返回错误码对应的 HTTP 状态码。"""

    return ERROR_HTTP_STATUS[code]


def default_message(code: ErrorCode) -> str:
    """返回错误码的展示用默认文案。"""

    return _DEFAULT_MESSAGES[code]
