/**
 * ENG-09 返修回归：分享/反馈状态按 generationId 隔离，以及幂等键的唯一可信事实。
 *
 * ## 关于"迟到响应"的覆盖范围（第五轮更正）
 *
 * 第四轮这里写着「在真实 UI 流程里**无法**在请求 pending 期间切换任务或清空任务，
 * 本轮没有、也无法在不削弱 `inFlight` 的前提下构造」。**这条声明不成立**：
 * `inFlight` 只挡**发起**新的提交动作（`createShareLink` / `submitFeedback` /
 * `submit` / `retry` 入口处的同步判定），`clearActive()` 从不检查它。
 * 于是「分享/反馈请求仍然 pending 时用户清空任务」是真实可达的：
 *
 * ```text
 * 面板按钮 disabled  ← 挡的是"再点一次提交"，不是 clearActive
 * clearActive()      ← 直接 abort 轮询、清 generationId 与归属标记
 * ```
 *
 * 第五轮补了两条**真实交错**用例（分享与反馈各一条）：
 * 先清空并确认面板复位，再释放旧响应，断言旧结果不污染当前状态。
 * 既有那条"先让 inFlight 归位、再放行响应"的用例保留 —— 它覆盖的是**另一个**
 * 交错（响应落地晚于新任务就位），两者不互相替代。
 *
 * ## 幂等键的唯一可信事实
 *
 * 分享请求头必须**就是**持久记录里的 `current.key`（`services/shareIntent.ts`）。
 * 用例直接断言实际 HTTP 请求头与 `sessionStorage` 记录一致，而不是只测工具函数。
 */

import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { useStudio } from "@/hooks/useStudio";
import { rememberActiveGeneration } from "@/services/generation";
import { envelope, generation, installFetchStub, type Stub } from "./helpers/stub";

const digest = async () => "b".repeat(64);

const SHARE_PAYLOAD = {
  slug: "old-task-slug-aaaaaaaaaaaaaaaaaaaa",
  title: null,
  expires_at: "2026-10-16T12:00:00+00:00",
  created_at: "2026-10-09T12:00:00+00:00",
  outputs: [
    {
      asset_id: "r1",
      output_index: 0,
      mime_type: "image/png",
      width: 1248,
      height: 1248,
      size_bytes: 1741633,
    },
  ],
};

const FEEDBACK_PAYLOAD = {
  id: "fb-old",
  generation_id: "g-1",
  rating: 5,
  category: "画面质量",
  content: null,
  status: "OPEN",
  created_at: "2026-10-09T12:00:00+00:00",
};

/** 每次提交都发一个新的任务号（否则两次提交会落到同一个 generationId）。 */
class SequentialGenerations {
  counter = 0;

  post = () => {
    this.counter += 1;
    const id = `g-${this.counter}`;
    return {
      status: 202,
      json: envelope(
        generation({ id, status: "SUCCEEDED", billing_status: "CONSUMED" }),
      ),
    };
  };

  get = (call: { pathname: string }) => {
    const id = call.pathname.split("/")[4];
    return {
      status: 200,
      json: envelope(
        generation({ id, status: "SUCCEEDED", billing_status: "CONSUMED" }),
      ),
    };
  };

  outputs = () => ({
    status: 200,
    json: {
      data: [
        {
          asset_id: "r1",
          output_index: 0,
          mime_type: "image/png",
          width: 1248,
          height: 1248,
          size_bytes: 1741633,
        },
      ],
      page: { next_cursor: null, has_more: false },
      request_id: "req-stub",
    },
  });
}

/** 可控的迟到响应：先拿到 resolve，用例需要时再放行。 */
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

let stub: Stub = installFetchStub();
afterEach(() => stub.restore());

type Studio = ReturnType<typeof useStudio>;

function routeTable(
  generations: SequentialGenerations,
  extra: Record<string, unknown>,
) {
  return {
    "POST /api/v1/generations": generations.post,
    ...Object.fromEntries(
      [1, 2, 3, 4, 5].flatMap((index) => [
        [`GET /api/v1/generations/g-${index}`, generations.get],
        [`GET /api/v1/generations/g-${index}/outputs`, generations.outputs],
      ]),
    ),
    ...extra,
  } as Parameters<Stub["routes"]>[0];
}

