"""极简 Chrome DevTools Protocol 客户端（ENG-10 浏览器 E2E）。

## 为什么不装 Playwright / Selenium

- 本机 `npm install` 会被 `EBUSY` 打断并把原生包截断（见 `windows-sandbox-workarounds`
  第 13 条），装 Playwright 还要再下 100MB+ 的浏览器；而本机**已经有** Chrome。
- 直接说 CDP 只需要一个 WebSocket 客户端（venv 里 `websockets` 已随 `uvicorn[standard]`
  存在），没有额外依赖，也没有额外需要维护的版本。

## 交互是"真事件"而不是 `element.click()`

点击走 `Input.dispatchMouseEvent`（在元素中心按下 + 抬起），输入走 `Input.insertText`，
文件上传走 `DOM.setFileInputFiles` —— 都是浏览器真实输入路径。用
`Runtime.evaluate("el.click()")` 会绕过命中测试与事件冒泡，那种"通过"没有说服力。

## 截图的两个已知坑（本机实测）

`Page.captureScreenshot` 在 `deviceScaleFactor > 1` 或 `clip.scale > 1` 时会**永久挂起**
（不是慢，是不返回），并把后续命令一起拖死。因此这里固定 dsf=1、不传 clip，
并且给每条命令都加了超时；截图失败只记录，不判定用例失败。
"""

from __future__ import annotations

import asyncio
import base64
import json
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import websockets

__all__ = ["CdpPage", "Chrome", "SyncPage", "find_chrome"]

#: 本机 Chrome 的常见安装位置（按顺序探测）
_CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)


def find_chrome() -> Path:
    for candidate in _CHROME_CANDIDATES:
        path = Path(candidate)
        if path.exists():
            return path
    found = shutil.which("chrome") or shutil.which("msedge")
    if found:
        return Path(found)
    raise RuntimeError("找不到 Chrome/Edge 可执行文件。")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class Chrome:
    """以 `--headless=new` 启动一个受控浏览器实例。"""

    def __init__(self, *, profile_dir: Path | None = None, window: str = "1440,900") -> None:
        self.port = _free_port()
        self._temp = profile_dir is None
        self.profile = profile_dir or Path(tempfile.mkdtemp(prefix="eng10-chrome-"))
        self._window = window
        self.process: subprocess.Popen | None = None

    # ------------------------------------------------------------- 直接连接
    def _ws_url_for(self, target_id: str) -> str:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}/json/list", timeout=10
        ) as response:
            targets = json.loads(response.read().decode("utf-8"))
        for target in targets:
            if target.get("id") == target_id:
                return str(target["webSocketDebuggerUrl"])
        raise RuntimeError(f"找不到 target {target_id} 的调试地址")

    def attach_page(
        self, target_id: str, *, browser_context_id: str | None = None
    ) -> "SyncPage":
        """连到一个已存在的 target（配合 `Target.createTarget` 使用）。"""

        page = SyncPage(CdpPage(self._ws_url_for(target_id)))
        page._browser_context_id = browser_context_id  # noqa: SLF001 - 供匿名上下文清理
        return page

    def __enter__(self) -> "Chrome":
        # `--remote-debugging-port=<port>` **必须写成 `=` 连接的单参数**。
        # 拆成两个 argv（`--remote-debugging-port` + `"9222"`）时 Chrome 会把端口当成
        # 一个位置参数（要打开的目标），于是**根本不监听调试端口**、也不写
        # `DevToolsActivePort`，表现为「进程活着但连不上」——本机实测（ENG-10）：
        # joined 形式 ready=True，separate 形式 ready=False（profile、stdio 都无关）。
        args = [
            str(find_chrome()),
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--disable-background-networking",
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={self.profile}",
            f"--window-size={self._window}",
            "about:blank",
        ]
        self.process = subprocess.Popen(  # noqa: S603 - 参数来自本模块常量
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        self._wait_ready()
        return self

    def _wait_ready(self, timeout: float = 30.0) -> None:
        end = time.monotonic() + timeout
        last: str = "未尝试"
        while time.monotonic() < end:
            if self.process is not None and self.process.poll() is not None:
                raise RuntimeError(
                    f"Chrome 已退出（退出码 {self.process.returncode}），"
                    f"调试端口 {self.port} 不会就绪。"
                )
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}/json/version", timeout=2
                ) as response:
                    if response.status == 200:
                        return
                    last = f"status={response.status}"
            except (urllib.error.URLError, OSError) as error:
                last = f"{type(error).__name__}"
                time.sleep(0.2)
        raise RuntimeError(
            f"Chrome 调试端口 {self.port} 在 {timeout:.0f}s 内未就绪（最后 {last}）。"
        )

    def new_page(self, url: str = "about:blank") -> "CdpPage":
        """开一个新标签页并连上它的 page target（不需要 sessionId）。"""

        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/json/new?{url}", method="PUT"
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            target = json.loads(response.read().decode("utf-8"))
        return CdpPage(target["webSocketDebuggerUrl"])

    def close_page(self, target_id: str) -> None:
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/json/close/{target_id}", timeout=5
            ).read()
        except Exception:  # noqa: BLE001 - 关闭失败不影响断言
            pass

    def __exit__(self, *exc: object) -> None:
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
        if self._temp:
            shutil.rmtree(self.profile, ignore_errors=True)


