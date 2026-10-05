"""「参考图片找灵感」相关服务端配置的读取口径。

这三项决定了「能力开不开」与「一天最多花多少钱」，默认值必须是**关与少**；
非法值要响亮失败，而不是默默用默认值顶上（否则一份写错的 `.env` 会变成失控的账单）。
"""

from __future__ import annotations

import pytest

from app.config import ConfigError, load_settings, reset_settings_cache

REQUIRED = {"DATABASE_URL": "postgresql+psycopg://ai_avatar:placeholder@db:5432/ai_avatar"}


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch):
    """给一个干净的服务端环境变量底稿，用例只覆盖自己关心的那几项。"""

    for name, value in REQUIRED.items():
        monkeypatch.setenv(name, value)
    for name in (
        "ENABLE_IMAGE_ANALYSIS",
        "ANALYSIS_DAILY_LIMIT_PER_USER",
        "ANALYSIS_GLOBAL_DAILY_LIMIT",
        "AI_VISION_MODEL",
        "AI_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)
    reset_settings_cache()
    yield monkeypatch
    reset_settings_cache()


def test_defaults_are_off_and_conservative(env) -> None:  # noqa: ANN001
    settings = load_settings()

    assert settings.enable_image_analysis is False
    assert settings.analysis_daily_limit_per_user == 10
    assert settings.analysis_global_daily_limit == 50
    assert settings.ai_vision_model == ""


def test_vision_model_is_a_separate_slot_from_the_generation_model(env) -> None:
    """看图与出图是两个型号：读不到 `AI_VISION_MODEL` 时绝不能拿 `AI_MODEL` 顶上。"""

    env.setenv("AI_MODEL", "gpt-image-2.5-flare")
    env.setenv("AI_VISION_MODEL", "some-vision-model")

    settings = load_settings()

    assert settings.ai_model == "gpt-image-2.5-flare"
    assert settings.ai_vision_model == "some-vision-model"

    env.setenv("AI_VISION_MODEL", "")
    assert load_settings().ai_vision_model == ""


def test_capability_flag_accepts_the_usual_boolean_spellings(env) -> None:
    for value in ("true", "TRUE", "1", "yes", "on"):
        env.setenv("ENABLE_IMAGE_ANALYSIS", value)
        assert load_settings().enable_image_analysis is True, value
    for value in ("false", "0", "no", "off"):
        env.setenv("ENABLE_IMAGE_ANALYSIS", value)
        assert load_settings().enable_image_analysis is False, value


def test_non_boolean_capability_flag_fails_loudly(env) -> None:
    env.setenv("ENABLE_IMAGE_ANALYSIS", "maybe")

    with pytest.raises(ConfigError, match="ENABLE_IMAGE_ANALYSIS"):
        load_settings()


@pytest.mark.parametrize("name", ["ANALYSIS_DAILY_LIMIT_PER_USER", "ANALYSIS_GLOBAL_DAILY_LIMIT"])
@pytest.mark.parametrize("value", ["0", "-5", "many", "1.5"])
def test_quota_limits_must_be_positive_integers(env, name: str, value: str) -> None:
    """留空按「未设置」处理并回退默认值；这里给的是**设错了**的值，必须报错。"""

    env.setenv(name, value)

    with pytest.raises(ConfigError, match=name):
        load_settings()


def test_quota_limits_are_read_from_the_environment(env) -> None:
    env.setenv("ANALYSIS_DAILY_LIMIT_PER_USER", "3")
    env.setenv("ANALYSIS_GLOBAL_DAILY_LIMIT", "40")

    settings = load_settings()

    assert settings.analysis_daily_limit_per_user == 3
    assert settings.analysis_global_daily_limit == 40
