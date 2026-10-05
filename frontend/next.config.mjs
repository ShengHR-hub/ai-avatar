/**
 * Next.js 配置：同源 API 代理（部署收口）。
 *
 * 为什么需要它：契约的 `paths` 自带 `/api/v1`，而浏览器直连 `http://localhost:8000`
 * 时 `localhost:3000` 与 `localhost:8000` 是**跨源**（同站、不同源）。后端没有、也不应
 * 为了本地开发新增 CORS 中间件，因此浏览器只能在**同源**下调用 API；把 `/api/v1/*`
 * 在 Next 服务端转发到后端，Cookie（`SameSite=Lax`）、`Origin`、`credentials: include`
 * 与状态码语义都保持原样，前端一行 URL 拼接都不用改。
 *
 * 两个变量的分工（这是本文件唯一容易被误解的地方）：
 *
 * - `NEXT_PUBLIC_API_BASE_URL`：**构建期**由 Next 内联进浏览器 bundle，因此它必须
 *   与部署形态**无关**——固定写相对路径 `/api/v1`，浏览器解析成「当前页面自己的源」。
 *   写死 `http://localhost:8000/api/v1` 会同时踩两个坑：换域名要重新构建，
 *   而且再次变成跨源请求。
 * - `API_PROXY_TARGET`：**只存在于服务端进程**（不叫 `NEXT_PUBLIC_*`，绝不出现在
 *   浏览器 bundle 里）。Next 在**构建期**把它写进 `routes-manifest.json` 的 `rewrites`，
 *   所以换后端地址要重新 `next build`；这正是「代理目标属于构建产物」的正确代价，
 *   比在客户端暴露内部主机名安全得多。
 *
 * 未设置 `API_PROXY_TARGET` 时不注册任何 rewrite：本地 `next dev` 想直连别的后端时
 * 不会静默把请求转发到某个猜出来的地址，缺配置就是「前端自己的 404」。
 */

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  async rewrites() {
    const target = (process.env.API_PROXY_TARGET ?? "").trim().replace(/\/+$/, "");
    if (!target) return [];
    return [{ source: "/api/v1/:path*", destination: `${target}/api/v1/:path*` }];
  },
};

export default nextConfig;
