"use client";

import { useCallback, useEffect, useState } from "react";

import { fetchPointBalance } from "@/services/api";
import { ApiFailure } from "@/services/errors";
import type { PointBalance } from "@/generated/types.gen";

/**
 * 点数余额：只读服务端。未登录时不发请求（那注定 401），也不在前端做任何加减。
 * 每次生成到达终局后由页面触发一次重读，用来反映服务端真实的预留/消费结果。
 */
export function usePoints(enabled: boolean) {
  const [balance, setBalance] = useState<PointBalance | null>(null);
  const [failure, setFailure] = useState<ApiFailure | null>(null);

  const reload = useCallback(async () => {
    if (!enabled) {
      setBalance(null);
      return;
    }
    try {
      setBalance(await fetchPointBalance());
      setFailure(null);
    } catch (error) {
      setFailure(error instanceof ApiFailure ? error : null);
    }
  }, [enabled]);

  useEffect(() => {
    void reload();
  }, [reload]);

  return { balance, failure, reload };
}
