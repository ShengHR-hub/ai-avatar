"""MinIO / S3 兼容驱动（ENG-05 第 1 步）。

`09-repository-boundary.md` 第 4 节：只有驱动模块允许导入对象存储 SDK。
本模块因此把 `minio` 的导入放在方法内部，`app.storage` 与 Service 在没有安装
SDK 的进程里依然可导入（本批次禁止启动 MinIO，真实连通性未验证）。

异常边界：所有 SDK 异常都归一为 `app.storage.ports` 的 `ObjectNotFound` /
`StorageUnavailable`，不外泄 `S3Error`、urllib3 异常、端点或凭据。
签名 URL 是凭据，**不得进入日志或错误响应**。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from io import BytesIO
from typing import Any
from urllib.parse import urlsplit

from app.storage.ports import (
    ObjectMetadata,
    ObjectNotFound,
    SignedUrl,
    StorageError,
    StorageUnavailable,
    StoredObject,
    clamp_signed_url_ttl,
)

__all__ = ["MinioStorageAdapter"]

#: 判定「对象不存在」的 S3 错误码（其余一律按依赖不可用处理）
_NOT_FOUND_CODES = frozenset({"NoSuchKey", "NoSuchBucket", "404", "NotFound"})

#: `STORAGE_ENDPOINT` 允许的 scheme；它们同时决定要不要用 TLS 访问
_SCHEMES = {"http": False, "https": True}

#: `host[:port]` 形式：主机名或带方括号的 IPv6 字面量，可选端口；
#: 不含路径、查询、片段或凭据
_AUTHORITY_PATTERN = re.compile(
    r"^(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9][A-Za-z0-9.\-_]*)(?::[0-9]{1,5})?$"
)


def normalize_endpoint(endpoint: str, *, secure: bool = False) -> tuple[str, bool]:
    """把配置里的对象存储端点归一成 `minio` SDK 接受的形态。

    SDK 只接受**不带 scheme** 的 `host[:port]`，而 `app/config.py` 的默认值与
    `docker/compose.yml` 给的都带 scheme（`http://minio:9000`）。原样交给 SDK 会在
    构造时抛 `ValueError: path in endpoint is not allowed`——装配看起来成功了，
    直到第一次读写对象才炸，因此归一必须留在驱动入口。

    两种写法都支持，并且**由 scheme 决定 TLS**：配置写了 `https://` 却被默认的
    `secure=False` 关掉加密是静默降级。没有 scheme 时沿用调用方给的 `secure`。

    拒绝的形态（路径、查询、片段、凭据、非 HTTP 协议、空主机）一律抛
    `StorageUnavailable`，绝不退回某个「看起来能用」的地址：错误端点意味着数据会被
    写进错误的桶，那是比启动失败更糟的结果。异常文本也不得回显原值，因为凭据可能
    就写在里面。
    """

    raw = (endpoint or "").strip()
    if not raw:
        raise StorageUnavailable("对象存储端点未配置。")

    if "://" in raw:
        scheme, _separator, rest = raw.partition("://")
        tls = _SCHEMES.get(scheme.lower())
        if tls is None:
            raise StorageUnavailable("对象存储端点的协议不受支持（只支持 http/https）。")
        parsed = urlsplit(raw)
        if not parsed.netloc or parsed.hostname is None:
            raise StorageUnavailable("对象存储端点缺少主机名。")
        if "@" in parsed.netloc:
            raise StorageUnavailable(
                "对象存储端点不得内嵌凭据；请使用 STORAGE_ACCESS_KEY 与 STORAGE_SECRET_KEY。"
            )
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise StorageUnavailable(
                "对象存储端点只能是主机与端口；路径与查询参数属于配置错误。"
            )
        host = parsed.hostname
        if ":" in host:
            # `urlsplit` 会剥掉 IPv6 的方括号，交回 SDK 前必须包回去，否则端口无法区分
            host = f"[{host}]"
        authority = f"{host}:{parsed.port}" if parsed.port else host
        return authority, tls

    # 尾斜杠是同一个端点，不是一段路径；除此之外不接受任何路径
    authority = raw[:-1] if raw.endswith("/") else raw
    if not _AUTHORITY_PATTERN.match(authority):
        # 不带 scheme 的写法里也不能藏路径、查询或凭据
        raise StorageUnavailable(
            "对象存储端点形如 host 或 host:port；路径、查询与凭据都属于配置错误。"
        )
    return authority, secure


class MinioStorageAdapter:
    """`StorageAdapter` 的 MinIO 实现（本地桶私有，只发短期签名 URL）。"""

    def __init__(
        self,
        *,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        secure: bool = False,
        region: str | None = None,
    ) -> None:
        from minio import Minio

        # 构造只建客户端对象，不发起网络请求（与 RedisClientAdapter 同口径）
        self._bucket = bucket
        authority, tls = normalize_endpoint(endpoint, secure=secure)
        self._client: Any = Minio(
            authority,
            access_key=access_key,
            secret_key=secret_key,
            secure=tls,
            region=region or None,
        )

    # ------------------------------------------------------------------ 端口实现
    def bucket_available(self) -> bool:
        """桶级可达性（供 `/ready` 探针使用）。

        这是驱动能力，**不加进 `StorageAdapter` 端口**：端口扩张会把对象存储 SDK 的
        概念渗进业务 Service，而只有就绪探针需要它。
        """

        return bool(self._call("bucket", self._client.bucket_exists, self._bucket))
    def create_signed_url(
        self,
        key: str,
        method: str,
        expires_in: timedelta,
        content_type: str | None = None,
    ) -> SignedUrl:
        ttl = clamp_signed_url_ttl(method, expires_in)
        # `presigned_put_object` 在 minio-py 里没有 `content_type` 形参，URL 签名也不绑定
        # 它：直传声明的类型本来就不可信，资源只有在 `complete_upload` 按实际字节重新
        # 判定 MIME 后才可用（`04-ai-provider.md` 第 8 节），所以这里不假装绑定。
        presign = self._client.presigned_put_object if method == "PUT" else self._client.presigned_get_object
        url = self._call("presigned", presign, self._bucket, key, expires=ttl)
        return SignedUrl(url=str(url), expires_at=datetime.now(timezone.utc) + ttl)

    def upload(self, key: str, content: bytes, content_type: str) -> StoredObject:
        etag = self._call(
            "upload",
            self._client.put_object,
            self._bucket,
            key,
            BytesIO(content),
            len(content),
            content_type=content_type,
        )
        return StoredObject(
            key=key, size_bytes=len(content), etag=getattr(etag, "etag", None) or None
        )

    def read(self, key: str) -> tuple[bytes, ObjectMetadata]:
        stat = self._stat(key)

        response = self._call("read", self._client.get_object, self._bucket, key)
        try:
            content = response.read()
        finally:
            response.close()
            response.release_conn()

        return content, self._metadata_from_stat(stat, fallback_size=len(content))

    def exists(self, key: str) -> bool:
        try:
            self._stat(key)
        except ObjectNotFound:
            return False
        return True

    def delete(self, key: str) -> None:
        """幂等删除：S3 的 RemoveObject 对不存在的 key 也返回成功。"""

        self._call("delete", self._client.remove_object, self._bucket, key)

    # ---------------------------------------------------------------------- 内部
    def _stat(self, key: str) -> Any:
        return self._call("stat", self._client.stat_object, self._bucket, key)

    @staticmethod
    def _metadata_from_stat(stat: Any, *, fallback_size: int) -> ObjectMetadata:
        size = getattr(stat, "size", None)
        return ObjectMetadata(
            size_bytes=int(size) if size is not None else fallback_size,
            content_type=getattr(stat, "content_type", None) or None,
            etag=getattr(stat, "etag", None) or None,
        )

    def _call(self, action: str, function: Any, *args: Any, **kwargs: Any) -> Any:
        """执行一次 SDK 调用并把异常归一（异常信息不得含 key 之外的敏感内容）。"""

        from minio.error import S3Error

        try:
            return function(*args, **kwargs)
        except S3Error as exc:
            code = str(getattr(exc, "code", "") or "")
            if code in _NOT_FOUND_CODES:
                raise ObjectNotFound(f"对象不存在（{action}）。") from exc
            raise StorageUnavailable(f"对象存储不可用（{action}）。") from exc
        except (OSError, ValueError) as exc:  # 连接、DNS、TLS、超时等传输层故障
            raise StorageUnavailable(f"对象存储不可用（{action}）。") from exc
        except StorageError:
            raise
        except Exception as exc:  # 未归类的 SDK 内部错误也必须越界为存储异常
            raise StorageUnavailable(f"对象存储调用失败（{action}）。") from exc