class CdpPage:
    """一个 page target 上的 CDP 会话。"""

    def __init__(self, ws_url: str) -> None:
        self._ws_url = ws_url
        self._ws: Any = None
        self._next_id = 0
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> "CdpPage":
        self._ws = await websockets.connect(self._ws_url, max_size=64 * 1024 * 1024)
        for domain in ("Page", "Runtime", "DOM", "Network"):
            await self.send(f"{domain}.enable")
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._ws is not None:
            await self._ws.close()

    async def send(
        self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 30.0
    ) -> dict[str, Any]:
        """发一条命令并等它自己的响应（跳过事件推送）。"""

        async with self._lock:
            self._next_id += 1
            message_id = self._next_id
            payload = {"id": message_id, "method": method}
            if params:
                payload["params"] = params
            await self._ws.send(json.dumps(payload))
            while True:
                raw = await asyncio.wait_for(self._ws.recv(), timeout=timeout)
                message = json.loads(raw)
                if message.get("id") != message_id:
                    continue
                if "error" in message:
                    raise RuntimeError(f"{method} 失败：{message['error']}")
                return message.get("result", {})

    # ---------------------------------------------------------------- 页面
    async def goto(self, url: str, *, timeout: float = 45.0) -> None:
        await self.send("Page.navigate", {"url": url}, timeout=timeout)
        await self.wait_for(
            "document.readyState === 'complete' || document.readyState === 'interactive'",
            timeout=timeout,
        )

    async def evaluate(self, expression: str, *, timeout: float = 30.0) -> Any:
        result = await self.send(
            "Runtime.evaluate",
            {
                "expression": f"(async () => {{ {expression} }})()",
                "awaitPromise": True,
                "returnByValue": True,
            },
            timeout=timeout,
        )
        if result.get("exceptionDetails"):
            raise RuntimeError(f"页面内异常：{result['exceptionDetails'].get('text')}")
        return result.get("result", {}).get("value")

    async def wait_for(self, expression: str, *, timeout: float = 30.0, interval: float = 0.2) -> Any:
        """轮询一个返回真值的表达式。超时抛错，不静默返回。"""

        deadline = asyncio.get_event_loop().time() + timeout
        last: Any = None
        while asyncio.get_event_loop().time() < deadline:
            try:
                last = await self.evaluate(f"return Boolean({expression});", timeout=10)
            except Exception as error:  # noqa: BLE001 - 页面重载期间会短暂取不到
                last = str(error)
            if last is True:
                return True
            await asyncio.sleep(interval)
        raise TimeoutError(f"等待超时：{expression}（最后取值 {last!r}）")

    async def wait_for_text(self, text: str, *, timeout: float = 30.0) -> None:
        await self.wait_for(
            f"document.body && document.body.innerText.includes({json.dumps(text)})",
            timeout=timeout,
        )

    async def body_text(self) -> str:
        return await self.evaluate("return document.body ? document.body.innerText : '';") or ""

    async def url(self) -> str:
        return await self.evaluate("return location.href;") or ""

    # ---------------------------------------------------------------- 输入
    async def _box(self, selector: str) -> dict[str, float]:
        box = await self.evaluate(
            f"""
            const el = document.querySelector({json.dumps(selector)});
            if (!el) return null;
            el.scrollIntoView({{block: 'center'}});
            const r = el.getBoundingClientRect();
            if (r.width === 0 || r.height === 0) return null;
            return {{x: r.left + r.width / 2, y: r.top + r.height / 2}};
            """
        )
        if not box:
            raise RuntimeError(f"元素不可见或不存在：{selector}")
        return box

    async def click(self, selector: str, *, timeout: float = 20.0) -> None:
        """真实鼠标事件点击（按下 + 抬起），不是 `element.click()`。"""

        box = await self._box(selector)
        for event_type in ("mousePressed", "mouseReleased"):
            await self.send(
                "Input.dispatchMouseEvent",
                {
                    "type": event_type,
                    "x": box["x"],
                    "y": box["y"],
                    "button": "left",
                    "buttons": 1 if event_type == "mousePressed" else 0,
                    "clickCount": 1,
                },
                timeout=timeout,
            )
            await asyncio.sleep(0.05)

    async def click_text(self, text: str, *, tag: str = "button", timeout: float = 20.0) -> None:
        """按可见文本点击某个标签（在真实 DOM 里找同一个节点再发鼠标事件）。"""

        box = await self.evaluate(
            f"""
            const nodes = Array.from(document.querySelectorAll({json.dumps(tag)}));
            const target = nodes.find((n) => (n.innerText || '').trim().includes({json.dumps(text)}));
            if (!target) return null;
            target.scrollIntoView({{block: 'center'}});
            const r = target.getBoundingClientRect();
            if (r.width === 0 || r.height === 0) return null;
            return {{x: r.left + r.width / 2, y: r.top + r.height / 2}};
            """
        )
        if not box:
            raise RuntimeError(f"找不到或不可见：<{tag}> 内含 {text!r}")
        for event_type in ("mousePressed", "mouseReleased"):
            await self.send(
                "Input.dispatchMouseEvent",
                {
                    "type": event_type,
                    "x": box["x"],
                    "y": box["y"],
                    "button": "left",
                    "buttons": 1 if event_type == "mousePressed" else 0,
                    "clickCount": 1,
                },
                timeout=timeout,
            )
            await asyncio.sleep(0.05)

    async def type_into(self, selector: str, text: str, *, timeout: float = 20.0) -> None:
        await self.click(selector, timeout=timeout)
        await self.evaluate(
            f"""
            const el = document.querySelector({json.dumps(selector)});
            el.focus();
            return true;
            """
        )
        await self.send("Input.insertText", {"text": text}, timeout=timeout)
        await asyncio.sleep(0.1)

    async def set_file(self, selector: str, path: Path, *, timeout: float = 30.0) -> None:
        """真实文件上传：`DOM.setFileInputFiles` 把路径交给 input[type=file]。"""

        document = await self.send("DOM.getDocument", {"depth": -1}, timeout=timeout)
        node = await self.send(
            "DOM.querySelector",
            {"nodeId": document["root"]["nodeId"], "selector": selector},
            timeout=timeout,
        )
        node_id = node.get("nodeId")
        if not node_id:
            raise RuntimeError(f"找不到文件输入：{selector}")
        await self.send(
            "DOM.setFileInputFiles",
            {"files": [str(path)], "nodeId": node_id},
            timeout=timeout,
        )

    # ---------------------------------------------------------------- 取证
    async def screenshot(self, path: Path, *, timeout: float = 20.0) -> bool:
        """截图取证。**失败只返回 False**：本机 2 倍缩放的截图会永久挂起。"""

        try:
            result = await self.send(
                "Page.captureScreenshot", {"format": "png"}, timeout=timeout
            )
        except Exception:  # noqa: BLE001 - 取证失败不影响用例判定
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(result["data"]))
        return True


