"""`Idempotency-Key` 的存在性与格式校验（ENG-02 第 2 步）。

唯一来源：`docs/architecture/03-api-contract.md` 第 4 节与第 7 节：

- 写请求必须发送 `Idempotency-Key`，ASCII、1-128 字符；
- 缺少 Header → `IDEMPOTENCY_REQUIRED`（400）；
- 空白、非 ASCII 或超长值 → `IDEMPOTENCY_INVALID`（400）。

本步**只做存在性与格式校验**：不实现 Redis 存储、TTL、request fingerprint、
重复请求回放或 `IDEMPOTENCY_CONFLICT` / `IDEMPOTENCY_IN_PROGRESS` 判定。

用法（后续卡片在写接口上声明）：

```python
@router.post("/generations", status_code=202)
def create_generation(
    idempotency_key: str = Depends(require_idempotency_key),
) -> ...: ...
```
"""

from __future__ import annotations

from starlette.requests import Request

from app.errors.exceptions import IdempotencyKeyInvalid, IdempotencyKeyRequired

__all__ = [
    "IDEMPOTENCY_KEY_HEADER",
    "IDEMPOTENCY_KEY_MAX_LENGTH",
    "is_valid_idempotency_key",
    "require_idempotency_key",
]

IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
IDEMPOTENCY_KEY_MAX_LENGTH = 128


def is_valid_idempotency_key(raw: str | None) -> bool:
    """判断幂等键是否满足契约要求（ASCII、1-128 字符、非空白）。"""

    if raw is None:
        return False
    if not raw.isascii():
        return False
    if not 1 <= len(raw) <= IDEMPOTENCY_KEY_MAX_LENGTH:
        return False
    if not raw.strip():
        return False
    return all(ch >= " " for ch in raw)


def require_idempotency_key(request: Request) -> str:
    """FastAPI 依赖：校验并返回 `Idempotency-Key`。

    - 缺少 Header：抛 `IdempotencyKeyRequired`（`IDEMPOTENCY_REQUIRED`，400）；
    - 格式非法：抛 `IdempotencyKeyInvalid`（`IDEMPOTENCY_INVALID`，400）。
    """

    raw = request.headers.get(IDEMPOTENCY_KEY_HEADER)
    if raw is None:
        raise IdempotencyKeyRequired()
    if not is_valid_idempotency_key(raw):
        raise IdempotencyKeyInvalid()
    return raw
