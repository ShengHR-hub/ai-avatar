import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { useStudio } from "@/hooks/useStudio";
import { envelope, errorBody, generation, installFetchStub } from "./helpers/stub";

let stub = installFetchStub();
afterEach(() => stub.restore());

const digest = async () => "c".repeat(64);
const DESCRIPTION = "一个人背对着镜头站在草地上，远处是起伏的山脊，天空占了大半画面，整体偏青绿色调。";

function referenceFile(): File {
  return new File([new Uint8Array([1, 2, 3])], "inspo.png", { type: "image/png" });
}

/** 参考图上传链路：意图按 REFERENCE 签发，完成校验后确认 READY。 */
function uploadStack() {
  return {
    "POST /api/v1/assets/uploads": {
      json: envelope({
        asset_id: "ref-1",
        purpose: "REFERENCE",
        status: "PENDING_UPLOAD",
        method: "PUT",
        upload_url: "http://minio.invalid/signed/ref-1",
        expires_at: "2026-01-01T00:10:00Z",
      }),
    },
    "PUT /signed/ref-1": { status: 200 },
    "POST /api/v1/assets/ref-1/complete": { json: envelope({ id: "ref-1", status: "READY" }) },
  };
}

function analyzeStack(description = DESCRIPTION, fromCache = false) {
  return {
    "POST /api/v1/analyses": {
      json: envelope({
        asset_id: "ref-1",
        description,
        model: "mock-vision-1",
        from_cache: fromCache,
      }),
    },
  };
}

function successStack() {
  const body = envelope(generation({ id: "g1", status: "SUCCEEDED", billing_status: "CONSUMED" }));
  return {
    "POST /api/v1/generations": { json: body },
    "GET /api/v1/generations/g1": { json: body },
    "GET /api/v1/generations/g1/outputs": {
      json: {
        data: [
          { asset_id: "r1", output_index: 0, mime_type: "image/png", width: 1248, height: 1248, size_bytes: 100 },
          { asset_id: "r2", output_index: 1, mime_type: "image/png", width: 1248, height: 1248, size_bytes: 100 },
        ],
        page: { next_cursor: null, has_more: false },
        request_id: "req-stub",
      },
    },
  };
}

async function pickAndAnalyze(studio: { current: ReturnType<typeof useStudio> }) {
  await act(async () => {
    await studio.current.pickReferenceFile(referenceFile());
  });
  await act(async () => {
    await studio.current.analyzeReference();
  });
}

