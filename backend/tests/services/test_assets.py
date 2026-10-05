"""Asset Service 与图片校验测试（ENG-05 第 2-4 步，**不依赖 PostgreSQL / MinIO**）。

覆盖总控 §7 要求：任务金额无关（本卡为资源）路径下的 purpose 映射、生命周期 TTL、
Feature Flag、邮箱验证、幂等回放与冲突、完成校验（存在性/哈希/解码/尺寸/动画/伪 MIME）、
所有权与 visibility 授权、短期签名、逻辑删除幂等、依赖故障映射与事务提交边界。
"""

from __future__ import annotations

import hashlib
import io
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image

from app.errors.codes import ErrorCode
from app.errors.exceptions import ApiError
from app.schemas.asset import UploadIntentRequest
from app.services.assets.policy import ORIGINAL_TTL, RESULT_TTL
from app.services.assets.service import AssetService, effective_expired
from app.services.assets.validation import (
    MAX_IMAGE_BYTES,
    MAX_IMAGE_SIDE,
    compute_sha256,
    sniff_format,
    validate_image,
)
from app.storage import InMemoryStorageAdapter
from tests.services.fakes import make_user_row, png_with_markup_in_metadata
from tests.storage.fakes import AssetUnitOfWork, make_media_asset

USER_ID = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
CLOCK = (lambda: datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc))


def make_image_bytes(
    *, fmt: str = "PNG", size: tuple[int, int] = (8, 6), frames: int = 1
) -> bytes:
    """生成真实可解码的合成图片（不含任何真实照片）。"""

    buffer = io.BytesIO()
    if frames > 1:
        first = Image.new("RGB", size, (10, 20, 30))
        frames_list = [Image.new("RGB", size, (c, c, c)) for c in range(frames)]
        frames_list[0].save(buffer, format=fmt, save_all=True, append_images=frames_list[1:])
        del first
    else:
        Image.new("RGB", size, (10, 20, 30)).save(buffer, format=fmt)
    return buffer.getvalue()


def intent_for(content: bytes, **overrides: object) -> UploadIntentRequest:
    payload = {
        "purpose": "SOURCE",
        "filename": "avatar.png",
        "mime_type": "image/png",
        "size_bytes": len(content),
        "sha256": compute_sha256(content),
    }
    payload.update({key: value for key, value in overrides.items() if value is not None})
    return UploadIntentRequest(**payload)


@pytest.fixture
def uow() -> AssetUnitOfWork:
    unit = AssetUnitOfWork(CLOCK)
    unit.users.seed(make_user_row())
    unit.emails.add(user_id=USER_ID, email="owner@example.com", is_primary=True)
    return unit


@pytest.fixture
def storage() -> InMemoryStorageAdapter:
    return InMemoryStorageAdapter(clock=CLOCK)


def _service(
    uow: AssetUnitOfWork, storage: InMemoryStorageAdapter, **overrides: object
) -> AssetService:
    params: dict[str, object] = {
        "uow": uow,
        "storage": storage,
        "storage_provider": "MINIO",
        "enable_reference_image": True,
        "clock": CLOCK,
    }
    params.update(overrides)
    return AssetService(**params)  # type: ignore[arg-type]


def _verified(uow: AssetUnitOfWork) -> None:
    uow.emails.mark_verified(uow.emails.find_primary(USER_ID), CLOCK())


# =========================================================================== #
# 图片校验
# =========================================================================== #
def test_validate_accepts_real_png_and_returns_observed_metadata() -> None:
    content = make_image_bytes(fmt="PNG", size=(8, 6))

    validated = validate_image(
        content,
        declared_mime_type="image/png",
        declared_size_bytes=len(content),
        declared_sha256=compute_sha256(content),
        filename_hint="photo.png",
    )

    assert (validated.mime_type, validated.width, validated.height) == (
        "image/png",
        8,
        6,
    )
    assert validated.size_bytes == len(content)


