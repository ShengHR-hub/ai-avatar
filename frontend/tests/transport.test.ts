import { afterEach, describe, expect, it, vi } from "vitest";

import { fetchSession, fetchAsset, submitGeneration } from "@/services/api";
import { instrumentedFetch, putSignedBytes } from "@/services/http";
import { envelope, errorBody, generation, installFetchStub } from "./helpers/stub";

let stub = installFetchStub();
afterEach(() => stub.restore());

describe("传输层口径", () => {
  it("给每个请求补 X-Request-Id，并带 Session Cookie", async () => {
    stub = installFetchStub({
      "GET /api/v1/auth/session": {
        json: envelope({ session_id: "s", user_id: "u", email_verified: true, expires_at: "2026-01-01T00:00:00Z" }),
      },
    });
    await fetchSession();
    const call = stub.last("GET /api/v1/auth/session")!;
    expect(call.headers["x-request-id"]).toBeTruthy();
    expect(call.credentials).toBe("include");
  });

  it("security 为空的资产接口也必须带凭据，否则私有资源会被当成不存在", async () => {
    stub = installFetchStub({
      "GET /api/v1/assets/a1": { json: envelope({ id: "a1", status: "READY" }) },
    });
    await fetchAsset("a1").catch(() => undefined);
    const call = stub.last("GET /api/v1/assets/a1")!;
    expect(call.credentials).toBe("include");
    expect(call.headers["x-request-id"]).toBeTruthy();
  });

  it("路径只有一个 /api/v1 前缀", async () => {
    stub = installFetchStub({ "POST /api/v1/generations": { json: envelope(generation()) } });
    await submitGeneration({ draftId: "d1", mode: "TEXT_PROMPT", prompt: "酷一点", assetIds: [] });
    const call = stub.last("POST /api/v1/generations")!;
    expect(call.pathname).toBe("/api/v1/generations");
    expect(call.url).toBe("http://backend.invalid/api/v1/generations");
  });

  it("预签名 PUT 不带 Cookie、不带 Authorization、只送原始字节", async () => {
    const real = globalThis.fetch;
    const calls: { url: string; init?: RequestInit }[] = [];
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ url: String(input), init });
      return new Response(null, { status: 200 });
    }) as unknown as typeof fetch;

    try {
      await putSignedBytes("http://minio.invalid/signed/abc?X-Amz-Signature=1", new ArrayBuffer(8));
    } finally {
      globalThis.fetch = real;
    }

    expect(calls[0].init?.credentials).toBe("omit");
    const headers = new Headers(calls[0].init?.headers);
    expect(headers.get("authorization")).toBeNull();
    expect(headers.get("cookie")).toBeNull();
    expect(headers.get("x-request-id")).toBeNull();
    expect(calls[0].init?.body).toBeInstanceOf(ArrayBuffer);
  });

  it("非 2xx 归一为稳定码，不解析 message 文本", async () => {
    stub = installFetchStub({ "POST /api/v1/generations": { status: 402, json: errorBody("INSUFFICIENT_POINTS", "点数不够") } });
    await expect(
      submitGeneration({ draftId: "d2", mode: "TEXT_PROMPT", prompt: "x", assetIds: [] }),
    ).rejects.toMatchObject({ code: "INSUFFICIENT_POINTS", status: 402, action: "recharge" });
  });
});

describe("出站主机守卫", () => {
  it("相对路径或打错主机的请求在出门前就失败，而不是悄悄落到前端自己的域", async () => {
    const real = globalThis.fetch;
    const seen: string[] = [];
    globalThis.fetch = (async (input: RequestInfo | URL) => {
      seen.push(String(input instanceof Request ? input.url : input));
      return new Response(null, { status: 204 });
    }) as unknown as typeof fetch;

    try {
      await expect(
        instrumentedFetch(new Request("http://frontend.invalid/api/v1/me")),
      ).rejects.toThrow(/不一致/);
      expect(seen).toHaveLength(0);

      const ok = await instrumentedFetch(new Request("http://backend.invalid/api/v1/me"));
      expect(ok.status).toBe(204);
      expect(seen).toEqual(["http://backend.invalid/api/v1/me"]);
    } finally {
      globalThis.fetch = real;
    }
  });

  /**
   * 部署形状：`NEXT_PUBLIC_API_BASE_URL=/api/v1`（相对同源）。
   *
   * 此时请求必须原样发到**当前页面的源**——那正是 Next 同源代理的入口。补一个绝对
   * origin 会绕开代理并重新变成跨源请求（Cookie 与 CORS 都会出问题），
   * 而打到别的域则必须在出门前就被拦下。
   */
  it("相对同源配置：请求落在当前源，跨主机在出门前被拦下", async () => {
    vi.stubEnv("NEXT_PUBLIC_API_BASE_URL", "/api/v1");
    const page = new URL(document.baseURI).origin;
    const real = globalThis.fetch;
    const seen: string[] = [];
    globalThis.fetch = (async (input: RequestInfo | URL) => {
      seen.push(String(input instanceof Request ? input.url : input));
      return new Response(null, { status: 204 });
    }) as unknown as typeof fetch;

    try {
      const ok = await instrumentedFetch(new Request(`${page}/api/v1/me/points`));
      expect(ok.status).toBe(204);
      expect(seen).toEqual([`${page}/api/v1/me/points`]);

      await expect(
        instrumentedFetch(new Request("http://elsewhere.invalid/api/v1/me/points")),
      ).rejects.toThrow(/页面源/);
      expect(seen).toHaveLength(1);
    } finally {
      globalThis.fetch = real;
      vi.unstubAllEnvs();
    }
  });
});
