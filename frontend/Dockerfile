# AI Avatar Studio V1.0 — 前端镜像（ENG-10）
#
# 为什么是**两阶段**：`next build` 需要 devDependencies（TypeScript、Turbopack 插件），
# 而运行期只需要生产依赖 + 构建产物。单阶段镜像会把编译器一起发到线上。
#
# ## 两个变量必须在**构建期**给到（这是本文件最容易搞错的地方）
#
# `NEXT_PUBLIC_*` 会被 Next 静态替换进浏览器 bundle，`API_PROXY_TARGET` 会被写进
# `routes-manifest.json` 的 `rewrites`（见 `frontend/next.config.mjs` 的说明）。
# 两者都是**构建产物**的一部分，因此只能用 `ARG` 在 build 阶段注入；
# 在运行期 `-e` 传是无效的（浏览器拿到的仍是构建时的值）。
#
# 默认值与 `docker/compose.yml` 的前端环境变量保持一致：
# - `NEXT_PUBLIC_API_BASE_URL=/api/v1`：浏览器用**相对同源**路径，避免跨源；
# - `API_PROXY_TARGET=http://backend:8000`：容器网络内的后端服务名。
# 换成别的后端地址必须**重新构建**，这正是「代理目标属于构建产物」的代价。
#
# ## 浏览器不持有任何服务端密钥
#
# 本镜像不接收 `DATABASE_URL`、`SESSION_SECRET`、`STORAGE_*`、`AI_API_KEY`；
# 上面两个 ARG 都是公开值（`09-repository-boundary.md` 第 6.3 节）。

FROM node:22-alpine AS build

WORKDIR /app

# 先装依赖：源码改动不会让依赖层失效
COPY package.json package-lock.json ./
RUN npm ci

COPY . .

ARG NEXT_PUBLIC_API_BASE_URL=/api/v1
ARG NEXT_PUBLIC_APP_URL=http://localhost:3000
ARG API_PROXY_TARGET=http://backend:8000

ENV NEXT_PUBLIC_API_BASE_URL=$NEXT_PUBLIC_API_BASE_URL \
    NEXT_PUBLIC_APP_URL=$NEXT_PUBLIC_APP_URL \
    API_PROXY_TARGET=$API_PROXY_TARGET \
    NEXT_TELEMETRY_DISABLED=1

RUN npm run build


FROM node:22-alpine AS runtime

WORKDIR /app

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000

# 运行期只需要生产依赖 + 构建产物 + 静态资源
COPY package.json package-lock.json ./
RUN npm ci --omit=dev && npm cache clean --force

COPY --from=build /app/.next ./.next
COPY --from=build /app/next.config.mjs ./
COPY --from=build /app/public ./public

EXPOSE 3000

CMD ["npm", "run", "start"]
