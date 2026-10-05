export type Recorded = {
  method: string;
  url: string;
  pathname: string;
  headers: Record<string, string>;
  credentials: string | undefined;
  body: unknown;
};

export type Reply = { status?: number; json?: unknown };

export type Stub = {
  calls: Recorded[];
  restore: () => void;
  routes: (table: Record<string, Reply | ((call: Recorded) => Reply)>) => void;
  last: (match: string | RegExp) => Recorded | undefined;
  count: (match: string | RegExp) => number;
};

function headerRecord(init?: RequestInit): Record<string, string> {
  const headers = new Headers(init?.headers);
  const out: Record<string, string> = {};
  headers.forEach((value, key) => {
    out[key.toLowerCase()] = value;
  });
  return out;
}

/**
 * 只替身 `globalThis.fetch`：真实实现仍走 `services/http.ts` 的包装层，
 * 因此 `X-Request-Id`、`credentials`、生成客户端拼出的完整 URL 都被原样审计。
 */
export function installFetchStub(initial?: Record<string, Reply | ((call: Recorded) => Reply)>): Stub {
  const calls: Recorded[] = [];
  let table: Record<string, Reply | ((call: Recorded) => Reply)> = initial ?? {};
  const original = globalThis.fetch;

  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    // 生成客户端是以 `Request` 实参调用 fetch 的，替身必须从 Request 上取回口径，
    // 否则审计到的会是空 init 与 "[object Request]"，测试就会在假问题上打转
    const request = input instanceof Request ? input : undefined;
    const url = request ? request.url : typeof input === "string" ? input : input instanceof URL ? input.toString() : String(input);
    // 按 fetch 语义合并：`init` 里给出的成员覆盖 Request 上的同名成员
    const headers = new Headers(request?.headers);
    if (init?.headers) {
      new Headers(init.headers).forEach((value, key) => headers.set(key, value));
    }
    const bodySource = request
      ? await request.clone().text()
      : typeof init?.body === "string"
        ? init.body
        : undefined;
    const call: Recorded = {
      method: (init?.method ?? request?.method ?? "GET").toUpperCase(),
      url,
      pathname: new URL(url).pathname,
      headers: Object.fromEntries(headers.entries()),
      credentials: init?.credentials ?? request?.credentials,
      body: bodySource && bodySource.length > 0 ? bodySource : undefined,
    };
    calls.push(call);
    const matched = table[`${call.method} ${call.pathname}`];
    // 函数式回复**必须 await**：`Reply | ((call) => Reply)` 的类型允许返回 Promise，
    // 而「挂起 / 受控放行」这类用例正是靠它实现的。不 await 的话拿到的是 Promise
    // 对象本身，`reply?.status ?? 200` 会静默退化成「200 + 空响应体」，
    // 于是所有异步回复都在假问题上打转（`await` 对非 Promise 值是空操作，因此
    // 同步回复的既有行为不变）。
    const reply =
      typeof matched === "function" ? await matched(call) : await matched;
    const status = reply?.status ?? 200;
    return new Response(reply?.json === undefined ? null : JSON.stringify(reply.json), {
      status,
      headers: { "content-type": "application/json", "x-request-id": "req-stub" },
    });
  }) as typeof fetch;

  const find = (match: string | RegExp) =>
    calls.filter((call) =>
      typeof match === "string"
        ? `${call.method} ${call.pathname}` === match || call.pathname === match
        : match.test(call.url),
    );

  return {
    calls,
    restore: () => {
      globalThis.fetch = original;
    },
    routes: (next) => {
      table = next;
    },
    last: (match) => {
      const list = find(match);
      return list[list.length - 1];
    },
    count: (match) => find(match).length,
  };
}

export function envelope<T>(data: T) {
  return { data, request_id: "req-stub" };
}

export function errorBody(code: string, message = "stub", details: Record<string, unknown> = {}) {
  return { error: { code, message, details }, request_id: "req-stub" };
}

export const generation = (overrides: Record<string, unknown> = {}) => ({
  id: "gen-1",
  mode: "PHOTO_PROMPT",
  status: "QUEUED",
  billing_status: "RESERVED",
  prompt: "把照片转成黏土风头像",
  requested_count: 2,
  reserved_points: 20,
  failure_code: null,
  ...overrides,
});
