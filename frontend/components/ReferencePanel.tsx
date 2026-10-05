"use client";

import { failureTitle } from "@/services/errors";
import type { ReferenceState, ReferenceStage } from "@/hooks/useStudio";
import { FrameMark } from "./BrandMark";
import { Notice } from "./Notice";

type Props = {
  reference: ReferenceState;
  busy: boolean;
  locked: boolean;
  onGate: () => void;
  onPick: (file: File) => void | Promise<void>;
  onAnalyze: () => void | Promise<void>;
  onApply: (mode: "replace" | "append") => void;
  onReset: () => void;
};

const STAGE_TEXT: Record<ReferenceStage, string> = {
  idle: "",
  uploading: "正在把这张图传到你自己的空间",
  uploaded: "图已经收好了，下一步让它看图说话",
  analyzing: "正在看这张图，一般十几秒到四十秒",
  described: "读好了，可以改字了",
  failed: "这一步没走通",
};

/**
 * 「参考图片找灵感」面板。
 *
 * 口径必须说准（`docs/product/00-product-direction.md`）：
 * 这段文字是**模型看你这张图现写的近似画面描述**，不是这张图原本的提示词，
 * 也不保证照它能画出一模一样的图。用户改完这段字，走的仍然是一次普通的文字创作——
 * 参考图本身不参与生成，也不是 `REFERENCE_IMAGE` 那条路径。
 *
 * 上传与读图分成两下：读图会在服务端触发一次真实的外部调用，
 * 所以它由用户明确按下去，而不是选完文件就自动发生。
 */
export function ReferencePanel({
  reference,
  busy,
  locked,
  onGate,
  onPick,
  onAnalyze,
  onApply,
  onReset,
}: Props) {
  const uploading = reference.stage === "uploading";
  const analyzing = reference.stage === "analyzing";
  const hasDescription = reference.description.length > 0;
  // 描述框空 → 读出来直接填进去（不覆盖任何东西）；非空 → 等用户点「填进去 / 追加」。
  const autoFilled = reference.autoFilled;

  function pickFile(file: File | undefined) {
    if (file) void onPick(file);
  }

  return (
    <section className="panel inspire" aria-label="用参考图找灵感">
      <div className="inspire__row">
        <div className="inspire__mark">01</div>
        <div className="inspire__body">
          <div className="inspire__label">
            <b>放一张你喜欢的图</b>
            <span>用它来找回感觉，不是用它来改图</span>
          </div>

          {reference.fileName ? (
            <div className="picked">
              <span className="picked__thumb">
                {reference.previewUrl ? (
                  <img src={reference.previewUrl} alt="你选择的参考图预览" />
                ) : null}
              </span>
              <div>
                <p className="picked__name">{reference.fileName}</p>
                <p className="picked__state">
                  {reference.failure ? (
                    <>
                      <span className="tag tag--danger">没走通</span>
                      {failureTitle(reference.failure.code)}
                    </>
                  ) : (
                    <>
                      {reference.assetId ? <span className="tag tag--ok">已收到</span> : null}
                      {STAGE_TEXT[reference.stage]}
                    </>
                  )}
                </p>
                {uploading || analyzing ? (
                  <div className="picked__bar" aria-hidden="true">
                    <i />
                  </div>
                ) : null}
                <div className="btnrow picked__acts">
                  <button
                    type="button"
                    className="btn btn--quiet"
                    onClick={onReset}
                    disabled={busy || uploading || analyzing}
                  >
                    换一张参考图
                  </button>
                </div>
              </div>
            </div>
          ) : locked ? (
            <button type="button" className="drop" onClick={onGate}>
              <FrameMark />
              <span className="drop__t">开通后就能放参考图</span>
              <span className="drop__d">需要先兑换邀请码并验证邮箱；点这里开始</span>
            </button>
          ) : (
            <label className="drop">
              <input
                type="file"
                accept="image/jpeg,image/png,image/webp"
                disabled={uploading || analyzing}
                onChange={(event) => {
                  pickFile(event.target.files?.[0]);
                  event.target.value = "";
                }}
              />
              <FrameMark />
              <span className="drop__t">选一张参考图</span>
              <span className="drop__d">看到过的头像、插画、照片都行；jpg / png / webp</span>
            </label>
          )}

          {reference.failure ? (
            <Notice
              tone="danger"
              title="这张参考图暂时用不了"
              failure={reference.failure}
            >
              {reference.failure.message}
            </Notice>
          ) : null}
        </div>
      </div>

      <div className="inspire__row">
        <div className="inspire__mark">02</div>
        <div className="inspire__body">
          <div className="inspire__label">
            <b>让它看图说话</b>
            <span>出来的是一段可以改的画面描述，不是这张图原本的写法</span>
          </div>

          {reference.assetId && !hasDescription ? (
            <button
              type="button"
              className="btn btn--primary"
              onClick={() => void onAnalyze()}
              disabled={analyzing || busy}
            >
              {analyzing ? "正在看这张图" : "读出画面描述"}
            </button>
          ) : null}

          {analyzing ? (
            <p className="hint" role="status" aria-live="polite">
              {STAGE_TEXT.analyzing}。这一步只是看图说话，不会创建任务、也不会占点数。
            </p>
          ) : null}

          {hasDescription ? (
            <div className="inspire__out">
              <p className="inspire__text">{reference.description}</p>
              <p className="inspire__meta">
                {reference.fromCache
                  ? "这是刚才那次读出来的结果，这次没有再看一遍。"
                  : "这是它看你这张图现写的一段话。"}
              </p>

              <div className="btnrow inspire__acts">
                {autoFilled ? (
                  <span className="tag tag--ok">已填进下面的描述框</span>
                ) : (
                  <>
                    <button type="button" className="btn btn--primary" onClick={() => onApply("replace")}>
                      填进描述框
                    </button>
                    <button type="button" className="btn" onClick={() => onApply("append")}>
                      追加到我写的后面
                    </button>
                  </>
                )}
              </div>

              {reference.failure ? (
                <Notice tone="danger" title="这次没读出来" failure={reference.failure}>
                  {reference.failure.message}
                </Notice>
              ) : null}
            </div>
          ) : null}

          <p className="hint">
            改完这段字，下面按「文字创作」出两张属于你的图。想要更接近这张参考图本身的效果，
            那得走另一条路（把参考图当生成条件），本产品当前没有开放那条能力。
          </p>
        </div>
      </div>
    </section>
  );
}
