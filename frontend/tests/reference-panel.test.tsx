import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ReferencePanel } from "@/components/ReferencePanel";
import type { ReferenceState } from "@/hooks/useStudio";
import { ApiFailure } from "@/services/errors";

const BASE: ReferenceState = {
  stage: "idle",
  fileName: "",
  assetId: null,
  previewUrl: null,
  description: "",
  model: "",
  fromCache: false,
  autoFilled: false,
  failure: null,
};

function noop() {
  return () => undefined;
}

function panel(overrides: Partial<ReferenceState> = {}, props: Record<string, unknown> = {}) {
  const handlers = {
    onGate: noop(),
    onPick: noop(),
    onAnalyze: noop(),
    onApply: noop(),
    onReset: noop(),
  };
  return render(
    <ReferencePanel
      reference={{ ...BASE, ...overrides }}
      busy={false}
      locked={false}
      {...handlers}
      {...props}
    />,
  );
}

describe("参考图片找灵感面板", () => {
  it("没登录时不给文件选择器，只引导开通", () => {
    panel({}, { locked: true });

    expect(document.querySelector("input[type=file]")).toBeNull();
    expect(screen.getByRole("button", { name: /开通后就能放参考图/ })).toBeInTheDocument();
  });

  it("登录后第一步才是选一张参考图", () => {
    panel();

    const input = document.querySelector("input[type=file]") as HTMLInputElement | null;
    expect(input).not.toBeNull();
    expect(input?.accept).toBe("image/jpeg,image/png,image/webp");
    expect(screen.queryByRole("button", { name: /读出画面描述/ })).toBeNull();
  });

  it("上传完成但没有描述时，读图这一步由用户明确按下去", () => {
    panel({ stage: "uploaded", fileName: "inspo.png", assetId: "ref-1" });

    expect(screen.getByText("inspo.png")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "读出画面描述" })).toBeInTheDocument();
  });

  it("正在读图时给出进行中的说明，并明确它不创建任务也不占点数", () => {
    panel({ stage: "analyzing", fileName: "inspo.png", assetId: "ref-1" });

    expect(screen.getByRole("status").textContent).toContain("正在看这张图");
    expect(document.body.textContent).toContain("不会创建任务、也不会占点数");
    expect(screen.getByRole("button", { name: "正在看这张图" })).toBeDisabled();
  });

  it("描述框原本是空的时，说明已经填进去了，不再摆一个多余的按钮", () => {
    panel({
      stage: "described",
      fileName: "inspo.png",
      assetId: "ref-1",
      description: "一个人背对镜头站在草地上",
      autoFilled: true,
    });

    expect(screen.getByText("一个人背对镜头站在草地上")).toBeInTheDocument();
    expect(screen.getByText("已填进下面的描述框")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "填进描述框" })).toBeNull();
  });

  it("用户已经有草稿时绝不覆盖，给出「填进去」和「追加」两条明确出路", () => {
    const onApply = vi.fn();
    panel(
      {
        stage: "described",
        fileName: "inspo.png",
        assetId: "ref-1",
        description: "一个人背对镜头站在草地上",
        autoFilled: false,
      },
      { onApply },
    );

    expect(screen.getByRole("button", { name: "填进描述框" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "追加到我写的后面" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "追加到我写的后面" }));
    expect(onApply).toHaveBeenCalledWith("append");
  });

  it("口径必须说准：这是现写的描述，不是这张图原本的写法", () => {
    panel({ stage: "described", assetId: "ref-1", description: "一段画面描述" });

    expect(document.body.textContent).toContain("现写");
    expect(document.body.textContent).not.toContain("还原");
  });

  it("复用缓存那次如实说明没有重新读图，不假装刚算过", () => {
    panel({ stage: "described", assetId: "ref-1", description: "一段画面描述", fromCache: true });

    expect(document.body.textContent).toContain("这是刚才那次读出来的结果");
  });

  it("读图失败时把服务端的状态与原因原样呈现", () => {
    const failure = new ApiFailure({
      code: "RATE_LIMITED",
      message: "请求过于频繁，请稍后重试",
      status: 429,
      details: { scope: "user" },
    });
    panel({ stage: "failed", fileName: "inspo.png", assetId: "ref-1", failure });

    expect(document.body.textContent).toContain("请求过于频繁，请稍后重试");
    expect(screen.getByText(/问题编号/)).toBeInTheDocument();
    expect(document.body.textContent).toContain("RATE_LIMITED");
  });

  it("上传这一步就失败时也留有换一张的出路", () => {
    panel({ stage: "failed", fileName: "bad.png", failure: null });

    expect(screen.getByRole("button", { name: "换一张参考图" })).toBeInTheDocument();
  });
});
