import { afterEach, describe, expect, it } from "vitest";

import { createDownloadUrl, createUploadIntent, submitGeneration } from "@/services/api";
import { idempotencyKey, releaseIdempotencyKey, submitScope } from "@/services/idempotency";
import { rotateDraftId } from "@/services/generation";
import { envelope, generation, installFetchStub } from "./helpers/stub";

let stub = installFetchStub();
afterEach(() => stub.restore());

describe("幂等键按意图复用", () => {
  it("同一作用域返回同一个键，释放后才是新意图", () => {
    const first = idempotencyKey("submit:d1");
    expect(idempotencyKey("submit:d1")).toBe(first);
    releaseIdempotencyKey("submit:d1");
    expect(idempotencyKey("submit:d1")).not.toBe(first);
  });

  it("同一草稿的重复提交复用同一把键", async () => {
    stub = installFetchStub({ "POST /api/v1/generations": { json: envelope(generation()) } });
    const submit = () =>
      submitGeneration({ draftId: "same-draft", mode: "TEXT_PROMPT", prompt: "两张", assetIds: [] });
    await submit();
    await submit();
    const keys = stub.calls.map((call) => call.headers["idempotency-key"]);
    expect(keys[0]).toBe(keys[1]);
    expect(keys[0]).toMatch(/^[0-9a-f-]{36}$/i);
  });

  it("终局后换草稿号，下一次提交是新键", async () => {
    stub = installFetchStub({ "POST /api/v1/generations": { json: envelope(generation()) } });
    await submitGeneration({ draftId: "d-old", mode: "TEXT_PROMPT", prompt: "x", assetIds: [] });
    const nextDraft = rotateDraftId();
    expect(submitScope(nextDraft)).not.toBe(submitScope("d-old"));
    await submitGeneration({ draftId: nextDraft, mode: "TEXT_PROMPT", prompt: "x", assetIds: [] });
    const keys = stub.calls.map((call) => call.headers["idempotency-key"]);
    expect(keys[0]).not.toBe(keys[1]);
  });

  it("请求体固定两张，且不发送无接口可读的 style_id / prompt_template_id", async () => {
    stub = installFetchStub({ "POST /api/v1/generations": { json: envelope(generation()) } });
    await submitGeneration({ draftId: "d3", mode: "PHOTO_PROMPT", prompt: "x", assetIds: ["a1"] });
    const body = JSON.parse(stub.last("POST /api/v1/generations")!.body as string);
    expect(body.output_count).toBe(2);
    expect(body.input_asset_ids).toEqual(["a1"]);
    expect("style_id" in body).toBe(false);
    expect("prompt_template_id" in body).toBe(false);
  });

  it("上传意图按（摘要,大小）复用一个键；下载键在成功后释放", async () => {
    stub = installFetchStub({
      "POST /api/v1/assets/uploads": {
        json: envelope({
          asset_id: "a1",
          purpose: "SOURCE",
          status: "PENDING_UPLOAD",
          method: "PUT",
          upload_url: "http://minio.invalid/u",
          expires_at: "2026-01-01T00:10:00Z",
        }),
      },
      "POST /api/v1/assets/a1/download-url": {
        json: envelope({ method: "GET", url: "http://minio.invalid/d", expires_at: "2026-01-01T00:05:00Z" }),
      },
    });
    const intent = { filename: "a.png", mimeType: "image/png", sizeBytes: 10, sha256: "ab".repeat(32) };
    await createUploadIntent(intent);
    await createUploadIntent(intent);
    const uploadKeys = stub.calls
      .filter((call) => call.pathname === "/api/v1/assets/uploads")
      .map((call) => call.headers["idempotency-key"]);
    expect(uploadKeys[0]).toBe(uploadKeys[1]);

    await createDownloadUrl("a1");
    await createDownloadUrl("a1");
    const downloadKeys = stub.calls
      .filter((call) => call.pathname === "/api/v1/assets/a1/download-url")
      .map((call) => call.headers["idempotency-key"]);
    expect(downloadKeys[0]).not.toBe(downloadKeys[1]);
  });
});
