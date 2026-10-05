import type { ReactNode } from "react";

import type { ApiFailure } from "@/services/errors";

export type NoticeTone = "info" | "success" | "warning" | "danger";

const MARK: Record<NoticeTone, string> = {
  info: "i",
  success: "✓",
  warning: "!",
  danger: "!",
};

/**
 * 状态说明条：等待、失败、复核、成功都用同一套语义。
 *
 * 稳定错误码会被保留（用户报障时需要），但降级成次要信息——界面上先说的是人话，
 * 而不是把 `READY`、`Provider` 这类内部术语当成给用户看的文案。
 */
export function Notice({
  tone = "info",
  title,
  children,
  failure,
}: {
  tone?: NoticeTone;
  title: string;
  children?: ReactNode;
  failure?: ApiFailure | null;
}) {
  return (
    <div className={`notice notice--${tone}`} role={tone === "danger" ? "alert" : "status"}>
      <span className="notice__icon" aria-hidden="true">
        {MARK[tone]}
      </span>
      <div>
        <p className="notice__title">{title}</p>
        {children ? <div className="notice__body">{children}</div> : null}
        {failure ? (
          <p className="notice__meta">
            问题编号 <code>{failure.code}</code>
            {failure.requestId ? (
              <>
                {" · 请求 "}
                <code>{failure.requestId}</code>
              </>
            ) : null}
          </p>
        ) : null}
      </div>
    </div>
  );
}
