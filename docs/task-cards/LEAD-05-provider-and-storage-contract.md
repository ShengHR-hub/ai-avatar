# LEAD-05：AI Provider 与 Storage Adapter 契约

## 会话目的

冻结 Generation Service 与 AI Provider、对象存储之间的内部契约，使同步/异步 Provider、轮询/Webhook 以及 MinIO/COS/OSS/S3 驱动均可替换，而不让业务层依赖具体供应商或 SDK。详细规范以 [AI Avatar Studio V1.0 Provider 与 Storage 契约](../architecture/04-ai-provider.md) 为准。

本卡只产出设计契约和后续任务边界：不实现 Provider 或存储驱动，不调用真实供应商，不修改 API/数据库字段，不提交 Git。Provider 能力、型号和生产 Storage 供应商均未在本卡中选定或验证。

## 冻结内容

### Provider

- `AIProvider` 定义 `generate(request) -> ProviderResult`、`check_status(provider_request_id) -> ProviderResult`、`cancel(provider_request_id) -> ProviderResult`。
- `ProviderState` 仅为 `SUCCEEDED`、`PENDING`、`FAILED`。同步结果直接 `SUCCEEDED/FAILED`；异步提交为带 `provider_request_id` 的 `PENDING`。Provider `UNKNOWN` 是 attempt outcome，不是第四种 Provider 状态；无法确认时 Generation Service 进入 `NEEDS_REVIEW`，点数保持预留。
- Webhook 与 polling 都归一到同一个状态对账入口。Webhook 先验签、校验时间窗和 event ID/nonce，再由统一幂等的 Service 完成状态和结算；重复、迟到或乱序事件不得重复结算或复活终态。
- 分离应用 `request_id`、每次逻辑 Provider attempt 的 `client_request_id`、外部 `provider_request_id`。现有 `provider_attempts.request_id` 保存外部 ID；不增加数据库字段。
- 错误分类固定为 `VALIDATION`、`AUTHENTICATION`、`CONTENT_POLICY`、`RATE_LIMIT`、`TEMPORARY`、`TIMEOUT`、`UNKNOWN`，并带可否重试和请求是否可能已接受的信息。UNKNOWN 或接受状态不明禁止盲目重试。
- 已有超时、退避、最大重试、provider_attempts 状态和成本字段按 `04-ai-provider.md` 解释；用量/成本未知写 `NULL`，成本只用于内部核算，不改变用户点数。
- MockProvider 必须覆盖成功、延迟、失败、提交前/后的超时、UNKNOWN、成功但没有图片、重复/迟到回调。场景确定、无真实网络和凭据，走同一 Service、校验、Storage 和结算路径。

### Storage 与图片

- `StorageAdapter` 定义上传、读取、短期签名 URL、幂等删除、存在性检查和过期对象批量清理。DB 中既有 `media_assets.expires_at` 驱动生命周期；不新增字段。
- 本地 MinIO 仅通过本地驱动；生产 COS/OSS/S3 为可选驱动边界。具体 SDK 只允许出现在驱动模块，服务端业务使用统一异常和类型。
- 所有客户端输入与 Provider 输出经过服务端格式嗅探、MIME/大小/尺寸校验、完整解码和 SHA-256 校验。对象桶保持私有，默认 PRIVATE；SHARED 仅限有效分享范围，PUBLIC 仅限显式公开内容。
- Feature Flag 关闭时，`REFERENCE_IMAGE` 在接受/关联资源或入队之前返回既有 `FEATURE_NOT_AVAILABLE`；禁止静默忽略参考图。
- 推荐初始图片限制为 10 MiB、最长边 4096 px、16 MP；读/上传签名 URL 建议最多 5/10 分钟。这些数值需技术负责人确认，不能覆盖已有 API 约束而不经评审。

## Webhook 安全门禁

- HTTPS；对原始请求体按已选供应商协议验签，恒定时间比较；服务端保管和轮换密钥。
- 必须有签名时间戳（建议 5 分钟窗口）和 event ID/nonce；短期原子去重并检查已有 attempt 的终态/单调性。
- 只按已配置 Provider 下已存在的 `provider_request_id` 关联；忽略回调中的用户/任务授权声明。
- 回调不可直接改点数、媒体输出或任务状态；只进入幂等 Service。拒绝伪造、过期、重放、无法关联事件。
- 不记录签名、密钥、完整 payload、Prompt 或图片 URL。实际签名头、算法和 canonicalization 必须等选定供应商后验证。

## ENG 交接

| 卡片 | 获授权后的范围 | 禁止/门禁 |
|---|---|---|
| `ENG-05` | 实现 StorageAdapter 与本地 MinIO 驱动、上传完成校验、资源所有权/visibility 授权、短期签名、基于现有 `expires_at` 的清理 | 不接 AI Provider、不改 API/数据库字段、不启用参考图；必须验证校验失败不完成资源、越权读取/签名失败、删除重试幂等 |
| `ENG-06` | 实现 Generation Service、Celery 编排、Provider 状态轮询/恢复、统一 reconciliation 和完整 MockProvider 场景 | 依赖本卡和 LEAD-04；不连真实 Provider、不做自动路由、不改 Schema/API；UNKNOWN 保留点数，重复回调只结算一次 |
| `ENG-07` | 仅在 ENG-06 Mock 闭环通过并经 LEAD-05 复核后，适配一个经批准的 PrimaryProvider | 当前未授权；先验证具体模型能力、异步/同步行为、请求幂等、状态查询、取消、图片格式、Webhook 验签和价格/用量；不能假定缺失能力或引入多模型自动路由 |

## 审核门禁

1. Provider 结果三态、调用错误分类、ID 关系、timeout/retry/UNKNOWN 处理与既有任务/账务状态机一致。
2. MockProvider 七类场景定义完整，可验证结果缺失、Worker 恢复及重复 Webhook 的幂等性。
3. Storage API 覆盖上传、读取、签名、删除、存在性、清理，且实现边界不泄漏驱动 SDK。
4. 图片安全、私有对象、visibility 授权、签名时长和参考图 Flag 规则清楚。
5. 明确未改 API/Schema、未接真实服务，所有未验证供应商能力和未决限额均列出。

## 尚未确定

- 图片字节/像素限制和签名 URL 时长需产品/安全/Provider 约束确认。
- 图片解析/重编码库及是否 V1 要求恶意软件扫描。
- 生产对象存储供应商、区域、加密和删除/保留策略。
- PrimaryProvider 及其模型能力、幂等、查询、取消、Webhook 与计价能力。
- 是否需新增持久化 Webhook event inbox；当前契约使用短期重放去重和现有 attempt 幂等，不授权新增表/字段。
- 后续真实成本修订是否需要追加式成本历史；当前不增加数据库结构。

## 当前任务结束点

本卡完成后停止并等待技术负责人审核。未收到明确“通过”前，不得执行 ENG-05、ENG-06、ENG-07 或其他后续卡片；即使通过，ENG-07 仍须 ENG-06 Mock 闭环验收和单独复核。
