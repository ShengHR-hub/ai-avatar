"""ENG-10 主闭环浏览器用例（桌面 + 移动端）：点数 → 上传 → 生成 → 结果 → 下载 → 分享 → 反馈。

真实 Chrome + 真实 Next.js 生产构建 + 真实 FastAPI/Worker/PostgreSQL/Redis/MinIO。
唯一的替身是邮件落盘（`tests/e2e/local_server.py`；邮件供应商在契约里是未决项）。

**注册链路走的是用户真实会走的那条**：一枚邀请码 → 邮箱验证 → **同一个会话**解锁。
以前这里靠「同邮箱的第二枚邀请码」换一张新会话来绕过 ENG-03 的会话快照缺陷，
那一步已经删除（缺陷已修，见 `conftest.redeem_and_verify` 的说明）。
"""

from __future__ import annotations

import json
import os
import re
import time

import sqlalchemy as sa

from tests.e2e.cdp import Chrome, SyncPage, open_page
from tests.e2e.conftest import (
    ARTIFACTS,
    WEB_URL,
    Account,
    browser_login,
    list_outputs,
    new_client,
    redeem_and_verify,
    studio_generation_id,
    synthetic_image_file,
    wait_generation,
)

WORK_DIR = ARTIFACTS / "synthetic"

#: 提交时用的合成描述（不含任何真实用户信息）
PROMPT = "生成一张测试头像，手绘，暖色，正方形构图"

#: 结果区里每张图的下载链接与图片信息（`<a download>` 是既有实现给的入口）
_DOWNLOAD_JS = """
const items = Array.from(document.querySelectorAll('.result')).map((card) => {
  const img = card.querySelector('img');
  const link = card.querySelector('a[download]');
  const meta = card.querySelector('.result__meta');
  return {
    href: link ? link.getAttribute('href') : null,
    download: link ? link.getAttribute('download') : null,
    naturalWidth: img ? img.naturalWidth : null,
    naturalHeight: img ? img.naturalHeight : null,
    meta: meta ? meta.innerText.trim() : null,
  };
});
return JSON.stringify(items);
"""


def _points_shown(page: SyncPage) -> int:
    """从真实 DOM 里读创作台显示的点数（`.points__v`）。"""

    raw = page.evaluate(
        "const el = document.querySelector('.points__v');"
        " return el ? el.innerText.trim() : null;"
    )
    assert raw is not None, "创作台没有渲染点数区（会话可能没被浏览器带上）"
    return int(str(raw).replace(",", ""))


def _wait_submit_enabled(page: SyncPage, *, timeout: float = 45.0) -> None:
    """等「开始生成」**真的可点**；超时把「为什么不给提交」的界面事实带出来。

    只看超时是不够的：本轮实测的失败现场是「按钮存在但 `disabled: true`，
    页面提示『先上传一张图片』」，如果只报超时会被误读成「重挂载把上传态弄丢了」。
    """

    try:
        page.wait_for(
            "return Array.from(document.querySelectorAll('button'))"
            "  .some(function (b) {"
            "    return (b.innerText || '').includes('开始生成') && !b.disabled;"
            "  });",
            timeout=timeout,
            raw=True,
        )
    except TimeoutError as error:
        state = page.evaluate(
            """
            const btn = Array.from(document.querySelectorAll('button'))
              .find(function (b) { return (b.innerText || '').includes('开始生成'); });
            const picked = document.querySelector('.picked__state');
            const notices = Array.from(document.querySelectorAll('.notice'))
              .map(function (n) {
                var t = (n.innerText || '').split(String.fromCharCode(10)).join(' ');
                return t.slice(0, 120);
              });
            return {
              submitText: btn ? btn.innerText.trim() : null,
              disabled: btn ? btn.disabled : null,
              hasFileInput: !!document.querySelector('input[type=file]'),
              promptValue: (document.querySelector('textarea[name=prompt]') || {}).value || null,
              pickedState: picked
                ? picked.innerText.split(String.fromCharCode(10)).join(' ').trim()
                : null,
              composerReady: (document.body.innerText || '').includes('已就绪'),
              notices: notices,
              hint: (document.body.innerText || '').indexOf('先上传一张图片') >= 0,
            };
            """
        )
        raise AssertionError(f"「开始生成」一直不可点：{state}（原始超时：{error}）") from error


def _click_submit(page: SyncPage) -> None:
    """点一次「开始生成」（真实鼠标点击），不碰描述框。

    单独拆出来是因为「第二次提交」不该再往描述框里插一遍字：
    `Input.insertText` 是**追加**语义，重复调用会把同一段描述写两遍。
    """

    _wait_submit_enabled(page)
    page.click_text_unique("开始生成")