def test_validate_accepts_jpeg_and_webp() -> None:
    for fmt, mime in (("JPEG", "image/jpeg"), ("WEBP", "image/webp")):
        content = make_image_bytes(fmt=fmt)
        validated = validate_image(
            content,
            declared_mime_type=mime,
            declared_size_bytes=len(content),
            declared_sha256=compute_sha256(content),
        )
        assert validated.mime_type == mime


def test_validate_rejects_declared_mime_outside_allowlist() -> None:
    content = make_image_bytes()

    with pytest.raises(ApiError) as excinfo:
        validate_image(
            content,
            declared_mime_type="image/tiff",
            declared_size_bytes=len(content),
            declared_sha256=compute_sha256(content),
        )

    assert excinfo.value.code is ErrorCode.MEDIA_TYPE_UNSUPPORTED
    assert excinfo.value.status_code == 415


def test_validate_rejects_size_mismatch() -> None:
    content = make_image_bytes()

    with pytest.raises(ApiError) as excinfo:
        validate_image(
            content,
            declared_mime_type="image/png",
            declared_size_bytes=len(content) + 1,
            declared_sha256=compute_sha256(content),
        )

    assert excinfo.value.code is ErrorCode.ASSET_INVALID


def test_validate_rejects_digest_mismatch() -> None:
    content = make_image_bytes()

    with pytest.raises(ApiError) as excinfo:
        validate_image(
            content,
            declared_mime_type="image/png",
            declared_size_bytes=len(content),
            declared_sha256="b" * 64,
        )

    assert excinfo.value.code is ErrorCode.ASSET_INVALID
    assert excinfo.value.details == {"reason": "digest_mismatch"}


def test_validate_rejects_oversize_payload() -> None:
    content = make_image_bytes() + b"\x00" * MAX_IMAGE_BYTES

    with pytest.raises(ApiError) as excinfo:
        validate_image(
            content,
            declared_mime_type="image/png",
            declared_size_bytes=len(content),
            declared_sha256=compute_sha256(content),
        )

    assert excinfo.value.code is ErrorCode.UPLOAD_TOO_LARGE
    assert excinfo.value.status_code == 413


def test_validate_rejects_corrupt_pixels_despite_valid_signature() -> None:
    """只读头会被伪造文件骗过，必须完整解码。"""

    content = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]) + b"junk" * 6

    with pytest.raises(ApiError) as excinfo:
        validate_image(
            content,
            declared_mime_type="image/png",
            declared_size_bytes=len(content),
            declared_sha256=compute_sha256(content),
        )

    assert excinfo.value.code is ErrorCode.ASSET_INVALID
    assert excinfo.value.details["reason"] == "decode_failed"

def test_validate_rejects_animated_images() -> None:
    content = make_image_bytes(fmt="GIF", frames=3)

    with pytest.raises(ApiError) as excinfo:
        validate_image(
            content,
            declared_mime_type="image/png",
            declared_size_bytes=len(content),
            declared_sha256=compute_sha256(content),
        )

    assert excinfo.value.code is ErrorCode.ASSET_INVALID


def test_validate_rejects_svg_and_unknown_signatures() -> None:
    for payload in (b"<svg xmlns='http://www.w3.org/2000/svg'><circle/></svg>", b"not-an-image"):
        with pytest.raises(ApiError) as excinfo:
            validate_image(
                payload,
                declared_mime_type="image/png",
                declared_size_bytes=len(payload),
                declared_sha256=compute_sha256(payload),
            )
        assert excinfo.value.code is ErrorCode.ASSET_INVALID


def test_validate_accepts_a_real_png_whose_metadata_mentions_markup() -> None:
    """本体是位图、元数据里出现 `<svg` 文本必须放行。

    厂商真实出图会携带内嵌清单/缩略图文本，按原始字节关键字否决会把主链路整批判死：
    判定只能看容器结构与解码结果。
    """

    content = png_with_markup_in_metadata()

    validated = validate_image(
        content,
        declared_mime_type="image/png",
        declared_size_bytes=len(content),
        declared_sha256=compute_sha256(content),
    )

    assert validated.mime_type == "image/png"
    assert validated.size_bytes == len(content)
    assert validated.width > 0 and validated.height > 0


