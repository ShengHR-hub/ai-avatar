"use client";

import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";

import type { UploadState } from "@/hooks/useStudio";
import { OUTPUT_COUNT, type OpenMode } from "@/services/config";
import { USE_PRESETS, type IdeaDimensionId } from "@/services/catalog";
import {
  applyPreset,
  assistPreview,
  composePrompt,
  type AssistSelection,
} from "@/services/promptAssist";
import type { UploadStage } from "@/services/upload";
import { failureTitle } from "@/services/errors";
import { FrameMark } from "./BrandMark";
import { Notice } from "./Notice";
import { IdeaPicker } from "./IdeaPicker";

/** 上传阶段一律用自然语言：不给普通用户看摘要、签名这类内部动作名。 */
const STAGE_TEXT: Record<UploadStage | "idle", string> = {
  idle: "还没选图片",
  hashing: "正在读取这张图片…",
  signing: "正在准备上传…",
  uploading: "正在上传图片…",
  completing: "正在检查图片能不能用…",
  confirming: "正在确认图片可用…",
  ready: "图片已就绪，可以开始生成",
};

const PLACEHOLDER: Record<OpenMode, string> = {
  PHOTO_PROMPT:
    "例如：改成蜡笔涂鸦的感觉，颜色更跳一点，背景留白，想改哪里就写哪里。人物、宠物、玩偶、风景都可以。",
  TEXT_PROMPT:
    "例如：一只橘猫趴在窗台上，手绘质感，暖色调，画面安静一点。想到什么写什么。",
};

/**
 * 创作台主体：一次生成需要的输入都在这一块玻璃面板里，用发丝线分区。
 *
 * 照片模式才有上传区；文字模式不出现任何照片步骤。提交前的门禁由 `Studio` 统一守。
 */
