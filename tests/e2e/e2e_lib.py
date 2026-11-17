# -*- coding: utf-8 -*-
"""
H3 Studio 真实浏览器 E2E 测试集 —— 基础设施。

【这套测试的定位】
它**不是**接口/mock 测试：每一条用例都用**真实 Chromium**（Playwright）
打开工作台页面，用**真实的点击 / 输入 / 拖拽**操作界面，然后断言
「界面 → 后端 → ComfyUI → 结果回到界面」这条链路是通的。

【为什么要有这一层】
仓库里原有的 tests/ 是**契约测试**（解析 HTML/JS 文本、断言 DOM 结构
「应该」长什么样）+ 若干手写 verify_*.py 走查脚本。它们都**没有**驱动
真实浏览器点一遍真实界面 —— 于是「界面能渲染但按钮点不动」「输入框
能输入但提交的是旧值」「卡片永远运行中」这类问题全都漏过去了。

【关键设计取舍】
1. **默认连线上 8190 部署**（用户真实在用的那套），不另起实例 ——
   E2E 要测的就是线上那套界面。起独立实例只在「需要干净数据」时用
   （--isolated），此时端口与 data_dir 都隔离，绝不碰线上数据。
2. **真浏览器是硬要求**：没有 playwright / chromium 就 skip 并明确报错，
   绝不静默降级成接口测试。
3. **每条用例都留证据**：截图 + 控制台日志 + 网络错误 + 步骤日志，
   落在 evidence/e2e/<run_id>/ 下。
4. **真实生成是可选的**：跑真模型要占 GPU（本机 4090D 常年满载），
   所以默认用「真浏览器 + 真实提交 + 真实后端」但把 ComfyUI 换成
   **受控的假 ComfyUI**（tests/fake_comfyui.py 已有的能力，本套测试
   自己再包一层）；需要真出图时用 --real-comfy 打开。
   两种模式都**真实点界面**，区别只在「ComfyUI 那头是真模型还是可控桩」。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent.parent
E2E_DIR = Path(__file__).resolve().parent
EVIDENCE_ROOT = REPO / "evidence" / "e2e"

#: 线上部署的默认地址（用户真实在用的那套）
DEFAULT_BASE_URL = os.environ.get("H3STUDIO_E2E_BASE", "http://127.0.0.1:8190")
#: 隔离实例的端口（--isolated 时用；与线上 8190 不冲突）
ISOLATED_PORT = int(os.environ.get("H3STUDIO_E2E_PORT", "8391"))
#: 假 ComfyUI 的端口（--fake-comfy 时用）
FAKE_COMFY_PORT = int(os.environ.get("H3STUDIO_E2E_FAKE_COMFY_PORT", "8399"))


# ------------------------------------------------------------------ 工具


def free_port() -> int:
    """让系统分配一个空闲端口（并发跑时避免撞端口）。"""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def http_json(url: str, timeout: float = 30.0) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def wait_http(url: str, timeout: float = 60.0, interval: float = 0.4) -> bool:
    """轮询直到 URL 返回 2xx。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                if resp.status < 500:
                    return True
        except urllib.error.HTTPError as exc:
            if exc.code < 500:
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def has_playwright() -> tuple[bool, str]:
    """检查 playwright 与 chromium 是否可用（不可用就明确报错，不静默跳过）。"""
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except Exception as exc:  # pragma: no cover - 环境缺依赖
        return False, f"playwright 未安装：{exc}"
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            exe = Path(p.chromium.executable_path)
            if not exe.exists():
                return False, f"chromium 未安装（期望 {exe}）"
        return True, str(exe)
    except Exception as exc:  # pragma: no cover
        return False, f"chromium 启动失败：{exc}"


# ------------------------------------------------------------------ 证据


@dataclass
class Evidence:
    """一条用例的证据收集器。"""

    run_id: str
    case_id: str
    root: Path
    steps: list[str] = field(default_factory=list)
    console: list[str] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)
    network_errors: list[str] = field(default_factory=list)
    screenshots: list[str] = field(default_factory=list)
    shots: int = 0

    def __post_init__(self) -> None:
        self.dir = self.root / self.case_id
        self.dir.mkdir(parents=True, exist_ok=True)

    def step(self, message: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {message}"
        self.steps.append(line)
        print(f"    {line}", flush=True)

    def screenshot(self, page, name: str) -> Path:
        self.shots += 1
        filename = f"{self.shots:02d}-{name}.png"
        path = self.dir / filename
        try:
            page.screenshot(path=str(path), full_page=False)
            self.screenshots.append(filename)
        except Exception as exc:  # pragma: no cover
            self.steps.append(f"[screenshot-failed] {name}: {exc}")
        return path

    def dump(self) -> Path:
        """把本条用例的证据落盘。"""
        payload = {
            "case_id": self.case_id,
            "run_id": self.run_id,
            "steps": self.steps,
            "console": self.console,
            "page_errors": self.page_errors,
            "network_errors": self.network_errors,
            "screenshots": self.screenshots,
        }
        path = self.dir / "evidence.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        (self.dir / "console.log").write_text(
            "\n".join(self.console + self.page_errors + self.network_errors),
            encoding="utf-8")
        return path