class SyncPage:
    """`CdpPage` 的**同步**外观：让 pytest 用例不必变成 async。

    为什么需要：本仓库**没有** pytest-asyncio 的配置（`asyncio_mode`），
    既有的 DB 用例也都是同步的。为了不引入一份新的 pytest 配置（那属于
    测试基建的范围变更），这里用私有事件循环把 CDP 的协程桥接成同步调用。

    只在一个线程里跑，因此事件循环不会跨线程使用。
    """

    def __init__(self, page: CdpPage) -> None:
        self._page = page
        self._loop = asyncio.new_event_loop()

    @property
    def raw(self) -> CdpPage:
        return self._page

    def _run(self, coro: Any, *, timeout: float) -> Any:
        return self._loop.run_until_complete(asyncio.wait_for(coro, timeout=timeout))

    def __enter__(self) -> "SyncPage":
        self._run(self._page.__aenter__(), timeout=30.0)
        return self

    def __exit__(self, *exc: object) -> None:
        try:
            self._run(self._page.__aexit__(*exc), timeout=20.0)
        finally:
            self._loop.close()

    #: 独立浏览器上下文 id（只有 `Chrome.attach_page` 会写它）
    _browser_context_id: str | None = None

    def dispose(self) -> None:
        """一次性收尾：关连接 → 关所属独立上下文 → 关事件循环。

        独立上下文的页面不能走普通的 `with` 收尾：`__exit__` 会把循环关掉，之后就再也
        发不出 `Target.disposeBrowserContext` 了（实测踩到）。这里自己安排顺序，并且
        **幂等**、**不抛异常**（清理失败不该盖住真正的断言失败）。
        """

        try:
            if self._browser_context_id:
                self._run(
                    self._page.send(
                        "Target.disposeBrowserContext",
                        {"browserContextId": self._browser_context_id},
                        timeout=20.0,
                    ),
                    timeout=40.0,
                )
        except Exception:  # noqa: BLE001 - 清理失败不影响断言
            pass
        try:
            self._run(self._page.__aexit__(None, None, None), timeout=20.0)
        except Exception:  # noqa: BLE001
            pass
        try:
            self._loop.close()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------ 同步转发
    def goto(self, url: str, *, timeout: float = 45.0) -> None:
        self._run(self._page.goto(url, timeout=timeout), timeout=timeout + 20)

    def evaluate(self, expression: str, *, timeout: float = 30.0) -> Any:
        return self._run(self._page.evaluate(expression, timeout=timeout), timeout=timeout + 20)

    def wait_for(
        self,
        expression: str,
        *,
        timeout: float = 30.0,
        interval: float = 0.2,
        raw: bool = False,
    ) -> bool:
        """轮询到表达式为真。

        `raw=False`（默认）时表达式是**布尔片段**（例如
        `document.body.innerText.includes('x')`）；`raw=True` 时传入的是
        **完整表达式**（自己写 `return ...;`），适合多语句判断。
        """

        if raw:
            deadline = time.monotonic() + timeout
            last: Any = None
            while time.monotonic() < deadline:
                try:
                    last = self.evaluate(expression, timeout=10)
                except Exception as error:  # noqa: BLE001 - 页面重载期间会短暂取不到
                    last = str(error)
                if last is True:
                    return True
                time.sleep(interval)
            raise TimeoutError(f"等待超时（raw）：{expression}（最后取值 {last!r}）")

        return bool(
            self._run(
                self._page.wait_for(expression, timeout=timeout, interval=interval),
                timeout=timeout + 25,
            )
        )

    def wait_for_text(self, text: str, *, timeout: float = 30.0) -> None:
        self._run(self._page.wait_for_text(text, timeout=timeout), timeout=timeout + 25)

    def body_text(self) -> str:
        return self._run(self._page.body_text(), timeout=30.0) or ""

    def url(self) -> str:
        return self._run(self._page.url(), timeout=30.0) or ""

    def click(self, selector: str, *, timeout: float = 20.0) -> None:
        self._run(self._page.click(selector, timeout=timeout), timeout=timeout + 15)

    def wait_visible(self, selector: str, *, timeout: float = 20.0) -> None:
        """等到元素存在、有尺寸且**位置不再变化**（用于带入场动画的弹层）。

        `click`/`click_text` 走的是真实鼠标坐标：元素还在位移时按下的按键会落到
        别的位置上，而弹层的 `onMouseDown` 恰好会「点外部关闭」——表现为
        「点了按钮却什么都没发生」。这里要求连续两次读到的 rect 完全一致。
        """

        self.wait_for(
            "const el = document.querySelector(%s);"
            " if (!el) return false;"
            " const r = el.getBoundingClientRect();"
            " if (r.width === 0 || r.height === 0) return false;"
            " const key = [r.left, r.top, r.width, r.height].join(',');"
            " const previous = window.__eng10Rect;"
            " window.__eng10Rect = key;"
            " return previous === key;"
            % json.dumps(selector),
            timeout=timeout,
            raw=True,
        )

    def click_text_unique(self, text: str, *, tag: str = "button", timeout: float = 25.0) -> None:
        """按可见文本点击，但**要求页面上只有一个可见匹配**。

        用于页面里同一段文案出现多次的场景（例如上传统计区与提交按钮都含「开始生成」，
        或分享面板的禁用按钮正好也含「提交反馈」的某一段）。多匹配时**报错而不是猜**：
        猜错会点到禁用按钮上，表现为「点了没反应」，非常难查（实测踩到）。

        比较用的是「去掉空白后的完全相等或以此开头」，避免把长段落当成目标。
        """

        box = self.evaluate(
            "const nodes = Array.from(document.querySelectorAll(%s));"
            " const wanted = %s;"
            " const hits = nodes.filter(function (n) {"
            "   const text = (n.innerText || '').replace(/\\s+/g, ' ').trim();"
            "   const r = n.getBoundingClientRect();"
            "   return r.width > 0 && r.height > 0"
            "     && (text === wanted || text.startsWith(wanted + ' ')"
            "         || text.startsWith(wanted + '\\n'));"
            " });"
            " if (hits.length !== 1) {"
            "   return {count: hits.length, texts: hits.map(function (n) {"
            "     return (n.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 40);"
            "   })};"
            " }"
            " const target = hits[0];"
            " target.scrollIntoView({block: 'center'});"
            " const rect = target.getBoundingClientRect();"
            " return {x: rect.left + rect.width / 2, y: rect.top + rect.height / 2,"
            "         count: 1, texts: []};"
            % (json.dumps(tag), json.dumps(text))
        )
        if not box or box.get("count") != 1:
            raise RuntimeError(
                f"期望唯一可见的 <{tag}> 文案 {text!r}，实际匹配 {box and box.get('count')} 个："
                f"{box and box.get('texts')}"
            )
        for event_type in ("mousePressed", "mouseReleased"):
            self._run(
                self._page.send(
                    "Input.dispatchMouseEvent",
                    {
                        "type": event_type,
                        "x": box["x"],
                        "y": box["y"],
                        "button": "left",
                        "buttons": 1 if event_type == "mousePressed" else 0,
                        "clickCount": 1,
                    },
                    timeout=timeout,
                ),
                timeout=timeout + 15,
            )
            time.sleep(0.05)

    def click_in_panel(
        self, panel_text: str, button_text: str, *, timeout: float = 25.0
    ) -> None:
        """在**包含 `panel_text` 的那个 `section.panel`** 里按文案点按钮。

        比全局按文本找更稳：页面上同名按钮可能属于不同面板（分享/反馈）。
        """

        box = self.evaluate(
            "const panels = Array.from(document.querySelectorAll('section.panel'));"
            " const panel = panels.find(function (p) {"
            "   return (p.innerText || '').includes(%s);"
            " });"
            " if (!panel) return {count: 0, texts: ['no panel']};"
            " const nodes = Array.from(panel.querySelectorAll('button'));"
            " const hits = nodes.filter(function (n) {"
            "   const text = (n.innerText || '').replace(/\\s+/g, ' ').trim();"
            "   const r = n.getBoundingClientRect();"
            "   return r.width > 0 && r.height > 0 && text.startsWith(%s);"
            " });"
            " if (hits.length !== 1) {"
            "   return {count: hits.length, texts: hits.map(function (n) {"
            "     return (n.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 40);"
            "   })};"
            " }"
            " const target = hits[0];"
            " target.scrollIntoView({block: 'center'});"
            " const rect = target.getBoundingClientRect();"
            " return {x: rect.left + rect.width / 2, y: rect.top + rect.height / 2,"
            "         count: 1, texts: [], disabled: target.disabled};"
            % (json.dumps(panel_text), json.dumps(button_text))
        )
        if not box or box.get("count") != 1:
            raise RuntimeError(
                f"面板「{panel_text}」里期望唯一按钮 {button_text!r}，实际 "
                f"{box and box.get('count')} 个：{box and box.get('texts')}"
            )
        for event_type in ("mousePressed", "mouseReleased"):
            self._run(
                self._page.send(
                    "Input.dispatchMouseEvent",
                    {
                        "type": event_type,
                        "x": box["x"],
                        "y": box["y"],
                        "button": "left",
                        "buttons": 1 if event_type == "mousePressed" else 0,
                        "clickCount": 1,
                    },
                    timeout=timeout,
                ),
                timeout=timeout + 15,
            )
            time.sleep(0.05)

    def click_text_stable(
        self, text: str, *, tag: str = "button", timeout: float = 25.0
    ) -> None:
        """等所在弹层几何稳定后再按文本点击（不再有「点到遮罩」的竞态）。"""

        self.wait_visible("[role=dialog]", timeout=timeout)
        self.click_text(text, tag=tag, timeout=timeout)

    def click_in_dialog(self, text: str, *, timeout: float = 25.0) -> None:
        """在 `[role=dialog]` **内部**按可见文本点击。

        为什么必须限定作用域：页面上的 `Composer` 在上传区未展开时，会把
        `EntryPanel` 的「兑换邀请码」按钮包进一个 `<button class="drop">` 里
        （浏览器解析出的 DOM 里它成了嵌套按钮的文本）。**全局**按文本找 `<button>`
        会先命中那个不在弹层里、且被禁用的按钮，于是鼠标事件打在弹层的遮罩上——
        表现为「按钮明明可点，点了却毫无反应」。用 `[role=dialog]` 收窄后，
        命中的一定是弹层里的那个真按钮。
        """

        self.wait_visible("[role=dialog]", timeout=timeout)
        box = self.evaluate(
            "const root = document.querySelector('[role=dialog]');"
            " if (!root) return null;"
            " const target = Array.from(root.querySelectorAll('button'))"
            "   .find(function (b) {"
            "     return (b.innerText || '').trim().includes(%s) && !b.disabled;"
            "   });"
            " if (!target) return null;"
            " target.scrollIntoView({block: 'center'});"
            " const r = target.getBoundingClientRect();"
            " if (r.width === 0 || r.height === 0) return null;"
            " return {x: r.left + r.width / 2, y: r.top + r.height / 2,"
            "         text: target.innerText.trim()};"
            % json.dumps(text)
        )
        if not box:
            raise RuntimeError(f"弹层里找不到可点击的按钮：{text!r}")
        for event_type in ("mousePressed", "mouseReleased"):
            self._run(
                self._page.send(
                    "Input.dispatchMouseEvent",
                    {
                        "type": event_type,
                        "x": box["x"],
                        "y": box["y"],
                        "button": "left",
                        "buttons": 1 if event_type == "mousePressed" else 0,
                        "clickCount": 1,
                    },
                    timeout=timeout,
                ),
                timeout=timeout + 15,
            )
            time.sleep(0.05)

    def type_in_dialog(self, selector: str, text: str, *, timeout: float = 25.0) -> None:
        """在弹层内部输入并核对（作用域限定，理由同 `click_in_dialog`）。"""

        scoped = f"[role=dialog] {selector}"
        self.wait_visible(scoped, timeout=timeout)
        self.type_into_checked(scoped, text, timeout=timeout)

    def type_into_checked(self, selector: str, text: str, *, timeout: float = 25.0) -> None:
        """输入并**核对真的进去了**：`Input.insertText` 依赖焦点，失败必须看得见。"""

        self.wait_visible(selector, timeout=timeout)
        self.type_into(selector, text, timeout=timeout)
        value = self.evaluate(
            "const el = document.querySelector(%s); return el ? el.value : null;"
            % json.dumps(selector)
        )
        if value != text:
            # 第二次机会：弹层刚出现时第一次聚焦可能丢失，这里重新点一次再插字
            self.type_into(selector, text, timeout=timeout)
            value = self.evaluate(
                "const el = document.querySelector(%s); return el ? el.value : null;"
                % json.dumps(selector)
            )
        if value != text:
            raise RuntimeError(f"输入没有生效：{selector} 期望 {text!r}，实际 {value!r}")

    def click_text(self, text: str, *, tag: str = "button", timeout: float = 20.0) -> None:
        self._run(self._page.click_text(text, tag=tag, timeout=timeout), timeout=timeout + 15)

    def type_into(self, selector: str, text: str, *, timeout: float = 20.0) -> None:
        self._run(self._page.type_into(selector, text, timeout=timeout), timeout=timeout + 15)

    def set_file(self, selector: str, path: Path, *, timeout: float = 30.0) -> None:
        self._run(self._page.set_file(selector, path, timeout=timeout), timeout=timeout + 20)

    def screenshot(self, path: Path, *, timeout: float = 20.0) -> bool:
        return bool(
            self._run(self._page.screenshot(path, timeout=timeout), timeout=timeout + 15)
        )

    def set_viewport(self, width: int, height: int, *, scale: float = 1.0) -> None:
        """真实设备指标覆盖（移动端验收用）。

        `deviceScaleFactor` 固定 1：本机 dsf>1 时 `Page.captureScreenshot` 会永久挂起
        （见模块头的截图说明），但**布局**仍然按 CSS 像素走 `width/height`，
        因此窄屏断点验证不受影响。
        """

        width = int(width)
        height = int(height)
        self._run(
            self._page.send(
                "Emulation.setDeviceMetricsOverride",
                {
                    "width": width,
                    "height": height,
                    "deviceScaleFactor": float(scale),
                    "mobile": width < 768,
                },
                timeout=20.0,
            ),
            timeout=40.0,
        )
        self._run(
            self._page.send(
                "Emulation.setTouchEmulationEnabled",
                {"enabled": width < 768, "maxTouchPoints": 5},
                timeout=20.0,
            ),
            timeout=40.0,
        )

    def viewport_size(self) -> dict[str, int]:
        return self.evaluate(
            "return {w: window.innerWidth, h: window.innerHeight,"
            " dw: document.documentElement.clientWidth};"
        )

    # ------------------------------------------------------------ 会话/导航
    def new_incognito_page(self, chrome: Any, url: str = "about:blank") -> "SyncPage":
        """在**独立浏览器上下文**里开一个页面（真正的「另一个访客」）。

        为什么不能只 `Network.clearBrowserCookies()`：Cookie 是**浏览器级**状态，
        清一次会把同一浏览器里其他标签页的会话也清掉（实测踩到：匿名分享页验证之后，
        主标签页提交反馈直接 401）。独立上下文才有隔离的 Cookie 存储。

        返回的页面**尚未进入**（未 `__enter__`），调用方需要自己 `__enter__()`；
        结束时用 `page.dispose()` 一次性收尾。
        """

        result = self._run(
            self._page.send("Target.createBrowserContext", {}, timeout=20.0), timeout=40.0
        )
        context_id = result["browserContextId"]
        target = self._run(
            self._page.send(
                "Target.createTarget",
                {"url": url, "browserContextId": context_id},
                timeout=20.0,
            ),
            timeout=40.0,
        )
        page = chrome.attach_page(target["targetId"], browser_context_id=context_id)
        return page

    def set_cookie(self, name: str, value: str, *, path: str = "/") -> None:
        """用 CDP 装一枚 `HttpOnly + Secure + SameSite=Lax` 的 Cookie。

        会话 Cookie 就是这三属性（契约第 3 节）。`document.cookie` 写不进
        `HttpOnly`，且非 HTTPS 来源上还写不进 `Secure`，因此必须走 CDP。
        浏览器把 loopback 当可信来源，所以 `secure=True` 在
        `http://127.0.0.1` 上依然会被正常发送。
        """

        result = self._run(
            self._page.send(
                "Network.setCookie",
                {
                    "name": name,
                    "value": value,
                    "domain": "127.0.0.1",
                    "path": path,
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "Lax",
                },
                timeout=20.0,
            ),
            timeout=40.0,
        )
        if not result.get("success", False):
            raise RuntimeError(f"CDP 未能写入 Cookie {name}：{result}")

    def cookies(self) -> list[dict[str, Any]]:
        result = self._run(
            self._page.send("Network.getCookies", {}, timeout=20.0), timeout=40.0
        )
        return list(result.get("cookies", []))

    def clear_cookies(self) -> None:
        self._run(self._page.send("Network.clearBrowserCookies", {}, timeout=20.0), timeout=40.0)

    def reload(self, *, timeout: float = 45.0) -> None:
        self._run(self._page.send("Page.reload", {}, timeout=timeout), timeout=timeout + 20)
        self.wait_for("document.readyState === 'complete'", timeout=timeout)


def open_page(chrome: "Chrome", url: str = "about:blank") -> SyncPage:
    """在给定 Chrome 上开一个新标签并返回同步外观（配合 `with` 使用）。"""

    return SyncPage(chrome.new_page(url))