@pytest.mark.parametrize(
    "payload",
    [
        b"<svg xmlns='http://www.w3.org/2000/svg'><circle/></svg>",
        b"  \r\n<svg xmlns='http://www.w3.org/2000/svg'/>",
        b"\xef\xbb\xbf<?xml version='1.0'?><svg/>",
        b"<!DOCTYPE html><svg></svg>",
        b"GIF89a\x01\x00\x01\x00",
    ],
)
def test_validate_rejects_files_whose_body_actually_is_markup_or_another_container(
    payload: bytes,
) -> None:
    """文件**开头**就是标记语言/GIF 魔数时按结构拒绝，与「元数据里提到 svg」是两件事。"""

    with pytest.raises(ApiError) as excinfo:
        validate_image(
            payload,
            declared_mime_type="image/png",
            declared_size_bytes=len(payload),
            declared_sha256=compute_sha256(payload),
        )

    assert excinfo.value.code is ErrorCode.ASSET_INVALID
    assert excinfo.value.details == {"reason": "forbidden_format"}


def test_validate_rejects_extension_disagreement_hint() -> None:
    content = make_image_bytes(fmt="JPEG")

    with pytest.raises(ApiError) as excinfo:
        validate_image(
            content,
            declared_mime_type="image/jpeg",
            declared_size_bytes=len(content),
            declared_sha256=compute_sha256(content),
            filename_hint="payload.png",
        )

    assert excinfo.value.details == {"reason": "extension_disagreement"}


def test_validate_rejects_oversize_image_sides() -> None:
    """单边超过 4096 必须拒绝（用真实可解码图，不伪造解码器）。"""

    content = make_image_bytes(fmt="PNG", size=(MAX_IMAGE_SIDE + 1, 2))

    with pytest.raises(ApiError) as excinfo:
        validate_image(
            content,
            declared_mime_type="image/png",
            declared_size_bytes=len(content),
            declared_sha256=compute_sha256(content),
        )

    assert excinfo.value.details["reason"] == "dimension_too_large"


def test_validate_rejects_total_pixel_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """总像素上限同样按解码后的真实尺寸判定（这里压低常量以避免生成 16 MP 大图）。"""

    import app.services.assets.validation as validation

    monkeypatch.setattr(validation, "MAX_IMAGE_SIDE", 100_000)
    monkeypatch.setattr(validation, "MAX_IMAGE_PIXELS", 10)
    content = make_image_bytes(size=(8, 6))  # 48 px > 10

    with pytest.raises(ApiError) as excinfo:
        validate_image(
            content,
            declared_mime_type="image/png",
            declared_size_bytes=len(content),
            declared_sha256=compute_sha256(content),
        )

    assert excinfo.value.details["reason"] == "pixel_count_too_large"


def test_sniff_recovers_formats_and_rejects_unknown() -> None:
    assert sniff_format(make_image_bytes(fmt="PNG")) == "PNG"
    assert sniff_format(make_image_bytes(fmt="JPEG")) == "JPEG"
    assert sniff_format(make_image_bytes(fmt="WEBP")) == "WEBP"
    assert sniff_format(b"whatever") is None


def test_error_details_never_echo_content_or_filenames() -> None:
    content = make_image_bytes()

    with pytest.raises(ApiError) as excinfo:
        validate_image(
            content,
            declared_mime_type="image/png",
            declared_size_bytes=len(content),
            declared_sha256="c" * 64,
        )

    assert "secret-filename" not in str(excinfo.value.details)
    assert content not in str(excinfo.value.details).encode().split(b"b'")


# =========================================================================== #
# 上传意图
# =========================================================================== #
def test_upload_intent_creates_pending_row_with_mapped_type_and_ttl(uow, storage) -> None:
    _verified(uow)
    service = _service(uow, storage)
    content = make_image_bytes()

    outcome = service.create_upload_intent(
        user_id=USER_ID, intent=intent_for(content), idempotency_key="idem-1"
    )

    asset = outcome.asset
    assert asset.status == "PENDING_UPLOAD"
    assert asset.asset_type == "ORIGINAL" and asset.purpose == "SOURCE"
    assert asset.visibility == "PRIVATE"
    assert asset.storage_provider == "MINIO"
    assert asset.idempotency_key == "idem-1"
    assert asset.expires_at == CLOCK() + ORIGINAL_TTL
    assert (outcome.method, outcome.upload_url.startswith("https://")) == ("PUT", True)
    assert storage.signed_urls[0][1] == "PUT"
    assert uow.commits == 1