export function Composer({
  mode,
  upload,
  busy,
  ready,
  prompt,
  canSubmit,
  working,
  locked,
  onGate,
  onPick,
  onReset,
  onPromptChange,
  onSubmit,
}: {
  mode: OpenMode;
  upload: UploadState;
  busy: boolean;
  ready: boolean;
  prompt: string;
  canSubmit: boolean;
  working: boolean;
  /** 访客：不上传、不提交，先走认证引导。 */
  locked: boolean;
  onGate: () => void;
  onPick: (file: File) => void | Promise<void>;
  onReset: () => void;
  onPromptChange: (value: string) => void;
  onSubmit: () => void;
}) {
  const photoMode = mode === "PHOTO_PROMPT";
  const uploading = busy && upload.stage !== "ready";
  const describeIndex = photoMode ? "02" : "01";

  /**
   * 灵感标签的状态。`base` 是用户自己写的那部分文字；标签片段单独记在选择里，
   * 合成时永远是 `base + 片段`，所以点选**不会删掉用户已经写的内容**。
   */
  const [base, setBase] = useState(prompt);
  const [selection, setSelection] = useState<AssistSelection>({});
  const [presetId, setPresetId] = useState<string | null>(null);
  const [history, setHistory] = useState<
    { text: string; base: string; selection: AssistSelection; presetId: string | null }[]
  >([]);
  const originalText = useRef(prompt);
  /** 我们自己写回去的文字：用来区分"用户改的字"和"我们合成的字"。 */
  const lastComposed = useRef(prompt);

  // 描述被外部改动（用户打字、从首页带进来）时，把当前文字认作新的用户内容，
  // 并清空标签选择——因为这时文字已经不等于"base + 片段"，继续按旧选择合成会覆盖用户的手改。
  useEffect(() => {
    if (prompt === lastComposed.current) return;
    lastComposed.current = prompt;
    setBase(prompt);
    setSelection({});
    setPresetId(null);
  }, [prompt]);

  // 换入口后可选维度也换了（改造图片没有「主题」），旧选择不再有意义；文字本身保留不动。
  useEffect(() => {
    setSelection({});
    setPresetId(null);
  }, [mode]);

  const preset = USE_PRESETS.find((item) => item.id === presetId) ?? null;
  const preview = useMemo(
    () => assistPreview(mode, base, selection, preset?.extra),
    [mode, base, selection, preset],
  );
  const canUndo = history.length > 0;
  const canRestore = prompt !== originalText.current;

  /**
   * 写回描述，并把这次变化记进撤销栈。
   *
   * `nextBase` 传 undefined 表示"用户自己写的那部分没变"（点标签时就是这样，
   * 只换片段，不能把合成后的整段当成新的用户内容，否则下一个片段会拼到结尾后面）。
   * 传字符串表示这次写入的就是新的用户内容（清空、恢复原文）。
   */
  function commit(
    text: string,
    nextSelection: AssistSelection,
    options: { nextBase?: string; nextPresetId?: string | null } = {},
  ) {
    setHistory((entries) => [...entries.slice(-19), { text: prompt, base, selection, presetId }]);
    lastComposed.current = text;
    if (options.nextBase !== undefined) setBase(options.nextBase);
    setSelection(nextSelection);
    setPresetId(options.nextPresetId ?? null);
    onPromptChange(text);
  }

  function toggleIdea(dimension: IdeaDimensionId, optionId: string) {
    const next: AssistSelection = { ...selection };
    if (next[dimension] === optionId) delete next[dimension];
    else next[dimension] = optionId;
    // 手改过某一维之后，预设的高亮就不再代表当前状态
    commit(composePrompt(mode, base, next, preset?.extra), next, { nextPresetId: presetId });
  }

  /** 用途预设：一键套用一组维度（可再逐项改），并把附加句带上。 */
  function applyUsePreset(id: string) {
    const item = USE_PRESETS.find((entry) => entry.id === id);
    if (!item) return;
    const { selection: nextSelection, extra } = applyPreset(item);
    commit(composePrompt(mode, base, nextSelection, extra), nextSelection, { nextPresetId: item.id });
  }

  function undo() {
    const previous = history[history.length - 1];
    if (!previous) return;
    setHistory((entries) => entries.slice(0, -1));
    lastComposed.current = previous.text;
    setBase(previous.base);
    setSelection(previous.selection);
    setPresetId(previous.presetId);
    onPromptChange(previous.text);
  }

  function restoreOriginal() {
    const text = originalText.current;
    commit(text, {}, { nextBase: text });
  }

  /** 清空也走同一条路径，这样"撤销上一步"对它同样有效。 */
  function clearPrompt() {
    commit("", {}, { nextBase: "" });
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    if (canSubmit) onSubmit();
  }

  return (
    <form className="panel composer" onSubmit={submit} aria-label="生成设置">
      {photoMode ? (
        <div className="composer__row">
          <div className="composer__mark">01</div>
          <div className="composer__body">
            <div className="composer__label">
              <b>上传一张图片</b>
              <span>检查通过后才能提交</span>
            </div>

            {upload.fileName ? (
              <div className="picked">
                <span className="picked__thumb">
                  {upload.previewUrl ? <img src={upload.previewUrl} alt="你选择的图片预览" /> : null}
                </span>
                <div>
                  <p className="picked__name">{upload.fileName}</p>
                  <p className="picked__state">
                    {upload.failure ? (
                      <>
                        <span className="tag tag--danger">没通过</span>
                        {failureTitle(upload.failure.code)}
                      </>
                    ) : (
                      <>
                        {ready ? <span className="tag tag--ok">已就绪</span> : <span className="tag">处理中</span>}
                        {STAGE_TEXT[upload.stage]}
                      </>
                    )}
                  </p>
                  {uploading && !upload.failure ? (
                    <div className="picked__bar" aria-hidden="true">
                      <i />
                    </div>
                  ) : null}
                  <div className="btnrow picked__acts">
                    <button type="button" className="btn btn--quiet" onClick={onReset} disabled={busy}>
                      换一张图片
                    </button>
                  </div>
                </div>
              </div>
            ) : locked ? (
              <button type="button" className="drop" onClick={onGate}>
                <FrameMark />
                <span className="drop__t">开通后就能上传图片</span>
                <span className="drop__d">需要先兑换邀请码并验证邮箱；点这里开始</span>
              </button>
            ) : (
              <label className="drop">
                <input
                  type="file"
                  accept="image/jpeg,image/png,image/webp"
                  disabled={busy}
                  onChange={(event) => {
                    const file = event.target.files?.[0];
                    if (file) void onPick(file);
                    event.target.value = "";
                  }}
                />
                <FrameMark />
                <span className="drop__t">选择一张图片</span>
                <span className="drop__d">主体清楚、光线均匀、不要被挡住即可；jpg / png / webp</span>
              </label>
            )}

            {upload.failure ? (
              <div className="btnrow--top">
                <Notice tone="danger" title="这张图片没能用起来" failure={upload.failure}>
                  {upload.failure.message}
                </Notice>
              </div>
            ) : null}
          </div>
        </div>
      ) : null}

      <div className="composer__row">
        <div className="composer__mark">{describeIndex}</div>
        <div className="composer__body">
          <div className="composer__label">
            <b>{photoMode ? "想要改成什么样" : "描述你想要的头像"}</b>
            <span>写得越具体，结果越接近你想要的</span>
          </div>

          <IdeaPicker
            mode={mode}
            selection={selection}
            preview={preview}
            presetId={presetId}
            canUndo={canUndo}
            canRestore={canRestore}
            onToggle={toggleIdea}
            onPreset={applyUsePreset}
            onUndo={undo}
            onRestore={restoreOriginal}
          />

          <label className="field">
            <textarea
              className="field__area"
              name="prompt"
              rows={4}
              value={prompt}
              onChange={(event) => onPromptChange(event.target.value)}
              placeholder={PLACEHOLDER[mode]}
            />
          </label>

          <p className="field__meta">
            <span>
              {photoMode
                ? "这张图片会作为改造底图发出去；写描述决定改成什么样。"
                : "不发送任何图片，完全根据这句话生成。"}
            </span>
            <span className="field__meta-acts">
              <span>{prompt.trim().length} 字</span>
              <button
                type="button"
                className="chip chip--sm field__clear"
                onClick={clearPrompt}
                disabled={prompt.length === 0}
              >
                清空
              </button>
            </span>
          </p>
        </div>
      </div>

      <div className="composer__row composer__row--foot">
        <div className="composer__foot">
          <div className="composer__terms">
            {/* 提交前把"实际会提交什么"摆出来，避免用户以为发的和看到的不一样 */}
            <p className="composer__confirm">
              <b>这次会提交</b>
              <span>{photoMode ? "改造我的图片" : "文字创作"}</span>
              <span aria-hidden="true">·</span>
              <span>
                {photoMode
                  ? upload.fileName
                    ? `1 张图片（${upload.fileName}）`
                    : "还没有图片"
                  : "不使用图片"}
              </span>
              <span aria-hidden="true">·</span>
              <span>{prompt.trim().length > 0 ? `描述 ${prompt.trim().length} 字` : "还没有描述"}</span>
            </p>
            {locked ? (
              <p className="hint hint--flush">开通并验证邮箱后就能上传图片、提交生成。</p>
            ) : photoMode && !ready ? (
              <p className="hint hint--flush">先上传一张图片，检查通过后就能开始生成。</p>
            ) : (
              <p className="hint hint--flush">
                一次固定产出 {OUTPUT_COUNT} 张，通常 30–90 秒；提交时按服务端结算先占用点数，
                失败会全额退回，需要人工确认时保持占用。
              </p>
            )}
            <p className="composer__terms-note">
              {photoMode
                ? "上传的图片只用于这一次生成，7 天后由服务端自动清理；结果保留 30 天，请及时下载。"
                : "不需要上传图片；结果保留 30 天，请及时下载。"}
            </p>
          </div>
          <button type="submit" className="btn btn--primary btn--lg" disabled={!canSubmit}>
            {working ? "正在生成…" : locked ? "开通后开始生成" : "开始生成"}
            <span className="btn__k">{OUTPUT_COUNT} 张</span>
          </button>
        </div>
      </div>
    </form>
  );
}
