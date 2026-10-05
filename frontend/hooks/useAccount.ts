"use client";

import { useCallback, useEffect, useState } from "react";

import { fetchGenerationPage, fetchPointBalance } from "@/services/api";
import { ApiFailure } from "@/services/errors";
import type { Generation, PointBalance } from "@/generated/types.gen";

export type AccountState = {
  balance: PointBalance | null;
  items: Generation[];
  nextCursor: string | null;
  loading: boolean;
  loadingMore: boolean;
  failure: ApiFailure | null;
};

/**
 * 余额与历史都以服务端为唯一事实：本页不做乐观扣点，也不把「已提交」当「已消费」。
 * 分页沿用契约的 cursor 语义，`has_more` 为假时不再发请求。
 */
export function useAccount(): AccountState & {
  loadMore: () => Promise<void>;
  reload: () => Promise<void>;
} {
  const [state, setState] = useState<AccountState>({
    balance: null,
    items: [],
    nextCursor: null,
    loading: true,
    loadingMore: false,
    failure: null,
  });

  const reload = useCallback(async () => {
    setState((previous) => ({ ...previous, loading: true, failure: null }));
    try {
      const [balance, page] = await Promise.all([fetchPointBalance(), fetchGenerationPage()]);
      setState({
        balance,
        items: page.items,
        nextCursor: page.hasMore ? page.nextCursor : null,
        loading: false,
        loadingMore: false,
        failure: null,
      });
    } catch (error) {
      setState((previous) => ({
        ...previous,
        loading: false,
        failure: error instanceof ApiFailure ? error : null,
      }));
    }
  }, []);

  const loadMore = useCallback(async () => {
    const cursor = state.nextCursor;
    if (!cursor) return;
    setState((previous) => ({ ...previous, loadingMore: true }));
    try {
      const page = await fetchGenerationPage(cursor);
      setState((previous) => ({
        ...previous,
        items: [...previous.items, ...page.items],
        nextCursor: page.hasMore ? page.nextCursor : null,
        loadingMore: false,
      }));
    } catch (error) {
      setState((previous) => ({
        ...previous,
        loadingMore: false,
        failure: error instanceof ApiFailure ? error : null,
      }));
    }
  }, [state.nextCursor]);

  useEffect(() => {
    void reload();
  }, [reload]);

  return { ...state, loadMore, reload };
}