def test_upload_intent_maps_reference_purpose_and_rejects_flag_off(uow, storage) -> None:
    _verified(uow)
    content = make_image_bytes()
    intent = intent_for(content, purpose="REFERENCE")

    allowed = _service(uow, storage).create_upload_intent(
        user_id=USER_ID, intent=intent, idempotency_key="idem-ref"
    )
    assert allowed.asset.asset_type == "REFERENCE"
    assert allowed.asset.expires_at == CLOCK() + ORIGINAL_TTL

    blocked = _service(uow, storage, enable_reference_image=False)
    rows_before = len(uow.media.rows)

    with pytest.raises(ApiError) as excinfo:
        blocked.create_upload_intent(
            user_id=USER_ID, intent=intent, idempotency_key="idem-ref-2"
        )

    assert excinfo.value.code is ErrorCode.FEATURE_NOT_AVAILABLE
    # 必须在写任何数据之前拒绝，不能静默忽略参考图
    assert len(uow.media.rows) == rows_before
    assert uow.commits == 1


def test_upload_intent_requires_verified_email(uow, storage) -> None:
    with pytest.raises(ApiError) as excinfo:
        _service(uow, storage).create_upload_intent(
            user_id=USER_ID,
            intent=intent_for(make_image_bytes()),
            idempotency_key="idem-1",
        )

    assert excinfo.value.code is ErrorCode.EMAIL_NOT_VERIFIED
    assert excinfo.value.status_code == 403
    assert uow.media.rows == ()


def test_upload_intent_is_idempotent_per_user_and_key(uow, storage) -> None:
    _verified(uow)
    service = _service(uow, storage)
    content = make_image_bytes()

    first = service.create_upload_intent(
        user_id=USER_ID, intent=intent_for(content), idempotency_key="same"
    )
    second = service.create_upload_intent(
        user_id=USER_ID, intent=intent_for(content), idempotency_key="same"
    )

    assert second.asset.id == first.asset.id
    assert len(uow.media.rows) == 1
    assert uow.commits == 1  # 回放路径不再提交


def test_upload_intent_replay_re_signs_url_without_reusing_dead_one(uow, storage) -> None:
    _verified(uow)
    service = _service(uow, storage)
    content = make_image_bytes()

    first = service.create_upload_intent(
        user_id=USER_ID, intent=intent_for(content), idempotency_key="same"
    )
    second = service.create_upload_intent(
        user_id=USER_ID, intent=intent_for(content), idempotency_key="same"
    )

    assert first.upload_url != second.upload_url or storage.signed_urls[-1][2] <= 600
    assert second.expires_at == CLOCK() + timedelta(minutes=10)


def test_upload_intent_same_key_different_body_is_conflict(uow, storage) -> None:
    _verified(uow)
    service = _service(uow, storage)
    service.create_upload_intent(
        user_id=USER_ID, intent=intent_for(make_image_bytes()), idempotency_key="same"
    )

    other = make_image_bytes(size=(4, 4))
    with pytest.raises(ApiError) as excinfo:
        service.create_upload_intent(
            user_id=USER_ID, intent=intent_for(other), idempotency_key="same"
        )

    assert excinfo.value.code is ErrorCode.IDEMPOTENCY_CONFLICT
    assert len(uow.media.rows) == 1


def test_storage_unavailable_maps_to_not_ready(uow, storage) -> None:
    _verified(uow)
    service = _service(uow, storage)
    content = make_image_bytes()
    asset = service.create_upload_intent(
        user_id=USER_ID, intent=intent_for(content), idempotency_key="idem-1"
    ).asset

    storage.unavailable = True

    with pytest.raises(ApiError) as excinfo:
        service.complete_upload(user_id=USER_ID, asset_id=asset.id)

    assert excinfo.value.code is ErrorCode.NOT_READY
    assert excinfo.value.status_code == 503
    assert asset.status == "PENDING_UPLOAD"


