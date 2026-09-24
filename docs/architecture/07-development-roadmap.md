# AI Avatar Studio V1.0 开发阶段路线图

> 状态：已冻结
> 日期：2026-09-24
> 原则：先在本地跑通闭环，再进入测试服务器和生产部署

## Phase 0：工程与环境基线

目标：任何开发人员可以用统一命令启动完整本地环境。

交付范围：

- 单仓库目录；
- Next.js 和 FastAPI 基础项目；
- PostgreSQL、Redis、MinIO；
- Celery Worker 和 Celery Beat；
- Docker Compose；
- SQLAlchemy、Alembic 和第一版迁移；
- OpenAPI 导出与前端类型生成；
- 健康检查；
- 环境变量模板。

验收：

```text
docker compose 启动成功
FastAPI ready 检查通过
Next.js 可以访问 FastAPI
数据库迁移可从空库执行
Worker 可以接收测试任务
MinIO 可以上传和读取对象
```

## Phase 1：账号与点数

目标：建立可恢复身份和可审计点数体系。

交付范围：

- 邀请码兑换；
- 用户创建；
- 邮箱绑定和验证；
- Redis Session；
- 用户资料；
- 点数钱包；
- 点数流水；
- 计费配置；
- 基础审计日志。

验收：

```text
邀请码只能按规则兑换
邮箱可以恢复用户身份
点数增加、预留、消费和释放均有流水
重复请求不会重复发放或扣除点数
禁止直接修改余额
```

## Phase 2：Mock AI 生成闭环

目标：不依赖真实 AI 费用，在本地跑通核心产品闭环。

交付范围：

- 图片上传；
- Storage Adapter；
- MinIO；
- 创建生成任务；
- Celery 队列；
- MockProvider；
- 任务轮询；
- 结果图片；
- 点数预留、消费和释放；
- 生成历史；
- 下载、分享和反馈；
- 过期资源清理。

MockProvider 场景：

```text
成功
失败
超时
延迟
UNKNOWN
成功但没有图片
```

验收：

```text
上传 → 扣点预留 → 生成 → 保存 → 结算 → 展示完整通过
失败自动释放点数
结果缺失进入 NEEDS_REVIEW
刷新或重启后任务可以恢复
重复回调不重复结算
```

## Phase 3：真实 AI 接入

目标：用 PrimaryProvider 替换 MockProvider，并保持业务层不变。

交付范围：

- PrimaryProvider；
- ProviderRouter；
- 同步或异步接口适配；
- 轮询与 Webhook；
- Provider 调用记录；
- 用量和估算成本；
- 错误分类；
- 指数退避；
- 超时恢复；
- 人工异常处理；
- 参考图 Feature Flag。

验收：

```text
真实照片生成成功
真实文字生成成功
Provider 错误被正确归类
失败释放点数
真实成本可以统计
无法确认结果时不盲目重试或自动退款
切换 Provider 不修改 Generation Service
```

## Phase 4：前端体验与公开内容

目标：完成适合真实用户测试的产品体验。

交付范围：

- 首页；
- 照片生成和纯 Prompt 生成；
- 风格选择；
- Prompt 模板；
- 上传与校验反馈；
- 异步等待页面；
- 结果页；
- 用户中心和历史记录；
- 分享页面；
- SEO 与国际化基础；
- 移动端适配；
- 可访问性和错误状态。

验收：

```text
陌生用户能理解产品并完成一次生成
任务等待期间可以离开并恢复
手机端可以完成上传、生成、下载和分享
公开案例页和分享页可被搜索引擎正确读取
所有错误状态都有可执行的用户反馈
```

## Phase 5：测试服务器

触发条件：Phase 0 至 Phase 4 的本地闭环和验收全部通过。

目标：让合伙人和测试用户在真实网络环境中验证产品。

交付范围：

- 单服务器 Docker Compose；
- Nginx 和 HTTPS；
- 托管 PostgreSQL；
- COS / OSS / S3；
- 备份与恢复；
- 监控和告警；
- 测试域名；
- 真实用户测试记录。

该阶段根据测试反馈决定生产环境投入，不提前引入 Kubernetes 或复杂多机架构。

## Task Card 拆分规则

路线图中的每个 Phase 必须继续拆成独立 Task Card。每张卡片必须包含：

```text
目标
背景与依赖
允许修改的文件
输入
输出
接口契约
状态和错误处理
测试要求
验收标准
明确不做的内容
```

Task Card 应按依赖顺序执行。任何涉及点数、任务状态或 Provider 行为的卡片，在实现前必须由技术负责人审核契约。
