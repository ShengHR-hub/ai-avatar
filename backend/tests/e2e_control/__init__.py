"""ENG-10 E2E 控制逻辑的回归测试包（**不启动任何真实资源**）。

这里只测「测试基建自己的控制流」——`tests/e2e/conftest.py` 里的
`worker_stopped` 上下文管理器。它刻意与 `tests/e2e/` 分开：`tests/e2e/conftest.py`
里有两个 `autouse=True` 的 session 夹具（栈可达性、栈身份核对），放在那个目录下会让
这些纯逻辑用例**必须先起一整套栈**才能跑，违背「故障注入不启动真实资源」的要求。
"""
