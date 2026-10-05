"use client";

import { useCallback, useEffect, useState } from "react";

import { fetchSession } from "@/services/api";
import { ApiFailure } from "@/services/errors";
import type { Session } from "@/generated/types.gen";

export type SessionPhase = "loading" | "signed-in" | "needs-verification" | "anonymous" | "error";

export type SessionState = {
  phase: SessionPhase;
  session: Session | null;
  failure: ApiFailure | null;
};

/**
 * 会话状态以 `GET /auth/session` 为唯一事实来源：前端不缓存「已登录」为本地标记，
 * 也不做乐观登录（`06-development-standard.md` 第 3 节：状态来源必须是服务端）。
 */
export function useSession(): SessionState & { refresh: () => Promise<void> } {
  const [state, setState] = useState<SessionState>({
    phase: "loading",
    session: null,
    failure: null,
  });

  const refresh = useCallback(async () => {
    try {
      const session = await fetchSession();
      setState({
        phase: session.email_verified ? "signed-in" : "needs-verification",
        session,
        failure: null,
      });
    } catch (error) {
      const failure = error instanceof ApiFailure ? error : null;
      if (failure?.isAuth) {
        setState({ phase: "anonymous", session: null, failure: null });
        return;
      }
      setState({ phase: "error", session: null, failure });
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return { ...state, refresh };
}
