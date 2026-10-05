/**
 * ENG-09 分享前端测试。
 *
 * 复用真实实现：`services/api.ts` 的封装、`services/idempotency.ts` 的键归属、
 * `components/SharePanel` 的交互，只替身 `globalThis.fetch`。
 *
 * 覆盖：
 * 1. 创建分享发送**选中**的资产集合（一张或两张）与幂等 Header；
 * 2. 双击/重试复用同一个 `Idempotency-Key`（同一意图不会变成两条链接）；
 * 3. 成功、冲突（`IDEMPOTENCY_CONFLICT`）、不可分享、其他失败的界面状态；
 * 4. 分享链接渲染的是服务端回放的 slug，不拼任何内部 ID；
 * 5. 匿名分享页：加载、结果渲染、404（撤销/过期/不存在）与短期地址过期后的重取。
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { FeedbackPanel } from "@/components/FeedbackPanel";
import { SharePage } from "@/components/SharePage";
import { SharePanel } from "@/components/SharePanel";
import type { OutputView } from "@/services/api";
import type { FeedbackState, ShareState } from "@/hooks/useStudio";
import { errorBody, envelope, installFetchStub, type Stub } from "./helpers/stub";

const OUTPUTS: OutputView[] = [
  {
    assetId: "asset-1",
    outputIndex: 0,
    mimeType: "image/png",
    width: 1024,
    height: 1024,
    sizeBytes: 2048,
  },
  {
    assetId: "asset-2",
    outputIndex: 1,
    mimeType: "image/png",
    width: 1024,
    height: 1024,
    sizeBytes: 4096,
  },
];

const SHARE_PAYLOAD = {
  slug: "opaque-slug-abcdefghijklmnopqrstuvwxyz",
  title: null,
  expires_at: new Date(Date.now() + 7 * 86_400_000).toISOString(),
  created_at: new Date().toISOString(),
  outputs: [
    {
      asset_id: "asset-1",
      output_index: 0,
      mime_type: "image/png",
      width: 1024,
      height: 1024,
      size_bytes: 2048,
    },
  ],
};

const GENERATION = "gen-1";
const IDLE_SHARE: ShareState = {
  stage: "idle",
  generationId: GENERATION,
  view: null,
  failure: null,
};
const IDLE_FEEDBACK: FeedbackState = {
  stage: "idle",
  generationId: GENERATION,
  view: null,
  failure: null,
};

let stub: Stub;

beforeEach(() => {
  window.sessionStorage.clear();
  window.history.replaceState({}, "", "/studio");
  stub = installFetchStub();
});

afterEach(() => {
  stub.restore();
  vi.restoreAllMocks();
});

function renderSharePanel(
  share: ShareState = IDLE_SHARE,
  onShare = vi.fn(),
  onAnother = vi.fn(),
) {
  render(
    <SharePanel
      outputs={OUTPUTS}
      share={share}
      busy={false}
      locked={false}
      onGate={vi.fn()}
      onShare={onShare}
      onAnother={onAnother}
    />,
  );
  return onShare;
}

// --------------------------------------------------------------------------- #
// 创建分享
// --------------------------------------------------------------------------- #
describe("创建分享", () => {
  it("默认把两张都选中，并只发送被选中的资产集合", async () => {
    stub.routes({ "POST /api/v1/shares": { status: 201, json: envelope(SHARE_PAYLOAD) } });
    const onShare = renderSharePanel();

    fireEvent.click(screen.getByRole("button", { name: "生成分享链接" }));

    expect(onShare).toHaveBeenCalledTimes(1);
    expect(onShare).toHaveBeenCalledWith(["asset-1", "asset-2"]);
  });

  it("取消勾选后只分享剩下那张（未选中的不会被公开）", async () => {
    const onShare = renderSharePanel();

    fireEvent.click(screen.getByLabelText(/第 2 张/));

    expect(screen.getByText("将公开 1 张")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "生成分享链接" }));
    expect(onShare).toHaveBeenCalledWith(["asset-1"]);
  });

  it("提交中禁用按钮，避免同一帧内的第二次点击", async () => {
    renderSharePanel({ stage: "creating", generationId: GENERATION, view: null, failure: null });

    const button = screen.getByRole("button", { name: "正在生成链接…" });
    expect((button as HTMLButtonElement).disabled).toBe(true);
  });

  it("成功后展示服务端回放的链接，且链接里只有 slug", () => {
    renderSharePanel({
      stage: "created",
      generationId: GENERATION,
      view: {
        slug: SHARE_PAYLOAD.slug,
        title: null,
        expiresAt: SHARE_PAYLOAD.expires_at,
        createdAt: SHARE_PAYLOAD.created_at,
        outputs: [
          {
            assetId: "asset-1",
            outputIndex: 0,
            mimeType: "image/png",
            width: 1024,
            height: 1024,
            sizeBytes: 2048,
          },
        ],
      },
      failure: null,
    });

    const link = screen.getByRole("link", { name: new RegExp(SHARE_PAYLOAD.slug) });
    expect(new URL(String(link.getAttribute("href")), "http://localhost").pathname).toBe(`/s/${SHARE_PAYLOAD.slug}`);
    expect(screen.getByText(/包含 1 张结果/)).toBeTruthy();
  });

  it("换了选中组合导致的 409 冲突如实说明，不假装成功", () => {
    renderSharePanel({
      stage: "failed",
      generationId: GENERATION,
      view: null,
      failure: {
        code: "IDEMPOTENCY_CONFLICT",
        message: "同键不同内容",
        status: 409,
        details: {},
      } as never,
    });

    expect(screen.getByText("这次选中的图和上一次不同")).toBeTruthy();
    expect(screen.queryByText("链接已生成")).toBeNull();
  });

  it("任务不可分享时给出可执行说明", () => {
    renderSharePanel({
      stage: "failed",
      generationId: GENERATION,
      view: null,
      failure: {
        code: "GENERATION_NOT_SHAREABLE",
        message: "不可分享",
        status: 409,
        details: {},
      } as never,
    });

    expect(screen.getByText("这个作品还不能分享")).toBeTruthy();
  });

  it("没有结果时不渲染分享面板", () => {
    render(
      <SharePanel
        outputs={[]}
        share={IDLE_SHARE}
        busy={false}
        locked={false}
        onGate={vi.fn()}
        onShare={vi.fn()}
        onAnother={vi.fn()}
      />,
    );

    expect(screen.queryByRole("button", { name: "生成分享链接" })).toBeNull();
  });

  it("明确提供「再生成一条分享链接」入口，并绑定到独立回调", () => {
    const onAnother = vi.fn();
    renderSharePanel(
      {
        stage: "created",
        generationId: GENERATION,
        view: {
          slug: SHARE_PAYLOAD.slug,
          title: null,
          expiresAt: SHARE_PAYLOAD.expires_at,
          createdAt: SHARE_PAYLOAD.created_at,
          outputs: [
            {
              assetId: "asset-1",
              outputIndex: 0,
              mimeType: "image/png",
              width: 1024,
              height: 1024,
              sizeBytes: 2048,
            },
          ],
        },
        failure: null,
      },
      vi.fn(),
      onAnother,
    );

    fireEvent.click(screen.getByRole("button", { name: "再生成一条分享链接" }));
    expect(onAnother).toHaveBeenCalledTimes(1);
  });

  it("冲突提示不再叫人刷新页面，而是指向新意图入口", () => {
    renderSharePanel({
      stage: "failed",
      generationId: GENERATION,
      view: null,
      failure: {
        code: "IDEMPOTENCY_CONFLICT",
        message: "同键不同内容",
        status: 409,
        details: {},
      } as never,
    });

    expect(screen.queryByText(/刷新页面/)).toBeNull();
    // 冲突说明指向新意图入口（按钮本身由「明确提供…入口」用例覆盖）
    expect(
      screen.getByText(/请点「再生成一条分享链接」/),
    ).toBeTruthy();
  });
});

// --------------------------------------------------------------------------- #
// 服务层：幂等键来自调用方（唯一可信事实），请求形状
// --------------------------------------------------------------------------- #
describe("分享请求契约", () => {
  it("把调用方给出的幂等键**原样**放进请求头（服务层不再自己生成/缓存键）", async () => {
    stub.routes({ "POST /api/v1/shares": { status: 201, json: envelope(SHARE_PAYLOAD) } });
    const { createShareLink } = await import("@/services/api");

    await createShareLink({
      generationId: "gen-1",
      outputAssetIds: ["asset-1"],
      idempotencyKey: "key-one",
    });
    await createShareLink({
      generationId: "gen-1",
      outputAssetIds: ["asset-1"],
      idempotencyKey: "key-one",
    });
    await createShareLink({
      generationId: "gen-1",
      outputAssetIds: ["asset-1"],
      idempotencyKey: "key-two",
    });

    const calls = stub.calls.filter((call) => call.pathname === "/api/v1/shares");
    expect(calls.map((call) => call.headers["idempotency-key"])).toEqual([
      "key-one",
      "key-one",
      "key-two",
    ]);
    expect(calls[0].credentials).toBe("include");
  });

  it("请求体字段与冻结契约一致（不含内部字段）", async () => {
    stub.routes({ "POST /api/v1/shares": { status: 201, json: envelope(SHARE_PAYLOAD) } });
    const { createShareLink } = await import("@/services/api");

    await createShareLink({
      generationId: "gen-1",
      outputAssetIds: ["asset-1", "asset-2"],
      title: "我的两张",
      idempotencyKey: "key-body",
    });

    const call = stub.last("POST /api/v1/shares");
    const body = JSON.parse(String(call?.body));
    expect(body).toEqual({
      generation_id: "gen-1",
      output_asset_ids: ["asset-1", "asset-2"],
      title: "我的两张",
    });
  });

  it("响应缺字段时按响应畸形处理，不把半个对象交给界面", async () => {
    stub.routes({
      "POST /api/v1/shares": { status: 201, json: envelope({ slug: "only-slug" }) },
    });
    const { createShareLink } = await import("@/services/api");

    await expect(
      createShareLink({
        generationId: "gen-1",
        outputAssetIds: ["asset-1"],
        idempotencyKey: "key-malformed",
      }),
    ).rejects.toMatchObject({ code: "UNKNOWN_RESPONSE" });
  });
});

// --------------------------------------------------------------------------- #
// 匿名分享页
// --------------------------------------------------------------------------- #
describe("匿名分享页", () => {
  it("未登录也能打开并渲染结果（图片走短期签名地址）", async () => {
    stub.routes({
      "GET /api/v1/shares/opaque": { status: 200, json: envelope(SHARE_PAYLOAD) },
      "POST /api/v1/assets/asset-1/download-url": {
        status: 200,
        json: envelope({
          asset_id: "asset-1",
          method: "GET",
          url: "https://storage.example.invalid/signed?token=abc",
          expires_at: new Date(Date.now() + 300_000).toISOString(),
        }),
      },
    });

    render(<SharePage slug="opaque" />);

    await waitFor(() =>
      expect(screen.getByAltText("第 1 张生成结果")).toBeTruthy(),
    );
    expect(screen.queryByText(/所有者|Prompt|storage_key/)).toBeNull();
  });

  it("404（不存在/撤销/过期）只说链接失效，不区分原因", async () => {
    stub.routes({
      "GET /api/v1/shares/gone": {
        status: 404,
        json: errorBody("GENERATION_NOT_FOUND"),
      },
    });

    render(<SharePage slug="gone" />);

    await waitFor(() =>
      expect(screen.getByText("这个分享链接已经失效")).toBeTruthy(),
    );
  });

  it("短期地址过期后提示并允许重新获取", async () => {
    const expired = new Date(Date.now() - 60_000).toISOString();
    stub.routes({
      "GET /api/v1/shares/opaque": { status: 200, json: envelope(SHARE_PAYLOAD) },
      "POST /api/v1/assets/asset-1/download-url": {
        status: 200,
        json: envelope({
          asset_id: "asset-1",
          method: "GET",
          url: "https://storage.example.invalid/signed?token=old",
          expires_at: expired,
        }),
      },
    });

    render(<SharePage slug="opaque" />);

    await waitFor(() => expect(screen.getByText("图片地址已过期")).toBeTruthy());
    expect(screen.getByRole("button", { name: "重新获取" })).toBeTruthy();
  });

  it("链接到期时也照常按 404 处理（服务端是唯一裁判）", async () => {
    stub.routes({
      "GET /api/v1/shares/expired": {
        status: 404,
        json: errorBody("GENERATION_NOT_FOUND"),
      },
    });

    render(<SharePage slug="expired" />);

    await waitFor(() =>
      expect(screen.getByText("这个分享链接已经失效")).toBeTruthy(),
    );
  });
});

// --------------------------------------------------------------------------- #
// 反馈面板
// --------------------------------------------------------------------------- #
describe("反馈面板", () => {
  it("非可反馈终态不渲染入口（NEEDS_REVIEW 结果未定）", () => {
    render(
      <FeedbackPanel
        feedback={IDLE_FEEDBACK}
        busy={false}
        locked={false}
        available={false}
        onGate={vi.fn()}
        onSubmit={vi.fn()}
      />,
    );

    expect(screen.queryByRole("button", { name: "提交反馈" })).toBeNull();
  });

  it("提交时带上评分、分类与正文", async () => {
    const onSubmit = vi.fn();
    render(
      <FeedbackPanel
        feedback={IDLE_FEEDBACK}
        busy={false}
        locked={false}
        available
        onGate={vi.fn()}
        onSubmit={onSubmit}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "3" }));
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "第二张更好" } });
    fireEvent.click(screen.getByRole("button", { name: "提交反馈" }));

    expect(onSubmit).toHaveBeenCalledWith({
      rating: 3,
      category: "画面质量",
      content: "第二张更好",
    });
  });

  it("提交中禁用按钮（防重复提交）", () => {
    render(
      <FeedbackPanel
        feedback={{ stage: "submitting", generationId: GENERATION, view: null, failure: null }}
        busy={false}
        locked={false}
        available
        onGate={vi.fn()}
        onSubmit={vi.fn()}
      />,
    );

    const button = screen.getByRole("button", { name: "正在提交…" });
    expect((button as HTMLButtonElement).disabled).toBe(true);
  });

  it("成功后转为只读展示", () => {
    render(
      <FeedbackPanel
        feedback={{
          stage: "submitted",
          generationId: GENERATION,
          view: {
            id: "fb-1",
            generationId: "gen-1",
            rating: 5,
            category: "画面质量",
            content: "都很满意",
            status: "OPEN",
            createdAt: new Date().toISOString(),
          },
          failure: null,
        }}
        busy={false}
        locked={false}
        available
        onGate={vi.fn()}
        onSubmit={vi.fn()}
      />,
    );

    expect(screen.getByText(/已记录：5 分 · 画面质量/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "提交反馈" })).toBeNull();
  });

  it("重复提交的 409 冲突如实说明「只能评价一次」", () => {
    render(
      <FeedbackPanel
        feedback={{
          stage: "failed",
          generationId: GENERATION,
          view: null,
          failure: {
            code: "IDEMPOTENCY_CONFLICT",
            message: "冲突",
            status: 409,
            details: {},
          } as never,
        }}
        busy={false}
        locked={false}
        available
        onGate={vi.fn()}
        onSubmit={vi.fn()}
      />,
    );

    expect(screen.getByText("这个作品已经评价过了")).toBeTruthy();
  });

  it("反馈幂等键按任务派生，重复提交复用同一个键", async () => {
    stub.routes({
      "POST /api/v1/feedback": {
        status: 201,
        json: envelope({
          id: "fb-1",
          generation_id: "gen-1",
          rating: 5,
          category: "画面质量",
          content: null,
          status: "OPEN",
          created_at: new Date().toISOString(),
        }),
      },
    });
    const { submitFeedback } = await import("@/services/api");

    await submitFeedback({ generationId: "gen-1", rating: 5, category: "画面质量" });
    await submitFeedback({ generationId: "gen-1", rating: 5, category: "画面质量" });

    const calls = stub.calls.filter((call) => call.pathname === "/api/v1/feedback");
    expect(calls).toHaveLength(2);
    expect(calls[0].headers["idempotency-key"]).toBe(calls[1].headers["idempotency-key"]);
    expect(JSON.parse(String(calls[0].body))).toEqual({
      generation_id: "gen-1",
      rating: 5,
      category: "画面质量",
      content: null,
    });
  });
});
