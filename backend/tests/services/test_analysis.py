"""`AnalysisService` 的边界与成本控制（「参考图片找灵感」）。

覆盖：能力开关、邮箱门槛、归属与 `purpose`/`status` 校验、送模型前的重编码降维与
元数据剥离、缓存回放不出网、供应商失败的分类型映射与锁释放、描述含私人信息时整段
不下发、以及闸门拒绝时绝不碰供应商。

全部依赖用内存替身：不启动 PostgreSQL/Redis/MinIO，**不出网**（看图实现用
`MockImageAnalyzer` 或可编程假适配器）。
"""

from __future__ import annotations

import io
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image

from app.errors.codes import ErrorCode
from app.errors.exceptions import ApiError
from app.providers.mock_analysis import MOCK_VISION_MODEL, MockImageAnalyzer
from app.providers.ports import (
    ImageDescriptionRequest,
    ImageDescriptionResult,
    ProviderCallError,
    ProviderErrorCategory,
    ProviderUsage,
)
from app.repositories.media import MediaAssetDraft
from app.services.analysis.guard import AnalysisLease, CachedDescription
from app.services.analysis.service import MAX_ANALYSIS_SIDE, AnalysisService
from app.services.assets.validation import compute_sha256
from app.storage import InMemoryStorageAdapter
from tests.services.fakes import make_user_row
from tests.storage.fakes import AssetUnitOfWork

CLOCK = lambda: datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)  # noqa: E731

OWNER = uuid.UUID("11223344-5566-7788-99aa-bbccddeeff00")
STRANGER = uuid.UUID("22223344-5566-7788-99aa-bbccddeeff00")


def png_bytes(*, size: tuple[int, int] = (8, 6), fmt: str = "PNG", exif_tags=None) -> bytes:
    buffer = io.BytesIO()
    image = Image.new("RGB", size, (10, 20, 30))
    save_kwargs = {"format": fmt}
    if exif_tags:
        exif = Image.Exif()
        for tag, value in exif_tags.items():
            exif[tag] = value
        save_kwargs["exif"] = exif.tobytes()
    image.save(buffer, **save_kwargs)
    return buffer.getvalue()


class RecordingGuard:
    """可编程闸门替身：记录动作顺序，可按需拒绝或返回缓存。"""

    def __init__(
        self,
        *,
        hit: CachedDescription | None = None,
        begin_error: Exception | None = None,
    ) -> None:
        self.hit = hit
        self.begin_error = begin_error
        self.actions: list[str] = []
        self.committed: tuple[str, str] | None = None

    def cached(self, *, user_id: uuid.UUID, sha256: str) -> CachedDescription | None:
        self.actions.append("cached")
        return self.hit

    def begin(self, *, user_id: uuid.UUID, sha256: str) -> AnalysisLease:
        self.actions.append("begin")
        if self.begin_error is not None:
            raise self.begin_error
        return AnalysisLease(cache_key="cache", lock_key="lock")

    def commit(self, lease: AnalysisLease, *, description: str, model: str) -> None:
        self.actions.append("commit")
        self.committed = (description, model)

    def rollback(self, lease: AnalysisLease) -> None:
        self.actions.append("rollback")

    @property
    def paid_calls(self) -> int:
        """真正跨过闸门出网的次数——本文件里几乎所有断言最终都在数这个。"""

        return self.actions.count("begin")


class ScriptedAnalyzer:
    """`ImageAnalyzer` 替身：返回给定结果或抛出给定异常，并记录收到的请求。"""

    def __init__(
        self,
        *,
        result: ImageDescriptionResult | None = None,
        error: Exception | None = None,
    ) -> None:
        self.result = result or ImageDescriptionResult(
            description="一个人背对镜头站在草地上", model="vision-test-1"
        )
        self.error = error
        self.requests: list[ImageDescriptionRequest] = []

    def describe_image(self, request: ImageDescriptionRequest) -> ImageDescriptionResult:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.result


