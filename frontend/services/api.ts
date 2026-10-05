import {
  completeAssetUpload,
  createAssetDownloadUrl,
  createAssetUpload,
  createFeedback,
  createGeneration,
  createReferenceAnalysis,
  createShare,
  getAsset,
  getGeneration,
  getMe,
  getPointBalance,
  getSession,
  getShare,
  listGenerationOutputs,
  listMyGenerations,
  logout,
  redeemInvite,
  requestEmailVerification,
  retryGeneration,
  verifyEmail,
} from "@/generated";
import type {
  Asset,
  AssetEnvelope,
  Envelope,
  Generation,
  GenerationEnvelope,
  PageEnvelope,
  PointBalance,
  PointBalanceEnvelope,
  Session,
  SessionEnvelope,
  User,
  UserEnvelope,
} from "@/generated/types.gen";
import { OUTPUT_COUNT } from "./config";
import { ApiFailure, toApiFailure } from "./errors";
import {
  analysisScope,
  completeScope,
  downloadScope,
  emailRequestScope,
  feedbackScope,
  idempotencyKey,
  redeemScope,
  releaseIdempotencyKey,
  retryScope,
  submitScope,
  uploadIntentScope,
  verifyScope,
} from "./idempotency";
import { unwrap, unwrapData } from "./http";

/**
 * 页面只从这里取数：契约的 snake_case DTO 不外泄到组件，字段转换集中在本层。
 *
 * `POST /assets/uploads` 与 `POST /assets/{id}/download-url` 的响应体在契约里是通用
 * `Envelope`（`data` 不受 Schema 约束），生成类型只能是 `unknown`。这里做运行时守卫
 * 而不是盲断言：缺字段就当响应畸形，绝不把半个对象交给上传或下载流程。
 */
export type UploadIntentView = {
  assetId: string;
  /** 服务端回读的实际用途；组件按它决定这张图是"要改造的原图"还是"灵感参考图"。 */
  purpose: "SOURCE" | "REFERENCE";
  status: string;
  method: string;
  uploadUrl: string;
  expiresAt: string;
};

export type DownloadUrlView = { method: string; url: string; expiresAt: string };

export type OutputView = {
  assetId: string;
  outputIndex: number;
  mimeType: string;
  width: number | null;
  height: number | null;
  sizeBytes: number;
};

export type GenerationPage = {
  items: Generation[];
  nextCursor: string | null;
  hasMore: boolean;
};

function malformed(field: string): ApiFailure {
  return new ApiFailure({
    code: "UNKNOWN_RESPONSE",
    message: `服务端响应缺少 ${field} 字段。`,
    status: 0,
    details: { field },
  });
}

function asRecord(value: unknown, field = "data"): Record<string, unknown> {
  if (typeof value !== "object" || value === null) throw malformed(field);
  return value as Record<string, unknown>;
}

function readText(record: Record<string, unknown>, key: string): string {
  const value = record[key];
  if (typeof value !== "string" || value.length === 0) throw malformed(key);
  return value;
}

function readNumber(record: Record<string, unknown>, key: string): number {
  const value = record[key];
  if (typeof value !== "number" || !Number.isFinite(value)) throw malformed(key);
  return value;
}

function readNullableNumber(record: Record<string, unknown>, key: string): number | null {
  const value = record[key];
  if (value === null || value === undefined) return null;
  return typeof value === "number" ? value : null;
}

/** 布尔位必须真是布尔：缺字段时宁可报"响应畸形"，也不要默默当成 false 或 true。 */
function readBoolean(record: Record<string, unknown>, key: string): boolean {
  const value = record[key];
  if (typeof value !== "boolean") throw malformed(key);
  return value;
}

/** 通用 Envelope 的内层数据；既支持 `{data:{...}}`，也容忍平铺在响应体上。 */
function envelopeBody(body: Envelope): Record<string, unknown> {
  const record = asRecord(body);
  return "data" in record ? asRecord(record.data, "data") : record;
}

