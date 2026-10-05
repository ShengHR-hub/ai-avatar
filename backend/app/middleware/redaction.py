"""日志脱敏工具（ENG-02 第 2 步）。

依据 `docs/architecture/09-repository-boundary.md` 第 7 节与
`06-development-standard.md` 第 6 节：日志在写出前必须做字段级脱敏。

禁止进入日志：API Key、数据库 / Redis / MinIO 密码、Session Cookie、
`Authorization`、邮箱验证令牌、签名 URL 查询参数、原图或 Base64、完整 Prompt、
完整 Provider 原始响应、未脱敏个人信息以及整个环境变量对象。

本模块只做“把值变安全”，不负责日志输出；输出侧见 `app.middleware.access_log`。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

__all__ = [
    "REDACTED",
    "SENSITIVE_HEADERS",
    "SENSITIVE_KEY_PATTERN",
    "redact_dsn",
    "redact_headers",
    "redact_mapping",
    "redact_url",
]

REDACTED = "[REDACTED]"

#: 直接按头名脱敏（小写比较）
SENSITIVE_HEADERS: frozenset[str] = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "x-auth-token",
        "x-session-secret",
        "x-csrf-token",
    }
)

#: 按键名脱敏（用于字典/结构化字段）
SENSITIVE_KEY_PATTERN = re.compile(
    r"(authorization|cookie|secret|token|password|passwd|api[_-]?key|"
    r"access[_-]?key|private[_-]?key|credential|dsn|database[_-]?url|"
    r"redis[_-]?url|session|signature|invite[_-]?code)",
    re.IGNORECASE,
)

_URL_USERINFO_PATTERN = re.compile(r"://[^/@\s]*@")


def redact_headers(headers: Mapping[str, str] | Iterable[tuple[str, str]]) -> dict[str, str]:
    """返回脱敏后的请求/响应头副本（敏感头替换为 `[REDACTED]`）。"""

    items = headers.items() if isinstance(headers, Mapping) else headers
    redacted: dict[str, str] = {}
    for name, value in items:
        key = str(name)
        redacted[key] = REDACTED if key.lower() in SENSITIVE_HEADERS else str(value)
    return redacted


def redact_mapping(data: Mapping[str, Any]) -> dict[str, Any]:
    """按键名递归脱敏结构化数据。"""

    result: dict[str, Any] = {}
    for key, value in data.items():
        name = str(key)
        if SENSITIVE_KEY_PATTERN.search(name):
            result[name] = REDACTED
        elif isinstance(value, Mapping):
            result[name] = redact_mapping(value)
        elif isinstance(value, (list, tuple)):
            result[name] = [
                redact_mapping(item) if isinstance(item, Mapping) else item for item in value
            ]
        else:
            result[name] = value
    return result


def redact_url(url: str) -> str:
    """去掉查询串与 fragment，避免签名 URL 参数进入日志。"""

    stripped = url.split("#", 1)[0]
    return stripped.split("?", 1)[0]


def redact_dsn(dsn: str) -> str:
    """剥离连接串中的用户名与密码，只保留主机与库名结构。"""

    return _URL_USERINFO_PATTERN.sub("://***@", dsn)
