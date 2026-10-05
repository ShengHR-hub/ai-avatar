import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ImageViewer } from "@/components/ImageViewer";
import { WorkCard } from "@/components/WorkCard";
import type { WorkEntry } from "@/services/catalog";

/** 一条"能证明同源"的作品：输入图带证据，两张结果按 output_index 排。 */
const PHOTO_WORK: WorkEntry = {
  id: "w-photo",
  input: {
    src: "/art/works/t-Input.png",
    width: 1200,
    height: 1200,
    alt: "输入照片：正面半身像",
    sameRunEvidence: "任务 7c1f…：同一次提交产出的输入与两张结果",
  },
  outputs: [
    { src: "/art/works/t-Out-1.png", width: 1248, height: 1248, alt: "结果 1：黏土质感" },
    { src: "/art/works/t-Out-2.png", width: 1248, height: 1248, alt: "结果 2：黏土质感" },
  ],
  mode: "PHOTO_PROMPT",
  note: "同一次生成，约 40 秒。",
  prompt: { kind: "as-submitted", text: "把这张照片转成 3D 黏土风格头像，纯净浅灰背景，正方形构图" },
  origin: { kind: "site-generated", model: "gpt-image-2.5-flare", madeAt: "2026-09-30", taskId: "7c1f" },
  authorization: "本人作品，已获准公开在首页展示。",
};

/** 纯文字生成的作品：不该硬配一张"输入图"。 */
const TEXT_WORK: WorkEntry = {
  id: "w-text",
  outputs: [
    { src: "/art/works/w1-output-1.png", width: 1248, height: 1248, alt: "结果 1：手绘侧脸" },
    { src: "/art/works/w1-output-2.png", width: 1248, height: 1248, alt: "结果 2：手绘侧脸" },
  ],
  mode: "TEXT_PROMPT",
  prompt: { kind: "as-submitted", text: "一个短发女孩的侧脸头像，手绘质感，暖色柔和光线，背景大面积留白" },
  origin: { kind: "site-generated", model: "gpt-image-2.5-flare", madeAt: "2026-09-30" },
  authorization: "由本站生成链路产出，站点负责人已同意用于首页展示。",
};

function detail(container: HTMLElement): HTMLElement {
  const node = container.querySelector(".work__detail");
  if (!node) throw new Error("no detail panel");
  return node as HTMLElement;
}

