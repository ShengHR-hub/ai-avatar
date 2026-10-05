import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { useStudio } from "@/hooks/useStudio";
import { isTerminal, nextDelay, rememberActiveGeneration } from "@/services/generation";
import { POLL_GIVE_UP_MS, POLL_MAX_MS } from "@/services/config";
import { envelope, generation, installFetchStub } from "./helpers/stub";

let stub = installFetchStub();
afterEach(() => stub.restore());

const digest = async () => "b".repeat(64);

function photoFile(): File {
  return new File([new Uint8Array([1, 2, 3])], "p.png", { type: "image/png" });
}

function readyStack() {
  return {
    "POST /api/v1/assets/uploads": {
      json: envelope({
        asset_id: "a1",
        purpose: "SOURCE",
        status: "PENDING_UPLOAD",
        method: "PUT",
        upload_url: "http://minio.invalid/signed/a1",
        expires_at: "2026-01-01T00:10:00Z",
      }),
    },
    "PUT /signed/a1": { status: 200 },
    "POST /api/v1/assets/a1/complete": { json: envelope({ id: "a1", status: "READY" }) },
  };
}

describe("studio 状态机", () => {
  it("照片模式在资源确认 READY 之前不可提交", async () => {
    stub = installFetchStub(readyStack());
    const { result } = renderHook(() => useStudio({ digest }));

    expect(result.current.canSubmit).toBe(false);
    await act(async () => {
      result.current.setPrompt("转成黏土风");
    });
    expect(result.current.canSubmit).toBe(false);

    await act(async () => {
      await result.current.pickFile(photoFile());
    });
    expect(result.current.ready).toBe(true);
    expect(result.current.canSubmit).toBe(true);
  });

  it("文字模式无需照片即可提交，且只发契约允许的字段", async () => {
    stub = installFetchStub({
      ...readyStack(),
      "POST /api/v1/generations": { json: envelope(generation({ id: "g1", status: "SUCCEEDED", billing_status: "CONSUMED" })) },
      "GET /api/v1/generations/g1": { json: envelope(generation({ id: "g1", status: "SUCCEEDED", billing_status: "CONSUMED" })) },
      "GET /api/v1/generations/g1/outputs": {
        json: {
          data: [
            { asset_id: "r1", output_index: 0, mime_type: "image/png", width: 1248, height: 1248, size_bytes: 1741633 },
            { asset_id: "r2", output_index: 1, mime_type: "image/png", width: 1248, height: 1248, size_bytes: 1784512 },
          ],
          page: { next_cursor: null, has_more: false },
          request_id: "req-stub",
        },
      },
    });

    const { result } = renderHook(() => useStudio({ digest }));
    // 交互之间必须让 React 把状态刷新到下一次渲染，否则 `submit` 读到的还是旧草稿
    await act(async () => {
      result.current.setMode("TEXT_PROMPT");
    });
    await act(async () => {
      result.current.setPrompt("扁平几何风头像");
    });
    await act(async () => {
      await result.current.submit();
    });

    const posted = stub.last("POST /api/v1/generations")!;
    expect(JSON.parse(posted.body as string)).toEqual({
      mode: "TEXT_PROMPT",
      prompt: "扁平几何风头像",
      input_asset_ids: [],
      output_count: 2,
    });

    await waitFor(() => expect(result.current.watch?.outputs).toHaveLength(2));
    expect(result.current.watch?.status).toBe("SUCCEEDED");
    expect(result.current.working).toBe(false);
    // 终局后必须交还操作权：否则用户只能刷新页面才能再发一次（真实回归）
    expect(result.current.busy).toBe(false);
    expect(result.current.canSubmit).toBe(true);
  });

  it("刷新恢复：只接住进行中的任务，不再发起提交", async () => {
    rememberActiveGeneration("g-recover");
    stub = installFetchStub({
      "GET /api/v1/generations/g-recover": {
        json: envelope(generation({ id: "g-recover", status: "NEEDS_REVIEW", billing_status: "RESERVED", failure_code: "provider_outcome_unknown" })),
      },
    });

    const { result } = renderHook(() => useStudio({ digest }));
    await waitFor(() => expect(result.current.watch?.status).toBe("NEEDS_REVIEW"));

    expect(stub.count("POST /api/v1/generations")).toBe(0);
    expect(result.current.working).toBe(false);
  });

  it("同一帧内的重复提交只发一个请求，且描述与键都按同一次意图发送", async () => {
    stub = installFetchStub({
      "POST /api/v1/generations": {
        json: envelope(generation({ id: "g1", status: "QUEUED", billing_status: "RESERVED" })),
      },
      "GET /api/v1/generations/g1": {
        json: envelope(generation({ id: "g1", status: "QUEUED", billing_status: "RESERVED" })),
      },
    });

    const { result } = renderHook(() => useStudio({ digest }));
    await act(async () => {
      result.current.setMode("TEXT_PROMPT");
    });
    await act(async () => {
      result.current.setPrompt("扁平几何风头像");
    });

    // 模拟双击/回车连击：同一帧内两次调用，第二次必须被同步挡下。
    // 服务端幂等键本来就会把两者折叠成一个任务，但前端不该先打两枪，
    // 也不该起两条并行轮询。
    await act(async () => {
      void result.current.submit();
      void result.current.submit();
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    const posts = stub.calls.filter((call) => call.pathname === "/api/v1/generations");
    expect(posts).toHaveLength(1);
    expect(JSON.parse(posts[0].body as string)).toEqual({
      mode: "TEXT_PROMPT",
      prompt: "扁平几何风头像",
      input_asset_ids: [],
      output_count: 2,
    });
    expect(posts[0].headers["idempotency-key"]).toBeTruthy();
  });

  it("防误触不会挡住用户随后主动再生成一次", async () => {
    stub = installFetchStub({
      "POST /api/v1/generations": {
        json: envelope(generation({ id: "g1", status: "SUCCEEDED", billing_status: "CONSUMED" })),
      },
      "GET /api/v1/generations/g1": {
        json: envelope(generation({ id: "g1", status: "SUCCEEDED", billing_status: "CONSUMED" })),
      },
      "GET /api/v1/generations/g1/outputs": {
        json: { data: [], page: { next_cursor: null, has_more: false }, request_id: "req-stub" },
      },
    });

    const { result } = renderHook(() => useStudio({ digest }));
    await act(async () => {
      result.current.setMode("TEXT_PROMPT");
    });
    await act(async () => {
      result.current.setPrompt("换个风格再来一次");
    });
    // 第一次提交跑到终局：`runWatch` 返回后守卫必须复位
    await act(async () => {
      await result.current.submit();
    });
    await waitFor(() => expect(result.current.watch?.status).toBe("SUCCEEDED"));

    // 第二次点击是新的主动生成意图：必须真的出网（不能为了防误触把功能锁死）
    await act(async () => {
      const again = result.current.submit();
      await Promise.race([again, new Promise((resolve) => setTimeout(resolve, 50))]);
    });

    const posts = stub.calls.filter((call) => call.pathname === "/api/v1/generations");
    expect(posts).toHaveLength(2);
    // 终局后草稿号已轮换：新意图必须换键，否则服务端会回放旧任务而不出图
    expect(posts[1].headers["idempotency-key"]).not.toBe(posts[0].headers["idempotency-key"]);
  });

  it("重试在途时不重复发请求（每次点击都带同一把重试键）", async () => {
    stub = installFetchStub({
      "GET /api/v1/generations/g2": {
        json: envelope(generation({ id: "g2", status: "FAILED", billing_status: "RELEASED", failure_code: "VALIDATION" })),
      },
      "POST /api/v1/generations/g2/retry": {
        json: envelope(generation({ id: "g2", status: "QUEUED", billing_status: "RESERVED" })),
      },
    });

    const { result } = renderHook(() => useStudio({ digest, initialGenerationId: "g2" }));
    await waitFor(() => expect(result.current.watch?.status).toBe("FAILED"));

    await act(async () => {
      void result.current.retry();
      void result.current.retry();
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    const retries = stub.calls.filter((call) => call.pathname === "/api/v1/generations/g2/retry");
    expect(retries).toHaveLength(1);
    // 重试沿用同一任务号：重试键按任务派生，反复点击也不会二次扣点
    expect(retries[0].headers["idempotency-key"]).toBeTruthy();
  });
});

describe("轮询节奏与终局判定", () => {
  it("退避 3s 起步、封顶 10s，超时后交回手动刷新", () => {
    expect(nextDelay(0)).toBe(3_000);
    expect(nextDelay(1)).toBe(6_000);
    expect(nextDelay(9)).toBe(POLL_MAX_MS);
    const started = 1_000_000;
    expect(nextDelay(0, started + POLL_GIVE_UP_MS, started)).toBe(0);
  });

  it("三种终局都停止轮询，进行中继续", () => {
    expect(["SUCCEEDED", "FAILED", "NEEDS_REVIEW"].every(isTerminal)).toBe(true);
    expect(isTerminal("PROCESSING")).toBe(false);
    expect(isTerminal("QUEUED")).toBe(false);
  });
});

describe("从首页入口带进来的预设", () => {
  it("文字模式 + 模板提示词直接进入可提交状态，不需要照片", () => {
    stub = installFetchStub();
    const { result } = renderHook(() =>
      useStudio({ digest, initialMode: "TEXT_PROMPT", initialPrompt: "生成一个英气的奇幻游戏角色头像" }),
    );
    expect(result.current.mode).toBe("TEXT_PROMPT");
    expect(result.current.ready).toBe(false);
    expect(result.current.canSubmit).toBe(true);
  });

  it("照片模式即使带了提示词也必须先等到 READY", () => {
    stub = installFetchStub(readyStack());
    const { result } = renderHook(() =>
      useStudio({ digest, initialMode: "PHOTO_PROMPT", initialPrompt: "转成黏土风" }),
    );
    expect(result.current.canSubmit).toBe(false);
  });
});