def seed_asset(
    uow: AssetUnitOfWork,
    storage: InMemoryStorageAdapter,
    content: bytes,
    *,
    owner: uuid.UUID = OWNER,
    purpose: str = "REFERENCE",
    status: str = "READY",
    mime_type: str = "image/png",
    declared_sha256: str | None = None,
    declared_size: int | None = None,
) -> uuid.UUID:
    """落一条资产行 + 把字节放进内存对象存储（默认声明值与真实字节一致）。"""

    asset = uow.media.create(
        MediaAssetDraft(
            user_id=owner,
            asset_type={"SOURCE": "ORIGINAL", "REFERENCE": "REFERENCE", "RESULT": "RESULT"}[
                purpose
            ],
            purpose=purpose,
            storage_provider="MINIO",
            storage_key=f"assets/{owner}/{uuid.uuid4().hex}",
            mime_type=mime_type,
            size_bytes=declared_size if declared_size is not None else len(content),
            sha256=declared_sha256 if declared_sha256 is not None else compute_sha256(content),
            idempotency_key="synthetic-key",
            expires_at=CLOCK() + timedelta(days=7),
        )
    )
    storage.upload(asset.storage_key, content, mime_type)
    if status == "READY":
        try:
            with Image.open(io.BytesIO(content)) as opened:
                width, height = opened.size
        except Exception:  # noqa: BLE001 - 故意投坏字节的用例自己声明尺寸
            width, height = 8, 6
        uow.media.mark_ready(
            asset, mime_type=mime_type, size_bytes=len(content), width=width, height=height
        )
    else:
        uow.media.set_status(asset, status)
    return asset.id


def make_service(
    *,
    content: bytes | None = None,
    analyzer: object | None = None,
    guard: RecordingGuard | None = None,
    enabled: bool = True,
    purpose: str = "REFERENCE",
    status: str = "READY",
    owner: uuid.UUID = OWNER,
    declared_sha256: str | None = None,
    declared_size: int | None = None,
    mime_type: str = "image/png",
    verified: bool = True,
) -> tuple[AnalysisService, RecordingGuard, AssetUnitOfWork, InMemoryStorageAdapter, uuid.UUID]:
    uow = AssetUnitOfWork(CLOCK)
    storage = InMemoryStorageAdapter(clock=CLOCK)

    user = make_user_row()
    user.id = owner
    uow.users.seed(user)
    email = uow.emails.add(user_id=owner, email="owner@example.com", is_primary=True)
    if verified:
        uow.emails.mark_verified(email, CLOCK())

    asset_id = seed_asset(
        uow,
        storage,
        content if content is not None else png_bytes(),
        owner=owner,
        purpose=purpose,
        status=status,
        mime_type=mime_type,
        declared_sha256=declared_sha256,
        declared_size=declared_size,
    )
    guard = guard or RecordingGuard()
    service = AnalysisService(
        uow=uow,
        storage=storage,
        analyzer=analyzer or MockImageAnalyzer(),
        guard=guard,
        enabled=enabled,
    )
    return service, guard, uow, storage, asset_id


def call(service: AnalysisService, asset_id: uuid.UUID, *, user_id: uuid.UUID = OWNER):
    return service.describe_reference_image(user_id=user_id, asset_id=asset_id)


# ------------------------------------------------------------------ 门禁与归属
def test_disabled_capability_refuses_before_touching_anything() -> None:
    service, guard, _uow, _storage, asset_id = make_service(enabled=False)

    with pytest.raises(ApiError) as excinfo:
        call(service, asset_id)
    assert excinfo.value.code is ErrorCode.FEATURE_NOT_AVAILABLE
    assert guard.actions == []


def test_unverified_email_is_refused_because_analysis_costs_money() -> None:
    service, guard, _uow, _storage, asset_id = make_service(verified=False)

    with pytest.raises(ApiError) as excinfo:
        call(service, asset_id)
    assert excinfo.value.code is ErrorCode.EMAIL_NOT_VERIFIED
    assert guard.actions == []


