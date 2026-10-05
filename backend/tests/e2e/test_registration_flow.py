"""ENG-10 真实浏览器 E2E：注册链路、验证后锁死实测、移动端视口。

这些用例跑在**真实 Chrome**（CDP，`--headless=new`）与**真实服务端**上：
Next.js 生产构建 + FastAPI + PostgreSQL + Redis + MinIO。唯一的替身是邮件落盘
（`tests/e2e/local_server.py`；邮件供应商在契约里是未决项）。

栈没起来时整组直接失败并给出启动命令（`conftest.stack_reachable`），不静默跳过。
"""

from __future__ import annotations

import time

from tests.e2e.cdp import Chrome, SyncPage
from tests.e2e.conftest import (
    ARTIFACTS,
    WEB_URL,
    Account,
    browser_login,
    goto_studio,
    new_client,
    new_email,
    new_invite_code,
    new_mail_token,
    points_of,
    redeem_and_verify,
    seed_invite_code,
    synthetic_image_file,
)

#: 首页 / 创作台上与「开通」有关的可见文案（取自真实 DOM）
OPEN_BUTTON_TEXT = "去开通"

WORK_DIR = ARTIFACTS / "synthetic"


def _open_gate(page: SyncPage) -> None:
    """点开访客门禁弹层并等到它出现。

    先等按钮**真的挂上 DOM** 再点：`click_text` 用的是真实鼠标坐标，
    水合尚未完成时正文里已经有这门禁的说明文案，但按钮还不存在。
    """

    page.wait_for(
        "return Array.from(document.querySelectorAll('button'))"
        "  .some(function (b) { return (b.innerText || '').includes('开通'); });",
        timeout=30,
        raw=True,
    )
    page.click_text(OPEN_BUTTON_TEXT)
    page.wait_for(
        "return document.querySelector('[role=dialog]') !== null;", timeout=25, raw=True
    )
    page.wait_visible("[role=dialog]", timeout=25)


def _redeem_in_gate(page: SyncPage, *, code: str, email: str) -> None:
    page.type_in_dialog("input[type=email]", email)
    page.type_in_dialog("input[autocomplete=off]", code)
    page.click_in_dialog("兑换邀请码")
    page.wait_for("document.body.innerText.includes('开通成功')", timeout=40)


def _session_cookie(page: SyncPage) -> dict:
    cookies = {item["name"]: item for item in page.cookies()}
    assert "session" in cookies, cookies
    return cookies["session"]


def test_browser_redeems_invite_from_the_real_gate(engine, page: SyncPage) -> None:
    """浏览器里走完「点去开通 → 填邮箱+邀请码 → 兑换」，并确认会话真的落在浏览器上。"""

    code = new_invite_code()
    email = new_email("browser")
    seed_invite_code(engine, code, points=100, max_redemptions=1)

    goto_studio(page)
    assert "先开通，再开始生成" in page.body_text()

    _open_gate(page)
    _redeem_in_gate(page, code=code, email=email)

    cookie = _session_cookie(page)
    assert cookie["httpOnly"] is True, cookie
    assert cookie["secure"] is True, cookie

    # 会话事实来自服务端：浏览器自己带 Cookie 打 `/auth/session` 必须是 200
    probe = page.evaluate(
        "const r = await fetch('/api/v1/auth/session', {credentials: 'same-origin'});"
        " const b = await r.json();"
        " return JSON.stringify({status: r.status, verified: b?.data?.email_verified});"
    )
    compact = probe.replace(" ", "")
    assert '"status":200' in compact, probe

    # 兑换真的落了账（点数来自服务端，不是界面猜测）
    client = new_client()
    try:
        balance = points_of(client, cookie=f"session={cookie['value']}")
    finally:
        client.close()
    assert balance["available_points"] == 100, balance


