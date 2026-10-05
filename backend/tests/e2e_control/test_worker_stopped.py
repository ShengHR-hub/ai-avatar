"""`worker_stopped` 的故障注入回归（**不起任何真实资源**）。

`worker_stopped` 是 E2E 里唯一会「停掉/拉起 Worker」的地方。它的失败模式很隐蔽：
停机失败、恢复失败、恢复**调用本身**抛异常（`TimeoutExpired` / `OSError`），
一旦处理不当，要么把 Worker 留在停着的状态，要么把**用例真正的失败原因顶掉**，
于是排查方向被带到完全无关的地方。

这组用例用注入的假控制面把五种情形钉死（`stop` / `start` 只是注入点，
正常路径的行为一字未变）：

1. 停止部分成功（脚本非零退出）→ 用例主体不执行，但**仍尝试恢复**；
2. 用例失败 + 恢复返回非零 → **保留原始异常**，同时明确记录恢复失败；
3. 用例失败 + 恢复抛 `TimeoutExpired`/`OSError` → 同样保留原始异常并记录；
4. 用例成功 + 恢复失败 → **必须让测试失败**，绝不吞掉；
5. 正常停止与恢复 → 静默通过。
"""

from __future__ import annotations

import subprocess

import pytest

from tests.e2e.conftest import worker_stopped


def _result(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    """造一个 `subprocess.CompletedProcess`（控制面脚本的返回值形状）。"""

    return subprocess.CompletedProcess(
        args=["fake-stack-script"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def test_stop_check_failure_still_attempts_restore() -> None:
    """1) 停止**部分成功**（脚本非零退出）：主体不执行，但恢复仍被尝试。"""

    calls: list[str] = []

    def stop() -> subprocess.CompletedProcess:
        calls.append("stop")
        return _result(returncode=1, stderr="worker 仍在运行，停机未生效")

    def start() -> subprocess.CompletedProcess:
        calls.append("start")
        return _result()

    with pytest.raises(AssertionError, match="停止 Worker 失败"):
        with worker_stopped(stop=stop, start=start):
            calls.append("body")

    assert calls == ["stop", "start"], f"停机检查失败后仍必须尝试恢复：{calls}"


def test_body_failure_keeps_original_exception_and_reports_restore_failure(capsys) -> None:
    """2) 用例失败 + 恢复非零退出：原始异常是主异常，恢复失败被明确记录。"""

    def stop() -> subprocess.CompletedProcess:
        return _result()

    def start() -> subprocess.CompletedProcess:
        return _result(returncode=1, stderr="compose start worker 失败")

    with pytest.raises(RuntimeError, match="用例自己的失败"):
        with worker_stopped(stop=stop, start=start):
            raise RuntimeError("用例自己的失败")

    printed = capsys.readouterr().out
    assert "恢复 Worker 失败" in printed, printed
    assert "退出码 1" in printed, printed


def test_body_failure_with_restore_call_exception_keeps_original_exception(capsys) -> None:
    """3) 用例失败 + 恢复**调用抛异常**：原始异常不能被顶掉。"""

    def stop() -> subprocess.CompletedProcess:
        return _result()

    def start() -> subprocess.CompletedProcess:
        raise subprocess.TimeoutExpired(cmd="eng10_compose_stack.py start-worker", timeout=180)

    with pytest.raises(RuntimeError, match="用例自己的失败"):
        with worker_stopped(stop=stop, start=start):
            raise RuntimeError("用例自己的失败")

    printed = capsys.readouterr().out
    assert "恢复 Worker 失败" in printed, printed
    assert "TimeoutExpired" in printed, printed


def test_body_failure_with_restore_oserror_keeps_original_exception(capsys) -> None:
    """3b) `OSError`（脚本/解释器起不来）同样不能顶掉原始异常。"""

    def stop() -> subprocess.CompletedProcess:
        return _result()

    def start() -> subprocess.CompletedProcess:
        raise OSError("无法启动栈控制脚本")

    with pytest.raises(RuntimeError, match="用例自己的失败"):
        with worker_stopped(stop=stop, start=start):
            raise RuntimeError("用例自己的失败")

    printed = capsys.readouterr().out
    assert "恢复 Worker 失败" in printed, printed
    assert "OSError" in printed, printed


def test_restore_nonzero_without_original_exception_fails_the_test() -> None:
    """4) 用例成功 + 恢复非零退出：**必须失败**，不能吞掉。"""

    def stop() -> subprocess.CompletedProcess:
        return _result()

    def start() -> subprocess.CompletedProcess:
        return _result(returncode=3, stderr="boom")

    with pytest.raises(AssertionError, match="恢复 Worker 失败"):
        with worker_stopped(stop=stop, start=start):
            pass


def test_restore_call_exception_without_original_exception_fails_the_test() -> None:
    """4b) 用例成功 + 恢复调用抛异常：同样**必须失败**。"""

    def stop() -> subprocess.CompletedProcess:
        return _result()

    def start() -> subprocess.CompletedProcess:
        raise subprocess.TimeoutExpired(cmd="eng10_ui_stack.py start-worker", timeout=180)

    with pytest.raises(AssertionError, match="恢复 Worker 失败"):
        with worker_stopped(stop=stop, start=start):
            pass


def test_normal_stop_and_restore_is_silent(capsys) -> None:
    """5) 正常路径：停 → 主体 → 恢复，顺序正确且不打印任何恢复失败。"""

    calls: list[str] = []

    def stop() -> subprocess.CompletedProcess:
        calls.append("stop")
        return _result()

    def start() -> subprocess.CompletedProcess:
        calls.append("start")
        return _result()

    with worker_stopped(stop=stop, start=start):
        calls.append("body")

    assert calls == ["stop", "body", "start"], calls
    assert "恢复 Worker 失败" not in capsys.readouterr().out