# =========================================================================== #
# 完成上传
# =========================================================================== #
def _pending_asset(uow, storage, content: bytes, *, key: str = "idem-1"):
    _verified(uow)
    service = _service(uow, storage)
    outcome = service.create_upload_intent(
        user_id=USER_ID, intent=intent_for(content), idempotency_key=key
    )
    return service, outcome.asset


def test_complete_uploads_writes_observed_metadata_and_ready_status(uow, storage) -> None:
    content = make_image_bytes(fmt="PNG", size=(12, 9))
    service, asset = _pending_asset(uow, storage, content)
    storage.upload(asset.storage_key, content, "image/png")

    result = service.complete_upload(user_id=USER_ID, asset_id=asset.id)

    assert result.status == "READY"
    assert (result.width, result.height, result.size_bytes) == (12, 9, len(content))
    assert uow.commits == 2


def test_complete_is_idempotent_and_does_not_reread_object(uow, storage) -> None:
    content = make_image_bytes()
    service, asset = _pending_asset(uow, storage, content)
    storage.upload(asset.storage_key, content, "image/png")
    service.complete_upload(user_id=USER_ID, asset_id=asset.id)

    storage.unavailable = True  # 已 READY 的重复调用不应再触碰对象存储
    again = service.complete_upload(user_id=USER_ID, asset_id=asset.id)

    assert again.status == "READY"


def test_complete_before_object_arrives_is_asset_not_ready(uow, storage) -> None:
    content = make_image_bytes()
    service, asset = _pending_asset(uow, storage, content)

    with pytest.raises(ApiError) as excinfo:
        service.complete_upload(user_id=USER_ID, asset_id=asset.id)

    assert excinfo.value.code is ErrorCode.ASSET_NOT_READY
    assert excinfo.value.status_code == 409


def test_complete_rejects_tampered_object_failing_digest(uow, storage) -> None:
    content = make_image_bytes()
    service, asset = _pending_asset(uow, storage, content)
    storage.upload(asset.storage_key, make_image_bytes(size=(9, 9)), "image/png")

    with pytest.raises(ApiError) as excinfo:
        service.complete_upload(user_id=USER_ID, asset_id=asset.id)

    assert excinfo.value.code is ErrorCode.ASSET_INVALID
    assert asset.status == "PENDING_UPLOAD"


def test_complete_requires_ownership_and_rejects_missing_asset(uow, storage) -> None:
    content = make_image_bytes()
    service, asset = _pending_asset(uow, storage, content)
    stranger = uuid.uuid4()

    with pytest.raises(ApiError) as forbidden:
        service.complete_upload(user_id=stranger, asset_id=asset.id)
    with pytest.raises(ApiError) as missing:
        service.complete_upload(user_id=USER_ID, asset_id=uuid.uuid4())

    assert forbidden.value.code is ErrorCode.ASSET_FORBIDDEN
    assert missing.value.code is ErrorCode.ASSET_NOT_FOUND


def test_complete_locks_the_asset_row(uow, storage) -> None:
    content = make_image_bytes()
    service, asset = _pending_asset(uow, storage, content)

    # 对象还没上传也会先锁行：锁顺序发生在任何对象存储访问之前
    with pytest.raises(ApiError) as excinfo:
        service.complete_upload(user_id=USER_ID, asset_id=asset.id)

    assert excinfo.value.code is ErrorCode.ASSET_NOT_READY
    assert uow.media.lock_calls == [asset.id]


# =========================================================================== #
# 读取、签名与删除
# =========================================================================== #
def _ready_asset(uow, storage):
    content = make_image_bytes()
    service, asset = _pending_asset(uow, storage, content)
    storage.upload(asset.storage_key, content, "image/png")
    service.complete_upload(user_id=USER_ID, asset_id=asset.id)
    return service, asset, content


