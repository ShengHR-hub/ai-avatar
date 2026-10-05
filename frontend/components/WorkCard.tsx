"use client";

import Link from "next/link";
import { useId, useState } from "react";

import { studioLink, type WorkEntry } from "@/services/catalog";
import { clipPrompt, scanPublicPrompt } from "@/services/promptDisclosure";
import { ImageViewer, type ViewerItem } from "./ImageViewer";

/**
 * 一张作品卡：本站真实产出的一组结果图，附**可查看、可复制、可带走改**的提示词。
 *
 * 三条动作刻意做成两条路都能走，不依赖翻转卡片：
 * 1. 图片本身可点，打开大图查看器（←/→ 在图之间对照，键盘和手机都能用）；
 * 2. 图片下方有「展开详情」，展开后是描述、口径标注与来源授权。
 *
 * 图是主角：提示词默认收在面板里，不叠在画面上。
 *
 * 描述先过 `scanPublicPrompt`：命中私人信息或内部参数时宁可不展示，也不放出去。
 * 「带进去改」走的是深链接——把这段文字原样填进创作台的描述框，用户还能继续改字。
 */
export function WorkCard({ work, label }: { work: WorkEntry; label: string }) {
  const panelId = useId();
  const [open, setOpen] = useState(false);
  const [viewerAt, setViewerAt] = useState<number | null>(null);
  const [copyState, setCopyState] = useState<"idle" | "ok" | "fail">("idle");

  const shots: { image: WorkEntry["outputs"][number]; caption: string }[] = [
    ...work.outputs.map((image, index) => ({ image, caption: `结果 ${index + 1}` })),
  ];
  if (work.input) shots.unshift({ image: work.input, caption: "输入照片" });

  const items: ViewerItem[] = shots.map((shot) => ({
    src: shot.image.src,
    alt: shot.image.alt,
    caption: shot.caption,
  }));

  const scan = scanPublicPrompt(work.prompt.text);
  const clipped = clipPrompt(work.prompt.text);

  async function copyPrompt() {
    try {
      await navigator.clipboard.writeText(clipped.text);
      setCopyState("ok");
    } catch {
      setCopyState("fail");
    }
  }

  return (
    <article className="work">
      <div className={work.input ? "work__grid work__grid--three" : "work__grid"}>
        {shots.map((shot, position) => (
          <button
            type="button"
            className={`media work__shot${shot.caption === "输入照片" ? " work__shot--in" : ""}`}
            key={shot.image.src}
            onClick={() => setViewerAt(position)}
            aria-label={`放大查看${shot.caption}（${label}）`}
          >
            <img
              src={shot.image.src}
              srcSet={shot.image.srcSet}
              sizes="(min-width: 1020px) 300px, (min-width: 760px) 260px, 44vw"
              width={shot.image.width}
              height={shot.image.height}
              alt={shot.image.alt}
              loading="lazy"
              decoding="async"
            />
            {shot.caption === "输入照片" ? (
              <span className="tag tag--on-media">输入照片</span>
            ) : null}
          </button>
        ))}
      </div>

      <div className="work__bar">
        <p className="work__note">
          <b>{label}</b>
          {work.note ? <span>{work.note}</span> : null}
          <span className="work__mode">
            {work.mode === "PHOTO_PROMPT" ? "照片改造" : "文字生成"}
          </span>
        </p>
        <button
          type="button"
          className="btn"
          aria-expanded={open}
          aria-controls={panelId}
          onClick={() => setOpen((value) => !value)}
        >
          {open ? "收起详情" : "展开详情与描述"}
        </button>
      </div>

      <div className="work__detail" id={panelId} hidden={!open}>
        <div className="work__prompt">
          <div className="work__prompt-head">
            <b>当时写的描述</b>
            <span className={work.prompt.kind === "as-submitted" ? "tag tag--ok" : "tag tag--warn"}>
              {work.prompt.kind === "as-submitted" ? "当时提交的内容" : "公开改写版"}
            </span>
          </div>

          {scan.ok ? (
            <>
              <p className="work__prompt-text">{clipped.text}</p>
              {clipped.clipped ? <p className="work__prompt-foot">内容较长，这里只显示前半段。</p> : null}
              {work.prompt.kind === "display-only" ? (
                <p className="work__prompt-foot">
                  这段是为公开改写过的，和当时提交的内容不完全一样：{work.prompt.reason}
                </p>
              ) : (
                <p className="work__prompt-foot">这段就是当时提交给服务端的内容，只做过排版整理。</p>
              )}
              <div className="btnrow btnrow--top">
                <Link className="btn btn--primary" href={studioLink(work.mode, clipped.text)}>
                  带进创作台改
                </Link>
                <button type="button" className="btn" onClick={() => void copyPrompt()}>
                  复制这段
                </button>
                <span className="work__copy" role="status" aria-live="polite">
                  {copyState === "ok"
                    ? "已复制到剪贴板。"
                    : copyState === "fail"
                      ? "复制被浏览器拦下了，可以手动选中上面的文字。"
                      : ""}
                </span>
              </div>
            </>
          ) : (
            <p className="work__prompt-blocked">
              这段描述里检测到{scan.reasons.join("、")}，按公开口径暂不展示。需要展示时请先由人核对并改写，
              并标明它是否仍等于当时提交的内容。
            </p>
          )}
        </div>

        <div className="work__credit">
          <b>来源与授权</b>
          <p>
            由本站生成链路于 {work.origin.madeAt} 产出（{work.origin.model}）
            {work.origin.taskId ? ` · 任务 ${work.origin.taskId}` : ""}。
            {work.input ? ` 输入图与上面两张来自同一次生成：${work.input.sameRunEvidence}` : ""}
          </p>
          <p>{work.authorization}</p>
        </div>

        <div className="btnrow btnrow--top">
          <button type="button" className="btn" onClick={() => setViewerAt(0)}>
            放大看这两张
          </button>
        </div>
      </div>

      {viewerAt !== null ? <ImageViewer items={items} startIndex={viewerAt} onClose={() => setViewerAt(null)} /> : null}
    </article>
  );
}
