"""错误处理层（ENG-02 第 2 步）。

- `app.errors.codes`：契约冻结的稳定错误码与 HTTP 映射；
- `app.errors.exceptions`：携带错误码的应用异常 `ApiError`；
- `app.errors.handlers`：FastAPI / Starlette 异常的统一映射与错误 envelope。
"""