def test_owner_can_read_and_download(uow, storage) -> None:
    service, asset, _ = _ready_asset(uow, storage)

    read = service.get_asset(asset_id=asset.id, viewer_user_id=USER_ID)
    signed = service.create_download_url(asset_id=asset.id, viewer_user_id=USER_ID)

    assert read.id == asset.id
    assert signed.url
    assert signed.expires_at == CLOCK() + timedelta(minutes=5)


def test_non_owner_cannot_see_private_asset_without_leaking_it(uow, storage) -> None:
    service, asset, _ = _ready_asset(uow, storage)

    with pytest.raises(ApiError) as excinfo:
        service.get_asset(asset_id=asset.id, viewer_user_id=uuid.uuid4())

    assert excinfo.value.code is ErrorCode.ASSET_NOT_FOUND
    assert excinfo.value.status_code == 404


def test_deleted_asset_is_invisible_and_download_url_requires_ready(uow, storage) -> None:
    service, asset, _ = _ready_asset(uow, storage)
    storage.delete(asset.storage_key)
    service.delete_asset(user_id=USER_ID, asset_id=asset.id)

    with pytest.raises(ApiError) as read:
        service.get_asset(asset_id=asset.id, viewer_user_id=USER_ID)
    assert read.value.code is ErrorCode.ASSET_NOT_FOUND

    pending_service, pending = _pending_asset(
        uow, storage, make_image_bytes(), key="idem-pending-2"
    )
    # （上面这行会用新键，见 _pending_asset 的 key 参数）
    storage.upload(pending.storage_key, make_image_bytes(), "image/png")
    with pytest.raises(ApiError) as url:
        pending_service.create_download_url(asset_id=pending.id, viewer_user_id=USER_ID)
    assert url.value.code is ErrorCode.ASSET_NOT_READY


def test_delete_removes_object_and_logically_deletes_row(uow, storage) -> None:
    service, asset, _ = _ready_asset(uow, storage)

    assert service.delete_asset(user_id=USER_ID, asset_id=asset.id) is True

    assert asset.status == "DELETED"
    assert asset.deleted_at == CLOCK()
    assert storage.exists(asset.storage_key) is False
    # 元数据保留，不物理删除
    assert uow.media.get(asset.id) is asset


def test_delete_is_idempotent_and_still_forbids_strangers(uow, storage) -> None:
    service, asset, _ = _ready_asset(uow, storage)
    service.delete_asset(user_id=USER_ID, asset_id=asset.id)

    assert service.delete_asset(user_id=USER_ID, asset_id=asset.id) is False
    with pytest.raises(ApiError) as stranger:
        service.delete_asset(user_id=uuid.uuid4(), asset_id=asset.id)
    assert stranger.value.code is ErrorCode.ASSET_FORBIDDEN
    with pytest.raises(ApiError) as ghost:
        service.delete_asset(user_id=USER_ID, asset_id=uuid.uuid4())
    assert ghost.value.code is ErrorCode.ASSET_NOT_FOUND


def test_lifecycle_expiry_is_reported_without_being_written(uow, storage) -> None:
    service, asset, _ = _ready_asset(uow, storage)
    asset.expires_at = CLOCK() - timedelta(seconds=1)

    assert effective_expired(asset, now=CLOCK()) is True
    read = service.get_asset(asset_id=asset.id, viewer_user_id=USER_ID)

    # 读路径不写库：持久化 EXPIRED 属生命周期任务（ENG-09）
    assert read.status == "READY"


def test_result_purpose_ttl_is_thirty_days() -> None:
    assert RESULT_TTL == timedelta(days=30)
    assert ORIGINAL_TTL == timedelta(days=7)


def test_sha256_helper_matches_hashlib() -> None:
    assert compute_sha256(b"abc") == hashlib.sha256(b"abc").hexdigest()


def test_asset_dto_never_exposes_server_side_facts(uow, storage) -> None:
    from app.services.assets.mappers import to_api_asset

    service, asset, _ = _ready_asset(uow, storage)
    payload = to_api_asset(asset).model_dump()

    assert {"storage_key", "storage_provider", "idempotency_key", "user_id"}.isdisjoint(payload)
    assert {"asset_type", "purpose", "status", "visibility", "sha256"} <= set(payload)