# ------------------------------------------------------------------ 服务


@dataclass
class StudioServer:
    """被测的 H3 Studio 服务实例。"""

    base_url: str
    process: subprocess.Popen | None = None
    data_dir: Path | None = None
    library_root: Path | None = None
    log_path: Path | None = None
    fake_comfy: bool = False
    fake_comfy_url: str = ""

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.process = None


def start_fake_comfy(port: int, root: Path) -> subprocess.Popen:
    """起一个**可控**的假 ComfyUI（tests/e2e/fake_comfyui_e2e.py）。

    它会真的产出媒体字节、真的落盘潜空间（接续链路必需），并支持注入
    「任务丢失」这类故障。
    """
    script = REPO / "tests" / "e2e" / "fake_comfyui_e2e.py"
    if not script.exists():
        raise RuntimeError(f"找不到假 ComfyUI 脚本：{script}")
    output_dir = root / "comfy-output"
    media_dir = root / "comfy-media"
    media_dir.mkdir(parents=True, exist_ok=True)
    # 视频素材（/view 吐出的 mp4 字节）
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=1280x736:duration=2",
         "-pix_fmt", "yuv420p", "-r", "24", str(media_dir / "out.mp4")],
        check=True, capture_output=True)
    proc = subprocess.Popen(
        [sys.executable, str(script), "--port", str(port),
         "--output-dir", str(output_dir), "--media-dir", str(media_dir)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    if not wait_http(f"http://127.0.0.1:{port}/queue", timeout=30):
        proc.terminate()
        raise RuntimeError(f"假 ComfyUI 启动失败（端口 {port}）")
    return proc


def make_library_root(tag: str) -> Path:
    """素材库根目录。

    ⚠ 必须落在**独立挂载点**上：h3studio/assets.py 的 check_disk_mounted
    会比对挂载点与父目录的 st_dev —— 相同就判定「磁盘没挂」，拒绝写入
    （那是为了防外接盘掉了把数据写满系统盘）。/dev/shm 是 tmpfs，
    与 / 的设备号不同，能通过该检查。
    """
    base = Path("/dev/shm") / f"h3s-e2e-{tag}-{os.getpid()}"
    base.mkdir(parents=True, exist_ok=True)
    return base


def start_studio(port: int, data_dir: Path, library_root: Path,
                 comfy_url: str) -> StudioServer:
    """起一个隔离的 H3 Studio 实例（独立端口 + 独立 data_dir + 独立素材库）。"""
    data_dir.mkdir(parents=True, exist_ok=True)
    library_root.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update({
        "H3STUDIO_PORT": str(port),
        "H3STUDIO_DATA_DIR": str(data_dir),
        "ASSET_LIBRARY_ROOT": str(library_root),
        "PYTHONPATH": str(REPO),
        # 不缓冲 stdout —— 否则进程被 terminate 时日志全丢，
        # 失败用例就看不到服务端真实原因了。
        "PYTHONUNBUFFERED": "1",
    })
    log_path = data_dir.parent / f"studio-{port}.log"
    proc = subprocess.Popen(
        [sys.executable, str(REPO / "server.py")],
        cwd=str(REPO), env=env,
        stdout=log_path.open("w", encoding="utf-8"),
        stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    if not wait_http(base + "/api/video-categories", timeout=90):
        proc.terminate()
        raise RuntimeError(f"Studio 实例启动失败（端口 {port}），日志：{log_path}")
    return StudioServer(base_url=base, process=proc, data_dir=data_dir,
                        library_root=library_root, log_path=log_path)


# ------------------------------------------------------------------ Provider 种子

#: 本机本地模型服务（Strata / llama.cpp 兼容 OpenAI 协议）。
#: 隔离实例默认没有 Provider -> /api/enhance 会 400「尚未配置可用的
#: 本地模型 Provider」—— 增强类用例就测不到真实链路。
#: 这个地址是**只读**的：测试只调用它，不改它的配置。
DEFAULT_LLM_BASE_URL = os.environ.get("H3STUDIO_E2E_LLM_URL", "http://127.0.0.1:5000")
DEFAULT_LLM_MODEL = os.environ.get(
    "H3STUDIO_E2E_LLM_MODEL", "qwen3.8-flash-next-iq3_xxs")


def seed_provider(base_url: str, llm_url: str = DEFAULT_LLM_BASE_URL,
                  model: str = DEFAULT_LLM_MODEL) -> dict:
    """给被测实例建一个本地模型 Provider（走真实 HTTP 接口）。

    幂等：已存在同名 Provider 就更新它。
    """
    try:
        with urllib.request.urlopen(base_url + "/api/providers", timeout=15) as r:
            existing = json.loads(r.read().decode()).get("providers") or []
    except Exception:
        existing = []
    payload = {
        "name": "E2E 本地模型",
        "type": "openai",
        "base_url": llm_url,
        "model": model,
        # 素材的「深度分析」要**读图** -> Provider 必须标记为多模态，
        # 否则服务端会明确拒绝：「未标记多模态能力，无法读图」。
        "multimodal": True,
        "enabled": True,
    }
    for item in existing:
        if item.get("name") == payload["name"]:
            req = urllib.request.Request(
                base_url + "/api/providers/" + item["id"],
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="PUT")
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
    req = urllib.request.Request(
        base_url + "/api/providers",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        created = json.loads(r.read().decode())
    # 设为默认（否则 resolve_default 找不到「默认」）
    provider_id = (created.get("provider") or created).get("id")
    if provider_id:
        req = urllib.request.Request(
            base_url + "/api/providers/" + provider_id + "/default",
            data=b"{}", headers={"Content-Type": "application/json"},
            method="POST")
        try:
            urllib.request.urlopen(req, timeout=15).read()
        except Exception:
            pass
    return created


# ------------------------------------------------------------------ 素材样例


def make_fixtures(target: Path, unique: bool = False) -> dict[str, Path]:
    """生成测试用的图片 / 音频 / 视频素材（真实文件，不是 mock）。

    【unique=True】每份素材的内容**互不相同**（带随机后缀）——
    必须这么做的原因：素材库按 sha256 **去重**（同内容复用原记录，
    见 h3studio/asset_api.py 的 dup 分支），而隔离实例是**整个 session
    共用**一个素材库 —— 所以同一条用例里上传两次同一份文件，第二次
    会被判「已存在，复用原素材」，标签也不会更新。
    带随机后缀后每次上传都是新素材，用例之间互不干扰。
    """
    from PIL import Image

    target.mkdir(parents=True, exist_ok=True)
    out: dict[str, Path] = {}
    suffix = f"-{os.getpid()}-{int(time.time() * 1000) % 100000}" if unique else ""

    img = target / f"e2e-sample{suffix}.png"
    # ⚠ 图片**内容**也必须随 unique 变 —— 素材库按 sha256 去重，
    #   只改文件名不改内容的话，第二次上传会被判「已存在，复用原素材」，
    #   标签也不会更新（实测：标签断言读到的是上一条用例的标签）。
    #   所以把 suffix 的哈希画进像素里，保证每份素材的内容都不同。
    if unique:
        seed = sum(ord(c) for c in suffix)
        pixels = bytes([
            (seed * 7 + i * 13) % 256 for i in range(96 * 96 * 3)
        ])
        Image.frombytes("RGB", (96, 96), pixels).save(img)
    else:
        Image.new("RGB", (96, 96), (220, 60, 60)).save(img)
    out["image"] = img

    wav = target / f"e2e-sample{suffix}.wav"
    freq = 440 + (sum(ord(c) for c in suffix) % 500) if unique else 440
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", f"sine=frequency={freq}:duration=1",
         "-ac", "1", "-ar", "44100", str(wav)],
        check=True, capture_output=True)
    out["audio"] = wav

    mp4 = target / f"e2e-sample{suffix}.mp4"
    # ⚠ 视频内容也要随 unique 变 —— 素材库按 sha256 去重，
    #   内容相同就会「复用原素材」（连文件名都是旧的）。
    #   用 hue 滤镜偏移色相（testsrc 源 + -vf hue=...），
    #   每份视频的字节都不一样。
    hue = (sum(ord(c) for c in suffix) % 360) if unique else 0
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=96x96:duration=1",
         "-vf", f"hue=h={hue}", "-pix_fmt", "yuv420p", str(mp4)],
        check=True, capture_output=True)
    out["video"] = mp4

    return out


