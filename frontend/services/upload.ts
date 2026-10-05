import { completeUpload, createUploadIntent, fetchAsset } from "./api";
import { putSignedBytes } from "./http";
import { uploadIntentScope, releaseIdempotencyKey } from "./idempotency";

/** 十六进制 SHA-256；浏览器里走 Web Crypto，测试可注入替身。 */
export type Digest = (bytes: ArrayBuffer) => Promise<string>;

export const webCryptoDigest: Digest = async (bytes) => {
  const subtle = globalThis.crypto?.subtle;
  if (!subtle) throw new Error("当前环境缺少 Web Crypto，无法计算文件摘要");
  const hash = await subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(hash)].map((part) => part.toString(16).padStart(2, "0")).join("");
};

export type UploadStage = "hashing" | "signing" | "uploading" | "completing" | "confirming" | "ready";

export type UploadResult = {
  assetId: string;
  status: string;
  /** 服务端确认的用途；调用方据此判断拿回来的确实是自己要的那一类图。 */
  purpose: "SOURCE" | "REFERENCE";
  sha256: string;
  sizeBytes: number;
};

/** 完成校验后资源可能仍是 `PENDING_UPLOAD`；按短间隔轮询直到 READY 或超限。 */
const CONFIRM_ATTEMPTS = 10;
const CONFIRM_INTERVAL_MS = 400;

async function sleep(ms: number): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * 走完「摘要 → 上传意图 → 按签名 URL 送原始字节 → 完成 → 确认 READY」整条路。
 *
 * 只有 `READY` 的资源才算可用：服务端在此之前不接受它作为生成输入、也不接受读图
 * （契约第 8 节），所以这里不把 `PENDING_UPLOAD` 当成功返回。
 *
 * `purpose` 决定这张图的身份：`SOURCE` 是"我要改造的那张图"，`REFERENCE` 是
 * "我拿来找灵感的参考图"。两者在服务端的用途校验不同，绝不能混用同一个意图。
 */
export async function uploadAsset(
  file: File,
  options: {
    purpose?: "SOURCE" | "REFERENCE";
    digest?: Digest;
    onStage?: (stage: UploadStage) => void;
    signal?: AbortSignal;
  } = {},
): Promise<UploadResult> {
  const purpose = options.purpose ?? "SOURCE";
  const digest = options.digest ?? webCryptoDigest;
  const onStage = options.onStage ?? (() => undefined);
  const abort = () => options.signal?.aborted === true;

  onStage("hashing");
  const bytes = await file.arrayBuffer();
  const sha256 = await digest(bytes);
  if (abort()) throw new DOMException("上传已取消", "AbortError");

  onStage("signing");
  const intent = await createUploadIntent({
    purpose,
    filename: file.name,
    mimeType: file.type || "application/octet-stream",
    sizeBytes: bytes.byteLength,
    sha256,
  });
  if (abort()) throw new DOMException("上传已取消", "AbortError");

  onStage("uploading");
  await putSignedBytes(intent.uploadUrl, bytes);

  onStage("completing");
  let status = (await completeUpload(intent.assetId)).status;

  onStage("confirming");
  for (let attempt = 0; status !== "READY" && attempt < CONFIRM_ATTEMPTS; attempt += 1) {
    await sleep(CONFIRM_INTERVAL_MS);
    status = (await fetchAsset(intent.assetId)).status;
  }

  // 意图已终局：释放键，换一张图或重传新内容都是新意图、拿新键
  releaseIdempotencyKey(uploadIntentScope(sha256, bytes.byteLength, purpose));
  onStage("ready");
  return { assetId: intent.assetId, status, purpose: intent.purpose, sha256, sizeBytes: bytes.byteLength };
}