# --------------------------------------------------------------------------- #
# 容器边界（尾随内容 / polyglot）
# --------------------------------------------------------------------------- #
_TAILS = (
    b"<svg xmlns='http://www.w3.org/2000/svg'><script/></svg>",
    b"<!doctype html><script>alert(1)</script>",
    b"PK\x03\x04" + b"\x00" * 26,
)


def validated(content: bytes, mime: str = "image/png"):
    return validate_image(
        content,
        declared_mime_type=mime,
        declared_size_bytes=len(content),
        declared_sha256=compute_sha256(content),
    )


def assert_trailing_content(content: bytes, mime: str = "image/png") -> None:
    with pytest.raises(ApiError) as excinfo:
        validated(content, mime)

    assert excinfo.value.code is ErrorCode.ASSET_INVALID
    assert excinfo.value.details == {"reason": "trailing_content"}


@pytest.mark.parametrize("tail", _TAILS)
def test_bytes_appended_past_the_png_boundary_are_rejected(tail: bytes) -> None:
    """`IEND` 之后还挂着字节 = polyglot：图能解码也不放行。"""

    assert_trailing_content(make_image_bytes() + tail)


def test_bytes_appended_past_the_jpeg_eoi_are_rejected() -> None:
    """接在 `EOI` 之后的第二段 JPEG 不能因为「文件末尾也是 FFD9」被放过。"""

    jpeg = make_image_bytes(fmt="JPEG")
    assert_trailing_content(jpeg + jpeg, "image/jpeg")


def test_bytes_appended_past_the_webp_declared_length_are_rejected() -> None:
    """RIFF 声明长度之外还有内容：解码器只看声明长度，尾巴却是别人的攻击面。"""

    assert_trailing_content(make_image_bytes(fmt="WEBP") + b"PK\x03\x04" + b"\x00" * 26, "image/webp")


@pytest.mark.parametrize(
    ("fmt", "mime"),
    [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")],
)
def test_plain_bitmaps_end_exactly_at_their_own_boundary(fmt: str, mime: str) -> None:
    """边界规则不得误杀正常编码产物：三种格式的成品都刚好收在容器末尾。"""

    validated(make_image_bytes(fmt=fmt), mime)


def test_progressive_and_lossless_variants_still_meet_their_boundary() -> None:
    """多扫描渐进 JPEG 与无损 WebP 的 marker/长度推进必须算到真正的末尾。"""

    buffer = io.BytesIO()
    Image.new("RGB", (48, 48), (9, 9, 9)).save(buffer, format="JPEG", progressive=True)
    progressive = buffer.getvalue()
    buffer = io.BytesIO()
    Image.new("RGB", (48, 48), (9, 9, 9)).save(buffer, format="WEBP", lossless=True)
    lossless = buffer.getvalue()

    validated(progressive, "image/jpeg")
    validated(lossless, "image/webp")


def test_a_segment_may_carry_another_jpeg_without_looking_like_trailing_content() -> None:
    """EXIF/注释段里嵌着整张缩略图（内含 `FF D9`）时，按段长度跳过而不是搜关键字。

    这条同时证明两件事：合法图不被误杀，且判定确实是「沿结构走」。
    """

    outer = make_image_bytes(fmt="JPEG")
    inner = make_image_bytes(fmt="JPEG")
    stitched = (
        outer[:2]
        + b"\xff\xfe"
        + (len(inner) + 2).to_bytes(2, "big")
        + inner
        + outer[2:]
    )

    validated(stitched, "image/jpeg")


def test_truncated_png_is_rejected_as_an_unterminated_container() -> None:
    """缺了 `IEND` 的 PNG 不能当「合法图 + 零尾随」放过：边界定位失败另有专名。"""

    png = make_image_bytes()
    with pytest.raises(ApiError) as excinfo:
        validated(png[: len(png) - 12])

    assert excinfo.value.code is ErrorCode.ASSET_INVALID
    assert excinfo.value.details == {"reason": "unterminated_container"}
