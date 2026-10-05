import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { Composer } from "@/components/Composer";
import type { UploadState } from "@/hooks/useStudio";
import type { OpenMode } from "@/services/config";

const IDLE: UploadState = {
  stage: "idle",
  fileName: "",
  assetId: null,
  status: "",
  failure: null,
  previewUrl: null,
};

/** 真实用法里描述由父级持有，所以测试用一个受控壳子包起来。 */
function Harness({ initial = "", mode = "TEXT_PROMPT" as OpenMode }: { initial?: string; mode?: OpenMode }) {
  const [prompt, setPrompt] = useState(initial);
  return (
    <Composer
      mode={mode}
      upload={IDLE}
      busy={false}
      ready={mode === "TEXT_PROMPT"}
      prompt={prompt}
      canSubmit
      working={false}
      locked={false}
      onGate={vi.fn()}
      onPick={vi.fn()}
      onReset={vi.fn()}
      onPromptChange={setPrompt}
      onSubmit={vi.fn()}
    />
  );
}

function area(container: HTMLElement): HTMLTextAreaElement {
  const node = container.querySelector("textarea");
  if (!node) throw new Error("no textarea");
  return node;
}

describe("创作台的灵感分类", () => {
  it("空描述时点标签生成整段描述，并且只改描述框", () => {
    const { container } = render(<Harness />);
    fireEvent.click(screen.getByRole("button", { name: "宠物" }));
    expect(area(container).value).toBe("生成宠物头像，正方形构图");
  });

  it("已有自写内容时只追加，原有文字一个字都不少", () => {
    const { container } = render(<Harness initial="一只橘猫趴在窗台上" />);
    fireEvent.click(screen.getByRole("button", { name: "蜡笔" }));
    expect(area(container).value).toBe("一只橘猫趴在窗台上，蜡笔涂鸦，正方形构图");
  });

  it("点选前先说清会发生什么", () => {
    render(<Harness initial="一只橘猫" />);
    expect(screen.getByText(/还没选/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "治愈" }));
    expect(screen.getByText(/会追加到你已有描述的末尾/)).toBeInTheDocument();
  });

  it("同一维换标签是替换而不是叠加", () => {
    const { container } = render(<Harness />);
    fireEvent.click(screen.getByRole("button", { name: "治愈" }));
    fireEvent.click(screen.getByRole("button", { name: "搞怪" }));
    const value = area(container).value;
    expect(value).toContain("搞怪夸张");
    expect(value).not.toContain("温暖治愈");
    expect(value.match(/正方形构图/g)).toHaveLength(1);
  });

  it("用途预设一键套用一组标签，并带上附加句", () => {
    const { container } = render(<Harness />);
    fireEvent.click(screen.getByRole("button", { name: "情侣头像" }));
    const value = area(container).value;
    expect(value).toContain("生成双人头像");
    expect(value).toContain("两张风格统一");
    expect(screen.getByRole("button", { name: "情侣头像" })).toHaveAttribute("aria-pressed", "true");
  });

  it("撤销上一步回到上一次的样子，恢复原文回到刚进来时的描述", () => {
    const { container } = render(<Harness initial="我自己的描述" />);
    fireEvent.click(screen.getByRole("button", { name: "手绘" }));
    expect(area(container).value).toContain("手绘");

    fireEvent.click(screen.getByRole("button", { name: "撤销上一步" }));
    expect(area(container).value).toBe("我自己的描述");

    fireEvent.click(screen.getByRole("button", { name: "正面" }));
    expect(area(container).value).toContain("正面构图");
    fireEvent.click(screen.getByRole("button", { name: "恢复刚进来时的描述" }));
    expect(area(container).value).toBe("我自己的描述");
  });

  it("清空也能撤销回来", () => {
    const { container } = render(<Harness initial="先写点东西" />);
    fireEvent.click(screen.getByRole("button", { name: "清空" }));
    expect(area(container).value).toBe("");
    fireEvent.click(screen.getByRole("button", { name: "撤销上一步" }));
    expect(area(container).value).toBe("先写点东西");
  });

  it("手动改字之后标签选择清空，不会把用户的修改覆盖回去", () => {
    const { container } = render(<Harness initial="起点" />);
    fireEvent.click(screen.getByRole("button", { name: "手绘" }));
    expect(area(container).value).toContain("手绘");

    fireEvent.change(area(container), { target: { value: "我自己重写的整段描述" } });
    expect(area(container).value).toBe("我自己重写的整段描述");
    expect(screen.getByRole("button", { name: "手绘" })).toHaveAttribute("aria-pressed", "false");
    expect(area(container).value).toBe("我自己重写的整段描述");
  });

  it("改造图片不提供「主题」：主体来自上传的那张图", () => {
    const { unmount } = render(<Harness mode="PHOTO_PROMPT" />);
    expect(screen.getByRole("button", { name: "蜡笔" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "宠物" })).toBeNull();
    unmount();

    render(<Harness mode="TEXT_PROMPT" />);
    expect(screen.getByRole("button", { name: "宠物" })).toBeInTheDocument();
  });

  it("提交前把方式、图片和描述摆出来", () => {
    render(<Harness initial="一只橘猫" />);
    expect(screen.getByText(/这次会提交/)).toBeInTheDocument();
    expect(screen.getByText("文字创作")).toBeInTheDocument();
    expect(screen.getByText("不使用图片")).toBeInTheDocument();
  });
});
