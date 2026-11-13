# -*- coding: utf-8 -*-
"""
【2026-11-13 审查目标 3/4】浏览器层验证：剪贴板粘贴媒体 + agent 管理面板。

【取证纪律】
1. **真 Chromium**（Playwright），加载**真实的** web/ 静态文件；
2. 剪贴板事件用**真实 ClipboardEvent + DataTransfer** 构造（浏览器里
   `new ClipboardEvent("paste", {clipboardData: dt})` 是标准做法），
   但**上传请求打到真实 HTTP 端点**（起一个真的 server）；
3. 断言打在**真实渲染出的 DOM** 上；
4. 不用「前端自造 mock 断言自己」。

运行：python3 -m pytest tests/test_agent_paste_browser.py -v
"""
import base64
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zlib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except Exception:                                   # pragma: no cover
    sync_playwright = None
    HAS_PLAYWRIGHT = False


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _tiny_png() -> bytes:
    """造一张 1x1 的真 PNG（用 zlib 手写，避免依赖 PIL）。"""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (len(data).to_bytes(4, "big") + tag + data
                + zlib.crc32(tag + data).to_bytes(4, "big"))

    raw = b"\x00\xff\x00\x00"          # 1 像素，filter=0，RGB=绿
    ihdr = (1).to_bytes(4, "big") + (1).to_bytes(4, "big") + bytes([8, 2, 0, 0, 0])
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


# ------------------------------------------------------------------ 静态契约


class TestPasteStaticContract(unittest.TestCase):
    """静态契约：粘贴代码在、绑定在、hint 在。"""

    def setUp(self):
        self.agent_js = (REPO / "web" / "agent.js").read_text(encoding="utf-8")
        self.index_html = (REPO / "web" / "index.html").read_text(encoding="utf-8")

    def test_paste_handler_exists(self):
        self.assertIn("function handlePaste", self.agent_js,
                      "缺少 handlePaste（剪贴板粘贴处理）")

    def test_paste_handler_is_bound_to_input(self):
        self.assertIn('addEventListener("paste", handlePaste)', self.agent_js,
                      "paste 事件没绑到输入框上")

    def test_paste_handles_media_files_not_only_text(self):
        """必须处理**文件**条目（媒体），不能只处理纯文本。"""
        self.assertIn("getAsFile", self.agent_js,
                      "粘贴处理没取 clipboard 文件条目（getAsFile）")
        self.assertIn("clipboardData", self.agent_js)
        self.assertIn("data:", self.agent_js,
                      "粘贴处理没有 dataURL 退路（图片字符串）")

    def test_text_paste_is_not_prevented(self):
        """纯文本粘贴必须走浏览器默认行为（不能被 preventDefault 吃掉）。"""
        # handlePaste 里只有「拿到媒体文件」才 preventDefault
        idx = self.agent_js.index("function handlePaste")
        body = self.agent_js[idx:idx + 900]
        self.assertIn("if (!files.length) return;", body,
                      "纯文本粘贴被拦截了（没有提前 return）")

    def test_hint_mentions_paste(self):
        self.assertIn("Ctrl+V", self.index_html,
                      "界面没告诉用户可以粘贴媒体")


class TestAgentManageStaticContract(unittest.TestCase):
    """静态契约：agent 管理面板文件 / 入口 / 接口都在。"""

    def setUp(self):
        self.index_html = (REPO / "web" / "index.html").read_text(encoding="utf-8")
        self.manage_js = (REPO / "web" / "agent-manage.js").read_text(
            encoding="utf-8")
        self.server_py = (REPO / "server.py").read_text(encoding="utf-8")

    def test_manage_script_is_loaded(self):
        self.assertIn("agent-manage.js", self.index_html)

    def test_manage_button_and_modal_exist(self):
        self.assertIn('id="agManageBtn"', self.index_html)
        self.assertIn('id="agManageModal"', self.index_html)
        self.assertIn('id="agManageBody"', self.index_html)

    def test_manage_script_talks_to_real_endpoints(self):
        for path in ("/api/agent/settings", "/api/agent/settings/test-engine"):
            self.assertIn(path, self.manage_js,
                          f"agent 管理面板没调用 {path}")

    def test_server_exposes_settings_endpoints(self):
        self.assertIn('add_get("/api/agent/settings"', self.server_py)
        self.assertIn('add_put("/api/agent/settings"', self.server_py)
        self.assertIn('add_post("/api/agent/settings/test-engine"', self.server_py)

    def test_manage_script_has_capability_and_engine_sections(self):
        self.assertIn("data-cap=", self.manage_js, "缺少能力开关渲染")
        self.assertIn("data-eng-url=", self.manage_js, "缺少搜索引擎 URL 配置项")
        self.assertIn("data-eng-test=", self.manage_js, "缺少连通性自检按钮")


# ------------------------------------------------------------------ 真浏览器