def test_unknown_asset_is_404_not_403() -> None:
    service, _guard, _uow, _storage, _asset_id = make_service()

    with pytest.raises(ApiError) as excinfo:
        call(service, uuid.uuid4())
    assert excinfo.value.code is ErrorCode.ASSET_NOT_FOUND


def test_someone_elses_asset_is_refused() -> None:
    """另一个人**已经验证过邮箱**时才会走到归属判断——邮箱门禁在前，顺序不能反。"""

    service, _guard, uow, _storage, asset_id = make_service()
    stranger = make_user_row()
    stranger.id = STRANGER
    uow.users.seed(stranger)
    email = uow.emails.add(user_id=STRANGER, email="other@example.com", is_primary=True)
    uow.emails.mark_verified(email, CLOCK())

    with pytest.raises(ApiError) as excinfo:
        call(service, asset_id, user_id=STRANGER)
    assert excinfo.value.code is ErrorCode.ASSET_FORBIDDEN


def test_non_reference_purpose_is_refused_rather_than_silently_read() -> None:
    """看别人的**原图**和看参考图是两件事：声明与实际用途不符必须拒绝。"""

    service, guard, _uow, _storage, asset_id = make_service(purpose="SOURCE")

    with pytest.raises(ApiError) as excinfo:
        call(service, asset_id)
    assert excinfo.value.code is ErrorCode.VALIDATION_ERROR
    assert excinfo.value.details["fields"] == ["asset_id"]
    assert guard.actions == []


@pytest.mark.parametrize("status", ["PENDING_UPLOAD", "DELETED", "EXPIRED"])
def test_asset_must_be_ready(status: str) -> None:
    service, guard, _uow, _storage, asset_id = make_service(status=status)

    with pytest.raises(ApiError) as excinfo:
        call(service, asset_id)
    assert excinfo.value.code is ErrorCode.ASSET_NOT_READY
    assert guard.actions == []


def test_declared_digest_mismatch_is_rejected_before_the_model() -> None:
    """资产声明与实际字节不一致时，看图这条路径同样不放过——用的还是生成链路那套校验。"""

    content = png_bytes()
    service, guard, _uow, _storage, asset_id = make_service(
        content=content, declared_sha256="b" * 64
    )

    with pytest.raises(ApiError) as excinfo:
        call(service, asset_id)
    assert excinfo.value.code is ErrorCode.ASSET_INVALID
    assert guard.actions == []


def test_missing_object_maps_to_dependency_not_ready() -> None:
    service, _guard, uow, storage, asset_id = make_service()
    storage.delete(uow.media.get(asset_id).storage_key)

    with pytest.raises(ApiError) as excinfo:
        call(service, asset_id)
    assert excinfo.value.code is ErrorCode.NOT_READY


# ------------------------------------------------------------------ 正常路径
def test_happy_path_returns_the_description_and_settles_the_lease() -> None:
    analyzer = ScriptedAnalyzer()
    service, guard, _uow, _storage, asset_id = make_service(analyzer=analyzer)

    outcome = call(service, asset_id)

    assert outcome.description == "一个人背对镜头站在草地上"
    assert outcome.model == "vision-test-1"
    assert outcome.asset_id == asset_id
    assert outcome.from_cache is False
    assert guard.actions == ["cached", "begin", "commit"]
    assert guard.committed == ("一个人背对镜头站在草地上", "vision-test-1")
    assert len(analyzer.requests) == 1


def test_mock_analyzer_is_deterministic_per_image() -> None:
    """假适配器按图片摘要轮换描述：同一张图永远同一段，换图就变——测试才敢断言结果。"""

    first = png_bytes(size=(8, 6))
    second = png_bytes(size=(9, 7))
    service_a, _g_a, uow_a, _s_a, asset_a = make_service(content=first, analyzer=MockImageAnalyzer())
    service_b, _g_b, _uow_b, _s_b, asset_b = make_service(content=second, analyzer=MockImageAnalyzer())

    a = call(service_a, asset_a)
    b = call(service_b, asset_b)
    assert a.model == MOCK_VISION_MODEL
    assert uow_a.media.get(asset_a).purpose == "REFERENCE"
    assert a.description != b.description


