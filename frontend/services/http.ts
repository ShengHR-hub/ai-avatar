import { client } from "@/generated/client.gen";
import { apiBase, apiBaseOrNull, requestBase } from "./config";
import { ApiFailure, toApiFailure } from "./errors";

/** 每个请求一个关联 ID：与后端 `request_id` 对齐，用户报障时才可能对得上账。 */
export function newRequestId(): string {
  if (typeof globalThis.crypto?.randomUUID === "function") return crypto.randomUUID();
  return `req-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

/**
 * 统一出站口径：`credentials: include` 保证 Session Cookie 随每个请求发出，
 * `X-Request-Id` 在这里补齐，而不是指望每个调用点都记得加。
 *
 * 生成客户端把请求包成 `Request` 再交给 fetch，所以这里必须从 `Request` 上取回
 * method / headers / body 再转发：只补一个头就重新发一遍 `init` 会把
 * `Content-Type`、`Idempotency-Key` 连同请求体一起丢掉。
 *
 * 契约把 `GET /assets/{id}` 与 `POST /assets/{id}/download-url` 标为 `security: []`
 * （匿名分享读取也走这两个口），生成物因此可能不带凭据；服务端按「可选 viewer」判
 * 所有权，不带 Cookie 时用户自己的私有资源会被当成不存在（404）。凭据必须在传输层
 * 无条件补上，不能依赖生成物里的 security 声明。
 */
export async function instrumentedFetch(
  input: RequestInfo | URL,
  init?: RequestInit,
): Promise<Response> {
  if (input instanceof Request) {
    const headers = new Headers(input.headers);
    if (!headers.has("X-Request-Id")) headers.set("X-Request-Id", newRequestId());
    const credentials = input.credentials === "omit" ? "omit" : (init?.credentials ?? "include");
    assertApiHost(input.url);
    // 以 (Request, init) 转发：未指定的成员（方法、请求体）沿用原 Request，
    // 只覆盖 headers/credentials。重建成新 URL 会把 `Idempotency-Key` 的请求体丢掉。
    return globalThis.fetch(input, { headers, credentials });
  }

  const headers = new Headers(init?.headers);
  if (!headers.has("X-Request-Id")) headers.set("X-Request-Id", newRequestId());
  assertApiHost(String(input));
  return globalThis.fetch(input, {
    ...init,
    headers,
    credentials: init?.credentials ?? "include",
  });
}

/**
 * 请求必须真的落在配置的 API base 上。
 *
 * 部署默认是**相对同源**（`NEXT_PUBLIC_API_BASE_URL=/api/v1`）：此时请求 URL 就是当前
 * 页面的源，由 Next 的同源代理转发到后端。这种配置下「打错主机」= 打到别的域，
 * 因此仍然逐字核对当前页面的源——带 Session Cookie 的请求绝不能因为配置形状变了
 * 就被放行到第三方主机。
 *
 * 绝对配置（前端与 API 分域）下核对的是配置里的 origin，语义完全一致。
 *
 * 对象存储的预签名 URL 不走这里（由 `putSignedBytes` 直连签发方给的目标）。
 */
function assertApiHost(url: string): void {
  const base = apiBaseOrNull();
  if (base === null) {
    throw new Error("缺少 NEXT_PUBLIC_API_BASE_URL：请求会被发到前端自己的域");
  }
  const target = new URL(url, typeof document === "undefined" ? undefined : document.baseURI);
  if (base) {
    if (target.origin !== base) {
      throw new Error(`请求目标 ${target.origin} 与配置的 API origin ${base} 不一致`);
    }
    return;
  }
  const page = requestBase();
  if (page && target.origin !== page) {
    throw new Error(
      `相对同源配置只允许请求当前页面的源；请求目标 ${target.origin} 与页面源 ${page} 不一致`,
    );
  }
}

/**
 * 生成路径自带 `/api/v1`，这里只补 base：
 *
 * - 相对配置（`""`）下**原样返回**，让浏览器把它解析成当前源的同源请求
 *   （这正是同源代理生效的形状；补一个绝对 origin 反而会绕开代理并变成跨源）；
 * - 绝对配置下补 origin，不再补第二次 `/api/v1` 前缀。
 */
function absolute(url: string): string {
  if (/^https?:\/\//i.test(url)) return url;
  const base = apiBase();
  const path = url.startsWith("/") ? url : `/${url}`;
  return base ? `${base}${path}` : path;
}

/** 同源断言用的基准（测试可见）：相对配置下是当前页面的源。 */
export const apiRequestBase = requestBase;

/**
 * 幂等装配：单例客户端只配置一次。构建期 `next build` 会预渲染页面，此时允许
 * 配置缺失（不静默编造默认 origin）；真正发请求时 `absolute()` 才会明确报错。
 *
 * 相对同源配置下 `baseUrl` 留空：生成客户端直接用契约里的相对路径发请求，
 * 浏览器补上当前的源，Next 的同源代理再把 `/api/v1/*` 转到后端。
 */
export function configureApiClient(): void {
  const base = apiBaseOrNull();
  client.setConfig({
    ...(base ? { baseUrl: base } : {}),
    credentials: "include",
    fetch: instrumentedFetch,
    headers: { Accept: "application/json" },
  });
}

configureApiClient();

/** 生成客户端在默认 `responseStyle: 'fields'` 下的结果形状。 */
export type RequestOutcome<TBody> = {
  data?: TBody;
  error?: unknown;
  request?: Request;
  response?: Response;
};

export type Outcome<TBody> = PromiseLike<RequestOutcome<TBody>> | RequestOutcome<TBody>;

function failureOf(outcome: RequestOutcome<unknown>): ApiFailure {
  const payload = outcome.error !== undefined ? outcome.error : (outcome.data ?? undefined);
  return toApiFailure(payload, outcome.response?.status ?? 0, outcome.response?.headers.get("X-Request-Id") ?? null);
}

/**
 * 取整个响应体（需要 `Envelope` 同级的 `page`/`request_id` 时用这个）。非 2xx、带
 * `error`、或 2xx 却没有载荷，一律抛稳定码失败——绝不把「没有载荷」当成功交给页面。
 */
export async function unwrap<TBody>(result: Outcome<TBody>): Promise<TBody> {
  const settled = await result;
  if (!settled.response || !settled.response.ok || settled.error !== undefined || settled.data === undefined) {
    throw failureOf(settled);
  }
  return settled.data;
}

/** 取 `Envelope.data`：单对象读取用；内层缺失按响应畸形处理。 */
export async function unwrapData<TEnvelope extends { data?: unknown }>(
  result: Outcome<TEnvelope>,
): Promise<NonNullable<TEnvelope["data"]>> {
  const body = await unwrap<TEnvelope>(result);
  if (typeof body !== "object" || body === null || body.data === undefined) {
    throw toApiFailure(body, 200, null);
  }
  return body.data as NonNullable<TEnvelope["data"]>;
}

/**
 * 对象存储直传：只按签好的 URL 送原始字节。
 *
 * 预签名 URL 本身就是短时凭据，`credentials: omit` 确保不带 Session Cookie，也不附加
 * `Authorization`；请求体用 `ArrayBuffer`，免得浏览器替我们猜一个签名口径之外的
 * `Content-Type`。
 */
export async function putSignedBytes(url: string, bytes: ArrayBuffer): Promise<void> {
  const response = await globalThis.fetch(url, {
    method: "PUT",
    body: bytes,
    credentials: "omit",
    headers: {},
  });
  if (!response.ok) {
    throw toApiFailure(undefined, response.status, response.headers.get("X-Request-Id"));
  }
}