@unittest.skipUnless(HAS_PLAYWRIGHT, "缺少 playwright")
class TestPasteInRealBrowser(unittest.TestCase):
    """真 Chromium + 真 HTTP 端点：粘贴一张图 -> 真的入库 -> 界面出现附件卡片。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="agent-paste-browser-"))
        cls.port = _free_port()
        cls.data_dir = cls.tmp / "data"
        cls.data_dir.mkdir(parents=True, exist_ok=True)
        cls.env = dict(os.environ)
        cls.env.update({
            "H3STUDIO_PORT": str(cls.port),
            "H3STUDIO_DATA_DIR": str(cls.data_dir),
            "AGENT_SESSION_DB": str(cls.data_dir / "agent_sessions.db"),
            "AGENT_MEMORY_DB": str(cls.data_dir / "agent_memory.db"),
            "PYTHONPATH": str(REPO),
        })
        cls.server = subprocess.Popen(
            ["/home/ubuntu/comfyui/venv/bin/python", "server.py"],
            cwd=str(REPO), env=cls.env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        # 等端口起来
        deadline = time.time() + 90
        ready = False
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", cls.port), timeout=1):
                    ready = True
                    break
            except OSError:
                if cls.server.poll() is not None:
                    break
                time.sleep(0.5)
        if not ready:
            out = cls.server.stdout.read().decode("utf-8", "replace") if cls.server.stdout else ""
            cls.server.kill()
            raise RuntimeError("服务端起不来：" + out[-2000:])
        cls._seed_provider()
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(args=["--no-sandbox"])

    @classmethod
    def _seed_provider(cls):
        """
        预置一个**真实可用**的多模态 Provider。

        【为什么必须预置】图片入库要跑多模态分析（分析素材需要多模态模型）。
        测试实例的 jobs.db 是**独立空库**（H3STUDIO_DATA_DIR 指到了临时目录），
        没有 Provider 的话上传会报「还没有可用的本地模型 Provider」——
        那是**环境缺配置**，不是粘贴功能坏了。

        这里指向本机真实跑着的 Qwen 多模态桥（127.0.0.1:5000）。
        """
        import urllib.request
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:5000/v1/models", timeout=5) as resp:
                models = json.loads(resp.read().decode())
            model = models["data"][0]["id"]
        except Exception:
            raise unittest.SkipTest(
                "本机 127.0.0.1:5000 的多模态桥不可用 —— 没法跑真实的图片入库")
        db_path = cls.data_dir / "jobs.db"
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS model_providers (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, type TEXT NOT NULL,
                base_url TEXT NOT NULL, llm_base_url TEXT DEFAULT '',
                use_proxy INTEGER DEFAULT 0, proxy_base_url TEXT DEFAULT '',
                model TEXT NOT NULL, api_key TEXT DEFAULT '',
                multimodal INTEGER DEFAULT 0, enabled INTEGER DEFAULT 1,
                created_at REAL);
            CREATE TABLE IF NOT EXISTS model_provider_prefs (
                key TEXT PRIMARY KEY, value TEXT);
            """
        )
        conn.execute(
            "INSERT OR REPLACE INTO model_providers VALUES "
            "('pasteprov','本机多模态','openai','http://127.0.0.1:5000','',0,'',"
            f"'{model}','',1,1,0)")
        conn.execute("INSERT OR REPLACE INTO model_provider_prefs "
                     "VALUES ('default','pasteprov')")
        conn.execute("INSERT OR REPLACE INTO model_provider_prefs "
                     "VALUES ('default_multimodal','pasteprov')")
        conn.commit()
        conn.close()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.browser.close()
            cls.playwright.stop()
        finally:
            cls.server.terminate()
            try:
                cls.server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                cls.server.kill()
            shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_paste_image_creates_real_attachment(self):
        page = self.browser.new_page()
        page.goto(f"http://127.0.0.1:{self.port}/", wait_until="domcontentloaded")
        # 打开 Agent 页（管理面板不需要，聊天页才需要）
        page.evaluate("() => { if (window.AgentUI) window.AgentUI.show && window.AgentUI.show(); }")
        page.wait_for_timeout(500)

        png_b64 = base64.b64encode(_tiny_png()).decode()
        # 在**真实浏览器**里构造 ClipboardEvent + DataTransfer，
        # 塞一个 image/png 的**文件条目**（模拟用户从别的应用复制一张图）。
        page.evaluate(
            """(b64) => {
                const bin = atob(b64);
                const bytes = new Uint8Array(bin.length);
                for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
                const file = new File([bytes], "clipboard_pasted.png",
                                      { type: "image/png" });
                const dt = new DataTransfer();
                dt.items.add(file);
                const input = document.querySelector("#agInput");
                input.focus();
                const evt = new ClipboardEvent("paste", {
                    clipboardData: dt, bubbles: true, cancelable: true,
                });
                input.dispatchEvent(evt);
            }""", png_b64)

        # 上传走真实 HTTP（素材库入库 + 多模态分析）—— 等它出现附件卡片
        #
        # 【选择器口径】附件卡片的真实 class 是 .ag-attach-thumb
        # （web/agent.js 的 renderAttachments），里面带 .ag-attach-remove 按钮。
        deadline = time.time() + 240
        found = False
        while time.time() < deadline:
            count = page.evaluate(
                "() => document.querySelectorAll('#agAttachments .ag-attach-thumb')"
                ".length")
            if count and count > 0:
                found = True
                break
            page.wait_for_timeout(1000)
        if not found:
            err = page.evaluate(
                "() => (document.querySelector('#agAttachHint')||{}).textContent || ''")
            self.fail(f"粘贴图片后没有出现附件卡片（hint={err!r}）")

        # 附件卡片必须带 asset_id（说明真的入库了）
        page.wait_for_timeout(500)
        asset_ids = page.evaluate(
            """() => [...document.querySelectorAll(
                    '#agAttachments .ag-attach-thumb')]
                 .map(n => n.getAttribute('data-asset-id')).filter(Boolean)""")
        self.assertTrue(asset_ids, "附件卡片没有 asset_id —— 说明没真的入库")
        self.assertGreaterEqual(len(asset_ids), 1)

        # 服务端真的收到了这张图（素材库里能查到）
        import urllib.request
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/api/assets?page=1&page_size=20",
                timeout=20) as resp:
            data = json.loads(resp.read().decode())
        items = data.get("assets") or data.get("items") or []
        names = [str(a.get("name") or "") for a in items]
        self.assertTrue(any("clipboard_pasted" in n for n in names),
                        f"素材库里没有粘贴进来的那张图：{names}")
        page.close()


if __name__ == "__main__":
    unittest.main()
