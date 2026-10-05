"""健康检查测试（ENG-02 第 3 步）。

覆盖：

- `/api/v1/health/live` 存活检查与成功 envelope；
- `/api/v1/health/ready` 依赖检查的成功、失败与 Worker 心跳过期场景（全部通过注入实现，不触网）；
- 检查函数的可注入性、异常安全与 60 秒阈值边界；
- `APP_ENV=production` 关闭交互式文档端点。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.api.health.checks as checks_module
from app.api.health.checks import (
    DEPENDENCY_NAMES,
    WORKER_HEARTBEAT_THRESHOLD_SECONDS,
    CheckResult,
    default_dependency_checks,
    make_check,
    make_object_storage_probe,
    make_postgresql_probe,
    make_redis_probe,
    make_worker_heartbeat_check,
    worker_heartbeat_result,
)
from app.middleware.request_id import REQUEST_ID_HEADER

NOW = datetime(2026, 9, 26, 0, 0, 0, tzinfo=timezone.utc)


def _ready(name: str):
    return lambda: CheckResult(name, True)


def _not_ready(name: str, detail: str = "synthetic failure"):
    return lambda: CheckResult(name, False, detail)


def _wire_fake_providers(
    monkeypatch: pytest.MonkeyPatch,
    *,
    heartbeat_age_seconds: float | None = 0,
    broken: str | None = None,
    missing_cost_modes: tuple[str, ...] = (),
) -> list[str]:
    """把 `checks` 的五个 provider 入口换成替身，并记录它们被问到的顺序。

    返回值用来证明「默认检查集合逐项问了五个真依赖」；`broken` 指定哪个依赖抛异常，
    `missing_cost_modes` 用来模拟「迁移完成但没有定价」。
    本函数只替换装配点，探针逻辑仍是产品代码里的真身。
    """

    calls: list[str] = []

    class _Connection:
        def __enter__(self) -> _Connection:
            return self

        def __exit__(self, *_exc: object) -> bool:
            return False

        def execute(self, _statement: object) -> None:
            if broken == "postgresql":
                raise RuntimeError("synthetic postgresql down")

    class _Engine:
        def connect(self) -> _Connection:
            return _Connection()

    class _Port:
        def get(self, _name: str) -> None:
            if broken == "redis":
                raise ConnectionError("synthetic redis down")
            return None

        def ping(self) -> bool:
            if broken == "redis":
                raise ConnectionError("synthetic redis down")
            return True

    class _Adapter:
        def bucket_available(self) -> bool:
            calls.append("object_storage")
            return broken != "object_storage"

        def exists(self, _key: str) -> bool:  # pragma: no cover - 有桶级能力时不该被用
            calls.append("object_storage")
            return broken != "object_storage"

    def _postgresql_engine() -> _Engine:
        calls.append("postgresql")
        return _Engine()

    def _redis_port() -> _Port:
        calls.append("redis")
        return _Port()

    def _storage_adapter() -> _Adapter:
        return _Adapter()

    def _heartbeat_timestamp() -> datetime | None:
        calls.append("worker_heartbeat")
        if heartbeat_age_seconds is None:
            return None
        return datetime.now(timezone.utc) - timedelta(seconds=heartbeat_age_seconds)

    def _cost_config_probe() -> tuple[str, ...]:
        calls.append("generation_costs")
        if broken == "generation_costs":
            raise RuntimeError("synthetic cost probe failure")
        return missing_cost_modes

    monkeypatch.setattr(checks_module, "_postgresql_engine", _postgresql_engine)
    monkeypatch.setattr(checks_module, "_redis_port", _redis_port)
    monkeypatch.setattr(checks_module, "_storage_adapter", _storage_adapter)
    monkeypatch.setattr(checks_module, "_heartbeat_timestamp", _heartbeat_timestamp)
    monkeypatch.setattr(checks_module, "_cost_config_probe", _cost_config_probe)
    return calls


# --------------------------------------------------------------------------- #
# liveness
# --------------------------------------------------------------------------- #
def test_liveness_returns_success_envelope(client: TestClient) -> None:
    response = client.get("/api/v1/health/live")

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"data", "request_id"}
    # 基线 HealthEnvelope 的 data 含可选 status 字段
    assert payload["data"] == {"status": "ok"}
    assert response.headers[REQUEST_ID_HEADER] == payload["request_id"]


def test_liveness_does_not_require_idempotency_key(client: TestClient) -> None:
    assert client.get("/api/v1/health/live").status_code == 200


# --------------------------------------------------------------------------- #
# readiness：注入式成功 / 失败
# --------------------------------------------------------------------------- #
def test_readiness_returns_200_when_all_checks_ready(
    client: TestClient, override_checks
) -> None:
    override_checks([_ready(name) for name in DEPENDENCY_NAMES])

    response = client.get("/api/v1/health/ready")

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"data", "request_id"}
    assert payload["data"] == {"status": "ok"}


def test_readiness_returns_503_not_ready_when_one_dependency_fails(
    client: TestClient, override_checks
) -> None:
    override_checks(
        [
            _ready("postgresql"),
            _not_ready("redis", "connection refused"),
            _ready("object_storage"),
            _ready("worker_heartbeat"),
            _ready("generation_costs"),
        ]
    )

    response = client.get("/api/v1/health/ready")

    assert response.status_code == 503
    payload = response.json()
    assert payload["error"]["code"] == "NOT_READY"
    assert payload["error"]["message"]
    reports = payload["error"]["details"]["dependencies"]
    assert [report["name"] for report in reports] == list(DEPENDENCY_NAMES)
    assert [report["ready"] for report in reports] == [True, False, True, True, True]
    assert reports[1]["detail"] == "connection refused"
    assert response.headers[REQUEST_ID_HEADER] == payload["request_id"]


def test_readiness_reports_worker_heartbeat_expired(
    client: TestClient, override_checks
) -> None:
    """Worker 心跳过期场景：阈值 60 秒，61 秒未上报即未就绪。"""

    override_checks(
        [
            _ready("postgresql"),
            _ready("redis"),
            _ready("object_storage"),
            make_worker_heartbeat_check(
                lambda: NOW - timedelta(seconds=WORKER_HEARTBEAT_THRESHOLD_SECONDS + 1),
                now=lambda: NOW,
            ),
        ]
    )

    response = client.get("/api/v1/health/ready")

    assert response.status_code == 503
    reports = response.json()["error"]["details"]["dependencies"]
    heartbeat = next(r for r in reports if r["name"] == "worker_heartbeat")
    assert heartbeat["ready"] is False
    assert "61s" in heartbeat["detail"]


def test_readiness_accepts_worker_heartbeat_within_threshold(
    client: TestClient, override_checks
) -> None:
    override_checks(
        [
            _ready("postgresql"),
            _ready("redis"),
            _ready("object_storage"),
            make_worker_heartbeat_check(
                lambda: NOW - timedelta(seconds=WORKER_HEARTBEAT_THRESHOLD_SECONDS),
                now=lambda: NOW,
            ),
        ]
    )

    assert client.get("/api/v1/health/ready").status_code == 200


def test_readiness_with_default_checks_uses_real_providers(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """默认检查集合必须是**真实探针**：每一项都指向真依赖，而不是「尚未接线」占位。

    这里只把各 provider 入口换成替身来断言「接线」本身（200 响应体按契约只有
    `status`，依赖明细只在未就绪时出现在错误详情里），探针各自的行为在下面逐个覆盖。
    """

    calls = _wire_fake_providers(monkeypatch, heartbeat_age_seconds=5)

    response = client.get("/api/v1/health/ready")

    assert response.status_code == 200, response.text
    assert response.json()["data"] == {"status": "ok"}
    assert calls == [
        "postgresql",
        "redis",
        "object_storage",
        "worker_heartbeat",
        "generation_costs",
    ]


def test_default_checks_reach_every_dependency(monkeypatch: pytest.MonkeyPatch) -> None:
    """每个探针都必须真的被问到（顺序与 `DEPENDENCY_NAMES` 一致）。"""

    _wire_fake_providers(monkeypatch)

    checks = default_dependency_checks()
    assert [check().name for check in checks] == list(DEPENDENCY_NAMES)
    results = [check() for check in checks]
    assert all(result.ready for result in results), results


@pytest.mark.parametrize(
    ("broken", "expected_name"),
    [
        ("postgresql", "postgresql"),
        ("redis", "redis"),
        ("object_storage", "object_storage"),
        ("generation_costs", "generation_costs"),
    ],
)
def test_each_dependency_failure_is_reported_and_makes_ready_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, broken: str, expected_name: str
) -> None:
    """任一真依赖不可用时 `/ready` 必须 503，并且不再有「未接线」占位。"""

    _wire_fake_providers(monkeypatch, broken=broken)

    response = client.get("/api/v1/health/ready")

    assert response.status_code == 503
    reports = {r["name"]: r for r in response.json()["error"]["details"]["dependencies"]}
    assert reports[expected_name]["ready"] is False
    for report in reports.values():
        assert "not wired" not in (report["detail"] or "")


# --------------------------------------------------------------------------- #
# 点数成本配置自检（新环境「迁移完但没定价」必须让 /ready 红）
# --------------------------------------------------------------------------- #
def test_missing_cost_price_makes_ready_503_with_operable_detail(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """详情要直接指出缺哪个模式的价格，运维不必再去翻日志猜 503 从哪来。"""

    _wire_fake_providers(monkeypatch, missing_cost_modes=("PHOTO_PROMPT",))

    response = client.get("/api/v1/health/ready")

    assert response.status_code == 503
    reports = {r["name"]: r for r in response.json()["error"]["details"]["dependencies"]}
    assert reports["generation_costs"]["ready"] is False
    assert reports["generation_costs"]["detail"] == (
        "missing active point prices for: PHOTO_PROMPT"
    )
    # 其余依赖照常就绪：不能因为一处未定价就把整个 /ready 变成无法定位的模糊失败
    assert [
        report["ready"] for name, report in reports.items() if name != "generation_costs"
    ] == [True, True, True, True]


def test_cost_config_check_reports_ready_when_every_mode_is_priced() -> None:
    check = checks_module.make_cost_config_check(probe=lambda: ())

    result = check()

    assert (result.name, result.ready, result.detail) == ("generation_costs", True, None)


def test_cost_config_check_shares_the_exact_model_rule_with_generation_creation() -> None:
    """readiness 与创建任务必须同口径：**精确型号优先，NULL 兜底**。

    `_cost_config_probe` 调的就是 `missing_cost_modes`（再往下是 `find_active`），
    所以这里直接用它 + 与生产同一条规则的内存仓储，证明：
      - 只给精确型号定价 → readiness 绿（这正是 ENG-10 里「/ready 绿、创建任务 503」
        那处不一致的反面）；
      - 换一个没定价的型号 → 如实报缺口，**不退回任何默认价**。
    """

    from app.bootstrap.cost_configs import expected_cost_modes, missing_cost_modes
    from tests.services.fakes import (
        InMemoryGenerationCostConfigRepository,
        make_generation_cost_config,
    )

    model = "mock-image-v1"
    costs = InMemoryGenerationCostConfigRepository()
    for mode in expected_cost_modes(enable_reference_image=False):
        costs.seed(make_generation_cost_config(mode=mode, model_name=model, points_cost=13))

    ready = checks_module.make_cost_config_check(
        probe=lambda: missing_cost_modes(
            costs,
            modes=expected_cost_modes(enable_reference_image=False),
            model_name=model,
            at=datetime.now(timezone.utc),
        )
    )()
    assert (ready.ready, ready.detail) == (True, None)

    unpriced = checks_module.make_cost_config_check(
        probe=lambda: missing_cost_modes(
            costs,
            modes=("TEXT_PROMPT",),
            model_name="another-model",
            at=datetime.now(timezone.utc),
        )
    )()
    assert unpriced.ready is False
    assert unpriced.detail == "missing active point prices for: TEXT_PROMPT"


def test_cost_config_check_swallows_probe_text_but_stays_not_ready() -> None:
    """探针异常文本可能含连接串：只报「探针出错」，不回显原文。"""

    def _probe() -> tuple[str, ...]:
        raise RuntimeError("password=hunter2 at postgres.internal")

    result = checks_module.make_cost_config_check(probe=_probe)()

    assert result.ready is False
    assert result.detail == "cost config probe raised an error"
    assert "hunter2" not in (result.detail or "")


def test_redis_probe_uses_a_port_surface_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """Redis 探针用冻结端口面上就存在的读操作：键不存在同样证明服务在线。"""

    probed: list[str] = []

    class Port:
        def get(self, name: str) -> str | None:
            probed.append(name)
            return None

    monkeypatch.setattr(checks_module, "_redis_port", lambda: Port())
    result = make_check("redis", make_redis_probe(), owner="ENG-06")()

    assert result.ready is True
    assert probed


def test_redis_probe_reports_not_ready_when_the_port_raises(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    class BrokenPort:
        def get(self, name: str) -> str | None:
            raise ConnectionError("synthetic redis down")

        def ping(self) -> bool:
            raise ConnectionError("synthetic redis down")

    monkeypatch.setattr(checks_module, "_redis_port", lambda: BrokenPort())
    result = make_check("redis", make_redis_probe(), owner="ENG-06")()

    assert result.ready is False


def test_object_storage_probe_reports_bucket_unavailable(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """桶不可用必须报未就绪：对象级 `exists()` 会把「桶不存在」也伪装成「对象不存在」。"""

    class Adapter:
        def bucket_available(self) -> bool:
            return False

    monkeypatch.setattr(checks_module, "_storage_adapter", lambda: Adapter())
    result = make_check("object_storage", make_object_storage_probe(), owner="ENG-05")()

    assert result.ready is False


def test_object_storage_probe_accepts_a_reachable_bucket(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    class Adapter:
        def bucket_available(self) -> bool:
            return True

    monkeypatch.setattr(checks_module, "_storage_adapter", lambda: Adapter())
    assert make_check(
        "object_storage", make_object_storage_probe(), owner="ENG-05"
    )().ready is True


def test_worker_heartbeat_probe_reads_the_real_heartbeat(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """心跳探针取自 Redis 里那份时间戳：新鲜则就绪，过期即未就绪（阈值 60 秒已冻结）。"""

    # 边界值本身由 `worker_heartbeat_result` 的纯函数用例覆盖；这里留 1 秒余量，
    # 避免「构造心跳」与「判定年龄」之间的真实耗时把用例变成 flaky
    _wire_fake_providers(
        monkeypatch, heartbeat_age_seconds=WORKER_HEARTBEAT_THRESHOLD_SECONDS - 1
    )
    assert client.get("/api/v1/health/ready").status_code == 200

    _wire_fake_providers(
        monkeypatch, heartbeat_age_seconds=WORKER_HEARTBEAT_THRESHOLD_SECONDS + 1
    )
    response = client.get("/api/v1/health/ready")

    assert response.status_code == 503
    reports = {r["name"]: r for r in response.json()["error"]["details"]["dependencies"]}
    assert reports["worker_heartbeat"]["ready"] is False
    assert "61s" in reports["worker_heartbeat"]["detail"]


def test_worker_heartbeat_probe_without_any_heartbeat(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    _wire_fake_providers(monkeypatch, heartbeat_age_seconds=None)

    result = default_dependency_checks()[3]()

    assert result.name == "worker_heartbeat"
    assert result.ready is False
    assert result.detail == "no worker heartbeat available"


def test_business_services_never_import_an_object_storage_or_redis_sdk() -> None:
    """SDK 只能待在驱动与装配层：`app/services/**` 里出现即违反边界。

    依据 `09-repository-boundary.md` 第 4 节（只有驱动模块允许导入对象存储 SDK）。
    就绪探针要的「可达性」经端口传递，而不是让 Service 直接引 SDK。
    """

    root = Path(checks_module.__file__).resolve().parents[2]
    offenders: list[str] = []
    for path in (root / "app" / "services").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for token in ("import minio", "from minio", "import redis", "from redis"):
            if token in text:
                offenders.append(f"{path.relative_to(root)}: {token}")
    assert offenders == []


def test_default_dependency_checks_perform_no_io_when_constructed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """装配默认检查集合不碰任何依赖：探针只有在被调用时才建立连接。"""

    def _forbidden() -> None:
        raise AssertionError("构造默认检查集合时访问了真依赖")

    monkeypatch.setattr(checks_module, "_postgresql_engine", _forbidden)
    monkeypatch.setattr(checks_module, "_redis_port", _forbidden)
    monkeypatch.setattr(checks_module, "_storage_adapter", _forbidden)
    monkeypatch.setattr(checks_module, "_heartbeat_timestamp", _forbidden)

    checks = default_dependency_checks()
    assert len(checks) == len(DEPENDENCY_NAMES)


@pytest.mark.parametrize(
    ("age_seconds", "expected_ready"),
    [
        (0, True),
        (1, True),
        (59, True),
        (60, True),  # 边界：恰好 60 秒仍视为就绪
        (61, False),
        (600, False),
    ],
)
def test_worker_heartbeat_threshold_boundaries(
    age_seconds: int, expected_ready: bool
) -> None:
    result = worker_heartbeat_result(
        last_beat_at=NOW - timedelta(seconds=age_seconds), now=NOW
    )

    assert result.ready is expected_ready
    assert result.name == "worker_heartbeat"


def test_worker_heartbeat_missing_is_not_ready() -> None:
    result = worker_heartbeat_result(last_beat_at=None, now=NOW)

    assert result.ready is False
    assert result.detail


def test_worker_heartbeat_handles_naive_datetimes() -> None:
    naive = (NOW - timedelta(seconds=30)).replace(tzinfo=None)
    assert worker_heartbeat_result(last_beat_at=naive, now=NOW).ready is True


def test_worker_heartbeat_reader_absent_is_not_ready() -> None:
    result = make_worker_heartbeat_check(None)()
    assert result.ready is False
    assert "ENG-06" in (result.detail or "")


def test_worker_heartbeat_reader_error_is_sanitized() -> None:
    def _broken_reader():
        raise RuntimeError("redis://user:SENTINEL-PASSWORD@redis:6379/0 unavailable")

    result = make_worker_heartbeat_check(_broken_reader)()

    assert result.ready is False
    assert result.detail == "heartbeat reader raised an error"
    assert "SENTINEL-PASSWORD" not in (result.detail or "")


def test_probe_error_is_sanitized() -> None:
    def _broken_probe() -> bool:
        raise RuntimeError("postgresql://ai_avatar:SENTINEL-PASSWORD@postgres:5432/ai_avatar")

    result = make_check("postgresql", _broken_probe, owner="test")()

    assert result.ready is False
    assert result.detail == "probe raised an error"
    assert "SENTINEL-PASSWORD" not in (result.detail or "")


def test_unwired_probe_reports_owner() -> None:
    result = make_check("redis", None, owner="ENG-06")()

    assert result.ready is False
    assert result.detail == "probe not wired yet (owner: ENG-06)"


def test_postgresql_probe_executes_select_one_with_injected_engine() -> None:
    """真实探针用注入的假引擎验证，不建立数据库连接。"""

    executed: list[str] = []

    class _FakeResult:
        pass

    class _FakeConnection:
        def execute(self, statement) -> _FakeResult:
            executed.append(str(statement))
            return _FakeResult()

        def __enter__(self) -> _FakeConnection:
            return self

        def __exit__(self, *args) -> None:
            return None

    class _FakeEngine:
        def connect(self) -> _FakeConnection:
            return _FakeConnection()

    probe = make_postgresql_probe(lambda: _FakeEngine())

    assert probe() is True
    assert executed == ["SELECT 1"]


def test_postgresql_probe_failure_is_reported_not_ready() -> None:
    def _broken_engine_factory():
        raise RuntimeError("could not connect")

    check = make_check(
        "postgresql", make_postgresql_probe(_broken_engine_factory), owner="test"
    )

    result = check()
    assert result.ready is False
    assert result.detail == "probe raised an error"


# --------------------------------------------------------------------------- #
# 文档端点开关（APP_ENV）
# --------------------------------------------------------------------------- #
def test_docs_endpoints_enabled_for_local(make_app) -> None:
    app = make_app(APP_ENV="local")
    with TestClient(app) as local_client:
        assert local_client.get("/docs").status_code == 200
        assert local_client.get("/redoc").status_code == 200
        assert local_client.get("/openapi.json").status_code == 200


def test_docs_endpoints_enabled_for_test_env(make_app) -> None:
    app = make_app(APP_ENV="test")
    with TestClient(app) as test_env_client:
        assert test_env_client.get("/docs").status_code == 200
        assert test_env_client.get("/openapi.json").status_code == 200


def test_docs_endpoints_disabled_for_production(make_app) -> None:
    app = make_app(APP_ENV="production")
    with TestClient(app) as production_client:
        assert production_client.get("/docs").status_code == 404
        assert production_client.get("/redoc").status_code == 404
        assert production_client.get("/openapi.json").status_code == 404
        # 健康检查仍然可用
        assert production_client.get("/api/v1/health/live").status_code == 200
