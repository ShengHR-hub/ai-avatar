"use client";

import { useEffect, useRef } from "react";

import type { SessionPhase } from "@/hooks/useSession";
import type { ApiFailure } from "@/services/errors";
import { EntryPanel } from "./EntryPanel";
import { Notice } from "./Notice";

/**
 * 访客门禁：浏览不受影响，只有真正要上传或提交时才弹出这一步。
 *
 * 它不绕过认证、也不替服务端判断资格——四种会话状态各自给出准确的下一步，
 * 提交后的判定仍然只由 `GET /auth/session` 与后端规则决定。
 */
export function AuthGate({
  phase,
  failure,
  onSessionChanged,
  onRetry,
  onClose,
}: {
  phase: SessionPhase;
  failure: ApiFailure | null;
  onSessionChanged: () => void | Promise<void>;
  onRetry: () => void | Promise<void>;
  onClose: () => void;
}) {
  const panel = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    panel.current?.querySelector<HTMLElement>("input")?.focus();
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const needsVerification = phase === "needs-verification";
  const title =
    phase === "error"
      ? "暂时连不上服务端"
      : needsVerification
        ? "验证邮箱后就能生成"
        : phase === "loading"
          ? "正在确认登录状态"
          : "开通后就能开始生成";

  return (
    <div
      className="dialog"      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        className="dialog__panel"
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-labelledby="gate-title"
      >
        <button type="button" className="dialog__close" onClick={onClose} aria-label="关闭">
          ×
        </button>

        <p className="eyebrow">开始创作前</p>
        <h2 className="dialog__title" id="gate-title">
          {title}
        </h2>
        <p className="dialog__sub">
          {phase === "error"
            ? "这不影响你先看作品和风格；等连接恢复后再回来生成。"
            : "作品和灵感分类可以随便看。上传图片与提交生成需要先开通，并完成一次邮箱验证——这一步是服务端要求的，页面不替你跳过。"}
        </p>

        {phase === "error" ? (
          <>
            <Notice tone="danger" title="服务端暂时不可达" failure={failure ?? undefined}>
              {failure?.message ?? "请稍后再试。"}
            </Notice>
            <div className="btnrow btnrow--top">
              <button type="button" className="btn" onClick={() => void onRetry()}>
                重新连接
              </button>
            </div>
          </>
        ) : phase === "loading" ? (
          <Notice tone="info" title="请稍等">
            正在读取会话状态，马上就好。
          </Notice>
        ) : (
          <EntryPanel needsVerification={needsVerification} onSessionChanged={onSessionChanged} />
        )}
      </div>
    </div>
  );
}
