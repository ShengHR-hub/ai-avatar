"""`CeleryGenerationQueue` 投递端口测试（ENG-06 第 4 步）。

冻结依据：

- `docs/task-cards/LEAD-04-points-and-state-machine.md` 第 6 节「创建与预留」：
  「提交后再投递 Celery；投递失败由恢复扫描重新入队，**不回滚已提交的账务**」；
- `docs/architecture/05-system-architecture.md` 第 3 节：依赖方向
  `API → Service → … → Celery producer → Redis`，Producer 只投递、不执行；
- `docs/architecture/04-ai-provider.md` 第 3 节 + `LEAD-04` 第 7 节：重试由 Service 用
  `countdown` 显式投递（每次留下独立 `provider_attempts` 行），因此投递层自己
  不得重试；`09-repository-boundary.md` 第 7 节禁止把 Prompt/URL/凭据写进日志。

只按任务名 `send_task`：Web 进程不需要导入 Worker 侧模块。
投递用记录器替身，不连接 Broker。
"""

from __future__ import annotations

import inspect
import logging
import re
import uuid
from collections.abc import Callable
from typing import Any

import pytest

from app.services.generation.ports import GenerationQueuePort
from app.tasks import worker
from app.tasks.celery_app import TASK_POLL_STATUS, TASK_RUN_GENERATION, celery_app
from app.tasks.queue import CeleryGenerationQueue

#: 只用于验证「异常消息不会进日志」的哨兵值，本身没有业务含义
PROMPT_SENTINEL = "SENTINEL-prompt-must-not-be-logged"
CREDENTIAL_SENTINEL = "SENTINEL-credential-must-not-be-logged"
BROKER_URL_SENTINEL = f"redis://user:{CREDENTIAL_SENTINEL}@broker.example.invalid:6379/0"

GENERATION_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")


