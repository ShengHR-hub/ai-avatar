/**
 * 运行时配置：只读 `NEXT_PUBLIC_*`。
 *
 * 冻结契约里 `paths` 自带 `/api/v1` 前缀（`servers` 是 `/`），生成客户端的每个
 * `url` 也都是 `/api/v1/...`。因此这里只接受两种形状：
 *
 * - **相对同源**（部署默认）：`/api/v1`——浏览器把它解析成「当前页面自己的源」，
 *   再由 Next 服务端的同源代理（`next.config.mjs` 的 `rewrites`）转发到后端。
 *   这是唯一与域名无关、也不会变成跨源请求的形状；
 * - **绝对 origin**（可选覆盖）：`https://api.example.com` 或
 *   `https://api.example.com/api/v1`——用于前端与 API 真的分域部署的场合。
 *
 * 两种形状都容忍尾部的 `/api/v1`（compose 模板历史值就带它），解析时剥掉，
 * 避免 `.../api/v1/api/v1/...`。
 *
 * 解析必须是惰性的：`next build` 会预渲染页面，模块顶层就抛错会让构建直接失败；
 * 缺配置是「第一次发请求时」才算真错误。
 */

const PREFIX = "/api/v1";

/** 去掉尾部所有 `/`，再剥掉契约自带的 `/api/v1` 后缀。 */
function stripPrefix(value: string): string {
  const withoutTrailing = value.replace(/\/+$/, "");
  return withoutTrailing.toLowerCase().endsWith(PREFIX)
    ? withoutTrailing.slice(0, -PREFIX.length)
    : withoutTrailing;
}

/**
 * 公开 API base 的**字符串**形状：相对路径（`/api/v1` → `""`）或绝对 origin。
 *
 * 不做 `new URL()` 校验：相对路径在 Node 里没有 base 可解析，硬套 `new URL` 会把
 * 合法配置判成错误。真正的「这个地址能不能用」由 `assertApiHost` 按请求实际落点核对。
 */
export function apiBaseOrNull(): string | null {
  const value = (process.env.NEXT_PUBLIC_API_BASE_URL ?? "").trim();
  if (!value) return null;
  const base = stripPrefix(value);
  if (!base) return ""; // 纯相对：浏览器补当前的源
  if (!/^https?:\/\//i.test(base)) {
    throw new Error("NEXT_PUBLIC_API_BASE_URL 必须是以 / 开头的相对路径或 http(s) origin");
  }
  return new URL(base).origin;
}

export function apiBase(): string {
  const base = apiBaseOrNull();
  if (base === null) throw new Error("缺少 NEXT_PUBLIC_API_BASE_URL 配置");
  return base;
}

/** 兼容既有调用点的名字：返回剥离前缀后的 base（相对配置返回空串）。 */
export const apiOriginOrNull = apiBaseOrNull;
export const apiOrigin = apiBase;

/**
 * 显式分域部署时把配置解析成绝对 origin（相对配置无法解析成 origin，会抛错）。
 *
 * 部署默认是相对同源，因此业务代码**不要**用它发请求——那正是会绕开同源代理的形状。
 * 它保留给「需要把 API 地址渲染成绝对链接」的少数场合，以及契约测试。
 */
export function resolveOrigin(raw: string | undefined | null): string {
  const value = (raw ?? "").trim();
  if (!value) throw new Error("缺少 NEXT_PUBLIC_API_BASE_URL 配置");
  const base = stripPrefix(value);
  if (!base || !/^https?:\/\//i.test(base)) {
    throw new Error("NEXT_PUBLIC_API_BASE_URL 必须是 http(s) origin");
  }
  const parsed = new URL(base);
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    throw new Error("NEXT_PUBLIC_API_BASE_URL 必须是 http(s) origin");
  }
  return parsed.origin;
}

/**
 * 请求落点与配置基址比对用的基准。
 *
 * 相对配置下基准取**当前页面自己的源**：`instrumentedFetch` 只能在浏览器里跑
 * （`next build` 期不执行），因此 `document.baseURI` 一定存在；测试环境没有
 * `document`，此时退化成「不做同源断言」，由测试自己控制 host。
 * 绝对配置下基准就是它自己。
 */
export function requestBase(): string | null {
  const base = apiBaseOrNull();
  if (base === null) return null;
  if (base) return base;
  if (typeof document !== "undefined" && document.baseURI) {
    return new URL(document.baseURI).origin;
  }
  return null;
}

/** 契约固定值：V1.0 每次生成恒为两张，前端不得提供数量选择。 */
export const OUTPUT_COUNT = 2 as const;

/**
 * 已开放的模式集合。`REFERENCE_IMAGE` 在服务端受 `ENABLE_REFERENCE_IMAGE=false`
 * 门禁，恒返回 `FEATURE_NOT_AVAILABLE`，因此入口不出现（不是静默忽略后误提交）。
 */
export const OPEN_MODES = [
  { value: "PHOTO_PROMPT", label: "照片转绘" },
  { value: "TEXT_PROMPT", label: "文字描述生成" },
] as const;

export type OpenMode = (typeof OPEN_MODES)[number]["value"];

/** 轮询节奏：3 秒起、指数到 10 秒封顶；实测单次出图 34 秒，等待可预期。 */
export const POLL_INITIAL_MS = 3_000;
export const POLL_MAX_MS = 10_000;

/** 服务端 `NEEDS_REVIEW`/`FAILED` 之外的等待上限，超过后停止轮询并提示刷新。 */
export const POLL_GIVE_UP_MS = 600_000;