def _submit(page: SyncPage) -> None:
    """在描述框里写内容并提交（真实鼠标点击「开始生成」）。"""

    page.type_into("textarea[name=prompt]", PROMPT)
    _click_submit(page)


def _wait_results(page: SyncPage, *, timeout: float = 300.0) -> None:
    page.wait_for_text("你的两张头像", timeout=timeout)
    _wait_all_download_links(page, timeout=timeout)


def _wait_all_download_links(page: SyncPage, *, timeout: float = 120.0) -> None:
    """等到**每一张**结果都有 `<a download>`（图片异步取地址，不能只看标题出现）。"""

    page.wait_for(
        "const cards = Array.from(document.querySelectorAll('.result'));"
        " return cards.length > 0"
        "   && cards.every(function (c) { return c.querySelector('a[download]') !== null; });",
        timeout=timeout,
        raw=True,
    )


def _download_links(page: SyncPage) -> list[dict]:
    _wait_all_download_links(page)
    return json.loads(page.evaluate(_DOWNLOAD_JS))


def _share_link(page: SyncPage) -> str:
    raw = page.evaluate(
        "const a = document.querySelector('.share-result__link a');"
        " return a ? a.getAttribute('href') : null;"
    )
    assert raw, "分享面板没有渲染链接"
    return str(raw)


def _generation_id(page: SyncPage) -> str:
    """当前任务 id（见 `conftest.studio_generation_id` 的说明：不在地址栏）。"""

    return studio_generation_id(page)


def _newest_generation_id(client, *, cookie: str) -> str:
    """从服务端读**最近的**任务 id。

    为什么不能只靠前端：`sessionStorage` 里那把 `active-generation` 在某些路径下
    不会被写入（例如结果区渲染时 `useStudio` 已经切到 watch 状态）。任务的真相在
    服务端，所以这里用 `GET /me/generations` 取列表头一条——它与界面显示的是同一份事实。
    """

    response = client.get("/api/v1/me/generations", headers={"cookie": cookie})
    assert response.status_code == 200, response.text
    items = response.json()["data"]
    assert items, "服务端没有返回任何任务"
    return str(items[0]["id"])


def _wait_upload_ready(page: SyncPage, *, timeout: float = 180.0) -> None:
    """等上传真的到 READY（界面显示「已就绪」），否则把失败文案带出来。"""

    try:
        page.wait_for_text("已就绪", timeout=timeout)
    except TimeoutError as error:
        state = page.evaluate(
            "return JSON.stringify({"
            "  body: (document.body.innerText || '').slice(0, 400),"
            "  picked: !!document.querySelector('.picked'),"
            "  tag: (document.querySelector('.picked__state') || {}).innerText || null"
            "});"
        )
        raise AssertionError(f"上传没有进入 READY：{state}（原始超时：{error}）") from error