def test_browser_email_verification_unlocks_the_same_session(
    engine, page: SyncPage
) -> None:
    """正确行为回归：**一枚邀请码** → 邮箱验证 → **同一个浏览器会话**解锁 → 上传 → 生成。

    这条路径就是用户真实会走的：兑换 → 点邮件链接 → 确认。修复前它走不通 ——
    `SessionRecord.email_verified` 只在邀请兑换时写入，`POST /auth/email/verify`
    只改数据库、**不续签会话**，于是前端 `useSession` 永远拿到 false，上传与提交入口
    一直锁着；而服务端授权读的是数据库，所以表现为**接口能过、界面锁死**。

    修复后 `GET /auth/session` 的 `email_verified` 以 `user_emails` 的**当前事实**为准
    （会话没有被重签、没有被延长、没有被复活），因此这里必须一次走通到底。
    """

    code = new_invite_code()
    email = new_email("verify")
    seed_invite_code(engine, code, points=100, max_redemptions=1)

    goto_studio(page)
    _open_gate(page)
    _redeem_in_gate(page, code=code, email=email)
    session_token = _session_cookie(page)["value"]
    cookie = f"session={session_token}"

    client = new_client()
    try:
        since = time.time()
        requested = client.post(
            "/api/v1/auth/email/request",
            headers={"cookie": cookie, "Idempotency-Key": "eng10-verify-request"},
            json={"email": email},
        )
        assert requested.status_code == 202, requested.text

        mail = new_mail_token(since=since, email=email)

        # 与用户点邮件链接等价：打开 /verify-email?token=...，再点「验证邮箱」确认。
        # 断言用的文案必须与页面真实文案一致：标题「确认你的邮箱」/按钮「验证邮箱」/
        # 完成「邮箱已验证」；页面里**没有**「邮箱验证」这四个字（顺序不同，不是子串），
        # 而 Suspense 回退态是「正在准备验证表单…」。
        page.goto(f"{WEB_URL}/verify-email?token={mail['token']}")
        page.wait_for_text("确认你的邮箱", timeout=30)
        page.click_text_unique("验证邮箱")
        page.wait_for_text("邮箱已验证", timeout=60)

        # 令牌是一次性的：页面（浏览器）已经消费掉它，所以这里的第二次提交必然失败。
        # 两种结果都说明链路真实走通：200 = 页面还没提交成功，422 = 令牌已被用掉。
        verified = client.post(
            "/api/v1/auth/email/verify",
            headers={"Idempotency-Key": "eng10-verify-confirm"},
            json={"token": mail["token"]},
        )
        assert verified.status_code in (200, 422), verified.text

        # **同一个会话**（同一枚 Cookie）现在必须报告已验证：缺陷回归的 API 侧断言
        session = client.get("/api/v1/auth/session", headers={"cookie": cookie})
        assert session.status_code == 200, session.text
        assert session.json()["data"]["email_verified"] is True, session.text

        # ---- 浏览器侧：同一枚 Cookie、同一个标签页，创作台必须已经解锁 ----
        goto_studio(page)
        body = page.body_text()
        assert "只差邮箱验证" not in body, body
        assert "可用点数" in body, body

        unlocked = page.evaluate(
            """
            const buttons = Array.from(document.querySelectorAll('button'));
            const submit = buttons.find((b) => (b.innerText || '').includes('开始生成'));
            return {
              submitText: submit ? submit.innerText.trim() : null,
              hasFileInput: document.querySelector('input[type=file]') !== null,
              uploadCta: (document.body.innerText || '').includes('开通后就能上传图片'),
              needsVerificationCopy:
                (document.body.innerText || '').includes('账号已开通，只差邮箱验证'),
            };
            """
        )
        assert unlocked["needsVerificationCopy"] is False, unlocked
        assert unlocked["uploadCta"] is False, unlocked
        assert unlocked["hasFileInput"] is True, unlocked
        assert unlocked["submitText"] is not None, unlocked

        page.screenshot(ARTIFACTS / "verify-then-unlocked.png")

        # ---- 解锁之后真的能上传并提交生成（界面与服务端同时放行） ----
        source = synthetic_image_file(
            WORK_DIR, "verify-source.png", width=64, height=64, seed=7
        )
        page.set_file("input[type=file]", source)
        page.wait_for_text("已就绪", timeout=180)

        page.type_into("textarea[name=prompt]", "验证通过后解锁的第一次生成。")
        page.wait_for(
            "return Array.from(document.querySelectorAll('button'))"
            "  .some(function (b) {"
            "    return (b.innerText || '').includes('开始生成') && !b.disabled;"
            "  });",
            timeout=45,
            raw=True,
        )
        page.click_text_unique("开始生成")
        page.wait_for_text("你的两张头像", timeout=300)

        # 结算真的发生了：这一枚邀请码的 100 点里，扣掉一次 PHOTO_PROMPT（40 点）
        balance = points_of(client, cookie=cookie)
        assert balance["available_points"] == 100 - 40, balance
        assert balance["reserved_points"] == 0, balance
    finally:
        client.close()


def test_mobile_viewport_shows_the_same_gate_and_entry_points(mobile_page: SyncPage) -> None:
    """移动端视口（375x812，mobile=true）下创作台与门禁同样是可用形态。"""

    goto_studio(mobile_page)
    size = mobile_page.viewport_size()
    assert size["w"] == 375, size
    assert size["dw"] <= 375, size

    body = mobile_page.body_text()
    assert "创作台" in body
    assert "改造我的图片" in body
    for way in ("入口 01", "入口 02", "入口 03"):
        assert way in body, f"移动端缺少{way}"

    # 三种入口在窄屏下都能点到（真实鼠标事件）
    mobile_page.click_text("文字创作")
    mobile_page.wait_for("document.body.innerText.includes('用文字做头像')", timeout=20)
    mobile_page.click_text("参考图片找灵感")
    mobile_page.wait_for("document.body.innerText.includes('从一张参考图找灵感')", timeout=20)

    _open_gate(mobile_page)
    mobile_page.wait_for("document.querySelector('input[type=email]') !== null", timeout=20)
    mobile_page.screenshot(ARTIFACTS / "mobile-gate.png")


def test_mobile_viewport_unlocks_the_full_flow_with_a_verified_session(
    engine, chrome: Chrome, mobile_page: SyncPage
) -> None:
    """移动端 + 已验证会话：点数、上传入口、提交按钮都进入可用态。"""

    account: Account = redeem_and_verify(engine, points=100, prefix="mobile")
    try:
        browser_login(mobile_page, account)
        body = mobile_page.body_text()
        assert "可用点数" in body, body
        state = mobile_page.evaluate(
            """
            const submit = Array.from(document.querySelectorAll('button'))
              .find((b) => (b.innerText || '').includes('开始生成'));
            return {
              submitDisabled: submit ? submit.disabled : null,
              hasFile: document.querySelector('input[type=file]') !== null,
              lockedCopy: (document.body.innerText || '').includes('开通后就能上传图片'),
            };
            """
        )
        assert state["lockedCopy"] is False, state
        assert state["hasFile"] is True, state
        assert state["submitDisabled"] is True, state  # 还没写描述
    finally:
        account.client.close()
