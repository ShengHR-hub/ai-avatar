"""MinIO 驱动签名 URL 测试（ENG-07 跨卡修复第 2 项）。

真实缺陷：minio-py 的 `Minio.presigned_put_object(bucket_name, object_name, expires=...)`
**没有** `content_type` 形参，而驱动把声明的 MIME 当关键字传了下去。于是每一次签发
浏览器直传 URL 都在 SDK 边界炸成 `TypeError`，被驱动的兜底分支归一成
`StorageUnavailable`，`POST /assets` 恒 `503 NOT_READY`——上传链路从来没能真正走通过。

替身刻意采用与真实 SDK **完全一致**的签名（不留 `**kwargs` 余地），驱动再多传一个参数
就会立刻复现这个 `TypeError`。不发任何网络请求。
"""

from __future__ import annotations

import inspect
from datetime import timedelta
from typing import Any

import pytest

from app.storage.ports import SignedUrl

BUCKET = "ai-avatar"
KEY = "assets/user-1/object-1"


class PresignClientSpy:
    """`minio.Minio` 替身：签名与真实 SDK 逐字对齐，并记录驱动交下来的实参。"""

    calls: list[tuple[str, dict[str, Any]]] = []

    def __init__(
        self,
        endpoint: str,
        *,
        access_key: str = "",
        secret_key: str = "",
        secure: bool = True,
        region: str | None = None,
        **_ignored: Any,
    ) -> None:
        self.endpoint = endpoint

    def presigned_put_object(
        self, bucket_name: str, object_name: str, expires: timedelta = timedelta(days=7)
    ) -> str:
        PresignClientSpy.calls.append(
            ("PUT", {"bucket_name": bucket_name, "object_name": object_name, "expires": expires})
        )
        return f"https://minio.invalid/{bucket_name}/{object_name}?method=PUT"

    def presigned_get_object(
        self, bucket_name: str, object_name: str, expires: timedelta = timedelta(days=7)
    ) -> str:
        PresignClientSpy.calls.append(
            ("GET", {"bucket_name": bucket_name, "object_name": object_name, "expires": expires})
        )
        return f"https://minio.invalid/{bucket_name}/{object_name}?method=GET"


@pytest.fixture(autouse=True)
def spy(monkeypatch: pytest.MonkeyPatch) -> type[PresignClientSpy]:
    import minio

    PresignClientSpy.calls = []
    monkeypatch.setattr(minio, "Minio", PresignClientSpy)
    yield PresignClientSpy
    PresignClientSpy.calls = []


def build() -> Any:
    from app.storage.minio_adapter import MinioStorageAdapter

    return MinioStorageAdapter(
        endpoint="http://minio:9000",
        access_key="placeholder",
        secret_key="placeholder",
        bucket=BUCKET,
    )


def test_put_presign_mints_a_url_without_extra_sdk_arguments(spy: type[PresignClientSpy]) -> None:
    """回归锚点：签发 PUT URL 必须成功，且不得给 SDK 添它不认识的参数。"""

    signed = build().create_signed_url(KEY, "PUT", timedelta(minutes=10), "image/png")

    assert isinstance(signed, SignedUrl)
    assert signed.url == f"https://minio.invalid/{BUCKET}/{KEY}?method=PUT"
    assert signed.expires_at is not None
    assert spy.calls == [
        ("PUT", {"bucket_name": BUCKET, "object_name": KEY, "expires": timedelta(minutes=10)})
    ]


def test_get_presign_is_untouched_by_the_put_fix(spy: type[PresignClientSpy]) -> None:
    signed = build().create_signed_url(KEY, "GET", timedelta(minutes=5))

    assert signed.url == f"https://minio.invalid/{BUCKET}/{KEY}?method=GET"
    assert spy.calls == [
        ("GET", {"bucket_name": BUCKET, "object_name": KEY, "expires": timedelta(minutes=5)})
    ]


def test_every_presign_argument_exists_in_the_real_sdk_signature(
    spy: type[PresignClientSpy],
) -> None:
    """把「驱动实参 ⊆ 真实 SDK 形参」冻成断言，SDK 升版漂移时立刻暴露。"""

    from minio import Minio

    adapter = build()
    adapter.create_signed_url(KEY, "PUT", timedelta(minutes=10), "image/png")
    adapter.create_signed_url(KEY, "GET", timedelta(minutes=5))

    assert [method for method, _ in spy.calls] == ["PUT", "GET"]
    for method, call in spy.calls:
        signature = inspect.signature(
            getattr(Minio, "presigned_put_object" if method == "PUT" else "presigned_get_object")
        )
        for name in call:
            assert name in signature.parameters, f"{method} 传了 SDK 不接受的 {name}"
