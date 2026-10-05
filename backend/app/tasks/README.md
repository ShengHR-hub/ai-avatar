# backend/app/tasks/

Celery app、任务入口和调度任务。

进程启动约定：

```text
Celery Worker：celery -A app.tasks.celery_app worker
Celery Beat：celery -A app.tasks.celery_app beat
```

Celery Task 只负责：解析任务参数、建立执行上下文、调用 Service、根据错误分类决定重试。

禁止在 Celery Task 函数中复制业务逻辑或直接修改账务表。

Worker 与 Beat 不对外暴露业务 HTTP 端口；Beat 只向 Redis 投递已注册任务，Worker 才执行任务。

ENG-00 不在此目录创建 Celery app 或任何任务。实现属于 `ENG-06`。
