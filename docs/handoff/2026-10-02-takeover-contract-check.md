# 2026-10-02 总控接任：ENG-09 契约核查

## 接任范围与状态

用户要求本会话接任总控并完成剩余两张任务卡。根据当前交接文件与任务索引，按 ENG-09、ENG-10 顺序理解该范围：先实现并验证 ENG-09，再审核其门禁，随后推进 ENG-10。

已核对当前交接文件、任务索引、工程协议，以及相关 OpenAPI、生成客户端、数据库模型和原始迁移。ENG-09 的分享和反馈路由尚未接入；既有下载链路应复用。下面两项涉及冻结契约，需要用户裁决后才能实现。

## 差异一：分享资产集合

- `shared/api-contract/openapi.json:173`：`ShareCreateRequest.output_asset_ids` 为 UUID 数组，没有单图数量限制。
- `frontend/generated/types.gen.ts:110`：生成类型同样使用 `output_asset_ids: Array<string>`。
- `backend/app/db/models/engagement.py:49`：每条 `Share` 只有一个非空 `asset_id`。
- `backend/alembic/versions/0014_shares_feedback.py:34`：原始迁移同样只有单资产外键，并对 `(user_id, idempotency_key)` 设置唯一约束。

仅靠现有分享行无法持久化任意选定的多图集合；不能忽略请求中的第二张图，也不能将同一任务全部输出擅自公开。

待裁决选项：

1. 推荐：保留数组接口，支持一条链接分享同一成功任务的一张或两张结果图；追加分享与媒体关联表迁移，保留既有单资产字段及历史兼容规则。
2. 限制每条链接只分享一张图，并同步修订接口数量约束与生成客户端。

关联表的精确结构与历史回填规则将在选项确定后写入任务实施方案，当前尚未创建。

## 差异二：反馈分类长度

- `shared/api-contract/openapi.json:174`：`FeedbackCreateRequest.category.maxLength = 64`。
- `backend/app/db/models/engagement.py:91`：`Feedback.category` 为 `String(32)`。
- `backend/alembic/versions/0014_shares_feedback.py:86`：原始迁移为 `varchar(32)`。

长度 33 至 64 的请求符合 API 声明，但不能由当前数据库列完整保存；不能截断用户输入或把数据库异常作为正常字段校验。

待裁决选项：

1. 推荐：保留接口 64 字符，通过追加迁移将数据库列扩宽到 64。
2. 保留数据库 32 字符，同步收紧 OpenAPI 与生成客户端。

## 暂停依据与后续门禁

当前交接文件第 7.4 节明确要求："发现契约缺口先停线，不自行补接口/迁移。" 两项选择已向用户提出，尚未收到裁决。此时不修改业务代码、OpenAPI 或迁移。

如选择追加迁移，只在本轮新建的隔离 PostgreSQL 中执行和验证，不迁移共享 `ai_avatar`，不改写已应用迁移。其余既有禁令持续有效：不调用真实 Provider、不读取或使用真实凭据、不使用真实用户图片、不提交或清理 Git、不操作其他项目资源。

选项确定后，补齐两张任务卡的精确文件白名单和验证步骤。ENG-09 门禁包括分享与反馈的权限、字段、幂等与匿名隐私，生命周期删除确认、失败重试和批量边界，以及后端非 DB/DB、OpenAPI、前端测试、类型检查、构建和 diff 检查。ENG-10 再验证隔离环境的完整 Mock 闭环与浏览器流程。

本轮目前仅执行只读核查并新增此差异记录。交接文件中的测试数字仍是历史证据，尚未在本轮复跑；容器、端口与数据库实时状态尚未重新验证。
