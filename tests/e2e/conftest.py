# -*- coding: utf-8 -*-
"""
E2E 测试集的 pytest 装置（fixtures）与配置钩子。

【为什么用 pytest 而不是自己写 runner】
- 需要「一条用例 = 一份独立证据」的粒度；
- 需要「失败不阻塞后续用例」；
- 需要与仓库现有 pytest 套件共存（`pytest tests/` 一把跑）。

【关键装置】
- ``studio``      —— 被测的 Studio 服务（线上 8190，或 --isolated 的独立实例）
- ``studio_page`` —— 真实 Chromium 页面 + 证据收集器
- ``evidence``    —— 本条用例的证据目录
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

E2E_DIR = Path(__file__).resolve().parent
REPO = E2E_DIR.parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(E2E_DIR))

import e2e_lib  # noqa: E402

def pytest_addoption(parser):
    """注册 E2E 参数。

    【为什么这里也注册一遍】pytest 只对「初始路径」的 conftest 调用
    ``pytest_addoption``；跑 ``pytest tests/`` 时仓库根 conftest.py 才是
    初始路径 —— 但跑 ``pytest tests/e2e/`` 时初始路径是 tests/e2e，
    根 conftest 不会生效。两处都注册（同名同定义，pytest 会去重），
    两种跑法都能用。
    """
    group = parser.getgroup("e2e")
    if group.name not in [g.name for g in parser._groups]:
        return
    for name, kwargs in _E2E_OPTIONS:
        try:
            group.addoption(name, **kwargs)
        except ValueError:
            # 已经注册过（根 conftest 注册过了）—— 忽略
            pass


#: 参数定义（根 conftest 与这里共用同一份，避免两处漂移）
_E2E_OPTIONS = [
    ("--e2e-base", dict(action="store", default=e2e_lib.DEFAULT_BASE_URL,
                        help="被测 Studio 的 base URL（默认线上 8190）")),
    ("--e2e-isolated", dict(action="store_true",
                             help="起一个隔离的 Studio 实例（独立端口 + 独立数据目录）")),
    ("--e2e-real-comfy", dict(action="store_true",
                               help="用真实 ComfyUI（会真的占 GPU 跑模型，默认关闭）")),
    ("--e2e-headed", dict(action="store_true",
                           help="有头模式（需要 DISPLAY；默认 headless）")),
    ("--e2e-run-id", dict(action="store", default="",
                          help="本次运行的证据目录名（默认按时间戳）")),
]


def _read_options(config):
    """从 pytest 的 config 里读参数（不依赖环境变量传递）。"""
    def opt(name, default):
        try:
            return config.getoption(name, default)
        except ValueError:
            return default
    return {
        "base_url": opt("--e2e-base", e2e_lib.DEFAULT_BASE_URL),
        "isolated": bool(opt("--e2e-isolated", False)),
        "fake_comfy": not bool(opt("--e2e-real-comfy", False)),
        "headed": bool(opt("--e2e-headed", False)),
        "run_id": opt("--e2e-run-id", "") or time.strftime("%Y%m%d-%H%M%S"),
    }


#: 运行时开关（在 pytest_configure 里填好）
OPTIONS: dict = {}


def pytest_collection_modifyitems(config, items):
    """给 E2E 用例打上 e2e 标记（便于 -m "not e2e" 排除）。"""
    for item in items:
        if "tests/e2e" in str(item.fspath):
            item.add_marker(pytest.mark.e2e)


def pytest_configure(config):
    OPTIONS.update(_read_options(config))
    root = e2e_lib.EVIDENCE_ROOT / OPTIONS["run_id"]
    root.mkdir(parents=True, exist_ok=True)
    OPTIONS["evidence_root"] = root


def pytest_report_header(config):
    return [
        f"E2E base={OPTIONS['base_url']} isolated={OPTIONS['isolated']} "
        f"fake_comfy={OPTIONS['fake_comfy']} headed={OPTIONS['headed']} "
        f"evidence={OPTIONS['evidence_root']}",
    ]


# ------------------------------------------------------------------ 装置


@pytest.fixture(scope="session")
def run_id() -> str:
    return OPTIONS["run_id"]


@pytest.fixture(scope="session")
def evidence_root() -> Path:
    return OPTIONS["evidence_root"]


@pytest.fixture(scope="session")
def studio():
    """被测的 Studio 服务。

    - 默认连线上 8190（用户真实在用的部署）；
    - ``--e2e-isolated`` 时起独立实例（独立端口 / 独立 data_dir / 独立素材库）；
    - 隔离实例默认把 ComfyUI 指向**假 ComfyUI**（``--e2e-real-comfy`` 可关）。
    """
    ok, detail = e2e_lib.has_playwright()
    if not ok:
        pytest.skip(f"真实浏览器不可用：{detail}")

    if not OPTIONS["isolated"]:
        base = OPTIONS["base_url"]
        if not e2e_lib.wait_http(base + "/api/video-categories", timeout=30):
            pytest.skip(f"线上服务 {base} 不可达")
        server = e2e_lib.StudioServer(base_url=base)
        yield server
        return

    port = e2e_lib.free_port()
    # ⚠ 素材库根目录必须落在**独立挂载点**上（h3studio/assets.py 的
    # check_disk_mounted 会拒绝写普通目录 —— 那是防「外接盘掉了把数据
    # 写满系统盘」的保护）。/tmp 是 tmpfs（独立挂载），/dev/shm 同理。
    root = Path(tempfile.mkdtemp(prefix=f"h3s-e2e-{OPTIONS['run_id']}-"))
    data_dir = root / "data"
    library = e2e_lib.make_library_root(OPTIONS["run_id"])

    fake_proc = None
    comfy_url = "http://127.0.0.1:8188"
    if OPTIONS["fake_comfy"]:
        fake_port = e2e_lib.free_port()
        fake_proc = e2e_lib.start_fake_comfy(fake_port, root)
        comfy_url = f"http://127.0.0.1:{fake_port}"
        # ⚠ 潜空间坐标也必须指向假 ComfyUI 的 output 目录 ——
        #   h3studio/continuation_service.comfy_output_dir() 默认按
        #   「监听 8188 的进程 cwd」解析，那会指向**真** ComfyUI 的目录，
        #   于是「这一段能不能接续」永远判否。
        os.environ["H3STUDIO_COMFY_OUTPUT"] = str(root / "comfy-output")

    # 隔离实例要连假 ComfyUI —— 用环境变量覆盖 config.json 里的 comfyui_url
    os.environ["H3STUDIO_COMFY_URL"] = comfy_url
    server = e2e_lib.start_studio(port, data_dir, library, comfy_url)
    server.fake_comfy = OPTIONS["fake_comfy"]
    server.fake_comfy_url = comfy_url
    # 隔离实例默认没有本地模型 Provider -> 增强类用例会 400。
    # 给它种一个（只读调用本机 5000 的模型服务，不改它的配置）。
    try:
        e2e_lib.seed_provider(server.base_url)
    except Exception as exc:  # pragma: no cover - 本机没起模型服务时跳过
        print(f"[e2e] 未能配置本地模型 Provider：{exc}")
    try:
        yield server
    finally:
        # 失败时把服务端日志也留下（诊断用）
        if server.log_path and server.log_path.is_file():
            try:
                target = evidence_root / "studio-server.log"
                target.write_text(server.log_path.read_text(encoding="utf-8",
                                                            errors="replace"),
                                   encoding="utf-8")
            except Exception:
                pass
        server.stop()
        if fake_proc is not None:
            fake_proc.terminate()
            try:
                fake_proc.wait(timeout=10)
            except Exception:
                fake_proc.kill()


@pytest.fixture
def evidence(request, evidence_root) -> e2e_lib.Evidence:
    """本条用例的证据收集器（结束时会落盘）。"""
    case_id = request.node.name
    case_id = case_id.replace("[", "-").replace("]", "")
    ev = e2e_lib.Evidence(run_id=OPTIONS["run_id"], case_id=case_id,
                          root=evidence_root)
    yield ev
    ev.dump()


@pytest.fixture
def studio_page(studio, evidence):
    """真实 Chromium 页面（headless 或有头）+ 证据钩子。"""
    from playwright.sync_api import sync_playwright

    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(
        headless=not OPTIONS["headed"],
        args=["--no-sandbox", "--disable-dev-shm-usage"],
    )
    context = browser.new_context(viewport={"width": 1440, "height": 950})
    page = context.new_page()

    page.on("console", lambda m: evidence.console.append(
        f"[{m.type}] {m.text}"))
    page.on("pageerror", lambda e: evidence.page_errors.append(str(e)))
    page.on("requestfailed", lambda r: evidence.network_errors.append(
        f"{r.method} {r.url} -> {r.failure}"))
    page.on("response", lambda r: (
        evidence.network_errors.append(f"HTTP {r.status} {r.url}")
        if r.status >= 400 else None))

    # 钩住 POST /api/jobs —— 让用例能断言「界面真正提交了什么」
    page.add_init_script("""
      (() => {
        const origFetch = window.fetch;
        window.__e2eLastSubmit = null;
        window.fetch = function (input, init) {
          try {
            const url = typeof input === 'string' ? input : (input && input.url) || '';
            if (url.indexOf('/api/jobs') >= 0 && init && init.body instanceof FormData) {
              const mode = init.body.get('mode');
              const raw = init.body.get('params');
              window.__e2eLastSubmit = {
                mode: mode,
                params: raw ? JSON.parse(raw) : null,
                fieldNames: Array.from(init.body.keys()),
              };
            }
          } catch (e) { /* 钩子失败不影响主流程 */ }
          return origFetch.apply(this, arguments);
        };
      })();
    """)

    helper = e2e_lib.StudioPage(page, evidence, studio.base_url)
    try:
        yield helper
    finally:
        try:
            evidence.screenshot(page, "final")
        except Exception:
            pass
        context.close()
        browser.close()
        playwright.stop()


@pytest.fixture
def fixtures_dir(tmp_path) -> Path:
    """测试用素材（图片 / 音频 / 视频）—— 真实文件，且**每条用例各不相同**。

    每条用例一份新素材是必须的：素材库按 sha256 去重，同内容第二次
    上传会被判「已存在，复用原素材」（标签也不会更新）—— 用例之间
    共用素材会让「标签生效」这类断言假失败。
    """
    return e2e_lib.make_fixtures(tmp_path, unique=True)
