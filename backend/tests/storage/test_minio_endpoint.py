"""MinIO 端点规范化测试（ENG-06 修复批次第 4 项）。

真实缺陷：`minio.Minio(endpoint)` 只接受**不带 scheme** 的 `host[:port]`，而
`app/config.py` 的默认值与 `docker/compose.yml` 给的都带 scheme（`http://minio:9000`）。
之前驱动把它原样交给 SDK，任何真实部署在构造适配器时就抛
`ValueError: path in endpoint is not allowed`——Web 进程与 Worker 都算「装配成功」，
直到第一次读写对象才炸。

这里冻结的是驱动的入参口径，不发网络请求：用替身替换 `minio.Minio`，
断言驱动交给 SDK 的到底是什么。

依据：`09-repository-boundary.md` 第 4 节（只有驱动模块允许导入对象存储 SDK）、
`05-system-architecture.md` 第 3 节（进程模型：装配失败必须发生在启动时而不是首次调用）。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.storage.ports import StorageUnavailable

#: 交给 SDK 的构造参数（endpoint, secure），由替身记录
RECORDED: list[tuple[str, bool]] = []


class MinioSpy:
    """`minio.Minio` 替身：只记录驱动交下来的 endpoint 与 secure。"""

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
        RECORDED.append((endpoint, secure))
        self.endpoint = endpoint
        self.secure = secure
        self.region = region


@pytest.fixture(autouse=True)
def spy(monkeypatch: pytest.MonkeyPatch):
    import minio

    RECORDED.clear()
    monkeypatch.setattr(minio, "Minio", MinioSpy)
    yield RECORDED
    RECORDED.clear()


def build(endpoint: str, *, secure: bool = False) -> Any:
    from app.storage.minio_adapter import MinioStorageAdapter

    return MinioStorageAdapter(
        endpoint=endpoint,
        access_key="synthetic-access-key",
        secret_key="synthetic-secret-key",
        bucket="ai-avatar",
        secure=secure,
        region="us-east-1",
    )


# --------------------------------------------------------------------------- #
# 必须兼容的三种写法
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("configured", "expected_endpoint", "expected_secure"),
    [
        # compose 与 config.py 的默认值：带 http scheme
        ("http://minio:9000", "minio:9000", False),
        # 生产常见的 https
        ("https://storage.example.com", "storage.example.com", True),
        ("https://storage.example.com:443", "storage.example.com:443", True),
        # SDK 原生的 host:port 写法必须继续可用
        ("minio:9000", "minio:9000", False),
        ("127.0.0.1:9000", "127.0.0.1:9000", False),
        # 尾斜杠是同一个端点，不是路径
        ("http://minio:9000/", "minio:9000", False),
        ("minio:9000/", "minio:9000", False),
        # 配置里的空白不应造成一次故障
        ("  http://minio:9000  ", "minio:9000", False),
        # 大写 scheme 与主机名大小写不敏感（DNS 主机名按小写处理）
        ("HTTP://Minio:9000", "minio:9000", False),
        # IPv6 必须带方括号：去掉就无法与端口区分
        ("http://[::1]:9000", "[::1]:9000", False),
        ("[::1]:9000", "[::1]:9000", False),
    ],
)
def test_supported_endpoint_shapes(
    spy: list[tuple[str, bool]], configured: str, expected_endpoint: str, expected_secure: bool
) -> None:
    build(configured)

    assert spy == [(expected_endpoint, expected_secure)]


def test_scheme_decides_tls_over_the_default_flag(
    spy: list[tuple[str, bool]]
) -> None:
    """配置写了 `https://` 就以它为准：让 `secure=False` 默认值把 TLS 关掉是静默降级。"""

    build("https://storage.example.com", secure=False)
    assert spy == [("storage.example.com", True)]


def test_without_scheme_the_explicit_secure_flag_is_kept(
    spy: list[tuple[str, bool]]
) -> None:
    build("storage.example.com", secure=True)
    assert spy == [("storage.example.com", True)]


def test_construction_still_does_not_open_a_connection(
    spy: list[tuple[str, bool]]
) -> None:
    """装配口径变了，但「构造不触网」这条进程模型约束不能变。"""

    adapter = build("http://minio:9000")
    assert adapter is not None
    assert len(spy) == 1


# --------------------------------------------------------------------------- #
# 必须拒绝的写法
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "configured",
    [
        "",
        "   ",
        # 路径：MinIO 的 endpoint 没有路径段，带上它一定是在配错的东西
        "http://minio:9000/bucket",
        "minio:9000/ai-avatar",
        # 查询与片段同理
        "http://minio:9000?versioning=1",
        "http://minio:9000#fragment",
        # 凭据属于 STORAGE_ACCESS_KEY/SECRET_KEY，不来自 URL
        "http://user:password@minio:9000",
        # 不是对象存储协议
        "ftp://minio:9000",
        "redis://redis:6379",
        # 有 scheme 却没有主机
        "http://",
        "http:///minio:9000",
    ],
)
def test_unusable_endpoints_fail_loudly_at_assembly(configured: str) -> None:
    """装配期就失败：绝不能静默退回某个默认端点，也绝不能拖到第一次读写对象。"""

    with pytest.raises(StorageUnavailable):
        build(configured)


def test_rejected_endpoint_does_not_fall_back_to_a_default() -> None:
    """拒绝时不能「顺手用一个看起来能用的地址」——那会把数据写进错误的桶。"""

    with pytest.raises(StorageUnavailable):
        build("http://minio:9000/bucket")
    assert RECORDED == []


def test_error_message_never_contains_the_credentials() -> None:
    """异常文本会进日志：端点里出现过凭据时不得把凭据原样带出去。"""

    with pytest.raises(StorageUnavailable) as raised:
        build("http://user:password@minio:9000")
    assert "password" not in str(raised.value)


# --------------------------------------------------------------------------- #
# 桶级可达性探针（供 /ready 使用，不引入 SDK 到上层）
# --------------------------------------------------------------------------- #
def test_bucket_available_is_a_driver_level_capability() -> None:
    """桶检查留在驱动里：`StorageAdapter` 端口不因就绪探针而扩张。"""

    from app.storage.minio_adapter import MinioStorageAdapter

    assert hasattr(MinioStorageAdapter, "bucket_available")


def test_probe_prefers_the_bucket_check_and_reports_reachability() -> None:
    from app.storage import probe_object_storage

    class Adapter:
        def __init__(self) -> None:
            self.bucket_calls = 0
            self.stat_calls = 0

        def bucket_available(self) -> bool:
            self.bucket_calls += 1
            return True

        def exists(self, key: str) -> bool:  # pragma: no cover - 不该被调用
            self.stat_calls += 1
            return False

    adapter = Adapter()
    assert probe_object_storage(adapter) is True
    assert (adapter.bucket_calls, adapter.stat_calls) == (1, 0)


def test_probe_falls_back_to_an_object_stat_for_adapters_without_bucket_api() -> None:
    """内存驱动没有桶概念：一次成功的 `stat` 同样证明可达，缺对象不算不可用。"""

    from app.storage.ports import ObjectNotFound
    from app.storage import probe_object_storage

    class StatOnlyAdapter:
        def __init__(self, *, raises: bool = False) -> None:
            self.raises = raises

        def exists(self, key: str) -> bool:
            if self.raises:
                raise ObjectNotFound("对象不存在。")
            return False

    assert probe_object_storage(StatOnlyAdapter()) is True
    assert probe_object_storage(StatOnlyAdapter(raises=True)) is True
