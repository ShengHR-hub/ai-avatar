"use client";

import { useCallback, useEffect, useRef, useState } from "react";

export type ViewerItem = {
  src: string;
  alt: string;
  /** 图下方的一行说明，例如「输入照片」「结果 1」。 */
  caption?: string;
  /** 可选：直接下载这一张。 */
  downloadHref?: string;
  downloadName?: string;
};

/**
 * 大图查看器。作品区和结果区共用同一份实现，避免两处各写一套。
 *
 * 键盘：`Esc` 关闭、`←`/`→` 换图、`Tab` 只在查看器内部循环；关闭后焦点回到打开它的元素。
 * 打开期间锁住页面滚动（并补偿滚动条宽度，避免整页横向跳动）。
 */
export function ImageViewer({
  items,
  startIndex = 0,
  onClose,
}: {
  items: ViewerItem[];
  startIndex?: number;
  onClose: () => void;
}) {
  const [index, setIndex] = useState(() => Math.min(Math.max(startIndex, 0), Math.max(items.length - 1, 0)));
  const panel = useRef<HTMLDivElement | null>(null);
  const closeBtn = useRef<HTMLButtonElement | null>(null);
  const restoreTo = useRef<HTMLElement | null>(null);

  const total = items.length;
  const item = items[index];

  const go = useCallback(
    (step: number) => {
      if (total < 2) return;
      setIndex((current) => (current + step + total) % total);
    },
    [total],
  );

  useEffect(() => {
    restoreTo.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    closeBtn.current?.focus();

    const { documentElement, body } = document;
    const gap = window.innerWidth - documentElement.clientWidth;
    const previousOverflow = documentElement.style.overflow;
    const previousPadding = body.style.paddingRight;
    documentElement.style.overflow = "hidden";
    if (gap > 0) body.style.paddingRight = `${gap}px`;

    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
        return;
      }
      if (event.key === "ArrowLeft") {
        event.preventDefault();
        go(-1);
        return;
      }
      if (event.key === "ArrowRight") {
        event.preventDefault();
        go(1);
        return;
      }
      if (event.key === "Tab") {
        // 只在查看器内部循环，避免焦点跑到被遮住的页面上
        const focusable = panel.current?.querySelectorAll<HTMLElement>("button, a[href]");
        if (!focusable || focusable.length === 0) return;
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      }
    };

    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      documentElement.style.overflow = previousOverflow;
      body.style.paddingRight = previousPadding;
      restoreTo.current?.focus?.();
    };
  }, [go, onClose]);

  if (!item) return null;

  return (
    <div
      className="viewer"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div className="viewer__panel" ref={panel} role="dialog" aria-modal="true" aria-label="查看大图">
        <div className="viewer__bar">
          <p className="viewer__title">
            {item.caption ?? "查看大图"}
            {total > 1 ? <span className="viewer__count">{index + 1} / {total}</span> : null}
          </p>
          <button ref={closeBtn} type="button" className="viewer__close" onClick={onClose} aria-label="关闭大图">
            ×
          </button>
        </div>

        <div className="viewer__stage">
          <img src={item.src} alt={item.alt} decoding="async" />
        </div>

        <div className="viewer__foot">
          <div className="viewer__nav">
            {total > 1 ? (
              <>
                <button type="button" className="btn" onClick={() => go(-1)}>
                  ← 上一张
                </button>
                <button type="button" className="btn" onClick={() => go(1)}>
                  下一张 →
                </button>
              </>
            ) : null}
          </div>
          {item.downloadHref ? (
            <a className="btn btn--primary" href={item.downloadHref} download={item.downloadName} target="_blank" rel="noopener noreferrer">
              下载这一张
            </a>
          ) : null}
        </div>

        <p className="viewer__hint">按 Esc 关闭{total > 1 ? "，← → 切换" : ""}。图片按原始比例显示，没有拉伸。</p>
      </div>
    </div>
  );
}
