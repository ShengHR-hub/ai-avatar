"""`StorageAdapter` 端口与统一类型（ENG-05 第 1 步）。

契约来源：`docs/architecture/04-ai-provider.md` 第 7 节与 `09-repository-boundary.md` 第 4 节：

- Service 只依赖本模块的协议与数据类型，**不得导入任何对象存储 SDK**；
- SDK 只允许出现在驱动模块（`app/storage/memory.py`、`app/storage/minio_adapter.py`）；
- 桶保持私有，浏览器只使用服务端签发的短期 URL；
- 签名 URL 时长上限：读取 5 分钟、上传 10 分钟，调用方可以缩短、不得放宽；
- 驱动不得让 SDK 异常、桶端点、凭据或原生异常类型越界（统一转成本模块异常）。

本模块**只提供 ENG-05 需要的子集**：签名 URL、上传、读取（服务端校验用）、存在性、幂等删除。
`04-ai-provider.md` 第 7 节的批量 `cleanup(expired_keys)` 属于生命周期清理（ENG-09），
按「方法与调用方同批落地」的纪律在此处不预留空壳。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

__all__ = [
    "MAX_DOWNLOAD_URL_TTL",
    "MAX_UPLOAD_URL_TTL",
    "ObjectMetadata",
    "ObjectNotFound",
    "SignedUrl",
    "StorageAdapter",
    "StorageError",
    "StorageUnavailable",
    "StoredObject",
    "clamp_signed_url_ttl",
]

#: 短期签名 URL 上限（`04-ai-provider.md` 第 7 节）
MAX_DOWNLOAD_URL_TTL = timedelta(minutes=5)
MAX_UPLOAD_URL_TTL = timedelta(minutes=10)

#: `create_signed_url` 的方法取值
SIGN_URL_METHODS = frozenset({"GET", "PUT"})


class StorageError(RuntimeError):
    """对象存储失败（驱动已把 SDK 异常归一为本族异常）。"""


class ObjectNotFound(StorageError):
    """对象不存在或不可读。"""


class StorageUnavailable(StorageError):
    """依赖不可用（网络、凭据、限流等），调用方可安全重试。"""


@dataclass(frozen=True, slots=True)
class StoredObject:
    """一次服务端写入的结果引用。"""

    key: str
    size_bytes: int
    etag: str | None = None


@dataclass(frozen=True, slots=True)
class ObjectMetadata:
    """对象元数据（`read()` 的第二返回值）。"""

    size_bytes: int
    content_type: str | None
    etag: str | None = None


@dataclass(frozen=True, slots=True)
class SignedUrl:
    """短期签名 URL。

    **禁止写入日志、错误响应或数据库**（`09-repository-boundary.md` 第 7 节）：
    查询参数即凭据。
    """

    url: str
    expires_at: datetime


def clamp_signed_url_ttl(method: str, expires_in: timedelta) -> timedelta:
    """校验签名 URL 时长没有突破上限。

    调用方可以缩短，不能放宽。超限是服务端编程错误而不是用户可触发条件，
    因此抛 `ValueError`（内部异常），不映射任何对外错误码。
    """

    if method not in SIGN_URL_METHODS:
        raise ValueError(f"不支持的签名方法 {method!r}，只允许 {sorted(SIGN_URL_METHODS)}。")
    if expires_in <= timedelta(0):
        raise ValueError("签名 URL 时长必须为正。")

    limit = MAX_UPLOAD_URL_TTL if method == "PUT" else MAX_DOWNLOAD_URL_TTL
    if expires_in > limit:
        raise ValueError(
            f"{method} 签名 URL 时长 {expires_in} 超过上限 {limit}，不得放宽。"
        )
    return expires_in


@runtime_checkable
class StorageAdapter(Protocol):
    """对象存储边界（ENG-05 子集）。"""

    def create_signed_url(
        self,
        key: str,
        method: str,
        expires_in: timedelta,
        content_type: str | None = None,
    ) -> SignedUrl:
        """签发短期上传（PUT）或下载（GET）URL。"""
        ...

    def upload(self, key: str, content: bytes, content_type: str) -> StoredObject:
        """服务端写入对象（不做浏览器直传）。"""
        ...

    def read(self, key: str) -> tuple[bytes, ObjectMetadata]:
        """服务端读取对象与元数据；不存在抛 `ObjectNotFound`。"""
        ...

    def exists(self, key: str) -> bool:
        """对象是否存在。"""
        ...

    def delete(self, key: str) -> None:
        """删除对象；对不存在的 key 也必须成功（幂等）。"""
        ...
