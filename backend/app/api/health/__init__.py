"""健康检查路由包（ENG-02 第 3 步）。

- `checks`：可注入的依赖检查函数（PostgreSQL、Redis、对象存储、Worker 心跳）；
- `router`：`/health/live` 与 `/health/ready`。
"""