// -------------------------------------------------------------------------- 认证
export async function redeemInviteWith(code: string, email: string): Promise<void> {
  const scope = redeemScope(code, email);
  try {
    await unwrap(redeemInvite({
      body: { invite_code: code, email },
      headers: { "Idempotency-Key": idempotencyKey(scope) },
    }));
    releaseIdempotencyKey(scope);
  } catch (error) {
    // 明确被拒（无效/已用）时不留旧键：换内容重试属于新意图；进行中则保留以便复用
    if (error instanceof ApiFailure && error.code !== "IDEMPOTENCY_IN_PROGRESS") {
      releaseIdempotencyKey(scope);
    }
    throw error;
  }
}

export async function requestEmailVerificationFor(email: string): Promise<void> {
  const scope = emailRequestScope(email);
  try {
    await unwrap(requestEmailVerification({
      body: { email },
      headers: { "Idempotency-Key": idempotencyKey(scope) },
    }));
  } finally {
    releaseIdempotencyKey(scope);
  }
}

export async function verifyEmailToken(token: string): Promise<void> {
  const scope = verifyScope(token);
  await unwrap(verifyEmail({
    body: { token },
    headers: { "Idempotency-Key": idempotencyKey(scope) },
  }));
  releaseIdempotencyKey(scope);
}

export function fetchSession(): Promise<Session> {
  return unwrapData<SessionEnvelope>(getSession({}));
}

export function fetchCurrentUser(): Promise<User> {
  return unwrapData<UserEnvelope>(getMe({}));
}

export function logoutSession(): Promise<unknown> {
  const scope = "logout";
  return unwrap(logout({ headers: { "Idempotency-Key": idempotencyKey(scope) } })).finally(() =>
    releaseIdempotencyKey(scope),
  );
}

// -------------------------------------------------------------------- 点数与历史
export function fetchPointBalance(): Promise<PointBalance> {
  return unwrapData<PointBalanceEnvelope>(getPointBalance({}));
}

/** `PageEnvelope.data` 是 `Array<unknown>`：游标与条目都从响应体自己取。 */
export async function fetchGenerationPage(cursor?: string): Promise<GenerationPage> {
  const page = await unwrap<PageEnvelope>(
    listMyGenerations({ query: cursor ? { cursor, limit: 20 } : { limit: 20 } }),
  );
  const record = asRecord(page);
  const rows = Array.isArray(record.data) ? record.data : [];
  const meta = asRecord(record.page, "page");
  return {
    items: rows as unknown as Generation[],
    nextCursor: typeof meta.next_cursor === "string" ? meta.next_cursor : null,
    hasMore: meta.has_more === true,
  };
}

// -------------------------------------------------------------------------- 资产
export async function createUploadIntent(input: {
  filename: string;
  mimeType: string;
  sizeBytes: number;
  sha256: string;
  /** 默认「要改造的原图」；「找灵感的参考图」显式传 `REFERENCE`。 */
  purpose?: "SOURCE" | "REFERENCE";
}): Promise<UploadIntentView> {
  const purpose = input.purpose ?? "SOURCE";
  const body = await unwrap<Envelope>(
    createAssetUpload({
      body: {
        purpose,
        filename: input.filename,
        mime_type: input.mimeType,
        size_bytes: input.sizeBytes,
        sha256: input.sha256,
      },
      headers: {
        "Idempotency-Key": idempotencyKey(
          uploadIntentScope(input.sha256, input.sizeBytes, purpose),
        ),
      },
    }),
  );
  const record = envelopeBody(body);
  return {
    assetId: readText(record, "asset_id"),
    purpose: readText(record, "purpose") === "REFERENCE" ? "REFERENCE" : "SOURCE",
    status: readText(record, "status"),
    method: readText(record, "method"),
    uploadUrl: readText(record, "upload_url"),
    expiresAt: readText(record, "expires_at"),
  };
}

export async function completeUpload(assetId: string): Promise<Asset> {
  const scope = completeScope(assetId);
  try {
    return await unwrapData<AssetEnvelope>(
      completeAssetUpload({
        path: { asset_id: assetId },
        headers: { "Idempotency-Key": idempotencyKey(scope) },
      }),
    );
  } finally {
    releaseIdempotencyKey(scope);
  }
}

/**
 * 资源元数据读取。契约把该 operation 标为匿名可访问（分享页也走它），实现按「可选
 * viewer」判所有权，因此凭据由传输层无条件补上，这里不需要额外处理。
 */
export function fetchAsset(assetId: string): Promise<Asset> {
  return unwrapData<AssetEnvelope>(getAsset({ path: { asset_id: assetId } }));
}