describe("参考图片找灵感", () => {
  it("选完参考图只上传，不自动触发读图——那一下必须用户自己按", async () => {
    stub = installFetchStub(uploadStack());
    const { result } = renderHook(() => useStudio({ digest, initialWay: "inspire" }));

    await act(async () => {
      await result.current.pickReferenceFile(referenceFile());
    });

    expect(result.current.reference.stage).toBe("uploaded");
    expect(result.current.reference.assetId).toBe("ref-1");
    expect(stub.count("POST /api/v1/analyses")).toBe(0);
    expect(result.current.mode).toBe("TEXT_PROMPT");
  });

  it("上传意图按 REFERENCE 走，幂等键里带得上用途", async () => {
    stub = installFetchStub(uploadStack());
    const { result } = renderHook(() => useStudio({ digest, initialWay: "inspire" }));

    await act(async () => {
      await result.current.pickReferenceFile(referenceFile());
    });

    const intent = stub.last("POST /api/v1/assets/uploads")!;
    expect(JSON.parse(intent.body as string).purpose).toBe("REFERENCE");
    expect(intent.headers["idempotency-key"]).toBeTruthy();
  });

  it("读出来的描述是可编辑文字，描述框空时自动填进去", async () => {
    stub = installFetchStub({ ...uploadStack(), ...analyzeStack() });
    const { result } = renderHook(() => useStudio({ digest, initialWay: "inspire" }));

    await pickAndAnalyze(result);

    expect(result.current.reference.stage).toBe("described");
    expect(result.current.reference.description).toBe(DESCRIPTION);
    expect(result.current.reference.model).toBe("mock-vision-1");
    expect(result.current.reference.fromCache).toBe(false);
    expect(result.current.reference.autoFilled).toBe(true);
    expect(result.current.prompt).toBe(DESCRIPTION);
    expect(result.current.canSubmit).toBe(true);
  });

  it("用户已经写了字时绝不覆盖草稿，改由明确动作决定", async () => {
    stub = installFetchStub({ ...uploadStack(), ...analyzeStack() });
    const { result } = renderHook(() => useStudio({ digest, initialWay: "inspire" }));

    await act(async () => {
      result.current.setPrompt("我想要圆一点的脸");
    });
    await pickAndAnalyze(result);

    expect(result.current.prompt).toBe("我想要圆一点的脸");
    expect(result.current.reference.autoFilled).toBe(false);

    await act(async () => {
      result.current.applyReferenceDescription("append");
    });
    expect(result.current.prompt).toBe(`我想要圆一点的脸，${DESCRIPTION}`);

    await act(async () => {
      result.current.applyReferenceDescription("replace");
    });
    expect(result.current.prompt).toBe(DESCRIPTION);
  });

  it("提交仍然走文字创作，参考图绝不作为生成输入", async () => {
    stub = installFetchStub({
      ...uploadStack(),
      ...analyzeStack(),
      ...successStack(),
    });
    const { result } = renderHook(() => useStudio({ digest, initialWay: "inspire" }));

    await pickAndAnalyze(result);
    await act(async () => {
      await result.current.submit();
    });

    const submit = stub.last("POST /api/v1/generations")!;
    expect(JSON.parse(submit.body as string)).toEqual({
      mode: "TEXT_PROMPT",
      prompt: DESCRIPTION,
      input_asset_ids: [],
      output_count: 2,
    });
    await waitFor(() => expect(result.current.watch?.status).toBe("SUCCEEDED"));
  });

  it("复用缓存那次要如实标出来，界面才可以说「这次没再看一遍」", async () => {
    stub = installFetchStub({ ...uploadStack(), ...analyzeStack(DESCRIPTION, true) });
    const { result } = renderHook(() => useStudio({ digest, initialWay: "inspire" }));

    await pickAndAnalyze(result);

    expect(result.current.reference.fromCache).toBe(true);
  });

  it("超每日限额：状态落到 failed，并给出「稍后再试」这一类动作", async () => {
    stub = installFetchStub({
      ...uploadStack(),
      "POST /api/v1/analyses": {
        status: 429,
        json: errorBody("RATE_LIMITED", "请求过于频繁，请稍后重试", {}),
      },
    });
    const { result } = renderHook(() => useStudio({ digest, initialWay: "inspire" }));

    await act(async () => {
      await result.current.pickReferenceFile(referenceFile());
    });
    await act(async () => {
      await result.current.analyzeReference();
    });

    expect(result.current.reference.stage).toBe("failed");
    expect(result.current.reference.failure?.code).toBe("RATE_LIMITED");
    expect(result.current.reference.failure?.action).toBe("wait");
    expect(result.current.reference.description).toBe("");
  });

  it("能力未开放时不谎称在读图", async () => {
    stub = installFetchStub({
      ...uploadStack(),
      "POST /api/v1/analyses": {
        status: 422,
        json: errorBody("FEATURE_NOT_AVAILABLE", "该功能暂未开放", {}),
      },
    });
    const { result } = renderHook(() => useStudio({ digest, initialWay: "inspire" }));

    await act(async () => {
      await result.current.pickReferenceFile(referenceFile());
    });
    await act(async () => {
      await result.current.analyzeReference();
    });

    expect(result.current.reference.stage).toBe("failed");
    expect(result.current.reference.failure?.action).toBe("unsupported");
    expect(result.current.prompt).toBe("");
  });

  it("上传这一步就失败时停在 failed，不发分析请求", async () => {
    stub = installFetchStub({
      "POST /api/v1/assets/uploads": {
        status: 422,
        json: errorBody("FEATURE_NOT_AVAILABLE", "该功能暂未开放", {}),
      },
    });
    const { result } = renderHook(() => useStudio({ digest, initialWay: "inspire" }));

    await act(async () => {
      await result.current.pickReferenceFile(referenceFile());
    });

    expect(result.current.reference.stage).toBe("failed");
    expect(result.current.reference.assetId).toBeNull();
    expect(stub.count("POST /api/v1/analyses")).toBe(0);
  });

  it("换一张参考图会清空上一次的描述与资产", async () => {
    stub = installFetchStub({ ...uploadStack(), ...analyzeStack() });
    const { result } = renderHook(() => useStudio({ digest, initialWay: "inspire" }));

    await pickAndAnalyze(result);
    await act(async () => {
      result.current.resetReference();
    });

    expect(result.current.reference.stage).toBe("idle");
    expect(result.current.reference.assetId).toBeNull();
    expect(result.current.reference.description).toBe("");
    // 已经填进描述框的字不会因为"换一张图"被清掉
    expect(result.current.prompt).toBe(DESCRIPTION);
  });

  it("切入口不丢草稿：inspire → photo → inspire 描述文字仍在", async () => {
    stub = installFetchStub({ ...uploadStack(), ...analyzeStack() });
    const { result } = renderHook(() => useStudio({ digest, initialWay: "inspire" }));

    await pickAndAnalyze(result);
    await act(async () => {
      result.current.setWay("photo");
    });
    expect(result.current.mode).toBe("PHOTO_PROMPT");
    expect(result.current.prompt).toBe(DESCRIPTION);

    await act(async () => {
      result.current.setWay("inspire");
    });
    expect(result.current.mode).toBe("TEXT_PROMPT");
    expect(result.current.prompt).toBe(DESCRIPTION);
  });

  it("深链接进来就是第三个入口，且不会带着 PHOTO 模式提交", async () => {
    stub = installFetchStub({ ...uploadStack(), ...analyzeStack(), ...successStack() });
    const { result } = renderHook(() => useStudio({ digest, initialMode: "PHOTO_PROMPT", initialWay: "inspire" }));

    expect(result.current.way).toBe("inspire");
    expect(result.current.mode).toBe("TEXT_PROMPT");
  });
});
