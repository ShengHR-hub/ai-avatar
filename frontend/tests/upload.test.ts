import { afterEach, describe, expect, it } from "vitest";

import { uploadAsset } from "@/services/upload";
import { envelope, errorBody, installFetchStub } from "./helpers/stub";

let stub = installFetchStub();
afterEach(() => stub.restore());

const DIGEST = "a".repeat(64);
const digest = async () => DIGEST;

function file(name = "avatar.png", bytes = new Uint8Array([1, 2, 3, 4])): File {
  return new File([bytes], name, { type: "image/png" });
}

const intentReply = (purpose = "SOURCE") =>
  envelope({
    asset_id: "asset-1",
    purpose,
    status: "PENDING_UPLOAD",
    method: "PUT",
    upload_url: "http://minio.invalid/signed/put-1",
    expires_at: "2026-01-01T00:10:00Z",
  });

const readyReply = () => ({
  "POST /api/v1/assets/asset-1/complete": { json: envelope({ id: "asset-1", status: "READY" }) },
});

describe("上传到 READY", () => {
  it("顺序必须是意图 → 直传 → 完成校验 → 就绪", async () => {
    stub = installFetchStub({
      "POST /api/v1/assets/uploads": { json: intentReply() },
      "PUT /signed/put-1": { status: 200 },
      "POST /api/v1/assets/asset-1/complete": {
        json: envelope({ id: "asset-1", status: "READY", mime_type: "image/png" }),
      },
    });
    const stages: string[] = [];
    const result = await uploadAsset(file(), {
      digest,
      onStage: (stage) => stages.push(stage),
    });

    expect(stub.calls.map((call) => `${call.method} ${call.pathname}`)).toEqual([
      "POST /api/v1/assets/uploads",
      "PUT /signed/put-1",
      "POST /api/v1/assets/asset-1/complete",
    ]);
    expect(result.status).toBe("READY");
    expect(result.purpose).toBe("SOURCE");
    expect(stages).toEqual(["hashing", "signing", "uploading", "completing", "confirming", "ready"]);
  });

  it("意图请求带摘要与字节数，且带幂等键", async () => {
    stub = installFetchStub({
      "POST /api/v1/assets/uploads": { json: intentReply() },
      "PUT /signed/put-1": { status: 200 },
      ...readyReply(),
    });
    await uploadAsset(file(), { digest });
    const intent = stub.last("POST /api/v1/assets/uploads")!;
    expect(JSON.parse(intent.body as string)).toEqual({
      purpose: "SOURCE",
      filename: "avatar.png",
      mime_type: "image/png",
      size_bytes: 4,
      sha256: DIGEST,
    });
    expect(intent.headers["idempotency-key"]).toBeTruthy();
  });

  it("灵感参考图按 purpose=REFERENCE 上传，并回读服务端确认的用途", async () => {
    stub = installFetchStub({
      "POST /api/v1/assets/uploads": { json: intentReply("REFERENCE") },
      "PUT /signed/put-1": { status: 200 },
      ...readyReply(),
    });

    const result = await uploadAsset(file("inspo.png"), { purpose: "REFERENCE", digest });

    const intent = stub.last("POST /api/v1/assets/uploads")!;
    expect(JSON.parse(intent.body as string).purpose).toBe("REFERENCE");
    expect(result.purpose).toBe("REFERENCE");
  });

  it("同一张图当原图和当参考图是两个意图：幂等键不得相同", async () => {
    stub = installFetchStub({
      "POST /api/v1/assets/uploads": { json: intentReply() },
      "PUT /signed/put-1": { status: 200 },
      ...readyReply(),
    });

    await uploadAsset(file(), { purpose: "SOURCE", digest });
    const sourceKey = stub.last("POST /api/v1/assets/uploads")!.headers["idempotency-key"];
    sessionStorage.clear();
    await uploadAsset(file(), { purpose: "REFERENCE", digest });
    const referenceKey = stub.last("POST /api/v1/assets/uploads")!.headers["idempotency-key"];

    expect(sourceKey).toBeTruthy();
    expect(referenceKey).toBeTruthy();
    expect(sourceKey).not.toBe(referenceKey);
  });

  it("完成校验失败时把稳定码带回来，不谎报就绪", async () => {
    stub = installFetchStub({
      "POST /api/v1/assets/uploads": { json: intentReply() },
      "PUT /signed/put-1": { status: 200 },
      "POST /api/v1/assets/asset-1/complete": {
        status: 422,
        json: errorBody("ASSET_INVALID", "校验未通过", { reason: "decode_failed" }),
      },
    });
    await expect(uploadAsset(file(), { digest })).rejects.toMatchObject({
      code: "ASSET_INVALID",
      action: "retry",
    });
  });

  it("服务端仍未 READY 时如实返回状态，由界面继续确认", async () => {
    stub = installFetchStub({
      "POST /api/v1/assets/uploads": { json: intentReply() },
      "PUT /signed/put-1": { status: 200 },
      "POST /api/v1/assets/asset-1/complete": {
        json: envelope({ id: "asset-1", status: "PENDING_UPLOAD" }),
      },
      "GET /api/v1/assets/asset-1": { json: envelope({ id: "asset-1", status: "READY" }) },
    });
    const result = await uploadAsset(file(), { digest });
    expect(result.status).toBe("READY");
    expect(stub.count("GET /api/v1/assets/asset-1")).toBeGreaterThan(0);
  });
});