/** 一次点击取图是一个意图：失败重试复用同键，成功后释放，下次点击拿新的短期 URL。 */
export async function createDownloadUrl(assetId: string): Promise<DownloadUrlView> {
  const scope = downloadScope(assetId);
  try {
    const body = await unwrap<Envelope>(
      createAssetDownloadUrl({
        path: { asset_id: assetId },
        headers: { "Idempotency-Key": idempotencyKey(scope) },
      }),
    );
    const record = envelopeBody(body);
    return {
      method: readText(record, "method"),
      url: readText(record, "url"),
      expiresAt: readText(record, "expires_at"),
    };
  } finally {
    releaseIdempotencyKey(scope);
  }
}

/**
 * 一次「读这张参考图」（`POST /api/v1/analyses`）。
 *
 * 返回的 `description` 是**近似画面描述**：模型看这张图现写的一段话，
 * 不是原图的原始提示词，也不保证照它能复刻出这张图。界面上必须按这个口径说。
 * `fromCache` 为真表示这次没有重新出网、没有再付费——同一张图 6 小时内回放。
 */
export type ReferenceDescriptionView = {
  assetId: string;
  description: string;
  model: string;
  fromCache: boolean;
};

export async function describeReferenceImage(
  assetId: string,
): Promise<ReferenceDescriptionView> {
  const scope = analysisScope(assetId);
  try {
    const body = await unwrap<Envelope>(
      createReferenceAnalysis({
        body: { asset_id: assetId },
        headers: { "Idempotency-Key": idempotencyKey(scope) },
      }),
    );
    const record = envelopeBody(body);
    return {
      assetId: readText(record, "asset_id"),
      description: readText(record, "description"),
      model: readText(record, "model"),
      fromCache: readBoolean(record, "from_cache"),
    };
  } finally {
    // 无论成功还是明确被拒都释放：换一张图、或用户点"再读一次"都是新意图
    releaseIdempotencyKey(scope);
  }
}

// -------------------------------------------------------------------------- 生成
export type SubmitInput = {
  draftId: string;
  mode: "PHOTO_PROMPT" | "TEXT_PROMPT";
  prompt: string;
  assetIds: string[];
};

/**
 * 提交一次生成。`output_count` 是契约常量 `2`；本卡没有任何读取风格或模板列表的接口，
 * 所以 `style_id`、`prompt_template_id` 一律不发，让服务端取默认值。
 */
export async function submitGeneration(input: SubmitInput): Promise<Generation> {
  return unwrapData<GenerationEnvelope>(
    createGeneration({
      body: {
        mode: input.mode,
        prompt: input.prompt,
        input_asset_ids: input.assetIds,
        output_count: OUTPUT_COUNT,
      },
      headers: { "Idempotency-Key": idempotencyKey(submitScope(input.draftId)) },
    }),
  );
}

export function fetchGeneration(generationId: string): Promise<Generation> {
  return unwrapData<GenerationEnvelope>(getGeneration({ path: { generation_id: generationId } }));
}

export async function fetchOutputs(generationId: string): Promise<OutputView[]> {
  const page = await unwrap<PageEnvelope>(
    listGenerationOutputs({ path: { generation_id: generationId } }),
  );
  const rows = Array.isArray(page.data) ? page.data : [];
  return rows.map((row) => {
    const item = asRecord(row);
    return {
      assetId: readText(item, "asset_id"),
      outputIndex: readNumber(item, "output_index"),
      mimeType: readText(item, "mime_type"),
      width: readNullableNumber(item, "width"),
      height: readNullableNumber(item, "height"),
      sizeBytes: readNumber(item, "size_bytes"),
    };
  });
}

/** 同一任务的重试沿用其重试键：反复点击也不会派生成多次扣点。 */
export function retryGenerationFor(generationId: string): Promise<Generation> {
  return unwrapData<GenerationEnvelope>(
    retryGeneration({
      path: { generation_id: generationId },
      headers: { "Idempotency-Key": idempotencyKey(retryScope(generationId)) },
    }),
  );
}

// -------------------------------------------------------------------- 分享
/** 分享页里一张被选中结果的呈现元数据（与 `GET /shares/{slug}` 的 `outputs[]` 对应）。 */
export type ShareOutputView = {
  assetId: string;
  outputIndex: number;
  mimeType: string;
  width: number | null;
  height: number | null;
  sizeBytes: number;
};

