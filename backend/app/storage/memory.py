"""`StorageAdapter` 的内存实现（ENG-05，测试与本地开发用）。

**不得用于生产**：它把对象放在进程内存里，签名 URL 也是可预测的假 URL，
不提供任何访问控制或持久性。存在它是为了在没有 MinIO 的条件下
（本卡禁止启动对象存储）完整测试 Service 的上传意图、完成校验、
签名、存在性判断与幂等删除路径。

与真实驱动保持同样的契约：越界类型只有 `app.storage.ports` 里的异常与数据类。
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from app.storage.ports import (
    ObjectMetadata,
    ObjectNotFound,
    SignedUrl,
    StorageUnavailable,
    StoredObject,
    clamp_signed_url_ttl,
)

__all__ = ["InMemoryStorageAdapter"]


class InMemoryStorageAdapter:
    """内存对象存储。

    - `unavailable=True` 时所有操作抛 `StorageUnavailable`，用于验证 Service 的
      依赖不可用路径不会被误判为「对象不存在」；
    - `signed_urls` 记录每次签发的 `(key, method, 秒数)`，便于断言时长上限；
      记录的只有 key 与方法，**不记录 URL**（URL 即凭据）。
    """

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        bucket: str = "ai-avatar",
    ) -> None:
        self._objects: dict[str, bytes] = {}
        self._content_types: dict[str, str] = {}
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._bucket = bucket
        self.unavailable = False
        self.signed_urls: list[tuple[str, str, int]] = []
        self.delete_calls: list[str] = []

    # ------------------------------------------------------------------ 端口实现
    def create_signed_url(
        self,
        key: str,
        method: str,
        expires_in: timedelta,
        content_type: str | None = None,
    ) -> SignedUrl:
        self._guard()
        ttl = clamp_signed_url_ttl(method, expires_in)
        expires_at = self._clock() + ttl
        self.signed_urls.append((key, method, int(ttl.total_seconds())))
        url = (
            f"https://memory.invalid/{self._bucket}/{key}"
            f"?method={method}&expires_at={expires_at.isoformat()}"
        )
        return SignedUrl(url=url, expires_at=expires_at)

    def upload(self, key: str, content: bytes, content_type: str) -> StoredObject:
        self._guard()
        self._objects[key] = bytes(content)
        self._content_types[key] = content_type
        return StoredObject(key=key, size_bytes=len(content), etag=self._etag(content))

    def read(self, key: str) -> tuple[bytes, ObjectMetadata]:
        self._guard()
        content = self._objects.get(key)
        if content is None:
            raise ObjectNotFound(f"对象 {key} 不存在。")
        metadata = ObjectMetadata(
            size_bytes=len(content),
            content_type=self._content_types.get(key),
            etag=self._etag(content),
        )
        return bytes(content), metadata

    def exists(self, key: str) -> bool:
        self._guard()
        return key in self._objects

    def delete(self, key: str) -> None:
        """幂等删除：不存在的 key 也静默成功。"""

        self._guard()
        self.delete_calls.append(key)
        self._objects.pop(key, None)
        self._content_types.pop(key, None)

    # -------------------------------------------------------------------- 测试辅助
    def keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._objects))

    @staticmethod
    def _etag(content: bytes) -> str:
        # 仅充当对象 etag（非安全用途），故关闭 MD5 告警
        return hashlib.md5(content, usedforsecurity=False).hexdigest()

    def _guard(self) -> None:
        if self.unavailable:
            raise StorageUnavailable("内存对象存储被置为不可用（测试开关）。")