def test_desktop_full_loop_points_upload_generate_download_share_feedback(
    engine, chrome: Chrome, page: SyncPage
) -> None:
    """桌面端完整闭环（含匿名分享页与反馈）。"""

    account: Account = redeem_and_verify(engine, points=200, prefix="loop")
    client = new_client()
    try:
        before = account.get("/api/v1/me/points").json()["data"]
        assert before["available_points"] == account.initial_points

        browser_login(page, account)

        # ---- 点数显示（服务端事实） ----
        assert _points_shown(page) == before["available_points"]
        page.screenshot(ARTIFACTS / "01-desktop-studio-unlocked.png")

        # ---- 上传一张合成图（改造我的图片） ----
        source = synthetic_image_file(
            WORK_DIR, "desktop-source.png", width=96, height=96, seed=5
        )
        page.set_file("input[type=file]", source)
        _wait_upload_ready(page)
        assert source.name in page.body_text()

        # ---- 提交生成（真实 Worker + Mock Provider） ----
        _submit(page)
        _wait_results(page)
        page.screenshot(ARTIFACTS / "02-desktop-results.png")

        # ---- 结果展示：两张、尺寸与 MIME 来自服务端 ----
        # 下载链接必须指向**本栈配置的对象存储端点**（预签名 URL 的 Host 来自
        # STORAGE_ENDPOINT）。这里从配置读，不写死地址：原生栈是 127.0.0.1:19000，
        # Compose 栈是容器与宿主都能到达的那个宿主地址。断言本身没有放宽 ——
        # 它仍然要求链接来自本栈的存储端点，而不是任意主机。
        storage_endpoint = os.environ.get("ENG10_STORAGE_ENDPOINT", "http://127.0.0.1:19000")
        links = _download_links(page)
        assert len(links) == 2, links
        for index, item in enumerate(links):
            assert item["href"] and item["href"].startswith(f"{storage_endpoint}/"), item
            assert item["download"] == f"avatar-{index + 1}.png", item
            assert item["naturalWidth"] == 4 and item["naturalHeight"] == 4, item
            assert "PNG" in (item["meta"] or ""), item

        # ---- 下载：真实取字节 ----
        fetched = client.get(links[0]["href"])
        assert fetched.status_code == 200, fetched.status_code
        assert fetched.headers["content-type"].startswith("image/"), fetched.headers
        assert fetched.content, "下载为空"
        (ARTIFACTS / "03-downloaded-result.png").write_bytes(fetched.content)

        # ---- 结算：服务端点数真的被消费 ----
        generation_id = _generation_id(page) or _newest_generation_id(client, cookie=account.cookie)
        assert generation_id, "前端没有记住当前任务号（sessionStorage active-generation）"
        generation = account.get(f"/api/v1/generations/{generation_id}").json()["data"]
        assert generation["status"] == "SUCCEEDED", generation
        assert generation["billing_status"] == "CONSUMED", generation

        after = account.get("/api/v1/me/points").json()["data"]
        assert after["reserved_points"] == 0, after
        assert (
            after["available_points"] == before["available_points"] - generation["reserved_points"]
        ), (before, after, generation)

        outputs = list_outputs(client, cookie=account.cookie, generation_id=generation_id)
        assert len(outputs) == 2, outputs

        # ---- 分享：浏览器里点「生成分享链接」 ----
        page.click_in_panel("把成果发给别人看", "生成分享链接")
        page.wait_for("document.querySelector('.share-result__link a') !== null", timeout=90)
        link = _share_link(page)
        slug = link.rsplit("/s/", 1)[1]
        assert re.fullmatch(r"[A-Za-z0-9_-]{16,}", slug), slug
        for output in outputs:
            assert output["asset_id"] not in slug, "slug 泄漏了内部资产 ID"
        page.screenshot(ARTIFACTS / "04-desktop-share.png")

        # ---- 匿名分享页：**另起一个浏览器**（真正不共享任何 Cookie 的访客） ----
        # 不用 `Network.clearBrowserCookies()`：Cookie 是浏览器级状态，清一次会把
        # 主标签页的会话也清掉（实测踩到：验证过匿名页之后，提交反馈直接 401）。
        # `Target.createBrowserContext` 在 headless Chrome 154 上返回
        # `Not allowed`，所以这里用一个全新的浏览器实例，隔离最彻底。
        with Chrome(window="1440,900") as anon_chrome, open_page(anon_chrome, "about:blank") as anon:
            anon.goto(link)
            anon.wait_for(
                "document.body.innerText.includes('第 1 张')"
                " || document.body.innerText.includes('第 2 张')",
                timeout=60,
            )
            anon_body = anon.body_text()
            assert "第 1 张" in anon_body or "第 2 张" in anon_body, anon_body
            # 不泄漏：所有者邮箱、Prompt 全文、存储细节
            assert account.email not in anon_body
            assert PROMPT not in anon_body
            assert "minio" not in anon_body.lower()
            assert account.user_id.hex not in anon_body.replace("-", "")
            anon.screenshot(ARTIFACTS / "05-anonymous-share-page.png")

        # ---- 反馈：浏览器里提交一次 ----
        page.click_in_panel("这次生成怎么样？", "提交反馈")
        page.wait_for("document.body.innerText.includes('已记录')", timeout=60)
        assert "5 分" in page.body_text()
        page.screenshot(ARTIFACTS / "06-desktop-feedback.png")

        with engine.begin() as conn:
            rows = conn.execute(
                sa.text(
                    "SELECT rating, category FROM feedback WHERE user_id = :uid"
                ),
                {"uid": account.user_id},
            ).all()
        assert len(rows) == 1, rows
        assert rows[0][0] == 5, rows
    finally:
        client.close()
        account.client.close()


