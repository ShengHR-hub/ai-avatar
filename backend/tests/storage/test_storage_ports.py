"""`StorageAdapter` 端口与内存驱动测试（ENG-05 第 1 步）。

覆盖：签名 URL 时长上限（读取 5 分钟 / 上传 10 分钟，可缩短不可放宽）、
方法白名单、幂等删除、对象不存在与依赖不可用的异常区分。
真实 MinIO 连通性不在本卡范围（禁止启动对象存储）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.storage import (
    MAX_DOWNLOAD_URL_TTL,
    MAX_UPLOAD_URL_TTL,
    InMemoryStorageAdapter,
    ObjectNotFound,
    StorageAdapter,
    StorageUnavailable,
    clamp_signed_url_ttl,
    storage_provider_label,
)
from app.storage.ports import SignedUrl


def _clock() -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc)


# ------------------------------------------------------------------ TTL 上限
def test_upload_ttl_boundary_is_accepted() -> None:
    assert clamp_signed_url_ttl("PUT", MAX_UPLOAD_URL_TTL) == timedelta(minutes=10)


def test_download_ttl_boundary_is_accepted() -> None:
    assert clamp_signed_url_ttl("GET", MAX_DOWNLOAD_URL_TTL) == timedelta(minutes=5)


def test_callers_may_shorten_ttl() -> None:
    assert clamp_signed_url_ttl("GET", timedelta(seconds=30)) == timedelta(seconds=30)


@pytest.mark.parametrize(
    ("method", "ttl"),
    [
        ("PUT", MAX_UPLOAD_URL_TTL + timedelta(seconds=1)),
        ("GET", MAX_DOWNLOAD_URL_TTL + timedelta(seconds=1)),
        ("GET", timedelta(hours=1)),
    ],
)
def test_ttl_over_limit_is_rejected(method: str, ttl: timedelta) -> None:
    """超限是服务端编程错误：必须抛内部异常，不得静默截断成上限值。"""

    with pytest.raises(ValueError):
        clamp_signed_url_ttl(method, ttl)


@pytest.mark.parametrize("method", ["DELETE", "POST", "get", ""])
def test_unknown_method_is_rejected(method: str) -> None:
    with pytest.raises(ValueError):
        clamp_signed_url_ttl(method, timedelta(minutes=1))


@pytest.mark.parametrize("ttl", [timedelta(0), timedelta(seconds=-1)])
def test_non_positive_ttl_is_rejected(ttl: timedelta) -> None:
    with pytest.raises(ValueError):
        clamp_signed_url_ttl("GET", ttl)


# ------------------------------------------------------------------ 驱动语义
def test_memory_adapter_satisfies_protocol() -> None:
    assert isinstance(InMemoryStorageAdapter(clock=_clock), StorageAdapter)


def test_memory_upload_read_and_exists_round_trip() -> None:
    storage = InMemoryStorageAdapter(clock=_clock)
    content = b"synthetic-bytes"

    stored = storage.upload("assets/u1/k1", content, "image/png")
    read_back, metadata = storage.read("assets/u1/k1")

    assert stored.size_bytes == len(content)
    assert read_back == content
    assert metadata.size_bytes == len(content)
    assert metadata.content_type == "image/png"
    assert storage.exists("assets/u1/k1") is True
    assert storage.exists("assets/u1/missing") is False


def test_memory_read_missing_object_raises_object_not_found() -> None:
    storage = InMemoryStorageAdapter(clock=_clock)

    with pytest.raises(ObjectNotFound):
        storage.read("assets/u1/nope")


def test_memory_delete_is_idempotent() -> None:
    storage = InMemoryStorageAdapter(clock=_clock)
    storage.upload("k", b"x", "image/png")

    storage.delete("k")
    storage.delete("k")  # 第二次也必须静默成功
    storage.delete("never-existed")

    assert storage.keys() == ()
    assert storage.delete_calls == ["k", "k", "never-existed"]


def test_memory_signed_url_records_no_url_and_honours_ttl() -> None:
    storage = InMemoryStorageAdapter(clock=_clock)

    signed: SignedUrl = storage.create_signed_url("k", "PUT", timedelta(minutes=2))

    assert signed.expires_at == _clock() + timedelta(minutes=2)
    assert storage.signed_urls == [("k", "PUT", 120)]
    # 记录里只有 key/方法/秒数，不含 URL（URL 即凭据）
    assert all("https" not in str(entry) for entry in storage.signed_urls)


def test_memory_unavailable_is_not_object_not_found() -> None:
    """依赖故障必须区别于「对象不存在」，否则会被误判成校验失败。"""

    storage = InMemoryStorageAdapter(clock=_clock)
    storage.unavailable = True

    with pytest.raises(StorageUnavailable):
        storage.exists("k")
    with pytest.raises(StorageUnavailable):
        storage.delete("k")


# ------------------------------------------------------------------ 驱动装配
@pytest.mark.parametrize(
    ("driver", "label"),
    [
        ("minio", "MINIO"),
        ("MINIO", "MINIO"),
        ("  minio  ", "MINIO"),
        ("local", "LOCAL"),
        ("cos", "COS"),
        ("oss", "OSS"),
        ("s3", "S3"),
    ],
)
def test_storage_provider_label_maps_lowercase_env_to_db_enum(
    driver: str, label: str
) -> None:
    """`STORAGE_DRIVER` 是小写，`media_assets.storage_provider` 是大写枚举。"""

    assert storage_provider_label(driver) == label


def test_unknown_driver_label_fails_loudly() -> None:
    with pytest.raises(StorageUnavailable):
        storage_provider_label("wasabi")


def test_unimplemented_driver_is_not_silently_defaulted() -> None:
    from app.storage import build_storage_adapter

    with pytest.raises(StorageUnavailable):
        build_storage_adapter(
            driver="s3",
            endpoint="example",
            bucket="b",
            region=None,
            access_key="a",
            secret_key="s",
        )
