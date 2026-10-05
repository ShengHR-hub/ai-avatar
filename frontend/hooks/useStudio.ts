"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  createDownloadUrl,
  createShareLink as createShareLinkRequest,
  describeReferenceImage,
  fetchShare,
  retryGenerationFor,
  submitFeedback as submitFeedbackRequest,
  submitGeneration,
  type DownloadUrlView,
  type FeedbackView,
  type ShareView,
  type SubmitInput,
} from "@/services/api";
import type { OpenMode } from "@/services/config";
import { ApiFailure } from "@/services/errors";
import {
  clearActiveGeneration,
  currentDraftId,
  isTerminal,
  readActiveGeneration,
  rememberActiveGeneration,
  rotateDraftId,
} from "@/services/generation";
import {
  advanceShareIntent,
  clearShareIntent,
  rememberShareSlug,
  restoreShareIntent,
  ShareIntentExhausted,
} from "@/services/shareIntent";
import { uploadAsset, type Digest, type UploadStage } from "@/services/upload";
import { watchGeneration, type WatchSnapshot } from "@/services/watch";
import type { Generation } from "@/generated/types.gen";

export type UploadState = {
  stage: UploadStage | "idle";
  fileName: string;
  assetId: string | null;
  status: string;
  failure: ApiFailure | null;
  /** 本地预览地址（object URL）。只用于界面预览，不参与提交。 */
  previewUrl: string | null;
};

/**
 * 「参考图片找灵感」的子流程状态：上传 → 读出描述 → 编辑 → 走普通文字创作。
 *
 * `uploaded` 与 `analyzing` 分开是有意的：读出描述是一次真实付费调用，
 * 所以界面把它做成**用户明确按下的那一下**，而不是选完文件就自动花钱。
 */
export type ReferenceStage =
  | "idle"
  | "uploading"
  | "uploaded"
  | "analyzing"
  | "described"
  | "failed";

export type ReferenceState = {
  stage: ReferenceStage;
  fileName: string;
  assetId: string | null;
  previewUrl: string | null;
  /** 模型看这张图**现写**的近似画面描述；不是原图的提示词，也不保证复刻。 */
  description: string;
  model: string;
  /** 这次复用了同一张图在有效期内的结果：没有出网、没有再付费。 */
  fromCache: boolean;
  /**
   * 描述框当时是空的，所以这段直接替用户填进去了（没有覆盖任何手写的字）。
   * 必须在写入的那一刻判定：写完了再回头看"框里有没有字"永远是"有"。
   */
  autoFilled: boolean;
  failure: ApiFailure | null;
};

/** 创作台当前停在哪个入口上。入口 03 不是第四种生成方式，它最终仍按文字创作提交。 */
export type StudioWay = "text" | "photo" | "inspire";

export type StudioState = {
  mode: OpenMode;
  way: StudioWay;
  prompt: string;
  ready: boolean;
  upload: UploadState;
  reference: ReferenceState;
  generationId: string | null;
  watch: WatchSnapshot | null;
  recovering: boolean;
  /** 这次等待是刷新后接上的旧任务，而不是刚提交的新任务。 */
  resumed: boolean;
  busy: boolean;
  failure: ApiFailure | null;
};

const IDLE_UPLOAD: UploadState = {
  stage: "idle",
  fileName: "",
  assetId: null,
  status: "",
  failure: null,
  previewUrl: null,
};

const IDLE_REFERENCE: ReferenceState = {
  stage: "idle",
  fileName: "",
  assetId: null,
  previewUrl: null,
  description: "",
  model: "",
  fromCache: false,
  autoFilled: false,
  failure: null,
};

/**
 * 分享状态。`created` 里带的是服务端回放的链接，界面据此渲染绝对地址；
 * 失败区分「这次没成功（可重试）」与「换了选中组合（要新开一次意图）」，
 * 后者是 409 `IDEMPOTENCY_CONFLICT`。
 *
 * `generationId` 是**归属标记**：分享与反馈都按任务隔离，旧任务迟到的响应
 * 不允许污染当前任务的面板（见 `useStudio` 里的 `belongsToCurrent`）。
 */
