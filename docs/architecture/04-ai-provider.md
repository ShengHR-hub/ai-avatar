# AI Avatar Studio V1.0 AI Provider 架构

> 状态：已冻结  
> 调用方式：Provider Adapter  
> 目标：隔离供应商差异，保证任务、成本和账务可恢复

## 1. 调用边界

```text
Generation API
    ↓
Generation Service
    ↓
AIProvider Interface
    ↓
ProviderRouter
    ↓
MockProvider / PrimaryProvider
```

业务代码禁止直接调用 OpenAI、国产模型或中转站 SDK。具体 Provider 只负责协议转换和错误转换。

## 2. 统一接口

逻辑接口：

```python
class AIProvider(Protocol):
    def generate(self, request: GenerateRequest) -> GenerateResult | PendingResult:
        ...

    def check_status(self, provider_task_id: str) -> ProviderStatus:
        ...

    def cancel(self, provider_task_id: str) -> CancelResult:
        ...
```

统一请求字段：

```text
mode
prompt
source_images
reference_images
style
output_count
model
client_request_id
```

统一结果：

```text
SUCCEEDED + outputs
PENDING + provider_task_id
FAILED + normalized error
```

统一 Provider 状态：

```text
PENDING
PROCESSING
SUCCEEDED
FAILED
CANCELLED
UNKNOWN
```

统一错误分类：

```text
VALIDATION_ERROR
AUTHENTICATION_ERROR
CONTENT_POLICY_ERROR
RATE_LIMIT_ERROR
TEMPORARY_PROVIDER_ERROR
TIMEOUT_ERROR
UNKNOWN_PROVIDER_ERROR
```

## 3. Provider 实现

```text
backend/app/providers/
├── base.py
├── router.py
├── mock_provider.py
└── primary_provider.py
```

第一阶段提供两个实现：

- `MockProvider`：本地闭环、自动化测试和异常模拟；
- `PrimaryProvider`：真实供应商适配器。

本地默认：

```text
AI_PROVIDER=mock
```

真实调用通过配置启用：

```text
AI_PROVIDER=primary
AI_MODEL=...
AI_API_KEY=...
```

AI 凭据只存在 FastAPI/Worker 服务端环境，不进入前端或日志。

## 4. 同步 Provider

```text
Worker
↓
provider.generate()
↓
图片结果
↓
图片校验与处理
↓
对象存储
↓
任务 SUCCEEDED
```

供应商原始响应不直接返回前端。

## 5. 异步 Provider

```text
Worker
↓
provider.generate()
↓
provider_task_id
↓
轮询 check_status() 或等待 Webhook
↓
归一化状态
↓
取得结果
↓
对象存储
↓
任务 SUCCEEDED
```

轮询和 Webhook 都调用同一个幂等的 `reconcile_provider_status()`，处理回调乱序、重复回调和 Worker 重启。

## 6. 任务和 Provider 调用状态

内部任务状态：

```text
CREATED → QUEUED → PROCESSING
                         ├──→ SUCCEEDED
                         ├──→ FAILED
                         ├──→ CANCELLED
                         └──→ NEEDS_REVIEW
```

Provider 调用状态：

```text
STARTED
SUBMITTED
SUCCEEDED
FAILED
TIMEOUT
UNKNOWN
```

`UNKNOWN` 只描述 Provider 调用结果未知；当系统无法确认最终图片或扣费结果时，业务任务进入 `NEEDS_REVIEW`。

## 7. ProviderRouter

第一阶段不做复杂自动路由。Router 根据 `generation_cost_configs` 和环境配置选择固定主 Provider 与模型：

```text
mode
model_name
provider_name
enabled
```

未来增加 Provider 时，只新增适配器、配置和能力声明，不修改 Generation Service。

## 8. 超时和重试

默认配置：

```text
连接超时：10 秒
读取超时：180 秒
任务最大处理时间：240 秒
临时错误最大重试：3 次
```

可重试：网络失败、Provider 5xx、连接/读取超时、限流。

不可重试：参数错误、图片无效、内容政策拒绝、凭据无效。

指数退避：

```text
5 秒 → 30 秒 → 120 秒
```

Provider 已接受请求但响应丢失时，必须先使用 `client_request_id` 或 `provider_request_id` 查询。无法确认时进入 `UNKNOWN` / `NEEDS_REVIEW`，禁止盲目重新生成。

## 9. Worker 并发与恢复

领取任务使用数据库行锁和 `SKIP LOCKED`，同一任务只能由一个 Worker 执行。

Worker 崩溃恢复：

```text
processing_at 超时
↓
watchdog 检查 provider_attempts
↓
已提交：查询原请求
未提交：重新入队
无法确认：NEEDS_REVIEW
```

## 10. 成功结算

一个数据库事务完成：

```text
锁定任务
→ 校验尚未结算
→ 保存 generation_outputs 和 media_assets
→ 更新任务 SUCCEEDED
→ 写入 point_ledger: CONSUME
→ 更新钱包 reserved_points / lifetime_consumed
→ 更新 provider_attempts
→ 写入 audit_logs
```

重复回调和 Worker 重试必须返回已有结算结果。

## 11. 失败释放和人工复核

普通明确失败：

```text
任务 FAILED
→ 写入 point_ledger: RELEASE
→ available_points 增加
→ reserved_points 减少
```

结果不确定：

```text
任务 NEEDS_REVIEW
→ 点数保持 RESERVED
→ 不自动退款
→ 管理员确认 CONSUME 或 RELEASE
→ 写入管理员审计日志
```

同一个 `generation_id` 最多一条 `CONSUME` 或 `RELEASE`，防止重复结算。

## 12. 成本记录

每次 Provider 调用写入 `provider_attempts`：

```text
input_tokens
output_tokens
image_count
estimated_cost
currency
```

同时记录模型、Provider、调用时长、外部请求 ID 和脱敏的请求/响应元数据。Token 字段对图片模型可以为空。成本按调用时价格快照估算，用于后台统计真实 AI 成本，不直接决定用户点数。

## 13. 图片处理

Worker 负责：

```text
接收 Provider 输出
→ 校验 MIME 和图片可解码性
→ 读取尺寸和哈希
→ 生成需要的展示版本
→ 上传对象存储
→ 写入 media_assets
```

数据库只保存对象存储引用，不保存 Base64 图片。

## 14. 参考图 Feature Flag

参考图模式虽然在 API 和数据模型中预留，但第一阶段默认关闭：

```text
ENABLE_REFERENCE_IMAGE=false
```

关闭时明确返回 `FEATURE_NOT_AVAILABLE`，不能接受参数后静默忽略参考图。

## 15. MockProvider 验收场景

MockProvider 必须能稳定模拟：

```text
成功
延迟
失败
超时
UNKNOWN
成功但没有图片
重复回调
```

这些场景用于验证点数、任务恢复、人工复核和幂等，不依赖真实 Provider 费用。