# ------------------------------------------------------------------ 送模型前降维
def test_image_is_downscaled_to_the_side_cap_and_reencoded_as_jpeg() -> None:
    analyzer = ScriptedAnalyzer()
    service, _guard, _uow, _storage, asset_id = make_service(
        content=png_bytes(size=(2400, 1600)), analyzer=analyzer
    )

    call(service, asset_id)

    image = analyzer.requests[0].image
    assert image.mime_type == "image/jpeg"
    with Image.open(io.BytesIO(image.content)) as sent:
        assert max(sent.size) <= MAX_ANALYSIS_SIDE
        assert sent.format == "JPEG"
        # 长边按比例缩，不是拉成正方形（2400×1600 → 3:2）
        assert abs(sent.size[0] / sent.size[1] - 1.5) < 0.02


def test_payload_digest_matches_the_bytes_that_will_be_sent() -> None:
    analyzer = ScriptedAnalyzer()
    service, _guard, _uow, _storage, asset_id = make_service(
        content=png_bytes(size=(1600, 1600)), analyzer=analyzer
    )

    call(service, asset_id)

    image = analyzer.requests[0].image
    assert image.sha256 == compute_sha256(image.content)


def test_camera_metadata_is_stripped_before_leaving_the_process() -> None:
    """EXIF 里的厂商/机型/作者一类字段与用户身份绑在一起，没有理由替用户送到中转站。"""

    tagged = png_bytes(
        size=(64, 64),
        fmt="JPEG",
        exif_tags={0x010F: "SyntheticCamera GmbH", 0x0110: "MODEL-XYZ-9000"},
    )
    analyzer = ScriptedAnalyzer()
    service, _guard, _uow, _storage, asset_id = make_service(
        content=tagged, analyzer=analyzer, mime_type="image/jpeg"
    )

    call(service, asset_id)

    sent = analyzer.requests[0].image.content
    assert b"MODEL-XYZ-9000" not in sent
    assert b"SyntheticCamera" not in sent
    assert b"Exif" not in sent[:64]


def test_small_images_are_reencoded_too_because_size_is_not_the_only_risk() -> None:
    """够小就不动手的短路会把上面那条脱敏保证变成运气——所以一律重编码。"""

    original = png_bytes(size=(64, 64))
    analyzer = ScriptedAnalyzer()
    service, _guard, _uow, _storage, asset_id = make_service(content=original, analyzer=analyzer)

    call(service, asset_id)

    assert analyzer.requests[0].image.content != original


def test_output_token_cap_is_passed_through() -> None:
    """预算必须容得下"思考 + 一段正文"：300 在真实型号上会把正文截成半句。"""

    analyzer = ScriptedAnalyzer()
    service, _guard, _uow, _storage, asset_id = make_service(analyzer=analyzer)

    call(service, asset_id)

    assert analyzer.requests[0].max_output_tokens == 900
    assert analyzer.requests[0].language == "zh"


def test_undecodable_object_is_rejected_as_invalid_asset() -> None:
    """字节本身坏掉（校验阶段就拒）不得被包装成「供应商不可用」。"""

    service, guard, _uow, _storage, asset_id = make_service(
        content=b"\x89PNG\r\n\x1a\n this is not a decodable image"
    )

    with pytest.raises(ApiError) as excinfo:
        call(service, asset_id)
    assert excinfo.value.code is ErrorCode.ASSET_INVALID
    assert guard.actions == []


# ------------------------------------------------------------------ 缓存回放
def test_cache_hit_never_reaches_the_analyzer_or_the_lock() -> None:
    guard = RecordingGuard(hit=CachedDescription(description="缓存里的描述", model="vision-9"))
    analyzer = ScriptedAnalyzer()
    service, _guard, _uow, _storage, asset_id = make_service(analyzer=analyzer, guard=guard)

    outcome = call(service, asset_id)

    assert outcome.description == "缓存里的描述"
    assert outcome.from_cache is True
    assert guard.actions == ["cached"]
    assert analyzer.requests == []