def test_second_submission_is_a_new_generation_and_costs_points_again(
    engine, page: SyncPage
) -> None:
    """重复提交：同一会话里再提交一次是**新任务**，点数再扣一次（不是幂等回放）。

    ## 第一次提交必须先把上传态做出来

    创作台默认入口是「改造我的图片」（`PHOTO_PROMPT`），`useStudio.canSubmit` 要求
    `state.ready`，所以**不先上传一张图，「开始生成」永远是禁用的**。
    本轮实测的失败现场正是如此：按钮文案是「开始生成 / 2 张」且 `disabled: true`，
    页面提示「先上传一张图片」——它是「用例漏了上传」，不是「重挂载把上传态弄丢了」。

    ## 第二次提交走哪条路（这条决定断言能不能成立）

    结果区里的「用同样的描述再来两张」绑定的是 `studio.submit`（**新**提交），
    不是 `studio.retry`：`POST /generations/{id}/retry` 只接受 `FAILED`
    （`app/services/generation/service.py::retry_generation`），对 `SUCCEEDED` 会 409。
    `useStudio.runWatch` 在任务 `SUCCEEDED` 终局后调用 `rotateDraftId()`，
    因此这一次提交拿到的是**新的幂等键** → 服务端创建新任务并再次扣点。
    """

    account: Account = redeem_and_verify(engine, points=200, prefix="repeat")
    try:
        browser_login(page, account)
        before = account.get("/api/v1/me/points").json()["data"]
        assert before["reserved_points"] == 0, before

        # ---- 第一次：上传 → 等到 READY → 提交 ----
        source = synthetic_image_file(
            WORK_DIR, "repeat-source.png", width=96, height=96, seed=17
        )
        page.set_file("input[type=file]", source)
        _wait_upload_ready(page)
        assert source.name in page.body_text()

        _submit(page)
        _wait_results(page)
        first = _generation_id(page) or _newest_generation_id(account.client, cookie=account.cookie)
        assert first, "第一次提交后前端没有记住任务号"
        first_task = wait_generation(
            account.client, cookie=account.cookie, generation_id=first
        )
        assert first_task["status"] == "SUCCEEDED", first_task
        assert first_task["billing_status"] == "CONSUMED", first_task

        # ---- 第二次：同一会话、同一张已就绪的图，从结果区再提交一次 ----
        # 先等创作台重新可提交（终局后 `busy/working` 都已复位，草稿号已轮换）
        _wait_submit_enabled(page)
        page.click_text_unique("用同样的描述再来两张")

        deadline = time.monotonic() + 300
        second = first
        while time.monotonic() < deadline:
            second = _newest_generation_id(account.client, cookie=account.cookie)
            if second != first:
                break
            time.sleep(1.0)
        assert second != first, f"「再来两张」没有产生新任务（仍是 {first}）"

        # 新任务真的跑完并结算（不是幂等回放旧任务）
        second_task = wait_generation(
            account.client, cookie=account.cookie, generation_id=second
        )
        assert second_task["status"] == "SUCCEEDED", second_task
        assert second_task["billing_status"] == "CONSUMED", second_task

        # 前端也确实切到了新任务（`rememberActiveGeneration(created.id)`）
        page.wait_for(
            "return sessionStorage.getItem('ai-avatar:active-generation') === %s;"
            % json.dumps(second),
            timeout=120,
            raw=True,
        )
        # 第二次终局后创作台再次可提交 = 界面真的走出了等待态
        _wait_submit_enabled(page, timeout=300)

        # ---- 点数：两次各扣一笔，预留清零 ----
        after = account.get("/api/v1/me/points").json()["data"]
        expected_consumed = first_task["reserved_points"] + second_task["reserved_points"]
        assert after["available_points"] == before["available_points"] - expected_consumed, (
            before,
            after,
            first_task,
            second_task,
        )
        assert after["available_points"] < before["available_points"], (before, after)
        assert after["reserved_points"] == 0, after
    finally:
        account.client.close()


def test_mobile_full_loop_reaches_results_and_share(
    engine, chrome: Chrome, mobile_page: SyncPage
) -> None:
    """移动端视口（375x812）跑一遍关键闭环：点数 → 上传 → 生成 → 结果 → 分享 → 下载。"""

    account: Account = redeem_and_verify(engine, points=200, prefix="mloop")
    client = new_client()
    try:
        browser_login(mobile_page, account)
        assert _points_shown(mobile_page) == account.initial_points

        source = synthetic_image_file(
            WORK_DIR, "mobile-source.png", width=80, height=80, seed=9
        )
        mobile_page.set_file("input[type=file]", source)
        _wait_upload_ready(mobile_page)

        _submit(mobile_page)
        _wait_results(mobile_page)
        links = _download_links(mobile_page)
        assert len(links) == 2, links
        mobile_page.screenshot(ARTIFACTS / "07-mobile-results.png")

        mobile_page.click_in_panel("把成果发给别人看", "生成分享链接")
        mobile_page.wait_for(
            "document.querySelector('.share-result__link a') !== null", timeout=90
        )
        link = _share_link(mobile_page)
        assert "/s/" in link, link
        mobile_page.screenshot(ARTIFACTS / "08-mobile-share.png")

        fetched = client.get(links[1]["href"])
        assert fetched.status_code == 200, fetched.status_code
        assert fetched.content, "移动端结果下载为空"

        # 移动端匿名分享页也能打开（另起一个浏览器，不动主标签页的会话）
        with Chrome(window="375,812") as anon_chrome, open_page(anon_chrome, "about:blank") as anon:
            anon.set_viewport(375, 812)
            anon.goto(link)
            anon.wait_for(
                "document.body.innerText.includes('第 1 张')"
                " || document.body.innerText.includes('第 2 张')",
                timeout=60,
            )
            anon.screenshot(ARTIFACTS / "09-mobile-anonymous-share.png")
    finally:
        client.close()
        account.client.close()
