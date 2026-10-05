# frontend/services/

API 调用与数据转换层（ENG-08）。所有请求经 `@hey-api/openapi-ts` 从冻结的
`shared/api-contract/openapi.json` 生成的客户端发出；本目录不手写后端 DTO 或路径。

| 模块 | 职责 |
|---|---|
| `config.ts` | 剥离 `/api/v1` 得到 origin；契约常量（固定 2 张）、开放模式、轮询节奏 |
| `http.ts` | `credentials: include`、`X-Request-Id`、目标主机守卫、`Envelope` 解包、预签名直传 |
| `api.ts` | 按 operationId 的调用面 + 自由形 `Envelope` 的运行时守卫 + DTO 视图 |
| `idempotency.ts` | 幂等键绑定「一次用户意图」，同意图重试复用、终局后释放 |
| `errors.ts` | 稳定 `error.code` → 界面动作与标题；只认码，不解析 message 文本 |
| `generation.ts` | 草稿号与进行中任务的会话存储、终局判定、退避节奏、状态文案 |
| `watch.ts` | 唯一一份轮询实现（含停止、恢复、超时交回手动刷新） |
| `upload.ts` | 摘要 → 上传意图 → 按签名 URL 送原始字节 → 完成校验 → 确认 `READY` |

两条不可让的口径：

1. 预签名 PUT 只送原始字节，`credentials: omit`，不带 Cookie/Authorization，也不加
   `X-Request-Id`——签名口径之外的头会破坏直传；
2. 资源只有在服务端 `complete` 后确认 `READY` 才算可用于生成；`PENDING_UPLOAD` 不是成功。