def test_cache_hit_reports_the_model_that_actually_produced_it() -> None:
    guard = RecordingGuard(hit=CachedDescription(description="旧描述", model="vision-old"))
    service, _guard, _uow, _storage, asset_id = make_service(
        analyzer=ScriptedAnalyzer(), guard=guard
    )

    assert call(service, asset_id).model == "vision-old"


# ------------------------------------------------------------------ 供应商失败
@pytest.mark.parametrize(
    ("category", "expected"),
    [
        (ProviderErrorCategory.TIMEOUT, ErrorCode.PROVIDER_UNAVAILABLE),
        (ProviderErrorCategory.TEMPORARY, ErrorCode.PROVIDER_UNAVAILABLE),
        (ProviderErrorCategory.RATE_LIMIT, ErrorCode.PROVIDER_UNAVAILABLE),
        (ProviderErrorCategory.AUTHENTICATION, ErrorCode.PROVIDER_UNAVAILABLE),
        (ProviderErrorCategory.VALIDATION, ErrorCode.ASSET_INVALID),
        (ProviderErrorCategory.CONTENT_POLICY, ErrorCode.ASSET_INVALID),
        (ProviderErrorCategory.UNKNOWN, ErrorCode.PROVIDER_BAD_GATEWAY),
    ],
)
def test_provider_failures_map_onto_existing_codes(category, expected) -> None:
    error = ProviderCallError(
        category=category,
        code="synthetic",
        safe_message="合成安全消息",
        request_accepted=None,
    )
    service, guard, _uow, _storage, asset_id = make_service(
        analyzer=ScriptedAnalyzer(error=error)
    )

    with pytest.raises(ApiError) as excinfo:
        call(service, asset_id)
    assert excinfo.value.code is expected
    assert guard.actions == ["cached", "begin", "rollback"]


def test_unexpected_analyzer_exception_is_contained_and_releases_the_lock() -> None:
    service, guard, _uow, _storage, asset_id = make_service(
        analyzer=ScriptedAnalyzer(error=RuntimeError("boom"))
    )

    with pytest.raises(ApiError) as excinfo:
        call(service, asset_id)
    assert excinfo.value.code is ErrorCode.PROVIDER_BAD_GATEWAY
    # 异常原文（可能带内部信息）不得出现在响应 details 之外的任何地方
    assert "boom" not in str(excinfo.value.details)
    assert guard.actions == ["cached", "begin", "rollback"]


def test_gate_rejection_stops_before_the_provider_is_called() -> None:
    blocked = ApiError(ErrorCode.RATE_LIMITED)
    analyzer = ScriptedAnalyzer()
    service, guard, _uow, _storage, asset_id = make_service(
        analyzer=analyzer, guard=RecordingGuard(begin_error=blocked)
    )

    with pytest.raises(ApiError) as excinfo:
        call(service, asset_id)
    assert excinfo.value.code is ErrorCode.RATE_LIMITED
    assert analyzer.requests == []
    assert guard.actions == ["cached", "begin"]


# ------------------------------------------------------------------ 描述脱敏
@pytest.mark.parametrize(
    "text",
    [
        "画面里有一只猫，旁边写着 someone@example.com",
        "一张街景，路牌上有 13800001111",
        "背景是 https://evil.invalid/x 这样的字样",
        "桌面上能看到 C:\\Users\\nobody\\Desktop 的路径",
        "角落里的 seed=12345 被读出来了",
        "一行编号 0123456789abcdef0123456789abcdef",
        "一串长数字 123456789012345678",
    ],
)
def test_descriptions_carrying_private_info_are_withheld_wholesale(text: str) -> None:
    """模型输出不受我们控制：命中私人信息就整段不下发，而不是打码后放行。"""

    service, guard, _uow, _storage, asset_id = make_service(
        analyzer=ScriptedAnalyzer(
            result=ImageDescriptionResult(description=text, model="vision-test-1")
        )
    )

    with pytest.raises(ApiError) as excinfo:
        call(service, asset_id)
    assert excinfo.value.code is ErrorCode.PROVIDER_BAD_GATEWAY
    assert excinfo.value.details == {"reason": "description_withheld"}
    assert text not in str(excinfo.value.details)
    assert guard.actions == ["cached", "begin", "rollback"]
    assert guard.committed is None


