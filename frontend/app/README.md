# frontend/app/

Next.js App Router 路由与页面组合（ENG-08 已落地）：

| 路由 | 内容 |
|---|---|
| `/` | 工作台：未开通时给邀请码/邮箱入口，已验证会话直接进入上传 → 描述 → 结果 |
| `/verify-email` | 邮件链接落地页，读 `?token=` 后提交 `verifyEmail` |
| `/me` | 点数余额与生成历史（cursor 分页） |

约束（`06-development-standard.md` 第 3 节）：

- 页面不直接 `fetch`，一律经 `hooks/` → `services/` → 生成的 OpenAPI 客户端；
- 不在页面里解释后端错误码，`services/errors.ts` 是唯一映射处；
- 轮询只在 `services/watch.ts` 实现一次，刷新恢复走同一入口，绝不重复提交；
- 任务状态、点数余额与结果一律以服务端响应为准。

`useSearchParams` 的页面（`/`、`/verify-email`）必须有 `Suspense` 边界，否则构建期报错。