export type ShareStage = "idle" | "creating" | "created" | "failed";

export type ShareState = {
  stage: ShareStage;
  generationId: string | null;
  view: ShareView | null;
  failure: ApiFailure | null;
};

const IDLE_SHARE: ShareState = {
  stage: "idle",
  generationId: null,
  view: null,
  failure: null,
};

function idleShareFor(generationId: string | null): ShareState {
  return { stage: "idle", generationId, view: null, failure: null };
}

/** 反馈状态。每用户每任务一条：`submitted` 之后界面只读展示。 */
export type FeedbackStage = "idle" | "submitting" | "submitted" | "failed";

export type FeedbackState = {
  stage: FeedbackStage;
  generationId: string | null;
  view: FeedbackView | null;
  failure: ApiFailure | null;
};

const IDLE_FEEDBACK: FeedbackState = {
  stage: "idle",
  generationId: null,
  view: null,
  failure: null,
};

function idleFeedbackFor(generationId: string | null): FeedbackState {
  return { stage: "idle", generationId, view: null, failure: null };
}

function wayFor(mode: OpenMode): StudioWay {
  return mode === "TEXT_PROMPT" ? "text" : "photo";
}

/** jsdom 等环境没有 `URL.createObjectURL`：缺了就只做无预览的降级，不影响上传。 */
function objectUrlFor(file: File): string | null {
  if (typeof URL === "undefined" || typeof URL.createObjectURL !== "function") return null;
  try {
    return URL.createObjectURL(file);
  } catch {
    return null;
  }
}

function revoke(url: string | null) {
  if (url && typeof URL !== "undefined" && typeof URL.revokeObjectURL === "function") {
    URL.revokeObjectURL(url);
  }
}

/**
 * studio 页面的唯一状态机：上传 → 提交 → 轮询 → 终局，刷新后的恢复，
 * 以及「参考图片找灵感」这条前置子流程（传参考图 → 读出描述 → 改字 → 仍按文字创作提交）。
 *
 * 轮询只在 `services/watch.ts` 里实现一次；这里只负责驱动与呈现，页面不各自实现
 * 等待逻辑（`06-development-standard.md` 第 3 节）。视觉重做没有改动提交、恢复、
 * 幂等和签名下载的语义。
 *
 * 三条入口共用同一个**始终可编辑**的描述区域：入口 03 读出来的描述只是往这个框里
 * 填了一次字，参考图本身永远不进生成请求（`TEXT_PROMPT` 不接受输入资产，契约如此）。
 */
