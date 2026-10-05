/**
 * ENG-09 返修第 6 项：匿名分享页的必要状态与重试入口。
 *
 * 缺陷：首次加载失败时只有「稍后再试」文案，没有任何可点的入口 ——
 * 用户唯一的动作是刷新浏览器。这里锁定修复后的行为：
 *
 * 1. 暂时性失败（5xx）给出**真实可用**的重试按钮，点击后重新加载并就地恢复；
 * 2. 提交中禁用按钮，重复点击不会打出第二个请求；
 * 3. 链接失效（404）不提供重试（服务端事实是"这个链接不成立"），只给出口；
 * 4. 短期图片地址过期时能重新获取（既有行为不回退）；
 * 5. 重试后仍然失败时如实显示失败，不假装成功。
 */

import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { SharePage } from "@/components/SharePage";
import { envelope, installFetchStub, type Stub } from "./helpers/stub";

const SLUG = "synthetic-slug-aaaaaaaaaaaaaaaaaaaa";

const SHARE = {
  slug: SLUG,
  title: "合成作品",
  expires_at: "2026-11-01T12:00:00+00:00",
  created_at: "2026-10-09T12:00:00+00:00",
  outputs: [
    {
      asset_id: "r1",
      output_index: 0,
      mime_type: "image/png",
      width: 1024,
      height: 1024,
      size_bytes: 2048,
    },
  ],
};

const SIGNED = {
  url: "https://minio.invalid/signed/r1",
  method: "GET",
  expires_at: "2026-10-09T12:05:00+00:00",
};

function problem(status: number, code: string, message: string) {
  return { status, json: { error: { code, message, details: {} }, request_id: "req-stub" } };
}

let stub: Stub = installFetchStub();
afterEach(() => stub.restore());

describe("匿名分享页", () => {
  it("暂时性失败给出可点的重试入口，点击后就地恢复", async () => {
    let attempts = 0;
    stub = installFetchStub({
      [`GET /api/v1/shares/${SLUG}`]: () => {
        attempts += 1;
        if (attempts === 1) {
          return problem(503, "NOT_READY", "依赖暂时不可用");
        }
        return { status: 200, json: envelope(SHARE) };
      },
      "POST /api/v1/assets/r1/download-url": { status: 200, json: envelope(SIGNED) },
    });

    render(<SharePage slug={SLUG} />);

    const retry = await screen.findByRole("button", { name: "重试一次" });
    await act(async () => {
      fireEvent.click(retry);
    });

    await waitFor(() => expect(screen.getByText("合成作品")).toBeTruthy());
    expect(stub.count(`GET /api/v1/shares/${SLUG}`)).toBe(2);
  });

  it("重试中的重复点击不会打出第二个请求", async () => {
    let release: (() => void) | null = null;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    let attempts = 0;
    stub = installFetchStub({
      [`GET /api/v1/shares/${SLUG}`]: () => {
        attempts += 1;
        if (attempts === 1) {
          return problem(503, "NOT_READY", "依赖暂时不可用");
        }
        // 第二次挂起，制造"重试中"的窗口
        return gate.then(() => ({ status: 200, json: envelope(SHARE) })) as never;
      },
      "POST /api/v1/assets/r1/download-url": { status: 200, json: envelope(SIGNED) },
    });

    render(<SharePage slug={SLUG} />);
    const retry = await screen.findByRole("button", { name: "重试一次" });

    // 只数「这一次重试窗口内」打出了几个请求：挂起期间连点三下也只能有一个
    stub.calls.length = 0;
    await act(async () => {
      fireEvent.click(retry);
    });
    const midFlight = screen.queryByRole("button", { name: /重试|重新加载/ });
    if (midFlight) {
      // 仍能点到按钮时它是禁用的（防重复触发的可观察证据）
      expect((midFlight as HTMLButtonElement).disabled).toBe(true);
      fireEvent.click(midFlight);
      fireEvent.click(midFlight);
    }
    expect(stub.count(`GET /api/v1/shares/${SLUG}`)).toBe(1);
    expect(attempts).toBe(2);

    await act(async () => {
      release?.();
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    await waitFor(() => expect(screen.getByText("合成作品")).toBeTruthy());
  });

  it("链接失效不提供重试（服务端事实是它不成立），只给出口", async () => {
    stub = installFetchStub({
      [`GET /api/v1/shares/${SLUG}`]: problem(
        404,
        "GENERATION_NOT_FOUND",
        "分享不存在",
      ),
    });

    render(<SharePage slug={SLUG} />);

    await waitFor(() => expect(screen.getByText("这个分享链接已经失效")).toBeTruthy());
    expect(screen.queryByRole("button", { name: "重试一次" })).toBeNull();
    expect(screen.getByRole("link", { name: "自己做一个" })).toBeTruthy();
    expect(stub.count(`GET /api/v1/shares/${SLUG}`)).toBe(1);
  });

  it("短期图片地址过期后可以重新获取", async () => {
    stub = installFetchStub({
      [`GET /api/v1/shares/${SLUG}`]: { status: 200, json: envelope(SHARE) },
      "POST /api/v1/assets/r1/download-url": {
        status: 200,
        json: envelope({ ...SIGNED, expires_at: "2020-01-01T00:00:00+00:00" }),
      },
    });

    render(<SharePage slug={SLUG} />);

    const refresh = await screen.findByRole("button", { name: "重新获取" });
    await act(async () => {
      fireEvent.click(refresh);
    });

    await waitFor(() =>
      expect(stub.count("POST /api/v1/assets/r1/download-url")).toBe(2),
    );
  });

  it("重试后仍然失败时如实显示失败，不假装成功", async () => {
    stub = installFetchStub({
      [`GET /api/v1/shares/${SLUG}`]: problem(503, "NOT_READY", "依赖暂时不可用"),
    });

    render(<SharePage slug={SLUG} />);
    const retry = await screen.findByRole("button", { name: "重试一次" });
    await act(async () => {
      fireEvent.click(retry);
    });

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "重试一次" })).toBeTruthy(),
    );
    expect(screen.queryByText("合成作品")).toBeNull();
    // 首次加载 1 次 + 点击重试后的重新加载 1 次（没有第三次空转）
    expect(stub.count(`GET /api/v1/shares/${SLUG}`)).toBe(2);
  });
});
