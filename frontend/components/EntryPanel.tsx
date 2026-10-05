"use client";

import { useState } from "react";

import { requestEmailVerificationFor, redeemInviteWith } from "@/services/api";
import { ApiFailure, failureTitle } from "@/services/errors";
import { Notice } from "./Notice";

/**
 * 开通与邮箱验证的字段区。会话是否成立一律由父级重新读 `GET /auth/session` 决定，
 * 这里不自行记住「已登录」，也不推测邮箱是否已被占用（服务端统一 202 防枚举）。
 *
 * 标题与容器由外层弹层提供，因此这里只是一段表单。
 */
export function EntryPanel({
  needsVerification,
  onSessionChanged,
}: {
  needsVerification: boolean;
  onSessionChanged: () => void | Promise<void>;
}) {
  const [code, setCode] = useState("");
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<ApiFailure | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  async function run(action: () => Promise<void>, successText: string) {
    setBusy(true);
    setFailure(null);
    setNotice(null);
    try {
      await action();
      await onSessionChanged();
      setNotice(successText);
    } catch (error) {
      setFailure(error instanceof ApiFailure ? error : null);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <label className="formrow">
        <span>邮箱</span>
        <input
          className="input"
          value={email}
          onChange={(event) => setEmail(event.target.value)}
          type="email"
          autoComplete="email"
          placeholder="you@example.com"
        />
      </label>

      {needsVerification ? null : (
        <label className="formrow">
          <span>邀请码</span>
          <input
            className="input"
            value={code}
            onChange={(event) => setCode(event.target.value)}
            autoComplete="off"
            placeholder="兑换后自动创建账号"
          />
        </label>
      )}

      <div className="btnrow">
        {needsVerification ? (
          <button
            type="button"
            className="btn btn--primary btn--block"
            disabled={busy || !email}
            onClick={() => run(() => requestEmailVerificationFor(email), "验证邮件已发出，请到邮箱里点开链接。")}
          >
            {busy ? "正在发送…" : "重新发送验证邮件"}
          </button>
        ) : (
          <button
            type="button"
            className="btn btn--primary btn--block"
            disabled={busy || !code || !email}
            onClick={() => run(() => redeemInviteWith(code, email), "开通成功，接下来完成邮箱验证。")}
          >
            {busy ? "正在兑换…" : "兑换邀请码"}
          </button>
        )}
      </div>

      {notice ? (
        <div className="stack-sm">
          <Notice tone="success" title="已提交">
            {notice}
          </Notice>
        </div>
      ) : null}
      {failure ? (
        <div className="stack-sm">
          <Notice tone="danger" title={failureTitle(failure.code)} failure={failure}>
            {failure.message}
          </Notice>
        </div>
      ) : null}
    </div>
  );
}
