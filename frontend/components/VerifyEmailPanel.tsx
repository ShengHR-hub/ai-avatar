"use client";

import Link from "next/link";
import { useState } from "react";

import { verifyEmailToken } from "@/services/api";
import { ApiFailure, failureTitle } from "@/services/errors";
import { ApertureSpinner } from "./BrandMark";
import { Notice } from "./Notice";

/** 验证链接是一次性令牌：提交后由服务端判定，前端不缓存"已验证"。 */
export function VerifyEmailPanel({
  token,
  onVerified,
}: {
  token: string;
  onVerified: () => void | Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);
  const [failure, setFailure] = useState<ApiFailure | null>(null);

  async function verify() {
    setBusy(true);
    setFailure(null);
    try {
      await verifyEmailToken(token);
      await onVerified();
      setDone(true);
    } catch (error) {
      setFailure(error instanceof ApiFailure ? error : null);
    } finally {
      setBusy(false);
    }
  }

  if (!token) {
    return (
      <div className="panel">
        <div className="empty">
          <p className="empty__t">这个页面需要从邮件里的链接进入</p>
          <p className="empty__d">
            我们发出的验证邮件里带一次性链接，点开后会自动回到这里。如果你已经打开过，可以回到创作台重新发送一封。
          </p>
          <Link className="btn btn--primary" href="/studio">
            回到创作台
          </Link>
        </div>
      </div>
    );
  }

  return (
    <div className="panel sheet--narrow">
      <div className="empty">
        {done ? null : <ApertureSpinner />}
        <p className="empty__t">{done ? "邮箱已验证" : "确认你的邮箱"}</p>
        <p className="empty__d">
          {done
            ? "现在可以回到创作台上传照片或用文字生成头像了。"
            : "确认后即可解锁上传与生成流程。这个链接只能用一次。"}
        </p>

        {done ? (
          <Link className="btn btn--primary" href="/studio">
            开始创作
          </Link>
        ) : (
          <button type="button" className="btn btn--primary btn--lg" disabled={busy} onClick={verify}>
            {busy ? "正在验证…" : "验证邮箱"}
          </button>
        )}

        {failure ? (
          <div className="empty__full">
            <Notice tone="danger" title={failureTitle(failure.code)} failure={failure}>
              {failure.message}
            </Notice>
          </div>
        ) : null}
      </div>
    </div>
  );
}
