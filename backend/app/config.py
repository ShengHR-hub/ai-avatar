"""服务端配置读取（ENG-02 第 1 步：服务端环境变量全集）。

边界（见 `docs/architecture/09-repository-boundary.md` 第 6.1 节）：

- 只读取服务端环境变量（由 FastAPI / Worker / Beat 注入），不读取任何前端变量；
- 高敏感值（`DATABASE_URL`、`STORAGE_SECRET_KEY`、`AI_API_KEY`、`SESSION_SECRET`）
  禁止写入日志、错误响应或 OpenAPI；
- 本模块只做配置读取与校验，不创建引擎、不建立连接、不实现业务逻辑；
- 本地默认值与 `docker/compose.yml` 的 `${VAR:-default}` 保持一致
  （ENG-00 第 3 步裁决：Compose 默认值是唯一默认值来源）；
- `DATABASE_URL` **没有默认值**：缺失即抛 `ConfigError`，避免静默连到错误的数据库。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

__all__ = [
    "APP_ENVS",
    "DATABASE_URL_ENV",
    "STORAGE_DRIVER_VALUES",
    "ConfigError",
    "Settings",
    "get_database_url",
    "get_settings",
    "load_settings",
    "reset_settings_cache",
]

DATABASE_URL_ENV = "DATABASE_URL"

#: 运行环境标识（`docker/compose.yml` 默认 `local`）
APP_ENVS: tuple[str, ...] = ("local", "test", "production")

#: `STORAGE_DRIVER` 环境变量取值，**小写**
#: （`docker/compose.yml` 默认 `minio`、`docker/.env.example` 亦为小写）。
#: 注意与 `02-database-design.md` 第 3.3 节 `media_assets.storage_provider` 的
#: 大写枚举（`LOCAL/MINIO/COS/OSS/S3`）区分：由 Storage Adapter 在 ENG-05 负责映射。
STORAGE_DRIVER_VALUES: tuple[str, ...] = ("local", "minio", "cos", "oss", "s3")

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


class ConfigError(RuntimeError):
    """缺少或非法必需服务端配置。异常信息不得包含配置值。"""


def _read(name: str, default: str) -> str:
    """读取字符串型配置；空字符串视为未设置，回退到默认值。"""

    raw = os.environ.get(name, "")
    stripped = raw.strip()
    return stripped if stripped else default


def _read_optional(name: str) -> str:
    """读取可空字符串型配置（例如仅在启用真实 Provider 时才填写的 `AI_*`）。"""

    return os.environ.get(name, "").strip()


def _read_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in _TRUE_VALUES:
        return True
    if raw in _FALSE_VALUES:
        return False
    raise ConfigError(f"服务端环境变量 {name} 必须是布尔值（true/false）。")


def _read_choice(name: str, default: str, allowed: tuple[str, ...]) -> str:
    value = _read(name, default)
    if value not in allowed:
        # 只回显变量名与允许集合，不回显实际取值以外的高敏感信息
        raise ConfigError(f"服务端环境变量 {name} 必须是以下之一：{', '.join(allowed)}。")
    return value


def _read_positive_int(name: str, default: int) -> int:
    """读取正整数型配置。

    付费额度这类值一旦配成 0 或负数，语义是「全部放行」还是「全部拒绝」完全说不清，
    所以这里不接受非正数；非数字同样响亮失败，而不是默默用默认值顶上去。
    """

    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"服务端环境变量 {name} 必须是整数。") from exc
    if value <= 0:
        raise ConfigError(f"服务端环境变量 {name} 必须是正整数。")
    return value


def get_database_url() -> str:
    """返回 SQLAlchemy 数据库连接串。

    只从服务端环境变量 `DATABASE_URL` 读取；未配置或为空时抛出 `ConfigError`。
    异常信息只包含变量名，不回显取值，避免凭据进入日志或错误响应。
    """

    value = os.environ.get(DATABASE_URL_ENV, "").strip()
    if not value:
        raise ConfigError(
            f"缺少服务端环境变量 {DATABASE_URL_ENV}。"
            "请以 docker/.env.example 为模板生成本地配置，并在启动进程时注入。"
        )
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    """服务端配置快照；字段与 `09-repository-boundary.md` 第 6.1 节逐项对应。"""

    app_env: str
    app_base_url: str
    database_url: str
    redis_url: str
    storage_driver: str
    storage_endpoint: str
    storage_bucket: str
    storage_region: str
    storage_access_key: str
    storage_secret_key: str
    ai_provider: str
    ai_base_url: str
    ai_model: str
    ai_api_key: str
    #: 图片描述（参考图分析）用的视觉模型；与出图模型分开配置，
    #: 因为两者能力与计价都不同，混用一个型号会同时弄坏两边。
    ai_vision_model: str
    #: 「参考图片找灵感」能力开关。默认关：它会出网付费，必须由部署方显式打开。
    #: 与 `enable_reference_image` 无关——后者管的是把参考图当**生成条件**的
    #: `REFERENCE_IMAGE` 模式（上游能力未证实，继续关闭）。
    enable_image_analysis: bool
    #: 每用户每日分析次数上限（成本闸门，按 UTC 日历日计）
    analysis_daily_limit_per_user: int
    #: 全站每日分析次数上限（第二道闸门：单个脚本刷爆余额之前先整体兜住）
    analysis_global_daily_limit: int
    session_secret: str
    enable_reference_image: bool

    @property
    def is_local(self) -> bool:
        return self.app_env == "local"

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


def load_settings() -> Settings:
    """从环境变量读取一份新的配置快照（不缓存）。"""

    return Settings(
        app_env=_read_choice("APP_ENV", "local", APP_ENVS),
        app_base_url=_read("APP_BASE_URL", "http://localhost:8000"),
        database_url=get_database_url(),
        redis_url=_read("REDIS_URL", "redis://redis:6379/0"),
        storage_driver=_read_choice("STORAGE_DRIVER", "minio", STORAGE_DRIVER_VALUES),
        storage_endpoint=_read("STORAGE_ENDPOINT", "http://minio:9000"),
        storage_bucket=_read("STORAGE_BUCKET", "ai-avatar"),
        storage_region=_read("STORAGE_REGION", "us-east-1"),
        storage_access_key=_read("STORAGE_ACCESS_KEY", "placeholder"),
        storage_secret_key=_read("STORAGE_SECRET_KEY", "placeholder"),
        ai_provider=_read("AI_PROVIDER", "mock"),
        ai_base_url=_read_optional("AI_BASE_URL"),
        ai_model=_read_optional("AI_MODEL"),
        ai_api_key=_read_optional("AI_API_KEY"),
        ai_vision_model=_read_optional("AI_VISION_MODEL"),
        enable_image_analysis=_read_bool("ENABLE_IMAGE_ANALYSIS", False),
        analysis_daily_limit_per_user=_read_positive_int(
            "ANALYSIS_DAILY_LIMIT_PER_USER", 10
        ),
        analysis_global_daily_limit=_read_positive_int(
            "ANALYSIS_GLOBAL_DAILY_LIMIT", 50
        ),
        session_secret=_read("SESSION_SECRET", "placeholder"),
        enable_reference_image=_read_bool("ENABLE_REFERENCE_IMAGE", False),
    )


_settings: Settings | None = None


def get_settings() -> Settings:
    """返回进程级配置快照；首次调用时读取环境变量并缓存。"""

    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings


def reset_settings_cache() -> None:
    """清空配置缓存，供测试在修改环境变量后重新读取。"""

    global _settings
    _settings = None
