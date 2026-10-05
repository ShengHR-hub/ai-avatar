"""E2E 专用 ASGI 入口：**真实的 `app.main:app`** + 唯一的测试替身（邮件端口）。

## 为什么需要它

`EmailPort` 是 ENG-03 冻结的端口，但**没有任何本地实现**：
`app/services/ports.py::get_email_port()` 返回 `UnavailableEmailPort`，任何真实进程里
`POST /api/v1/auth/email/verification` 都是 `503 NOT_READY`；而契约
`03-api-contract.md` 第 12 节第 2 项把「邮件供应商」明确列为**未决项**。
没有可用的邮件传输，本地闭环就走不到「邮箱已验证 → 上传」这一步。

本模块是**测试代码**（在 `tests/` 下、不进镜像、不进生产代码路径），做法与既有
`tests/api/test_auth_endpoints.py` 完全一致：`create_app()` 之后只覆盖
`get_email_port_dep` 一个依赖，其余路由、中间件、异常处理器、配置、数据库、
Redis、对象存储全部是真实实现。

**必须如实标注**：这条闭环里"邮件送达"是替身（写本地文件），其余全部真实。
如果哪天实现了真实邮件供应商，这个入口应当直接删除。

## 用法

```bash
ENG10_MAIL_DIR=D:/tmp/eng10-run/mail \
DATABASE_URL=... REDIS_URL=... STORAGE_* ... \
python -m uvicorn tests.e2e.local_server:app --host 127.0.0.1 --port 8100
```
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from app.api.deps import get_email_port_dep
from app.main import create_app

__all__ = ["FileEmailSink", "app"]


class FileEmailSink:
    """把「已发出的验证邮件」写到本地目录，供 E2E 读取令牌。

    只落盘 `to_email`、`token`、`expires_in_seconds` 与时间戳 —— 与
    `InMemoryEmailSink` 记录的内容一致，只是换成文件（跨进程可读）。
    目录由 `ENG10_MAIL_DIR` 指定，默认落在系统临时目录，**不写进仓库**。
    """

    def __init__(self, out_dir: Path) -> None:
        self._out_dir = out_dir
        self._out_dir.mkdir(parents=True, exist_ok=True)

    def send_verification_email(
        self, *, to_email: str, token: str, expires_in_seconds: int
    ) -> None:
        payload: dict[str, Any] = {
            "to_email": to_email,
            "token": token,
            "expires_in_seconds": expires_in_seconds,
            "sent_at": time.time(),
        }
        name = f"{int(time.time() * 1000)}-{abs(hash(to_email)) % 10**8}.json"
        (self._out_dir / name).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )


_OUT_DIR = Path(os.environ.get("ENG10_MAIL_DIR", Path(os.environ.get("TEMP", ".")) / "eng10-mail"))
_SINK = FileEmailSink(_OUT_DIR)

app = create_app()
app.dependency_overrides[get_email_port_dep] = lambda: _SINK