class SendTaskRecorder:
    """`Celery` 的投递面替身：只记录 `send_task` 调用，绝不建立连接。"""

    def __init__(self, *, raises: BaseException | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._raises = raises

    def send_task(self, name: str, **options: Any) -> Any:
        self.calls.append((name, options))
        if self._raises is not None:
            raise self._raises
        return f"synthetic-message-id-{len(self.calls)}"

    @property
    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


def failing_broker_error() -> RuntimeError:
    """一个「消息里塞满了敏感上下文」的 Broker 异常：日志只允许记它的类型名。"""

    return RuntimeError(
        f"connect failed to {BROKER_URL_SENTINEL} for prompt {PROMPT_SENTINEL}"
    )


def make_queue(
    *, raises: BaseException | None = None
) -> tuple[CeleryGenerationQueue, SendTaskRecorder]:
    """构造注入记录器的队列端口，返回 `(端口, 记录器)`。"""

    recorder = SendTaskRecorder(raises=raises)
    return CeleryGenerationQueue(app=recorder), recorder


# --------------------------------------------------------------------------- #
# 端口形状
# --------------------------------------------------------------------------- #
def test_queue_satisfies_the_generation_queue_port() -> None:
    """投递端口必须继续满足 Service 侧冻结的 `GenerationQueuePort` 协议。"""

    queue, _ = make_queue()

    assert isinstance(queue, GenerationQueuePort)


def test_default_producer_app_is_the_single_shipped_celery_instance() -> None:
    """默认参数就是 `app.tasks.celery_app` 的那个实例（Web 进程无需自己传 app）。

    本用例只读默认值，**不**用它投递：真实实例会去连 `redis://redis:6379/0`。
    """

    default = inspect.signature(CeleryGenerationQueue.__init__).parameters["app"].default

    assert default is celery_app


def test_delivered_names_are_the_names_the_worker_registers() -> None:
    """投递名与消费名必须来自同一处：名字写错等于消息永远躺在队列里无人执行。"""

    assert worker.run_generation.name == TASK_RUN_GENERATION
    assert worker.poll_generation_status.name == TASK_POLL_STATUS
    assert {worker.run_generation.name, worker.poll_generation_status.name} <= set(
        celery_app.tasks
    )

    queue, recorder = make_queue()
    queue.enqueue_generation(GENERATION_ID)
    queue.enqueue_status_poll(GENERATION_ID, attempt_no=1, countdown_seconds=0)

    assert recorder.names == [
        worker.run_generation.name,
        worker.poll_generation_status.name,
    ]


# --------------------------------------------------------------------------- #
# generation.run 投递形状
# --------------------------------------------------------------------------- #
def test_enqueue_generation_sends_task_name_with_string_id() -> None:
    """参数只有任务名字符串形式的 ID：Worker 侧自己解析，Producer 不导入 Worker 模块。"""

    queue, recorder = make_queue()

    assert queue.enqueue_generation(GENERATION_ID) is True
    assert recorder.names == [TASK_RUN_GENERATION]
    name, options = recorder.calls[0]
    assert name == TASK_RUN_GENERATION
    assert isinstance(options["args"], tuple)
    assert options["args"] == (str(GENERATION_ID),)
    assert type(options["args"][0]) is str
    assert uuid.UUID(options["args"][0]) == GENERATION_ID


@pytest.mark.parametrize("countdown_seconds", [0, 1, 5, 30, 120])
def test_countdown_is_only_forwarded_when_positive(countdown_seconds: int) -> None:
    """`countdown_seconds=0` 不得写 `countdown` 键；正数必须原样传给 Broker。

    退避节奏（5s/30s/120s）由 Service 决定，投递层丢掉 countdown 就等于把
    退避重试变成立即重试（`LEAD-04` 第 7 节的错误分类会随之失效）。
    """

    queue, recorder = make_queue()

    assert queue.enqueue_generation(GENERATION_ID, countdown_seconds=countdown_seconds) is True
    _, options = recorder.calls[0]
    if countdown_seconds > 0:
        assert options["countdown"] == countdown_seconds
    else:
        assert "countdown" not in options


def test_delivery_options_stay_within_args_and_countdown() -> None:
    """投递只允许携带 `args`/`countdown`：不得开启 Broker 侧 `retry`、也不得改路由。

    `send_task(retry=True)` 会在传输层重投同一消息，与 Service 的显式重试叠加
    就是同一笔预留被重复消费；指定 `queue`/`routing_key` 则会绕过 `task_default_queue`。
    """

    queue, recorder = make_queue()

    queue.enqueue_generation(GENERATION_ID, countdown_seconds=30)
    queue.enqueue_status_poll(GENERATION_ID, attempt_no=2, countdown_seconds=5)

    for _, options in recorder.calls:
        assert set(options) <= {"args", "countdown"}, options
        assert "retry" not in options
        assert "retry_policy" not in options
        assert "queue" not in options and "routing_key" not in options


# --------------------------------------------------------------------------- #
# generation.poll_status 投递形状
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(("attempt_no", "countdown_seconds"), [(1, 0), (2, 20), (3, 120)])
def test_enqueue_status_poll_sends_id_and_attempt_number(
    attempt_no: int, countdown_seconds: int
) -> None:
    """轮询投递必须同时带上 ID 与 attempt 序号，并走 `generation.poll_status` 任务名。"""

    queue, recorder = make_queue()

    result = queue.enqueue_status_poll(
        GENERATION_ID, attempt_no=attempt_no, countdown_seconds=countdown_seconds
    )

    assert result is True
    name, options = recorder.calls[0]
    assert name == TASK_POLL_STATUS
    assert options["args"] == (str(GENERATION_ID), attempt_no)
    assert type(options["args"][1]) is int
    if countdown_seconds > 0:
        assert options["countdown"] == countdown_seconds
    else:
        assert "countdown" not in options


def test_attempt_number_is_normalised_to_int() -> None:
    """attempt 序号按 int 传：字符串序号会被 JSON 序列化成字符串，Worker 侧判定会跑偏。"""

    queue, recorder = make_queue()

    queue.enqueue_status_poll(GENERATION_ID, attempt_no="4", countdown_seconds=20)  # type: ignore[arg-type]

    _, options = recorder.calls[0]
    assert options["args"][1] == 4
    assert type(options["args"][1]) is int


# --------------------------------------------------------------------------- #
# 投递失败：返回 False、不抛、不重试、日志安全
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("action", "task_name"),
    [
        (lambda queue: queue.enqueue_generation(GENERATION_ID), TASK_RUN_GENERATION),
        (
            lambda queue: queue.enqueue_status_poll(
                GENERATION_ID, attempt_no=1, countdown_seconds=20
            ),
            TASK_POLL_STATUS,
        ),
    ],
    ids=["generation-run", "generation-poll-status"],
)
def test_lost_delivery_returns_false_and_never_raises(
    action: Callable[[CeleryGenerationQueue], bool], task_name: str
) -> None:
    """丢失的投递绝不能把异常抛回已提交账务的调用方（`LEAD-04` 第 6 节）。"""

    queue, recorder = make_queue(raises=failing_broker_error())

    assert action(queue) is False
    assert recorder.names == [task_name], "投递层不得自己重试，只允许一次尝试"