# ------------------------------------------------------------------ 页面助手


class StudioPage:
    """页面级助手：把「人在界面上做的事」封装成语义化的动作。"""

    def __init__(self, page, evidence: Evidence, base_url: str):
        self.page = page
        self.ev = evidence
        self.base_url = base_url

    # -- 导航 ---------------------------------------------------------
    def open(self, path: str = "/") -> None:
        self.page.goto(self.base_url + path, wait_until="domcontentloaded",
                       timeout=60000)
        self.page.wait_for_timeout(2500)
        self.ev.step(f"打开页面 {path}")

    def goto_mode(self, mode: str) -> None:
        """按 data-mode 切创作模式（视频的两级导航先点类别）。"""
        if mode in ("t2v", "flf", "r2v", "longfilm"):
            category = mode
            head = self.page.query_selector(f'.nav-cat-head[data-category="{category}"]')
            if head is not None:
                head.click()
                self.page.wait_for_timeout(800)
        item = self.page.query_selector(f'.nav-item[data-mode="{mode}"]')
        if item is None:
            raise AssertionError(f"导航里找不到 mode={mode}")
        item.click()
        self.page.wait_for_timeout(900)
        self.ev.step(f"切到创作模式 {mode}")

    def goto_tab(self, tab: str) -> None:
        """切生成库的状态标签（全部 / 进行中 / 已完成 / 失败 …）。"""
        self.page.click(f'.jobs-tabs button[data-st="{tab}"]')
        self.page.wait_for_timeout(1500)
        self.ev.step(f"切生成库标签 -> {tab}")

    def goto_page(self, nav_id: str) -> None:
        """切到左侧导航的独立页面（素材库 / 内置 Agent / 系统页）。"""
        self.page.click(f"#{nav_id}")
        self.page.wait_for_timeout(1500)
        self.ev.step(f"切到页面 #{nav_id}")

    # -- 表单 ---------------------------------------------------------
    def fill(self, selector: str, value: str) -> None:
        self.page.fill(selector, value)
        self.ev.step(f"输入 {selector} = {value!r}")

    def type_and_commit(self, selector: str, value: str) -> None:
        """像真人一样逐字敲进去，然后失焦提交（触发 change 吸附逻辑）。"""
        self.page.click(selector)
        self.page.fill(selector, "")
        self.page.type(selector, value, delay=15)
        self.page.eval_on_selector(selector, "e => e.blur()")
        self.page.wait_for_timeout(400)
        self.ev.step(f"逐字输入 {selector} = {value!r} 并失焦")

    def click(self, selector: str, label: str | None = None) -> None:
        self.page.click(selector)
        self.page.wait_for_timeout(300)
        self.ev.step(f"点击 {label or selector}")

    def click_text(self, selector: str, text: str, label: str | None = None) -> None:
        self.page.locator(selector, has_text=text).first.click()
        self.page.wait_for_timeout(300)
        self.ev.step(f"点击 {label or f'{selector} 含 {text!r}'}")

    def select_option(self, selector: str, value: str) -> None:
        self.page.select_option(selector, value)
        self.page.wait_for_timeout(400)
        self.ev.step(f"选择 {selector} = {value!r}")

    def text(self, selector: str) -> str:
        return self.page.inner_text(selector)

    def value(self, selector: str) -> str:
        return self.page.eval_on_selector(selector, "e => e.value")

    def visible(self, selector: str) -> bool:
        return self.page.is_visible(selector)

    # -- 提交 ---------------------------------------------------------
    def submit_generation(self) -> None:
        """点「生成」，必要时过「未增强提示词」闸门。

        提交失败时把**服务端返回的原因**带进异常信息 —— 否则只能看到
        一个「400」，定位不到是哪个参数不合法。
        """
        errors: list[str] = []
        self.page.on("response", lambda r: (
            errors.append(f"HTTP {r.status}") if r.status >= 400 else None))
        self.page.click("#genBtn")
        self.page.wait_for_timeout(1200)
        if self.page.is_visible("#enhanceGateModal"):
            self.ev.step("命中「提示词尚未增强」闸门 -> 点确认生成")
            self.page.click("#enhanceGateConfirm")
            self.page.wait_for_timeout(1500)
        self.ev.step("点「生成」")
        if errors:
            self.ev.step(f"提交返回错误：{errors}")
            raise AssertionError(
                f"提交生成失败：{errors}；页面提示："
                f"{self.page.inner_text('body')[:300]}")

    def last_submitted_params(self) -> dict:
        """取最近一次 POST /api/jobs 提交的 params（从窗口里的钩子读）。"""
        return self.page.evaluate("() => window.__e2eLastSubmit || null") or {}

    def wait_for_job(self, timeout: float = 180.0) -> dict:
        """等最近一条任务跑到终态（done / failed / canceled）。"""
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            jobs = http_json(self.base_url + "/api/jobs?limit=5").get("jobs", [])
            if jobs:
                last = jobs[0]
                if last.get("status") in ("done", "failed", "canceled"):
                    return last
            time.sleep(2.0)
        raise AssertionError(f"任务在 {timeout}s 内没到终态（最后状态："
                             f"{(last or {}).get('status')}）")
