import { afterEach, describe, expect, it, vi } from "vitest";

import nextConfig from "../next.config.mjs";

/**
 * 同源 API 代理的配置级回归。
 *
 * 这条代理是「浏览器同源 + Next 服务端转发」方案的唯一开关，而它的形状完全由
 * `next.config.mjs` 的 `rewrites()` 决定。因此这里直接调用真实的 `rewrites()`，
 * 钉住三件事：
 *
 * 1. 设置了 `API_PROXY_TARGET` 时，`/api/v1/**` 转发到 `<target>/api/v1/**`；
 * 2. `source` 覆盖 `/api/v1` 之下的**所有**路径（上传、分析、点数、生成、下载都在这条
 *    规则下面，少一条通配就等于某条链路静默 404）；
 * 3. 未设置目标时不注册任何 rewrite（本地 `next dev` 不会静默转发到猜出来的地址）。
 *
 * 真实端到端行为（Cookie、Origin、预签名 PUT）不在这里断言——那是浏览器闭环检查的
 * 职责，配置级测试只负责在改动本文件时立刻暴露代理形状的变化。
 */

type Rewrites = Awaited<ReturnType<NonNullable<typeof nextConfig.rewrites>>>;
/** `rewrites()` 的返回联合类型；本文件只断言数组形状，因此收窄成数组。 */
type RewriteRule = { source: string; destination: string };

async function rewritesWith(target: string | undefined): Promise<RewriteRule[]> {
  vi.stubEnv("API_PROXY_TARGET", target as string);
  return (await nextConfig.rewrites!()) as unknown as RewriteRule[];
}

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("同源 API 代理配置", () => {
  it("把 /api/v1/** 转发到配置的后端地址", async () => {
    const rules = await rewritesWith("http://127.0.0.1:8100");

    expect(rules).toEqual([
      { source: "/api/v1/:path*", destination: "http://127.0.0.1:8100/api/v1/:path*" },
    ]);
  });

  it("目标地址的尾部斜杠不会拼出双斜杠", async () => {
    const rules = await rewritesWith("http://backend:8000/");

    expect(rules).toEqual([
      { source: "/api/v1/:path*", destination: "http://backend:8000/api/v1/:path*" },
    ]);
  });

  it("通配覆盖全部已实现链路（上传 / 分析 / 点数 / 生成 / 下载）", async () => {
    const rules = await rewritesWith("http://backend:8000");
    const [rule] = rules;
    const paths = [
      "/api/v1/assets/uploads",
      "/api/v1/assets/00000000-0000-0000-0000-000000000000/complete",
      "/api/v1/assets/00000000-0000-0000-0000-000000000000/download-url",
      "/api/v1/analyses",
      "/api/v1/me/points",
      "/api/v1/me/points/ledger",
      "/api/v1/me/generations",
      "/api/v1/generations",
      "/api/v1/generations/00000000-0000-0000-0000-000000000000/outputs",
      "/api/v1/health/ready",
    ];

    // `/:path*` 的语义：前缀之后可以有任意多段
    const prefix = rule.source.replace("/:path*", "/");
    for (const path of paths) {
      expect(path.startsWith(prefix), path).toBe(true);
    }
  });

  it("未配置目标时不注册 rewrite，不猜默认后端", async () => {
    expect(await rewritesWith(undefined)).toEqual([]);
    expect(await rewritesWith("   ")).toEqual([]);
  });
});