export type ShareView = {
  slug: string;
  title: string | null;
  expiresAt: string | null;
  createdAt: string;
  outputs: ShareOutputView[];
};

/** `ShareEnvelope.data` 在契约里是未约束 object，因此逐字段做运行时守卫。 */
function toShareView(body: Envelope): ShareView {
  const record = envelopeBody(body);
  const rows = Array.isArray(record.outputs) ? record.outputs : [];
  return {
    slug: readText(record, "slug"),
    title: typeof record.title === "string" ? record.title : null,
    expiresAt: typeof record.expires_at === "string" ? record.expires_at : null,
    createdAt: readText(record, "created_at"),
    outputs: rows.map((row) => {
      const item = asRecord(row, "outputs[]");
      return {
        assetId: readText(item, "asset_id"),
        outputIndex: readNumber(item, "output_index"),
        mimeType: readText(item, "mime_type"),
        width: readNullableNumber(item, "width"),
        height: readNullableNumber(item, "height"),
        sizeBytes: readNumber(item, "size_bytes"),
      };
    }),
  };
}

/**
 * 创建分享链接。`outputAssetIds` 是**被明确选中**的结果图（一张或两张）；
 * 同任务未选中的结果不会被公开。
 *
 * `idempotencyKey` 由调用方给出，**且必须来自 `services/shareIntent.ts` 的
 * `current.key`** —— 这是分享幂等键的**唯一可信事实**。本函数不再自己生成或缓存
 * 任何键：以前那套「另一份 sessionStorage 缓存」会让「清空任务 → 重访同一任务」
 * 复用第一条分享的历史请求头（同一意图序号推出同一个缓存条目）。
 *
 * 语义分工：
 *
 * - 同一次意图（网络重试、双击、刷新后重试）→ 同一个 key，服务端回放同一条链接；
 * - 用户明确点「再生成一条分享链接」→ `advanceShareIntent` 换一把从未用过的 key，
 *   服务端才会真正新建。
 */
export async function createShareLink(input: {
  generationId: string;
  outputAssetIds: string[];
  title?: string | null;
  idempotencyKey: string;
}): Promise<ShareView> {
  const body = await unwrap<Envelope>(
    createShare({
      body: {
        generation_id: input.generationId,
        output_asset_ids: input.outputAssetIds,
        title: input.title ?? null,
      },
      headers: { "Idempotency-Key": input.idempotencyKey },
    }),
  );
  return toShareView(body);
}

/**
 * 匿名读取分享。**不带 Cookie 也能用**（契约把该 operation 标为 `security: []`），
 * 因此分享页在未登录状态下同样可以渲染结果。
 */
export async function fetchShare(slug: string): Promise<ShareView> {
  const body = await unwrap<Envelope>(getShare({ path: { slug } }));
  return toShareView(body);
}

// -------------------------------------------------------------------- 反馈
export type FeedbackView = {
  id: string;
  generationId: string;
  rating: number;
  category: string;
  content: string | null;
  status: string;
  createdAt: string;
};

/** 反馈的 `data` 同样是未约束 object，逐字段守卫。 */
function toFeedbackView(body: Envelope): FeedbackView {
  const record = envelopeBody(body);
  return {
    id: readText(record, "id"),
    generationId: readText(record, "generation_id"),
    rating: readNumber(record, "rating"),
    category: readText(record, "category"),
    content: typeof record.content === "string" ? record.content : null,
    status: readText(record, "status"),
    createdAt: readText(record, "created_at"),
  };
}

/**
 * 提交反馈。每用户每任务只有一条：重复提交（同键同内容）回放第一次结果，
 * 换了内容则服务端返回稳定冲突，界面提示「每个作品只有一次评价」。
 */
export async function submitFeedback(input: {
  generationId: string;
  rating: number;
  category: string;
  content?: string | null;
}): Promise<FeedbackView> {
  const body = await unwrap<Envelope>(
    createFeedback({
      body: {
        generation_id: input.generationId,
        rating: input.rating,
        category: input.category,
        content: input.content ?? null,
      },
      headers: {
        "Idempotency-Key": idempotencyKey(feedbackScope(input.generationId)),
      },
    }),
  );
  return toFeedbackView(body);
}

export { toApiFailure };
