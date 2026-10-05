# frontend/ — V1.0 前端边界（ENG-00 骨架）

> 状态：骨架占位，不含任何业务实现。
> 依据：`docs/architecture/09-repository-boundary.md`、`docs/task-cards/ENG-00-engineering-foundation.md`

## 职责

Next.js + React + TypeScript 前端。负责页面、布局、SEO、上传与生成交互、调用 FastAPI，并展示服务端状态。

## 目录

| 目录 | 职责 |
|---|---|
| `app/` | 路由、布局和页面组合 |
| `components/` | 可复用 UI 和领域组件 |
| `hooks/` | 客户端交互和状态封装 |
| `services/` | OpenAPI 客户端调用和 DTO 转换 |
| `types/` | 前端专用类型 |
| `generated/` | 由 OpenAPI 生成的客户端和类型 |
| `public/` | 不敏感静态资源 |

## 边界约束

- 不持有 PostgreSQL、Redis、MinIO 或 Provider 凭据；
- 不使用基础设施 SDK，浏览器对象传输只使用 API 签发的短期 URL；
- 不复制后端业务规则，不成为第二套 API 实现；
- 只接收 `NEXT_PUBLIC_*` 非敏感公开配置。

## 当前状态（ENG-08）

依赖已按 lockfile 锁定：Next 16.3.6、React 19.3.0、TypeScript 5.9.3、Vitest 5.0.2 +
React Testing Library 16.3.3、@hey-api/openapi-ts 0.99.0。

| 命令 | 作用 |
|---|---|
| `npm run generate:api` | 从 `../shared/api-contract/openapi.json` 重新生成 `generated/` |
| `npm run typecheck` | `tsc --noEmit` |
| `npm test` | Vitest + RTL（jsdom，`globalThis.fetch` 替身） |
| `npm run dev` / `build` / `start` | 本地开发、生产构建、运行 |

`NEXT_PUBLIC_API_BASE_URL` 必须含 `/api/v1`：契约的 `paths` 自带该前缀，
`services/config.ts` 会剥掉后缀，避免 `/api/v1/api/v1/...`；缺失或指向别的主机时
请求在出门前直接失败，而不是悄悄打到前端自己的域。构建期允许缺省（预渲染不炸），
运行期必须存在。

## 同源 API 代理（部署收口）

浏览器**不直连**后端端口：`localhost:3000` 与 `localhost:8000` 在浏览器里是跨源
（同站、不同源），而后端没有也不应为了本地开发新增 CORS 中间件。因此：

| 变量 | 生效位置 | 取值 |
|---|---|---|
| `NEXT_PUBLIC_API_BASE_URL` | 浏览器 bundle（**构建期**内联） | 相对路径 `/api/v1` → 请求落在当前站点自己的源 |
| `API_PROXY_TARGET` | Next 服务端进程（**构建期**写进 `routes-manifest.json`） | 后端可达地址，Compose 内是 `http://backend:8000` |

`next.config.mjs` 的 `rewrites()` 把 `/api/v1/:path*` 转发到
`${API_PROXY_TARGET}/api/v1/:path*`；未设置目标时不注册任何 rewrite（`next dev`
不会静默转发到猜出来的地址）。`API_PROXY_TARGET` **不是** `NEXT_PUBLIC_*`，
内部主机名不会进入浏览器 bundle。

两个值都在构建期固化，所以换部署形态要重新 `npm run build`。`tests/proxy-config.test.ts`
在配置层钉住这条规则；真实端到端行为（Cookie、Origin、预签名 PUT）由浏览器闭环检查覆盖。

`allowJs` 在 `tsconfig.json` 中为 `true`（`checkJs: false`）：只为让 `typecheck`
能解析 `next.config.mjs`（`tests/proxy-config.test.ts` 直接导入真实的 `rewrites()`），
不引入任何 JS 业务代码。

## 当前状态（ENG-08）

依赖已按 lockfile 锁定：Next 16.3.6、React 19.3.0、TypeScript 5.9.3、Vitest 5.0.2 +
React Testing Library 16.3.3、@hey-api/openapi-ts 0.99.0。

| 命令 | 作用 |
|---|---|
| `npm run generate:api` | 从 `../shared/api-contract/openapi.json` 重新生成 `generated/` |
| `npm run typecheck` | `tsc --noEmit` |
| `npm test` | Vitest + RTL（jsdom，`globalThis.fetch` 替身） |
| `npm run dev` / `build` / `start` | 本地开发、生产构建、运行 |

ENG-08 交付：邀请码/邮箱验证入口、照片上传并确认 `READY`、`PHOTO_PROMPT`/`TEXT_PROMPT`
两种模式、固定两张结果、等待与刷新恢复、`FAILED`/`NEEDS_REVIEW` 分列、结果短期 URL
下载、点数余额与生成历史。分享、反馈、生命周期清理与营销页属 `ENG-09`/后续卡片。

### 已知环境限制：Vitest 并行度

默认的文件级并行（`forks`/`threads` 池）在本机可用内存低于约 3GB 时会让 Node
worker 以 exit code 134（V8 `Zone Allocation failed`）崩溃。内存紧张时用

```powershell
npx vitest run --no-file-parallelism
```

结果与默认并行度完全一致（同一套用例），只是不并发跑测试文件。