describe("作品卡", () => {
  it("有成组输入图时展示三张图，描述默认收在面板里不压画面", () => {
    const { container } = render(<WorkCard work={PHOTO_WORK} label="作品 1" />);
    expect(container.querySelectorAll(".work__shot")).toHaveLength(3);
    expect(screen.getByRole("button", { name: "展开详情与描述" })).toHaveAttribute("aria-expanded", "false");

    const panel = detail(container);
    expect(panel.hasAttribute("hidden")).toBe(true);
    expect(panel.textContent).toContain("把这张照片转成 3D 黏土风格头像");
    const outside = (container.textContent ?? "").replace(panel.textContent ?? "", "");
    expect(outside).not.toContain("黏土风格头像");
  });

  it("纯文字作品只摆两张结果，不假装配过一张输入图", () => {
    const { container } = render(<WorkCard work={TEXT_WORK} label="作品 1" />);
    expect(container.querySelectorAll(".work__shot")).toHaveLength(2);
    expect(container.querySelector(".work__grid--three")).toBeNull();
    expect(screen.queryByText("输入照片")).toBeNull();
  });

  it("展开后给出描述、口径标注、来源与授权、复制和带走改", () => {
    const { container } = render(<WorkCard work={PHOTO_WORK} label="作品 1" />);
    fireEvent.click(screen.getByRole("button", { name: "展开详情与描述" }));

    const panel = detail(container);
    expect(panel.hasAttribute("hidden")).toBe(false);
    expect(screen.getByRole("button", { name: "收起详情" })).toHaveAttribute("aria-expanded", "true");
    expect(panel.textContent).toContain("把这张照片转成 3D 黏土风格头像，纯净浅灰背景，正方形构图");
    expect(panel.textContent).toContain("当时提交的内容");
    expect(panel.textContent).toContain("本人作品，已获准公开在首页展示。");
    expect(panel.textContent).toContain("gpt-image-2.5-flare");
    // 输入图与结果的成组关系必须带证据，不能只是一句"看起来是同一组"
    expect(panel.textContent).toContain("同一次提交产出的输入与两张结果");
    expect(screen.getByRole("button", { name: "复制这段" })).toBeInTheDocument();
  });

  it("「带进创作台改」把当时那段描述原样带过去", () => {
    render(<WorkCard work={TEXT_WORK} label="作品 1" />);
    fireEvent.click(screen.getByRole("button", { name: "展开详情与描述" }));

    const link = screen.getByRole("link", { name: "带进创作台改" });
    expect(link.getAttribute("href")).toContain("mode=TEXT_PROMPT");
    expect(decodeURIComponent(link.getAttribute("href") ?? "")).toContain(
      "一个短发女孩的侧脸头像，手绘质感",
    );
  });

  it("展示用描述必须写明它和当时提交的不一样", () => {
    const work: WorkEntry = {
      ...PHOTO_WORK,
      prompt: {
        kind: "display-only",
        text: "把这张照片转成水彩插画风格头像",
        reason: "去掉了描述里的一句私人说明。",
      },
    };
    render(<WorkCard work={work} label="作品 1" />);
    fireEvent.click(screen.getByRole("button", { name: "展开详情与描述" }));
    expect(screen.getByText("公开改写版")).toBeInTheDocument();
    expect(screen.getByText(/去掉了描述里的一句私人说明/)).toBeInTheDocument();
    expect(screen.queryByText("当时提交的内容")).toBeNull();
  });

  it("描述里检测到私人信息时不展示原文，也不给复制与带走改", () => {
    const work: WorkEntry = {
      ...PHOTO_WORK,
      prompt: { kind: "as-submitted", text: "把这张照片转成黏土风，参考 me@example.com 里的那张" },
    };
    const { container } = render(<WorkCard work={work} label="作品 1" />);
    fireEvent.click(screen.getByRole("button", { name: "展开详情与描述" }));
    expect(container.textContent ?? "").not.toContain("me@example.com");
    expect(container.textContent ?? "").toContain("暂不展示");
    expect(screen.queryByRole("button", { name: "复制这段" })).toBeNull();
    expect(screen.queryByRole("link", { name: "带进创作台改" })).toBeNull();
  });

  it("点图片打开大图查看器，Esc 关闭并把焦点还给触发它的图", () => {
    render(<WorkCard work={PHOTO_WORK} label="作品 1" />);
    const trigger = screen.getByRole("button", { name: /放大查看结果 1/ });
    // 真实浏览器里点击按钮会聚焦它（键盘激活同理），jsdom 不会，这里手动对齐前提
    trigger.focus();
    fireEvent.click(trigger);
    expect(screen.getByRole("dialog", { name: "查看大图" })).toBeInTheDocument();
    expect(screen.getByText("结果 1")).toBeInTheDocument();

    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog", { name: "查看大图" })).toBeNull();
    expect(document.activeElement).toBe(trigger);
  });

  it("查看器里的第一张是可对照的输入图（有成组证据时）", () => {
    render(<WorkCard work={PHOTO_WORK} label="作品 1" />);
    fireEvent.click(screen.getByRole("button", { name: /放大查看输入照片/ }));
    const dialog = screen.getByRole("dialog", { name: "查看大图" });
    // 「输入照片」在卡片按钮上也会出现一次，所以只在弹层里断言当前这张
    expect(dialog.textContent).toContain("输入照片");
  });
});

describe("大图查看器", () => {
  const items = [
    { src: "/a.png", alt: "第一张", caption: "输入照片" },
    { src: "/b.png", alt: "第二张", caption: "结果 1" },
    { src: "/c.png", alt: "第三张", caption: "结果 2" },
  ];

  it("方向键在多张图之间循环切换", () => {
    render(<ImageViewer items={items} startIndex={0} onClose={() => {}} />);
    expect(screen.getByText("输入照片")).toBeInTheDocument();
    fireEvent.keyDown(document, { key: "ArrowRight" });
    expect(screen.getByText("结果 1")).toBeInTheDocument();
    fireEvent.keyDown(document, { key: "ArrowLeft" });
    expect(screen.getByText("输入照片")).toBeInTheDocument();
    fireEvent.keyDown(document, { key: "ArrowLeft" });
    expect(screen.getByText("结果 2")).toBeInTheDocument();
  });

  it("只有一张图时不显示切换按钮", () => {
    render(<ImageViewer items={[items[0]]} onClose={() => {}} />);
    expect(screen.queryByRole("button", { name: "下一张 →" })).toBeNull();
  });
});
