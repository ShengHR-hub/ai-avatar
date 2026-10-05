import type { Metadata, Viewport } from "next";

import { SiteHeader } from "@/components/SiteHeader";

import "./globals.css";

export const metadata: Metadata = {
  title: "头像工作室 · 用一张图片或一句话做出头像",
  description:
    "上传一张图片按描述改造，或者直接用文字描述生成；人物、宠物、虚构角色、风景、背影、抽象画面都能做成头像，每次两张。等待时可以离开页面。",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  themeColor: "#e8ecef",
  colorScheme: "light",
};

/**
 * 全局骨架：一层柔光下的真实材质背景（照片 + 方向性柔光 + 极轻颗粒），
 * 上面才是玻璃层。玻璃必须有东西可以透——否则只是浅灰色板子。
 */
export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body>
        <div className="stage" aria-hidden="true">
          <div className="stage__wall" />
          <div className="stage__wash" />
          <div className="stage__grain" />
        </div>

        <SiteHeader />
        <main className="shell">{children}</main>

        <footer className="footnote">
          <div className="footnote__row">
            <strong>头像工作室</strong>
            <span>上传一张图片按描述改造，或直接用一句话描述。每次生成两张。</span>
          </div>
          <p className="footnote__note">
            首页展示的作品图由本站自己的生成链路产出，来源、生成日期与当时提交的描述都随作品一起标出；
            拿不到可公开成品的位置宁可空着，也不会用别的来源的图冒充效果。
          </p>
        </footer>
      </body>
    </html>
  );
}
