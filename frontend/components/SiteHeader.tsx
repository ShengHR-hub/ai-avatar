"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";

import { useSession } from "@/hooks/useSession";
import { BrandMark } from "./BrandMark";

/**
 * 顶栏：品牌、创作入口、用户入口。
 *
 * 会话探测只决定"用户入口"长什么样：未登录、邮箱未验证或后端不可达时一律降级成
 * 访客态，绝不因此挡住作品与风格的浏览。
 *
 * 滚动后顶栏才变成实体玻璃条：首屏时它是透明的，图片和光才是主角。
 */
export function SiteHeader() {
  const session = useSession();
  const pathname = usePathname();
  const [stuck, setStuck] = useState(false);

  useEffect(() => {
    let frame = 0;
    const sync = () => {
      frame = 0;
      setStuck(window.scrollY > 10);
    };
    const onScroll = () => {
      if (frame === 0) frame = window.requestAnimationFrame(sync);
    };
    sync();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => {
      window.removeEventListener("scroll", onScroll);
      if (frame !== 0) window.cancelAnimationFrame(frame);
    };
  }, []);

  const signedIn = session.phase === "signed-in";
  const waiting = session.phase === "needs-verification";
  const onHome = pathname === "/";

  return (
    <header className={stuck ? "topbar topbar--stuck" : "topbar"}>
      <div className="topbar__inner">
        <Link className="brand" href="/" aria-label="回到首页">
          <BrandMark />
          <span className="brand__name">
            <b>头像工作室</b>
            <span>AI Avatar Studio</span>
          </span>
        </Link>

        <nav className="nav" aria-label="主导航">
          <Link className="nav__link nav__link--anchor" href={onHome ? "#works" : "/#works"}>
            作品
          </Link>
          <Link className="nav__link nav__link--anchor" href={onHome ? "#ideas" : "/#ideas"}>
            灵感方向
          </Link>
          <Link className="nav__link nav__link--go" href="/studio">
            开始创作
          </Link>
          {signedIn ? (
            <Link className="userchip" href="/me">
              <span className="userchip__dot userchip__dot--on" />
              我的点数与生成
            </Link>
          ) : (
            <Link className="userchip" href="/studio">
              <span className={waiting ? "userchip__dot userchip__dot--wait" : "userchip__dot"} />
              {waiting ? "邮箱待验证" : "开通 / 登录"}
            </Link>
          )}
        </nav>
      </div>
    </header>
  );
}
