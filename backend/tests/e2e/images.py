"""合成图片工具：**不含任何真实用户图片**（ENG-10 E2E）。

本模块只定义两条合成素材通道：

1. `synthetic_png()` —— 进程内直接生成 PNG 字节（`conftest.py` 之前用 Pillow
   的定义保持兼容，这里改为纯标准库，避免 E2E 依赖 Pillow）；
2. `synthetic_image_file()` —— 落盘成真实文件，供 `DOM.setFileInputFiles`
   把路径交给 `input[type=file]`（浏览器上传必须走真实文件）。

图片内容是确定性的渐变色块：同一参数永远同一字节，用例可以据此断言 SHA-256。
"""

from __future__ import annotations

import hashlib
import struct
import zlib
from pathlib import Path

__all__ = ["synthetic_image_file", "synthetic_png", "sha256_hex"]

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _chunk(tag: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + tag
        + payload
        + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
    )


def synthetic_png(width: int = 64, height: int = 64, seed: int = 0) -> bytes:
    """生成一张确定性的 RGB PNG（无外部依赖、无真实素材）。"""

    if width <= 0 or height <= 0:
        raise ValueError("宽高必须是正整数。")

    rows = bytearray()
    for y in range(height):
        rows.append(0)  # 过滤器类型：None
        for x in range(width):
            rows.extend(
                (
                    (x * 4 + seed * 37) % 256,
                    (y * 4 + seed * 53) % 256,
                    (x + y + seed * 11) % 256,
                )
            )
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        _PNG_SIGNATURE
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(bytes(rows), 9))
        + _chunk(b"IEND", b"")
    )


def sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def synthetic_image_file(
    directory: Path, name: str, *, width: int = 64, height: int = 64, seed: int = 0
) -> Path:
    """把一张合成 PNG 写到 `directory/name` 并返回路径（已存在则覆盖）。"""

    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(synthetic_png(width, height, seed))
    return path
