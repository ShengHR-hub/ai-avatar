"""服务端图片校验（ENG-05 第 3 步）。

契约来源：`04-ai-provider.md` 第 8 节与 `03-api-contract.md` 第 4.2 节：

- 只允许静态图 `image/jpeg`、`image/png`、`image/webp`；
- 客户端 MIME 与文件名扩展名**只当提示**，必须用文件签名与解码器结果交叉核对；
- 必须完整解码（不是只读头部），因此拒绝截断、畸形与 polyglot 文件；
- polyglot 按**容器边界**判：`IEND` / `EOI` / `RIFF` 声明长度之后还挂着字节就拒，
  尾巴是否被解码器读到与安全性无关，不能被别处当第二种格式再解析一次；
- 拒绝 SVG、GIF/动画、未知格式、零/非法尺寸、单边 > 4096、总像素 > 16 MP。
  拒与不拒一律按**容器结构 + 解码结果**判定：文件开头是标记语言、魔数不在白名单、
  解码器与签名不一致、解码报错、多帧，才算坏图；容器内部的元数据文本不参与否决，
  否则携带内嵌清单/缩略图的真实位图会被整批误杀；
- SHA-256 必须对**收到的原始字节**计算并与声明值比对；哈希是完整性证据，不是信任或授权。

错误映射只用既有稳定错误码（`03-api-contract.md` 第 7 节），不新增：

| 情形 | 错误码 | HTTP |
|---|---|---|
| 声明 MIME 不在允许集合 | `MEDIA_TYPE_UNSUPPORTED` | 415 |
| 超过大小上限 | `UPLOAD_TOO_LARGE` | 413 |
| 哈希、签名、解码、尺寸、扩展名、动画等其它校验失败 | `ASSET_INVALID` | 422 |

原始字节只在内存中短暂存在：**不得写日志、不得入库**（`09-repository-boundary.md` 第 7 节）。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageOps, UnidentifiedImageError

from app.errors.codes import ErrorCode
from app.errors.exceptions import ApiError

__all__ = [
    "ALLOWED_MIME_TYPES",
    "MAX_IMAGE_BYTES",
    "MAX_IMAGE_PIXELS",
    "MAX_IMAGE_SIDE",
    "ValidatedImage",
    "compute_sha256",
    "validate_image",
]

#: 允许的类型（`04-ai-provider.md` 第 8 节第 2 条）
ALLOWED_MIME_TYPES: frozenset[str] = frozenset({"image/jpeg", "image/png", "image/webp"})

#: 单图字节上限、单边与总像素上限（`04-ai-provider.md` 第 8 节第 1、3 条）
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_SIDE = 4096
MAX_IMAGE_PIXELS = 16_000_000

_MIME_BY_PIL_FORMAT = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
}

#: 解码器格式 -> 允许的文件扩展名提示（大小写不敏感）
_EXTENSIONS_BY_FORMAT = {
    "JPEG": {".jpg", ".jpeg"},
    "PNG": {".png"},
    "WEBP": {".webp"},
}

_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "JPEG"),
    (b"\x89PNG\r\n\x1a\n", "PNG"),
)

#: 容器**开头**就是标记语言 = 这张图的本体是 SVG/HTML，不是可解码的位图。
#: 判定只看文件从哪里开始，不看它内部携带了什么文本：真实位图的元数据盒里
#: 出现 `<svg`（内嵌缩略图、清单等）是合法的，关键字全文扫描会把好图判死。
_MARKUP_LEADINGS: tuple[bytes, ...] = (b"<?xml", b"<!doctype", b"<!entity", b"<svg", b"<html")

#: GIF 的魔数会伪装成「可识别容器」，`sniff_format` 认不出它已足够拒掉，
#: 这里再独立兜一层，免得以后有人放宽白名单时把这条路走通。
_FORBIDDEN_MAGICS: tuple[bytes, ...] = (b"GIF87a", b"GIF89a")


def _starts_with_markup(content: bytes) -> bool:
    """文件去掉前导空白与 UTF-8 BOM 后是否以标记语言开始。"""

    probe = content.lstrip()
    if probe.startswith(b"\xef\xbb\xbf"):
        probe = probe[3:]
    return probe[:16].lower().startswith(_MARKUP_LEADINGS)


def compute_sha256(content: bytes) -> str:
    """对原始字节计算十六进制 SHA-256（小写）。"""

    return hashlib.sha256(content).hexdigest()


def _invalid(detail: str) -> ApiError:
    """422 `ASSET_INVALID`；detail 只含类别词，不含文件名、字节或哈希值。"""

    return ApiError(ErrorCode.ASSET_INVALID, details={"reason": detail})


@dataclass(frozen=True, slots=True)
class ValidatedImage:
    """校验通过后的观测元数据（覆盖客户端声明值）。"""

    mime_type: str
    size_bytes: int
    width: int
    height: int


def sniff_format(content: bytes) -> str | None:
    """按文件签名识别格式；识别不出返回 `None`。"""

    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG"
    if content.startswith(b"\xff\xd8\xff"):
        return "JPEG"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "WEBP"
    return None


def _png_end(content: bytes) -> int | None:
    """沿 PNG 块链走到 `IEND`，返回容器真实结束偏移；读不懂返回 `None`。"""

    offset = len(b"\x89PNG\r\n\x1a\n")
    while offset + 12 <= len(content):
        length = int.from_bytes(content[offset : offset + 4], "big")
        kind = content[offset + 4 : offset + 8]
        end = offset + 12 + length
        if end > len(content):
            # 声明长度越出文件：截断或伪造的块头
            return None
        if kind == b"IEND":
            return end
        offset = end
    return None


def _jpeg_scan_end(content: bytes, start: int) -> int | None:
    """扫过一段熵编码数据，停在下一个真实 marker 的起始处。

    熵数据里 `FF00` 是字节填充、`FFD0`–`FFD7` 是 RST 重启标记，都不算段结束；
    其余 `FFxx` 才是真正的下一个 marker（多扫描渐进图就是靠它续到下一段 SOS）。
    """

    offset = start
    while offset + 1 < len(content):
        if content[offset] != 0xFF:
            offset += 1
            continue
        follower = content[offset + 1]
        if follower == 0x00 or 0xD0 <= follower <= 0xD7:
            offset += 2
            continue
        return offset
    return None


def _jpeg_end(content: bytes) -> int | None:
    """沿 JPEG marker 段走到 `EOI`（`FF D9`），返回容器真实结束偏移。

    必须按段头声明的长度跳过 payload：EXIF/缩略图里嵌着另一张完整 JPEG，
    用 `find`/`rfind` 搜 `FF D9` 要么在缩略图里误结束，要么被「图片后再接一张图片」骗过。
    """

    offset = 2  # 跳过 SOI
    while offset + 1 < len(content):
        if content[offset] != 0xFF:
            return None
        marker = content[offset + 1]
        if marker == 0xD9:
            return offset + 2
        if marker == 0x00:
            # `FF00` 只允许出现在熵数据里，出现在段层说明结构不对
            return None
        if marker == 0xFF:  # 连续填充字节
            offset += 1
            continue
        if marker == 0x01 or marker == 0xD8 or 0xD0 <= marker <= 0xD7:
            offset += 2
            continue
        if offset + 4 > len(content):
            return None
        length = int.from_bytes(content[offset + 2 : offset + 4], "big")
        if length < 2 or offset + 2 + length > len(content):
            return None
        offset += 2 + length
        if marker == 0xDA:  # SOS：payload 不是段，按熵数据扫过去
            resume = _jpeg_scan_end(content, offset)
            if resume is None:
                return None
            offset = resume
    return None


def _webp_end(content: bytes) -> int | None:
    """WebP 边界由 `RIFF` 头声明的长度给出（`8 + 声明长度`），填充字节也计在长度内。"""

    if len(content) < 12:
        return None
    return 8 + int.from_bytes(content[4:8], "little")


def _container_end(content: bytes, sniffed: str) -> int | None:
    """容器自声明的结束偏移；无法定位视为畸形。"""

    if sniffed == "PNG":
        return _png_end(content)
    if sniffed == "JPEG":
        return _jpeg_end(content)
    if sniffed == "WEBP":
        return _webp_end(content)
    return None


def validate_image(
    content: bytes,
    *,
    declared_mime_type: str,
    declared_size_bytes: int,
    declared_sha256: str,
    filename_hint: str = "",
) -> ValidatedImage:
    """完整校验一张静态图，失败按契约错误码抛出 `ApiError`。"""

    if declared_mime_type not in ALLOWED_MIME_TYPES:
        raise ApiError(ErrorCode.MEDIA_TYPE_UNSUPPORTED)

    if not content:
        raise _invalid("empty_payload")

    if len(content) > MAX_IMAGE_BYTES:
        raise ApiError(ErrorCode.UPLOAD_TOO_LARGE)

    if len(content) != declared_size_bytes:
        # 声明大小与实际字节不一致：不得用任何一侧作为事实
        raise _invalid("size_mismatch")

    if compute_sha256(content) != declared_sha256.lower():
        raise _invalid("digest_mismatch")

    sniffed = sniff_format(content)

    # 判定只看「这个容器是什么」，不看「原始字节里出现过什么词」：
    # 标记语言只在文件开头才是 SVG/HTML 本体；合法位图的元数据里带 `<svg` 文本
    # （内嵌清单/缩略图）必须放行，否则会把厂商真实出图整批判死。
    if _starts_with_markup(content) or content[:6] in _FORBIDDEN_MAGICS:
        raise _invalid("forbidden_format")

    if sniffed is None:
        raise _invalid("unrecognised_signature")

    try:
        with Image.open(BytesIO(content)) as opened:
            decoded_format = opened.format
            if decoded_format != sniffed:
                # 解码器看到的格式与签名不一致 = polyglot 或畸形文件
                raise _invalid("format_disagreement")
            if getattr(opened, "n_frames", 1) > 1 or getattr(opened, "is_animated", False):
                raise _invalid("animated_image")
            opened.load()  # 强制完整解码：截断数据在这里抛错
            oriented = ImageOps.exif_transpose(opened) or opened
            width, height = oriented.size
    except ApiError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
        # 解码失败、截断、像素数据非法等一律 ASSET_INVALID，不外泄异常文本
        raise _invalid("decode_failed") from exc

    # 边界核对放在完整解码之后：解码器负责判定结构是否畸形（截断走 decode_failed），
    # 这里只回答另一件事——容器自我声明的末尾之后不该再有字节，容器也必须真正收尾。
    # ZIP/EXE/第二段 HTML 接在 IEND/EOI/RIFF 长度之后是典型 polyglot：图片本身能解码，
    # 解码器也看不见尾巴，一旦被别处当第二种格式再读一次就变成攻击面。
    container_end = _container_end(content, sniffed)
    if container_end is None:
        raise _invalid("unterminated_container")
    if container_end != len(content):
        raise _invalid("trailing_content")

    if width <= 0 or height <= 0:
        raise _invalid("zero_dimensions")
    if width > MAX_IMAGE_SIDE or height > MAX_IMAGE_SIDE:
        raise _invalid("dimension_too_large")
    if width * height > MAX_IMAGE_PIXELS:
        raise _invalid("pixel_count_too_large")

    mime_type = _MIME_BY_PIL_FORMAT.get(decoded_format or "")
    if mime_type is None or mime_type != sniffed_mime(sniffed):
        raise _invalid("unsupported_decoded_format")

    if filename_hint:
        suffix = filename_hint[filename_hint.rfind(".") :].lower() if "." in filename_hint else ""
        allowed = _EXTENSIONS_BY_FORMAT.get(sniffed or "", set())
        if suffix and suffix not in allowed:
            # 扩展名只是提示，但与真实格式冲突时说明客户端声明自相矛盾，按拒绝处理
            raise _invalid("extension_disagreement")

    return ValidatedImage(
        mime_type=mime_type,
        size_bytes=len(content),
        width=int(width),
        height=int(height),
    )


def sniffed_mime(sniffed: str | None) -> str | None:
    """签名格式 -> MIME。"""

    return _MIME_BY_PIL_FORMAT.get(sniffed or "")
