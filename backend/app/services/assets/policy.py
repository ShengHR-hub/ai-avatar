"""媒体资源领域常量与映射（ENG-05）。

- `PURPOSE_TO_ASSET_TYPE`：总控 2026-09-27 裁决冻结的固定映射，
  `SOURCE -> ORIGINAL`、`REFERENCE -> REFERENCE`、`RESULT -> RESULT`；
  `asset_type` 的既有含义不变（`02-database-design.md` 第 3.3 节）。
- 生命周期 TTL：`02-database-design.md` 第 3.3 节「原图/参考图默认 7 天，结果图默认 30 天」。
- 签名 URL 时长上限来自 `app.storage.ports`（读取 5 分钟、上传 10 分钟）。
"""

from __future__ import annotations

from datetime import timedelta

__all__ = [
    "DOWNLOAD_URL_TTL",
    "ORIGINAL_TTL",
    "PURPOSE_TO_ASSET_TYPE",
    "RESULT_TTL",
    "UPLOAD_URL_TTL",
]

#: 上传用途 -> 资源类型（逆映射同样唯一，供 0019 回填与只读推导使用）
PURPOSE_TO_ASSET_TYPE: dict[str, str] = {
    "SOURCE": "ORIGINAL",
    "REFERENCE": "REFERENCE",
    "RESULT": "RESULT",
}

#: 原图与参考图的二进制保留期
ORIGINAL_TTL = timedelta(days=7)

#: 结果图的二进制保留期
RESULT_TTL = timedelta(days=30)

#: 签名 URL 时长（不得超过 `MAX_UPLOAD_URL_TTL` / `MAX_DOWNLOAD_URL_TTL`）
UPLOAD_URL_TTL = timedelta(minutes=10)
DOWNLOAD_URL_TTL = timedelta(minutes=5)
