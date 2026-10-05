"""ENG-10 端到端测试包（真实栈、真实 HTTP、真实浏览器）。

放在 `tests/` 下因此**不进镜像**（`backend/.dockerignore` 排除 `tests/`）：
这里的模块只在宿主机的 pytest 进程里运行，或作为 E2E 容器的显式覆盖入口被挂载。
"""
