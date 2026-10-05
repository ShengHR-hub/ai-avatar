"""对象存储适配层（ENG-05）。

- `ports`：`StorageAdapter` 协议、统一类型与异常、签名 URL 时长上限；
- `memory`：内存驱动，测试与本地开发用，**禁止生产**；
- `minio_adapter`：MinIO/S3 兼容驱动（唯一允许导入 SDK 的模块）。

本包还提供 `STORAGE_PROVIDER_LABELS`：`STORAGE_DRIVER` 环境变量是**小写**
（`config.py` 与 `docker/.env.example`），而 `media_assets.storage_provider` 是
**大写**枚举（`02-database-design.md` 第 3.3 节）。`09-repository-boundary.md`
把这一映射明确划给 ENG-05 负责，映射只在此处定义一次。
"""

from __future__ import annotations

from app.storage.memory import InMemoryStorageAdapter
from app.storage.ports import (
    MAX_DOWNLOAD_URL_TTL,
    MAX_UPLOAD_URL_TTL,
    ObjectMetadata,
    ObjectNotFound,
    SignedUrl,
    StorageAdapter,
    StorageError,
    StorageUnavailable,
    StoredObject,
    clamp_signed_url_ttl,
)

def probe_object_storage(
    adapter: StorageAdapter, *, probe_key: str = "health/readiness"
) -> bool:
    """对象存储可达性探针（供 `/ready` 使用）。

    优先做**桶级**检查：桶不存在或凭据无权访问桶时，一次对象 `stat` 与一次连接失败
    在端口面上都表现为异常，区分不了。桶级能力由驱动自己提供（`bucket_available()`），
    这里用能力探测而不是给 `StorageAdapter` 端口加方法——端口的形状属于既有契约。

    「探针对象不存在」是正常状态（探针键本来就不该被写进桶里），因此 `ObjectNotFound`
    与 `exists() == False` 都算可达；只有 `StorageError`/`StorageUnavailable` 才表示
    依赖不可用，由调用方按未就绪处理。
    """

    bucket_available = getattr(adapter, "bucket_available", None)
    if callable(bucket_available):
        return bool(bucket_available())

    try:
        adapter.exists(probe_key)
    except ObjectNotFound:
        return True
    return True


__all__ = [
    "MAX_DOWNLOAD_URL_TTL",
    "MAX_UPLOAD_URL_TTL",
    "STORAGE_PROVIDER_LABELS",
    "InMemoryStorageAdapter",
    "ObjectMetadata",
    "ObjectNotFound",
    "SignedUrl",
    "StorageAdapter",
    "StorageError",
    "StorageUnavailable",
    "StoredObject",
    "probe_object_storage",
    "build_storage_adapter",
    "clamp_signed_url_ttl",
    "storage_provider_label",
]

#: `STORAGE_DRIVER`（小写）→ `media_assets.storage_provider`（大写枚举）
STORAGE_PROVIDER_LABELS: dict[str, str] = {
    "minio": "MINIO",
    "local": "LOCAL",
    "cos": "COS",
    "oss": "OSS",
    "s3": "S3",
}


def storage_provider_label(driver: str) -> str:
    """返回写入 `media_assets.storage_provider` 的大写枚举值。"""

    try:
        return STORAGE_PROVIDER_LABELS[driver.strip().lower()]
    except KeyError as exc:
        raise StorageUnavailable(f"未配置的对象存储驱动 {driver!r}。") from exc


def build_storage_adapter(
    *,
    driver: str,
    endpoint: str,
    bucket: str,
    region: str | None,
    access_key: str,
    secret_key: str,
    secure: bool = False,
) -> StorageAdapter:
    """按服务端配置装配驱动。

    V1.0 本地与测试只用 `minio`（`09-repository-boundary.md` 第 5 节：本地即 MinIO）。
    COS/OSS/S3 要等生产供应商与凭据在 LEAD-05 复核后选定，本卡不预留空壳实现，
    配置错了必须响亮失败而不是静默退回到某个默认驱动。
    """

    normalized = driver.strip().lower()

    if normalized == "minio":
        from app.storage.minio_adapter import MinioStorageAdapter

        return MinioStorageAdapter(
            endpoint=endpoint,
            access_key=access_key,
            secret_key=secret_key,
            bucket=bucket,
            secure=secure,
            region=region,
        )

    raise StorageUnavailable(
        f"对象存储驱动 {normalized!r} 尚未在 V1.0 实现；本地与测试请使用 STORAGE_DRIVER=minio。"
    )