export function useStudio(
  options: {
    digest?: Digest;
    initialGenerationId?: string | null;
    initialMode?: OpenMode;
    initialWay?: StudioWay;
    initialPrompt?: string;
  } = {},
) {
  const initialMode = options.initialMode ?? "PHOTO_PROMPT";
  // 入口 03 不是第四种生成方式：它仍按文字创作提交，只是前面多了一段"看图说话"。
  const initialWay = options.initialWay ?? wayFor(initialMode);
  const [state, setState] = useState<StudioState>({
    mode: initialWay === "photo" ? initialMode : "TEXT_PROMPT",
    way: initialWay,
    prompt: options.initialPrompt ?? "",
    ready: false,
    upload: IDLE_UPLOAD,
    reference: IDLE_REFERENCE,
    generationId: null,
    watch: null,
    recovering: false,
    resumed: false,
    busy: false,
    failure: null,
  });
  const [downloads, setDownloads] = useState<Record<string, DownloadUrlView | "loading" | "error">>({});
  const draftId = useRef<string>(currentDraftId());
  const controller = useRef<AbortController | null>(null);
  const preview = useRef<string | null>(null);
  const referencePreview = useRef<string | null>(null);
  const fetched = useRef<Set<string>>(new Set());
  /**
   * 防误触：正在执行的动作名（`null` = 空闲）。
   *
   * `canSubmit` 是渲染态推导出来的，而按钮的 `disabled` 要等一次 React 渲染才生效：
   * 同一帧内的两下点击（双击、回车连击、触屏双触发）都会进到 `submit`，于是同一次
   * 用户意图会发出两个请求。服务端幂等键把它们折叠成同一个任务，但前端仍会白跑一次
   * 请求、并可能出现两个并行的轮询。
   *
   * 这是**同步**判定，所以在事件处理函数里立刻生效；用 `finally` 复位，因此失败后
   * 用户可以直接重试，成功进入终局后下一次点击就是新的主动生成意图（`draftId` 已轮换，
   * 服务端会当成新请求），不会被这里永久挡住。
   */
  const inFlight = useRef<string | null>(null);

  /**
   * 当前任务号（与渲染态同步镜像）。
   *
   * 分享/反馈的异步响应回来时用它核对归属：`state.generationId` 在闭包里已固定，
   * 只有 ref 能读到「现在」的任务号。旧任务的迟到响应因此会被丢弃，
   * 不会把上一条任务的链接或反馈状态画到新任务下面。
   */
  const latestGeneration = useRef<string | null>(null);

  const patch = useCallback((changes: Partial<StudioState>) => {
    setState((previous) => ({ ...previous, ...changes }));
  }, []);

  const runWatch = useCallback(
    async (generationId: string, resumed = false) => {
      controller.current?.abort();
      const signal = new AbortController();
      controller.current = signal;
      // `resumed` 只表示"这是刷新后接上的旧任务"，与 `recovering`（正在等待）区分开，
      // 否则新提交的等待会被误写成"已接上之前的任务"
      patch({ recovering: true, resumed, generationId, failure: null });
      try {
        const snapshot = await watchGeneration({
          generationId,
          signal: signal.signal,
          onSnapshot: (next) => patch({ watch: next }),
        });
        if (isTerminal(snapshot.status) && snapshot.status === "SUCCEEDED") {
          // 意图已终局：换新草稿号，下一次提交是新意图、拿新的幂等键
          draftId.current = rotateDraftId();
        }
        // 终局或放弃后必须交还操作权，否则用户只能刷新页面才能再发一次
        patch({ recovering: false, busy: false });
      } catch (error) {
        patch({ recovering: false, busy: false, failure: error instanceof ApiFailure ? error : null });
      }
    },
    [patch],
  );

  // 刷新恢复：优先接住深链接指定的任务，其次接住本标签页进行中的任务；
  // 两条路都只是接着轮询同一个任务，绝不重新提交（那会二次扣点）
  useEffect(() => {
    const target = options.initialGenerationId ?? readActiveGeneration();
    if (target) void runWatch(target, true);
    return () => controller.current?.abort();
  }, [options.initialGenerationId, runWatch]);

  // 组件卸载时释放预览地址，避免长期占用内存
  useEffect(
    () => () => {
      revoke(preview.current);
      revoke(referencePreview.current);
    },
    [],
  );

  const pickFile = useCallback(
    async (file: File) => {
      const nextPreview = objectUrlFor(file);
      revoke(preview.current);
      preview.current = nextPreview;
      setState((previous) => ({
        ...previous,
        busy: true,
        failure: null,
        watch: null,
        upload: { ...previous.upload, stage: "hashing", fileName: file.name, previewUrl: nextPreview },
      }));
      try {
        const result = await uploadAsset(file, {
          purpose: "SOURCE",
          digest: options.digest,
          onStage: (stage) =>
            setState((previous) => ({
              ...previous,
              upload: { ...previous.upload, stage, fileName: file.name, previewUrl: nextPreview },
            })),
        });
        setState((previous) => ({
          ...previous,
          busy: false,
          ready: result.status === "READY",
          upload: {
            stage: "ready",
            fileName: file.name,
            assetId: result.assetId,
            status: result.status,
            failure: null,
            previewUrl: nextPreview,
          },
        }));
      } catch (error) {
        setState((previous) => ({
          ...previous,
          busy: false,
          ready: false,
          upload: {
            ...previous.upload,
            stage: "idle",
            fileName: file.name,
            status: "FAILED",
            failure: error instanceof ApiFailure ? error : null,
            previewUrl: nextPreview,
          },
        }));
      }
    },
    [options.digest, patch],
  );

  const resetUpload = useCallback(() => {
    revoke(preview.current);
    preview.current = null;
    setState((previous) => ({
      ...previous,
      ready: false,
      failure: null,
      upload: { ...IDLE_UPLOAD },
    }));
  }, []);

  /**
   * 选一张参考图来找灵感：先把它作为 `purpose=REFERENCE` 的资产传到用户自己的空间里，
   * 停在 `uploaded` 等用户按「读出画面描述」——那一下才会真的花钱。
   */
  const pickReferenceFile = useCallback(
    async (file: File) => {
      const nextPreview = objectUrlFor(file);
      revoke(referencePreview.current);
      referencePreview.current = nextPreview;
      patch({ failure: null });
      setState((previous) => ({
        ...previous,
        reference: {
          ...previous.reference,
          stage: "uploading",
          fileName: file.name,
          previewUrl: nextPreview,
          assetId: null,
          description: "",
          fromCache: false,
          autoFilled: false,
          failure: null,
        },
      }));
      try {
        const result = await uploadAsset(file, {
          purpose: "REFERENCE",
          digest: options.digest,
        });
        setState((previous) => ({
          ...previous,
          reference: {
            stage: "uploaded",
            fileName: file.name,
            assetId: result.assetId,
            previewUrl: nextPreview,
            description: "",
            model: "",
            fromCache: false,
            autoFilled: false,
            failure: null,
          },
        }));
      } catch (error) {
        setState((previous) => ({
          ...previous,
          reference: {
            ...previous.reference,
            stage: "failed",
            fileName: file.name,
            assetId: null,
            failure: error instanceof ApiFailure ? error : null,
          },
        }));
      }
    },
    [options.digest, patch],
  );

  /** 真正出网的那一步：把参考图换成一段可编辑的画面描述。 */
  const analyzeReference = useCallback(async () => {
    const assetId = state.reference.assetId;
    // 一次付费调用：双击不应变成两次请求（服务端还会按图片去重，但前端不该先打两枪）
    if (!assetId || inFlight.current !== null) return;
    inFlight.current = "analyze";
    setState((previous) => ({
      ...previous,
      reference: { ...previous.reference, stage: "analyzing", failure: null },
    }));
    try {
      const view = await describeReferenceImage(assetId);
      const filled = view.description;
      setState((previous) => ({
        ...previous,
        // 描述框本来就是空的 → 直接填进去；用户已经写了字 → 绝不覆盖，
        // 交还给界面上的「填入 / 追加」两个明确动作（产品硬要求：不意外清空草稿）。
        prompt: previous.prompt.trim() ? previous.prompt : filled,
        reference: {
          stage: "described",
          fileName: previous.reference.fileName,
          assetId: view.assetId,
          previewUrl: previous.reference.previewUrl,
          description: filled,
          model: view.model,
          fromCache: view.fromCache,
          // 写入这一刻才知道"框原来是空的"；渲染时再去看 prompt 是否非空永远是"非空"
          autoFilled: !previous.prompt.trim(),
          failure: null,
        },
      }));
    } catch (error) {
      setState((previous) => ({
        ...previous,
        reference: {
          ...previous.reference,
          stage: "failed",
          failure: error instanceof ApiFailure ? error : null,
        },
      }));
    } finally {
      inFlight.current = null;
    }
  }, [state.reference.assetId]);

  /** 把已经读出来的描述放进描述框：替换或追加都由用户点一下决定。 */
  const applyReferenceDescription = useCallback(
    (mode: "replace" | "append") => {
      const text = state.reference.description;
      if (!text) return;
      setState((previous) => ({
        ...previous,
        prompt:
          mode === "append" && previous.prompt.trim()
            ? `${previous.prompt.trimEnd()}，${text}`
            : text,
      }));
    },
    [state.reference.description],
  );

  const resetReference = useCallback(() => {
    revoke(referencePreview.current);
    referencePreview.current = null;
    setState((previous) => ({ ...previous, reference: { ...IDLE_REFERENCE } }));
  }, []);

  const submit = useCallback(async () => {
    // 同步防误触：同一帧内的第二下点击在这里就被挡住，不会变成第二个请求
    if (inFlight.current !== null) return;
    inFlight.current = "submit";
    const input: SubmitInput = {
      draftId: draftId.current,
      mode: state.mode,
      prompt: state.prompt,
      // 只有完成校验并确认 READY 的资源才作为输入提交（契约第 8 节）
      assetIds:
        state.mode === "PHOTO_PROMPT" && state.ready && state.upload.assetId ? [state.upload.assetId] : [],
    };
    patch({ busy: true, failure: null, watch: null });
    try {
      const created: Generation = await submitGeneration(input);
      rememberActiveGeneration(created.id);
      await runWatch(created.id);
    } catch (error) {
      patch({ busy: false, failure: error instanceof ApiFailure ? error : null });
    } finally {
      // 终局或失败都复位：用户随后再点「重新生成」是一次新的主动意图
      inFlight.current = null;
    }
  }, [patch, runWatch, state.mode, state.prompt, state.ready, state.upload.assetId]);

  const retry = useCallback(async () => {
    const generationId = state.generationId;
    if (!generationId || inFlight.current !== null) return;
    inFlight.current = "retry";
    patch({ busy: true, failure: null });
    try {
      // 重试沿用同一任务号：重试键按任务派生，反复点击也不会二次扣点
      const retried = await retryGenerationFor(generationId);
      rememberActiveGeneration(retried.id);
      await runWatch(retried.id);
    } catch (error) {
      patch({ busy: false, failure: error instanceof ApiFailure ? error : null });
    } finally {
      inFlight.current = null;
    }
  }, [patch, runWatch, state.generationId]);

  const download = useCallback(async (assetId: string) => {
    setDownloads((previous) => ({ ...previous, [assetId]: "loading" }));
    try {
      const view = await createDownloadUrl(assetId);
      setDownloads((previous) => ({ ...previous, [assetId]: view }));
    } catch {
      setDownloads((previous) => ({ ...previous, [assetId]: "error" }));
    }
  }, []);

  // ---------------------------------------------------------------- 分享
  const [share, setShare] = useState<ShareState>(IDLE_SHARE);
  /**
   * 当前分享意图：序号 + 幂等键。
   *
   * `key` 是分享幂等键的**唯一可信事实**，会被原样放进请求头；它只在两种情况下变化：
   * 记录里读出来的当前意图，或用户明确点「再生成一条分享链接」（`advanceShareIntent`）。
   * 普通重试与刷新后重试都复用同一把键（见 `services/shareIntent.ts`）。
   */
  const shareIntent = useRef({ attempt: 0, key: "" });

  /**
   * 分享选中的结果图（一张或两张）。
   *
   * 幂等键 = 当前意图的 `key`（随机、已持久化）。双击或网络重试不会产生第二条链接；
   * 换成别的选中组合会拿到稳定冲突，界面提示改用「再生成一条分享链接」。
   * 响应回来时先核对归属：任务已经换成新的，就丢弃这次结果。
   */
  const createShareLink = useCallback(
    async (assetIds: string[]) => {
      const generationId = state.generationId;
      if (!generationId || inFlight.current !== null) return;
      // 兜底：极少数时序下意图还没恢复（例如刚挂载就触发），当场恢复一次，
      // 保证发出去的键一定来自持久记录，而不是空串或临时值
      if (!shareIntent.current.key) {
        const restored = restoreShareIntent(generationId, shareIntent.current.attempt);
        shareIntent.current = { attempt: restored.attempt, key: restored.key };
      }
      inFlight.current = "share";
      setShare({ stage: "creating", generationId, view: null, failure: null });
      const intent = shareIntent.current;
      try {
        const view = await createShareLinkRequest({
          generationId,
          outputAssetIds: assetIds,
          idempotencyKey: intent.key,
        });
        if (inFlight.current !== "share" || latestGeneration.current !== generationId) {
          return; // 旧任务的迟到响应：丢弃，不污染当前面板
        }
        // 记下「这次意图已经建出了哪条链接」：刷新后据此**重新读取**，
        // 而不是拿同一把键再发一次创建（那只会回放，看起来像"没生效"）。
        rememberShareSlug(generationId, intent, view.slug);
        setShare({ stage: "created", generationId, view, failure: null });
      } catch (error) {
        if (inFlight.current !== "share" || latestGeneration.current !== generationId) {
          return;
        }
        // 幂等冲突**不自动换键**：这个键已经绑定了别的内容，重试同样的请求永远
        // 不会成功，但"悄悄换一把键"等于把一次冲突变成一次新建，用户会以为
        // 自己的重试生效了。正确做法是把状态如实展示，让用户**明确**点
        // 「再生成一条分享链接」——那才是一次新的意图（`startAnotherShare`）。
        setShare({
          stage: "failed",
          generationId,
          view: null,
          failure: error instanceof ApiFailure ? error : null,
        });
      } finally {
        if (inFlight.current === "share") inFlight.current = null;
      }
    },
    [state.generationId],
  );

  /**
   * 明确的「再生成一条分享链接」入口。
   *
   * 递增意图序号 = 换一把**从未用过**的新随机幂等键，因此服务端会**真正新建**
   * 一条分享，而不是回放上一条。清空当前面板是为了不把旧链接当成新意图的结果展示。
   *
   * 序号按持久记录里的 `nextAttempt` 推进：刷新之后内存归零，但 `sessionStorage`
   * 里还留着记录，因此新键一定**从未使用过**（见 `services/shareIntent.ts`）。
   */
  const startAnotherShare = useCallback(() => {
    const generationId = state.generationId;
    if (!generationId) return;
    let advanced: { attempt: number; key: string };
    try {
      advanced = advanceShareIntent(generationId, shareIntent.current.attempt);
    } catch (error) {
      if (!(error instanceof ShareIntentExhausted)) throw error;
      // 序号用尽：**不回绕**（回绕会复用历史键），如实报错并保持原状
      setShare({
        stage: "failed",
        generationId,
        view: null,
        failure: new ApiFailure({
          code: "VALIDATION_ERROR",
          message: error.message,
          status: 422,
          requestId: null,
          details: { field: "idempotency_key" },
        }),
      });
      return;
    }
    shareIntent.current = advanced;
    setShare(idleShareFor(generationId));
  }, [state.generationId]);

  // ---------------------------------------------------------------- 反馈
  const [feedback, setFeedback] = useState<FeedbackState>(IDLE_FEEDBACK);

  /** 提交反馈；每用户每任务一条，重复提交回放第一次结果。 */
  const submitFeedbackFor = useCallback(
    async (input: { rating: number; category: string; content: string }) => {
      const generationId = state.generationId;
      if (!generationId || inFlight.current !== null) return;
      inFlight.current = "feedback";
      setFeedback((previous) => ({
        ...previous,
        stage: "submitting",
        generationId,
        failure: null,
      }));
      try {
        const view = await submitFeedbackRequest({
          generationId,
          rating: input.rating,
          category: input.category,
          content: input.content.trim() ? input.content.trim() : null,
        });
        if (
          inFlight.current !== "feedback" ||
          latestGeneration.current !== generationId
        ) {
          return; // 旧任务的迟到响应：丢弃
        }
        setFeedback({ stage: "submitted", generationId, view, failure: null });
      } catch (error) {
        if (
          inFlight.current !== "feedback" ||
          latestGeneration.current !== generationId
        ) {
          return;
        }
        setFeedback((previous) => ({
          ...previous,
          stage: "failed",
          generationId,
          failure: error instanceof ApiFailure ? error : null,
        }));
      } finally {
        if (inFlight.current === "feedback") inFlight.current = null;
      }
    },
    [state.generationId],
  );

  /**
   * 任务切换时恢复/复位分享与反馈面板。
   *
   * 这是「按 generationId 隔离」的第一道：上一轮的 `created` 链接、已提交的反馈
   * 以及失败提示都不应该出现在新任务下面。
   *
   * 同时做**意图恢复**（返修第 4 项）：回到一个已经有分享意图的任务时，
   * 序号从持久化记录里读回来，已经建出的链接按 slug 重新向服务端读取。
   * 这样「刷新 → 重试」复用原键，而「再生成一条」一定拿新键；
   * 服务端仍是唯一裁判（已撤销/已过期/资产失效都会 404，面板如实显示失败）。
   */
  useEffect(() => {
    const generationId = state.generationId;
    if (latestGeneration.current === generationId) return;
    latestGeneration.current = generationId;
    setFeedback(idleFeedbackFor(generationId));

    if (!generationId) {
      shareIntent.current = { attempt: 0, key: "" };
      setShare(idleShareFor(null));
      return;
    }

    const intent = restoreShareIntent(generationId, 0);
    shareIntent.current = { attempt: intent.attempt, key: intent.key };
    const restoredSlug = intent.slug;
    if (!restoredSlug) {
      setShare(idleShareFor(generationId));
      return;
    }

    let cancelled = false;
    setShare({ stage: "creating", generationId, view: null, failure: null });
    void (async () => {
      try {
        const view = await fetchShare(restoredSlug);
        if (cancelled || latestGeneration.current !== generationId) return;
        setShare({ stage: "created", generationId, view, failure: null });
      } catch (error) {
        if (cancelled || latestGeneration.current !== generationId) return;
        setShare({
          stage: "failed",
          generationId,
          view: null,
          failure: error instanceof ApiFailure ? error : null,
        });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [state.generationId]);

  // 结果一出来就自己取短期地址：作品是结果页的主体，不该先让用户点一下才看得见。
  // 每个资源只取一次；失败仍保留手动重取按钮。
  useEffect(() => {
    if (state.watch?.status !== "SUCCEEDED") return;
    for (const output of state.watch.outputs) {
      if (fetched.current.has(output.assetId)) continue;
      fetched.current.add(output.assetId);
      void download(output.assetId);
    }
  }, [download, state.watch]);

  const working = state.watch !== null && !isTerminal(state.watch.status);

  const canSubmit = useMemo(
    () =>
      !state.busy &&
      !working &&
      state.prompt.trim().length > 0 &&
      (state.mode === "TEXT_PROMPT" || state.ready),
    [state.busy, state.mode, state.prompt, state.ready, working],
  );

  return {
    ...state,
    working,
    canSubmit,
    downloads,
    share,
    feedback,
    /** 切入口只改「走哪条路」；描述框里的字一律保留（产品硬要求：不意外清空草稿）。 */
    setMode: (mode: OpenMode) => patch({ mode, way: wayFor(mode) }),
    setWay: (way: StudioWay) =>
      patch({ way, mode: way === "photo" ? "PHOTO_PROMPT" : "TEXT_PROMPT" }),
    setPrompt: (prompt: string) => patch({ prompt }),
    pickFile,
    resetUpload,
    pickReferenceFile,
    analyzeReference,
    applyReferenceDescription,
    resetReference,
    submit,
    retry,
    download,
    createShareLink,
    startAnotherShare,
    submitFeedback: submitFeedbackFor,
    clearActive: () => {
      controller.current?.abort();
      // 用户主动结束这个任务：它的分享意图也不再需要恢复
      if (state.generationId) clearShareIntent(state.generationId);
      clearActiveGeneration();
      // 归属标记必须一起清掉：只清 `watch` 会让分享/反馈面板继续挂在一个
      // 已经结束的任务上（而且 `generationId` 不变时隔离 effect 也不会触发复位）。
      patch({ watch: null, generationId: null });
    },
  };
}