def test_delivery_failure_log_contains_only_task_name_and_exception_type(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """安全摘要：只有任务名与异常类型名，异常消息本身不得进日志。

    真实 Broker 异常消息通常带完整连接串（含密码）与投递参数；
    把它原样记进日志就会同时违反 `09-repository-boundary.md` 第 7 节与
    `03-api-contract.md` 第 8 节的脱敏要求。
    """

    queue, _ = make_queue(raises=failing_broker_error())

    with caplog.at_level(logging.WARNING, logger="ai_avatar.tasks.queue"):
        assert queue.enqueue_generation(GENERATION_ID) is False

    assert [record.name for record in caplog.records] == ["ai_avatar.tasks.queue"]
    record = caplog.records[0]
    assert record.levelno == logging.WARNING
    message = record.getMessage()

    assert TASK_RUN_GENERATION in message
    assert "RuntimeError" in message
    # 「只有任务名与异常类型名」：整条摘要必须匹配这个形状，不留夹带参数的余地
    assert re.fullmatch(r"celery_delivery_failed task=\S+ error=\w+", message), message
    assert PROMPT_SENTINEL not in message
    assert CREDENTIAL_SENTINEL not in message
    assert BROKER_URL_SENTINEL not in message
    assert str(GENERATION_ID) not in message, "投递参数不得进入日志"
    assert record.exc_info is None and record.stack_info is None, "traceback 会带出投递参数"
    assert "://" not in message, "日志里不得出现任何 URL"


def test_delivery_failure_does_not_swallow_other_exceptions_silently(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """异常类型只影响日志摘要，不影响返回值：任何投递异常都是 `False`。

    这条断言把「吞掉异常」的范围钉死在 `Exception`（可恢复的投递失败）上，
    同时确认换一种异常类型也不会往上抛。
    """

    class UnexpectedBrokerFault(KeyError):
        """另一种异常形状（例如路由表缺失）。"""

    queue, recorder = make_queue(raises=UnexpectedBrokerFault("no route"))

    with caplog.at_level(logging.WARNING, logger="ai_avatar.tasks.queue"):
        assert queue.enqueue_generation(GENERATION_ID) is False

    assert recorder.names == [TASK_RUN_GENERATION]
    assert "UnexpectedBrokerFault" in caplog.text
    assert "no route" not in caplog.text, "异常消息不得进日志"


def test_successful_delivery_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    """成功路径不写日志：心跳之外的每次投递都在长任务热路径上，日志必须只记异常。"""

    queue, _ = make_queue()

    with caplog.at_level(logging.WARNING, logger="ai_avatar.tasks.queue"):
        assert queue.enqueue_generation(GENERATION_ID) is True

    assert caplog.records == []