def test_blank_description_cannot_cross_the_port_at_all() -> None:
    """端口层就拒绝空描述：`ImageDescriptionResult` 要求非空文本。"""

    with pytest.raises(ValueError):
        ImageDescriptionResult(description="   ", model="vision-test-1")


def test_description_that_normalizes_away_is_refused_not_returned_empty() -> None:
    """能穿过端口的「只有一对引号」在归一后变空——这种也不算成功，绝不回一段空文本。"""

    service, guard, _uow, _storage, asset_id = make_service(
        analyzer=ScriptedAnalyzer(
            result=ImageDescriptionResult(description='""', model="vision-test-1")
        )
    )

    with pytest.raises(ApiError) as excinfo:
        call(service, asset_id)
    assert excinfo.value.code is ErrorCode.PROVIDER_BAD_GATEWAY
    assert excinfo.value.details == {"reason": "description_withheld"}
    assert guard.committed is None


def test_markdown_and_line_breaks_are_flattened_into_one_paragraph() -> None:
    service, _guard, _uow, _storage, asset_id = make_service(
        analyzer=ScriptedAnalyzer(
            result=ImageDescriptionResult(
                description='  "一个人\n\n站在草地上，\n远山为背景"\n', model="vision-test-1"
            )
        )
    )

    outcome = call(service, asset_id)

    assert outcome.description == "一个人 站在草地上， 远山为背景"
    assert "\n" not in outcome.description


def test_usage_is_never_fabricated_when_the_provider_omits_it() -> None:
    """端口规定未上报保持 None（落库为 NULL）；这里确认服务不会替供应商编数字。"""

    result = ImageDescriptionResult(
        description="一片雪地里的剪影", model="vision-test-1", usage=None
    )
    service, _guard, _uow, _storage, asset_id = make_service(
        analyzer=ScriptedAnalyzer(result=result)
    )

    outcome = call(service, asset_id)

    assert outcome.model == "vision-test-1"
    assert not hasattr(outcome, "usage")


def test_provider_reported_usage_does_not_leak_into_the_outcome() -> None:
    """响应只给描述、型号与是否复用缓存：token 用量属于内部成本口径。"""

    service, _guard, _uow, _storage, asset_id = make_service(
        analyzer=ScriptedAnalyzer(
            result=ImageDescriptionResult(
                description="一张海报",
                model="vision-test-1",
                usage=ProviderUsage(input_tokens=1234, output_tokens=56),
            )
        )
    )

    outcome = call(service, asset_id)

    assert asdict(outcome) == {
        "asset_id": asset_id,
        "description": "一张海报",
        "model": "vision-test-1",
        "from_cache": False,
    }


def test_analysis_never_touches_points_or_tasks() -> None:
    """这条能力不建任务、不占点数、不进状态机：内存工作单元里除了读不该有任何写。"""

    uow = AssetUnitOfWork(CLOCK)
    storage = InMemoryStorageAdapter(clock=CLOCK)
    user = make_user_row()
    user.id = OWNER
    uow.users.seed(user)
    email = uow.emails.add(user_id=OWNER, email="owner@example.com", is_primary=True)
    uow.emails.mark_verified(email, CLOCK())
    asset_id = seed_asset(uow, storage, png_bytes())
    service = AnalysisService(
        uow=uow,
        storage=storage,
        analyzer=MockImageAnalyzer(),
        guard=RecordingGuard(),
        enabled=True,
    )
    uow.trace.clear()  # 播种阶段的写入与本用例无关

    outcome = call(service, asset_id)

    assert outcome.from_cache is False
    # 内存替身的所有写入都会进共享 `trace` 事件流；空流即证明这条路径一行都没写
    assert uow.trace == [], "分析路径不得产生任何仓储写入事件"
    assert uow.commits == 0