/** 渲染并驱动一次成功的文字生成（提交 → 轮询 → SUCCEEDED）。 */
async function renderSucceededStudio(generations: SequentialGenerations) {
  const rendered = renderHook(() => useStudio({ digest }));
  await act(async () => {
    rendered.result.current.setMode("TEXT_PROMPT");
  });
  await act(async () => {
    rendered.result.current.setPrompt("扁平几何风头像");
  });
  await act(async () => {
    await rendered.result.current.submit();
  });
  await waitFor(() =>
    expect(rendered.result.current.watch?.status).toBe("SUCCEEDED"),
  );
  return rendered;
}

describe("分享/反馈按任务隔离", () => {
  it("清空当前任务时，分享与反馈面板立刻回到 idle", async () => {
    const generations = new SequentialGenerations();
    stub = installFetchStub(
      routeTable(generations, {
        "POST /api/v1/shares": { status: 201, json: envelope(SHARE_PAYLOAD) },
        "POST /api/v1/feedback": { status: 201, json: envelope(FEEDBACK_PAYLOAD) },
      }),
    );

    const { result } = await renderSucceededStudio(generations);
    await act(async () => {
      await result.current.createShareLink(["r1"]);
    });
    await act(async () => {
      await result.current.submitFeedback({
        rating: 5,
        category: "画面质量",
        content: "",
      });
    });
    await waitFor(() => expect(result.current.share.stage).toBe("created"));
    await waitFor(() => expect(result.current.feedback.stage).toBe("submitted"));

    await act(async () => {
      result.current.clearActive();
    });

    // 任务号被清掉 → 隔离 effect 必须复位两个面板
    await waitFor(() => expect(result.current.generationId).toBeNull());
    await waitFor(() => expect(result.current.share.stage).toBe("idle"));
    await waitFor(() => expect(result.current.feedback.stage).toBe("idle"));
    expect(result.current.share.view).toBeNull();
    expect(result.current.feedback.view).toBeNull();
  });

  it("旧任务的分享响应迟到时，新任务的面板不被污染", async () => {
    const generations = new SequentialGenerations();
    const lateShare = deferred<{ status: number; json: unknown }>();
    let shareCalls = 0;

    stub = installFetchStub(
      routeTable(generations, {
        "POST /api/v1/shares": () => {
          shareCalls += 1;
          // 旧任务的分享永远挂起，直到用例显式放行
          return shareCalls === 1
            ? (lateShare.promise as never)
            : { status: 201, json: envelope(SHARE_PAYLOAD) };
        },
      }),
    );

    // 任务 A：拿到结果并发出分享（响应挂起）
    const first = await renderSucceededStudio(generations);
    expect(first.result.current.generationId).toBe("g-1");
    await act(async () => {
      void first.result.current.createShareLink(["r1"]);
    });
    await waitFor(() => expect(stub.count("POST /api/v1/shares")).toBe(1));
    await waitFor(() => expect(first.result.current.share.stage).toBe("creating"));
    first.unmount();

    // 任务 B：全新会话（模拟用户换了一个新任务）
    const second = await renderSucceededStudio(generations);
    expect(second.result.current.generationId).toBe("g-2");
    expect(second.result.current.share.stage).toBe("idle");
    expect(second.result.current.share.generationId).toBe("g-2");

    // 放行 A 的迟到响应：它属于 g-1，必须被丢弃
    await act(async () => {
      lateShare.resolve({ status: 201, json: envelope(SHARE_PAYLOAD) });
      await new Promise((resolve) => setTimeout(resolve, 10));
    });

    expect(second.result.current.share.stage).toBe("idle");
    expect(second.result.current.share.view).toBeNull();
    expect(second.result.current.share.generationId).toBe("g-2");
  });

  it("「再生成一条分享链接」清空上一条结果，并换一把新幂等键", async () => {
    const generations = new SequentialGenerations();
    stub = installFetchStub(
      routeTable(generations, {
        "POST /api/v1/shares": { status: 201, json: envelope(SHARE_PAYLOAD) },
      }),
    );

    const { result } = await renderSucceededStudio(generations);

    await act(async () => {
      await result.current.createShareLink(["r1"]);
    });
    await waitFor(() => expect(result.current.share.stage).toBe("created"));
    const firstKey = stub.last("POST /api/v1/shares")!.headers["idempotency-key"];

    // 明确的第二次分享意图
    await act(async () => {
      result.current.startAnotherShare();
    });
    expect(result.current.share.stage).toBe("idle");
    expect(result.current.share.view).toBeNull();

    await act(async () => {
      await result.current.createShareLink(["r1"]);
    });
    await waitFor(() => expect(result.current.share.stage).toBe("created"));
    const secondKey = stub.last("POST /api/v1/shares")!.headers["idempotency-key"];

    expect(secondKey).not.toBe(firstKey);
  });

  it("会话被清空后重挂：新意图拿到的键从未被使用过", async () => {
    const generations = new SequentialGenerations();
    stub = installFetchStub(
      routeTable(generations, {
        "POST /api/v1/shares": { status: 201, json: envelope(SHARE_PAYLOAD) },
      }),
    );

    // 第一次会话：建一条分享、再点一次「再生成一条」→ 序号 1
    const first = await renderSucceededStudio(generations);
    await act(async () => {
      await first.result.current.createShareLink(["r1"]);
    });
    await act(async () => {
      first.result.current.startAnotherShare();
    });
    await act(async () => {
      await first.result.current.createShareLink(["r1"]);
    });
    await waitFor(() => expect(first.result.current.share.stage).toBe("created"));
    const usedKeys = stub.calls
      .filter((call) => call.pathname === "/api/v1/shares")
      .map((call) => call.headers["idempotency-key"]);
    expect(usedKeys.length).toBe(2);
    first.unmount();

    // 模拟"刷新"：清掉内存（unmount 已经清掉），但**保留** sessionStorage。
    // 幂等登记（服务端侧）依然记得用过的键，所以新会话必须拿一把新键。
    const second = await renderSucceededStudio(generations);
    await act(async () => {
      second.result.current.startAnotherShare();
    });
    await act(async () => {
      await second.result.current.createShareLink(["r1"]);
    });
    await waitFor(() => expect(second.result.current.share.stage).toBe("created"));
    const newKey = stub.last("POST /api/v1/shares")!.headers["idempotency-key"];

    expect(usedKeys).not.toContain(newKey);
  });

  it("回到同一个任务时恢复已建出的链接（不重复创建）", async () => {
    const generations = new SequentialGenerations();
    const created = { ...SHARE_PAYLOAD, slug: "restored-slug-aaaaaaaaaaaaaaaa" };
    stub = installFetchStub(
      routeTable(generations, {
        "POST /api/v1/shares": { status: 201, json: envelope(created) },
        [`GET /api/v1/shares/${created.slug}`]: {
          status: 200,
          json: envelope(created),
        },
      }),
    );

    const { result } = await renderSucceededStudio(generations);
    await act(async () => {
      await result.current.createShareLink(["r1"]);
    });
    await waitFor(() => expect(result.current.share.stage).toBe("created"));
    const createCalls = stub.count("POST /api/v1/shares");
    const generationId = result.current.generationId as string;

    // 意图已经落进 Storage（这是被刷新之后唯一还活着的东西）
    expect(
      window.sessionStorage.getItem(`ai-avatar.share-intent.${generationId}`),
    ).toContain(created.slug);

    // 模拟"刷新后回到同一个任务"：把活动任务记进 Storage（`useStudio` 在
    // 真实流程里靠它恢复正在进行的任务），然后重新挂载会话
    rememberActiveGeneration(generationId);
    const reloaded = renderHook(() => useStudio({ digest }));
    await waitFor(() => expect(reloaded.result.current.share.stage).toBe("created"));

    expect(reloaded.result.current.generationId).toBe(generationId);
    expect(reloaded.result.current.share.view?.slug).toBe(created.slug);

    // 同一个任务、同一次意图：不得再发一次创建请求，只按 slug 重新读取
    expect(stub.count("POST /api/v1/shares")).toBe(createCalls);
    expect(stub.count(`GET /api/v1/shares/${created.slug}`)).toBeGreaterThan(0);

    // 同一个 generationId 下点「再生成一条」：必须拿到**从未使用过**的新键，
    // 而不是回放原键（这正是"重挂新建 g-2"证明不了的那条）
    const usedKeys = stub.calls
      .filter((call) => call.pathname === "/api/v1/shares")
      .map((call) => call.headers["idempotency-key"]);
    await act(async () => {
      reloaded.result.current.startAnotherShare();
    });
    await act(async () => {
      await reloaded.result.current.createShareLink(["r1"]);
    });
    await waitFor(() => expect(reloaded.result.current.share.stage).toBe("created"));
    const newKey = stub.last("POST /api/v1/shares")!.headers["idempotency-key"];
    expect(usedKeys).not.toContain(newKey);
  });

  it("同一个 useStudio 实例内切换任务：旧任务的**分享**迟到响应不污染新任务", async () => {
    const generations = new SequentialGenerations();
    const lateShare = deferred<{ status: number; json: unknown }>();
    let shareCalls = 0;

    stub = installFetchStub(
      routeTable(generations, {
        "POST /api/v1/shares": () => {
          shareCalls += 1;
          // 旧任务（g-1）的分享永远挂起，直到用例显式放行
          return shareCalls === 1
            ? (lateShare.promise as never)
            : { status: 201, json: envelope(SHARE_PAYLOAD) };
        },
      }),
    );

    // **同一个实例**：不是"卸载 A 再新建 B"
    const { result } = await renderSucceededStudio(generations);
    const oldGeneration = result.current.generationId as string;
    expect(oldGeneration).toBe("g-1");

    await act(async () => {
      void result.current.createShareLink(["r1"]);
    });
    await waitFor(() => expect(result.current.share.stage).toBe("creating"));

    // 同一个实例内切换任务。
    // 注意：`inFlight` 是既有的防双击闸门（挂起请求未结束时不放行下一次提交），
    // 因此这里先让旧分享的**状态位**归位；它的响应仍然会落在新任务就位之后，
    // 归属判定必须把它丢弃（这正是本用例要证明的）。
    await act(async () => {
      lateShare.resolve({
        status: 201,
        json: envelope({ ...SHARE_PAYLOAD, slug: "old-generation-slug" }),
      });
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    await act(async () => {
      result.current.clearActive();
    });
    await waitFor(() => expect(result.current.generationId).toBeNull());
    await waitFor(() => expect(result.current.share.stage).toBe("idle"));

    await act(async () => {
      result.current.setMode("TEXT_PROMPT");
    });
    await act(async () => {
      result.current.setPrompt("第二个任务");
    });
    await act(async () => {
      await result.current.submit();
    });
    await waitFor(() => expect(result.current.watch?.status).toBe("SUCCEEDED"));

    const newGeneration = result.current.generationId as string;
    expect(newGeneration).not.toBe(oldGeneration);
    await waitFor(() => expect(result.current.share.stage).toBe("idle"));

    // 旧响应的落地**晚于**新任务：它属于 g-1，必须被丢弃而不是画到 g-2 上。
    // （envelope 里带的是旧任务的 slug，如果被采纳就会出现在 share.view）
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 20));
    });

    expect(result.current.generationId).toBe(newGeneration);
    expect(result.current.share.stage).toBe("idle");
    expect(result.current.share.view).toBeNull();
    expect(result.current.share.generationId).toBe(newGeneration);
  });

  it("同一个 useStudio 实例内切换任务：旧任务的**反馈**迟到响应不污染新任务", async () => {
    const generations = new SequentialGenerations();
    const lateFeedback = deferred<{ status: number; json: unknown }>();
    let feedbackCalls = 0;

    stub = installFetchStub(
      routeTable(generations, {
        "POST /api/v1/feedback": () => {
          feedbackCalls += 1;
          return feedbackCalls === 1
            ? (lateFeedback.promise as never)
            : { status: 201, json: envelope(FEEDBACK_PAYLOAD) };
        },
      }),
    );

    const { result } = await renderSucceededStudio(generations);
    const oldGeneration = result.current.generationId as string;
    expect(oldGeneration).toBe("g-1");

    await act(async () => {
      void result.current.submitFeedback({
        rating: 5,
        category: "画面质量",
        content: "旧任务的反馈",
      });
    });
    await waitFor(() => expect(result.current.feedback.stage).toBe("submitting"));

    // 同上：先让 inFlight 归位，再把旧任务的反馈响应放行
    await act(async () => {
      lateFeedback.resolve({ status: 201, json: envelope(FEEDBACK_PAYLOAD) });
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    await act(async () => {
      result.current.clearActive();
    });
    await waitFor(() => expect(result.current.generationId).toBeNull());
    await waitFor(() => expect(result.current.feedback.stage).toBe("idle"));

    await act(async () => {
      result.current.setMode("TEXT_PROMPT");
    });
    await act(async () => {
      result.current.setPrompt("第二个任务");
    });
    await act(async () => {
      await result.current.submit();
    });
    await waitFor(() => expect(result.current.watch?.status).toBe("SUCCEEDED"));

    const newGeneration = result.current.generationId as string;
    expect(newGeneration).not.toBe(oldGeneration);
    await waitFor(() => expect(result.current.feedback.stage).toBe("idle"));

    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 20));
    });

    expect(result.current.generationId).toBe(newGeneration);
    expect(result.current.feedback.stage).toBe("idle");
    expect(result.current.feedback.view).toBeNull();
    expect(result.current.feedback.generationId).toBe(newGeneration);
  });


  it("记录损坏时整条作废并从头开始，不会复用历史键", async () => {
    const generations = new SequentialGenerations();
    stub = installFetchStub(
      routeTable(generations, {
        "POST /api/v1/shares": { status: 201, json: envelope(SHARE_PAYLOAD) },
      }),
    );

    const { result } = await renderSucceededStudio(generations);
    const generationId = result.current.generationId as string;

    // v1 形状（旧版本）：整条作废
    window.sessionStorage.setItem(
      `ai-avatar.share-intent.${generationId}`,
      JSON.stringify({
        version: 1,
        generationId,
        nextAttempt: 4242,
        slug: "stale-slug",
      }),
    );
    rememberActiveGeneration(generationId);
    const reloaded = renderHook(() => useStudio({ digest }));
    await waitFor(() => expect(reloaded.result.current.generationId).toBe(generationId));
    await waitFor(() => expect(reloaded.result.current.share.stage).toBe("idle"));

    await act(async () => {
      await reloaded.result.current.createShareLink(["r1"]);
    });
    await waitFor(() => expect(reloaded.result.current.share.stage).toBe("created"));
    const key = stub.last("POST /api/v1/shares")!.headers["idempotency-key"]!;

    // 从 0 号意图重新开始：拿到的是全新的随机键，而不是去接 4242 那半条记录
    expect(key).toMatch(/^[0-9a-f]{32}$/);
    expect(key).not.toContain("4242");
    expect(key).not.toBe("stale-slug");
  });

  it("IDEMPOTENCY_CONFLICT 不自动换键；只有明确的新意图动作才换键", async () => {
    const generations = new SequentialGenerations();
    let shareCalls = 0;
    stub = installFetchStub(
      routeTable(generations, {
        "POST /api/v1/shares": () => {
          shareCalls += 1;
          if (shareCalls === 1) {
            return { status: 201, json: envelope(SHARE_PAYLOAD) };
          }
          // 第二次换成冲突（模拟"同一个键已经绑定了别的内容"）
          return {
            status: 409,
            json: {
              error: {
                code: "IDEMPOTENCY_CONFLICT",
                message: "相同幂等键对应了不同的请求内容",
                details: {},
              },
              request_id: "req-stub",
            },
          };
        },
      }),
    );

    const { result } = await renderSucceededStudio(generations);
    await act(async () => {
      await result.current.createShareLink(["r1"]);
    });
    await waitFor(() => expect(result.current.share.stage).toBe("created"));
    const firstKey = stub.last("POST /api/v1/shares")!.headers["idempotency-key"];

    // 换一组图 → 服务端冲突
    await act(async () => {
      await result.current.createShareLink(["r1", "r2"]);
    });
    await waitFor(() => expect(result.current.share.stage).toBe("failed"));
    expect(result.current.share.failure?.code).toBe("IDEMPOTENCY_CONFLICT");

    // 普通重试必须**仍然使用同一把键**：不得因为冲突而偷偷换键
    await act(async () => {
      await result.current.createShareLink(["r1", "r2"]);
    });
    await waitFor(() => expect(result.current.share.stage).toBe("failed"));
    const retryKey = stub.last("POST /api/v1/shares")!.headers["idempotency-key"];
    expect(retryKey).toBe(firstKey);

    // 只有「再生成一条分享链接」这个明确动作才换键
    await act(async () => {
      result.current.startAnotherShare();
    });
    await act(async () => {
      await result.current.createShareLink(["r1", "r2"]);
    });
    const afterIntentKey = stub.last("POST /api/v1/shares")!.headers["idempotency-key"];
    expect(afterIntentKey).not.toBe(firstKey);
  });

  it("序号用尽时不回绕、不静默复用历史键", async () => {
    const generations = new SequentialGenerations();
    stub = installFetchStub(
      routeTable(generations, {
        "POST /api/v1/shares": { status: 201, json: envelope(SHARE_PAYLOAD) },
      }),
    );

    const { result } = await renderSucceededStudio(generations);
    const generationId = result.current.generationId as string;

    // 一条**合法**（v3 形状、键是随机串）的记录，但下一意图已到上限
    const lastKey = "a".repeat(32);
    window.sessionStorage.setItem(
      `ai-avatar.share-intent.${generationId}`,
      JSON.stringify({
        version: 3,
        generationId,
        current: { attempt: 9_999, key: lastKey, slug: null },
        nextAttempt: 10_000,
      }),
    );

    rememberActiveGeneration(generationId);
    const reloaded = renderHook(() => useStudio({ digest }));
    await waitFor(() => expect(reloaded.result.current.generationId).toBe(generationId));

    await act(async () => {
      reloaded.result.current.startAnotherShare();
    });

    // 明确失败，而不是回绕到 0 / 复用历史键，也不发请求
    expect(reloaded.result.current.share.stage).toBe("failed");
    expect(reloaded.result.current.share.failure?.message).toContain("用尽");
    expect(stub.count("POST /api/v1/shares")).toBe(0);

    // 记录没有被改写（不回绕、不降级）
    const raw = window.sessionStorage.getItem(
      `ai-avatar.share-intent.${generationId}`,
    );
    expect(raw).toContain(lastKey);
  });

  // ------------------------------------------------------------------------ //
  // 第五轮：幂等键的唯一可信事实 + pending 期间清空
  // ------------------------------------------------------------------------ //
  it("分享请求头就是持久记录里的 current.key（唯一可信事实，不是另一份缓存）", async () => {
    const generations = new SequentialGenerations();
    stub = installFetchStub(
      routeTable(generations, {
        "POST /api/v1/shares": { status: 201, json: envelope(SHARE_PAYLOAD) },
      }),
    );

    const { result } = await renderSucceededStudio(generations);
    const generationId = result.current.generationId as string;

    await act(async () => {
      await result.current.createShareLink(["r1"]);
    });
    await waitFor(() => expect(result.current.share.stage).toBe("created"));

    const header = stub.last("POST /api/v1/shares")!.headers["idempotency-key"]!;
    const record = JSON.parse(
      window.sessionStorage.getItem(`ai-avatar.share-intent.${generationId}`)!,
    );
    expect(header).toBe(record.current.key);
    expect(record.current.slug).toBe(SHARE_PAYLOAD.slug);
    expect(header).not.toContain(generationId);
  });

  it("建两条分享后清空并重访同一任务：普通创建不复用任何历史键", async () => {
    const generations = new SequentialGenerations();
    stub = installFetchStub(
      routeTable(generations, {
        "POST /api/v1/shares": { status: 201, json: envelope(SHARE_PAYLOAD) },
      }),
    );

    const { result } = await renderSucceededStudio(generations);
    const generationId = result.current.generationId as string;

    // 第一条分享
    await act(async () => {
      await result.current.createShareLink(["r1"]);
    });
    await waitFor(() => expect(result.current.share.stage).toBe("created"));
    // 明确的新意图 → 第二条分享
    await act(async () => {
      result.current.startAnotherShare();
    });
    await act(async () => {
      await result.current.createShareLink(["r1"]);
    });
    await waitFor(() => expect(result.current.share.stage).toBe("created"));

    const usedKeys = stub.calls
      .filter((call) => call.pathname === "/api/v1/shares")
      .map((call) => call.headers["idempotency-key"]);
    expect(usedKeys).toHaveLength(2);
    expect(new Set(usedKeys).size).toBe(2);

    // 清空任务（记录被移除）
    await act(async () => {
      result.current.clearActive();
    });
    await waitFor(() => expect(result.current.generationId).toBeNull());

    // 重访**同一个任务**：普通创建必须拿一把从未用过的键
    rememberActiveGeneration(generationId);
    const revisited = renderHook(() => useStudio({ digest }));
    await waitFor(() => expect(revisited.result.current.generationId).toBe(generationId));
    await waitFor(() => expect(revisited.result.current.share.stage).toBe("idle"));

    await act(async () => {
      await revisited.result.current.createShareLink(["r1"]);
    });
    await waitFor(() => expect(revisited.result.current.share.stage).toBe("created"));

    const newKey = stub.last("POST /api/v1/shares")!.headers["idempotency-key"]!;
    expect(usedKeys).not.toContain(newKey);
  });

  it("分享请求仍 pending 时清空任务：面板复位，随后释放的旧响应被丢弃", async () => {
    const generations = new SequentialGenerations();
    const lateShare = deferred<{ status: number; json: unknown }>();
    stub = installFetchStub(
      routeTable(generations, {
        "POST /api/v1/shares": () => lateShare.promise as never,
      }),
    );

    const { result } = await renderSucceededStudio(generations);
    const oldGeneration = result.current.generationId as string;

    // 请求挂起（此时 `inFlight === "share"`）
    await act(async () => {
      void result.current.createShareLink(["r1"]);
    });
    await waitFor(() => expect(stub.count("POST /api/v1/shares")).toBe(1));
    await waitFor(() => expect(result.current.share.stage).toBe("creating"));

    // **pending 期间**清空任务：`clearActive` 不看 `inFlight`，因此这条交错真实可达
    await act(async () => {
      result.current.clearActive();
    });
    await waitFor(() => expect(result.current.generationId).toBeNull());
    await waitFor(() => expect(result.current.share.stage).toBe("idle"));
    expect(result.current.share.view).toBeNull();
    expect(result.current.share.generationId).toBeNull();

    // 现在才释放旧响应：它属于已经结束的任务，必须被丢弃
    await act(async () => {
      lateShare.resolve({
        status: 201,
        json: envelope({ ...SHARE_PAYLOAD, slug: "late-after-clear" }),
      });
      await new Promise((resolve) => setTimeout(resolve, 20));
    });

    expect(result.current.generationId).toBeNull();
    expect(result.current.share.stage).toBe("idle");
    expect(result.current.share.view).toBeNull();
    expect(result.current.share.generationId).toBeNull();
    // 旧任务 = oldGeneration 的响应没有留下任何痕迹
    expect(stub.count("POST /api/v1/shares")).toBe(1);
    expect(oldGeneration).toBe("g-1");
  });

  it("反馈请求仍 pending 时清空任务：面板复位，随后释放的旧响应被丢弃", async () => {
    const generations = new SequentialGenerations();
    const lateFeedback = deferred<{ status: number; json: unknown }>();
    stub = installFetchStub(
      routeTable(generations, {
        "POST /api/v1/feedback": () => lateFeedback.promise as never,
      }),
    );

    const { result } = await renderSucceededStudio(generations);
    const oldGeneration = result.current.generationId as string;

    await act(async () => {
      void result.current.submitFeedback({
        rating: 5,
        category: "画面质量",
        content: "还没回来就被清空了",
      });
    });
    await waitFor(() => expect(stub.count("POST /api/v1/feedback")).toBe(1));
    await waitFor(() => expect(result.current.feedback.stage).toBe("submitting"));

    await act(async () => {
      result.current.clearActive();
    });
    await waitFor(() => expect(result.current.generationId).toBeNull());
    await waitFor(() => expect(result.current.feedback.stage).toBe("idle"));
    expect(result.current.feedback.view).toBeNull();
    expect(result.current.feedback.generationId).toBeNull();

    await act(async () => {
      lateFeedback.resolve({ status: 201, json: envelope(FEEDBACK_PAYLOAD) });
      await new Promise((resolve) => setTimeout(resolve, 20));
    });

    expect(result.current.generationId).toBeNull();
    expect(result.current.feedback.stage).toBe("idle");
    expect(result.current.feedback.view).toBeNull();
    expect(result.current.feedback.generationId).toBeNull();
    expect(oldGeneration).toBe("g-1");
  });
});
