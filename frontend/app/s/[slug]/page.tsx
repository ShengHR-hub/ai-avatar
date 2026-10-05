import type { Metadata } from "next";

import { SharePage } from "@/components/SharePage";

/**
 * 匿名分享页（`/s/[slug]`）。
 *
 * 路由本身只是把 slug 交给客户端组件；真正的授权判定在服务端
 * （`GET /api/v1/shares/{slug}`）：不存在、已撤销、已过期都是 404。
 * 这里不做任何本地缓存或预渲染判断，避免「页面能打开但接口说没有」的两套真相。
 */

export const metadata: Metadata = {
  title: "分享的作品 · AI Avatar Studio",
  robots: { index: false, follow: false },
};

export default async function ShareRoute({
  params,
}: {
  params: Promise<{ slug: string }>;
}) {
  const { slug } = await params;
  return <SharePage slug={slug} />;
}
