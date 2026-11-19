# -*- coding: utf-8 -*-
"""接续开关「接续段也用二采」的**可见性**契约测试（真浏览器 + 真后端）。

【这个测试在防什么】
「接续段也用二采」是接续段唯一的二采放行入口（服务端
`h3studio/twopass_policy.py` 默认禁二采，只有 `params["continuation"]
["allow_twopass"]` 为真才放行）。

它的可见性由 `web/video-panel.js::renderTwopass()` 决定：

    hasContinuation = window.Continuation.state().current?.continuation
    allowRow.hidden = !hasContinuation || !videoState.twopass.enabled

而 `web/continuation.js::start()` 的时序是：

    applyToPanel(ctx)   -> VIDEO_PANEL.render() -> renderTwopass()
                           ↑ **此刻 current 还是旧的（null）** => 开关被藏
    current = ctx       -> 现在接续上下文才真正就绪
    renderPanel()       -> 只渲染「接续上下文」只读区，**不碰**视频面板
    applyAssetRefs(...) -> **有**素材时末尾调 renderModeKeepSlots() -> 重渲染
                           **无**素材时**早退**，**不重渲染** => 开关永远藏着

⇒ 结果：接续上下文明明已就绪（「接续上下文」区域都显示了），
  用户却在二采区块里**看不到**那个开关 —— 且这与「flf / r2v 未传
  params.continuation」的现象**长得一模一样**（用户视角：开关不出现）。

【实测口径 —— 12 组矩阵（3 模式 × 有/无素材 × 二采开/关）】
修复前：
    t2v|refs=0|tp=1 -> hidden    flf|refs=0|tp=1 -> hidden（缺陷）
    flf|refs=1|tp=1 -> 显形      r2v|refs=1|tp=1 -> 显形
修复后（全部 12 组）：
    「有二采开关」≡「有二采上下文 且 二采已开」，与模式、与有无素材**无关**。

【为什么「二采开/关」由**上游任务记录**决定，而不是测试里先勾复选框】
接续的语义是「照上一段的配置继续」—— 服务端下发的接续上下文里
`params.twopass` 就是**上一段的二采设置**，前端 `applyToPanel` 会把它
回填到面板（`applyModeConfig` 里 `videoState.twopass = {...旧, ...cfg.twopass}`）。
⇒ 测试里**先**勾复选框再点接续，那个勾会被接续动作**自己改回去**
（回填成上游的值）—— 测的就不是用户真实的路径了。
所以本文件把「上游那一段用了二采 / 没用二采」直接写进任务记录，
让面板状态由**回填**决定，与真实用户路径一致。

【为什么用真实浏览器】
开关的显隐是 DOM 状态 + 模块内私有变量（`current`）的组合 ——
静态读源码证明不了「点下去之后到底显不显示」。

【与 tests/test_continuation_frontend_params.py 的分工】
那份测的是「提交体里有没有 params.continuation」（**参数**层）。
本份测的是「开关在界面上显不显示」（**可见性**层）。
两层都断，用户看到的都是「接续功能好像没生效」—— 但根因完全不同。

运行：python3 -m pytest tests/test_continuation_switch_visibility.py -v
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except Exception:
    HAS_PLAYWRIGHT = False

from test_frontend_defaults import StudioServer  # noqa: E402

#: 被测的三个视频模式（与服务端 CONTINUABLE_MODES 一致）。
MODES = ("t2v", "flf", "r2v")


@unittest.skipUnless(HAS_PLAYWRIGHT, "未安装 playwright")
class TestContinuationSwitchVisibility(unittest.TestCase):
    """接续开关的可见性。"""

    @classmethod
    def setUpClass(cls):
        cls.server = StudioServer(seed_jobs=0)
        assert cls.server.start(), "Studio 实例启动失败"
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(args=["--no-sandbox"])
        cls.ctx = cls.browser.new_context(viewport={"width": 1280, "height": 900})
        cls.page = cls.ctx.new_page()
        cls.dialogs = []

        def _on_dialog(d):
            cls.dialogs.append(f"{d.type}: {d.message}")
            d.accept()

        # alert/confirm 会**阻塞**页面脚本 —— 必须处理。
        cls.page.on("dialog", _on_dialog)
        cls.errors = []
        cls.page.on("pageerror", lambda e: cls.errors.append(str(e)))
        cls.page.goto(f"http://127.0.0.1:{cls.server.port}/",
                      wait_until="domcontentloaded")
        cls.page.wait_for_timeout(2500)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.stop()

    # ------------------------------------------------------------ 工具
    def _seed_job(self, job_id: str, mode: str,
                  twopass_enabled: bool,
                  asset_refs: list | None = None) -> None:
        """造一条服务端认为**可接续**的记录。

        服务端判「可接续」要三件事：模式在白名单 + 非二采 + 潜空间在盘上。
        潜空间文件在 ComfyUI 的 output 目录下 —— 测试里造一个假的
        （内容无所谓，服务端只检查文件**存在**）。

        【twopass_enabled 是「上一段有没有开二采」】
        它决定接续回填后面板上的二采状态 —— 从而决定开关显不显示。

        【为什么**总是**写 twopass 字段（哪怕 enabled=False）】
        真实前端每次提交都会带上 `twopass: {...videoState.twopass}`（见
        video-panel.js::submitParams）—— 上一段没用二采时，
        落库记录里就是 `twopass={"enabled": false}`。
        而 `build_context` 的 CONTEXT_FIELDS 含 "twopass"，
        回填时 `videoState.twopass = {...旧, ...cfg.twopass}` 会**显式**覆盖。
        若**不写**这个字段，回填就「不覆盖」—— 面板会保留用户原有的二采状态
        （可能开着）⇒ 测的就不是真实路径了。

        【注意】不要在 twopass 里写 enabled=True 以外的二采细节 ——
        `cont.continuation_allowed()` 会因为「二采段缺落盘潜空间」
        而拒绝接续（见 continuation_service.continuation_state）。
        本文件给二采段造了 stage 文件（= 它的落盘潜空间），所以能接续。
        """
        from h3studio import continuation_service as cs

        run_id = cs.segment_run_id(job_id)
        stage = cs.stage_path(run_id, 0)
        stage.parent.mkdir(parents=True, exist_ok=True)
        stage.write_bytes(b"fake-av-latent")

        params = {
            "prompt": "一只橘猫在旧书桌上走过",
            "model_key": "fl2va_int8",
            "width": 768, "height": 448,
            "duration": 5, "steps": 4,
            "sampler": "euler", "scheduler": "simple",
            "gen_type": mode,
            # 【二采段要能接续】二采段必须**自己**有落盘潜空间 ——
            # 上面的 stage 文件就是它。
            "twopass": {"enabled": bool(twopass_enabled)},
            "continuation": {
                "chain_id": job_id, "stage_index": 0,
                "source_job": "", "source_segment": -1,
                "latent_run_id": run_id, "latent_stage": 0,
                "context_frames": 22, "anchor_frames": 22,
                "chain_total_frames": 0, "segment_frames": 124,
            },
        }
        if asset_refs is not None:
            params["asset_refs"] = asset_refs
        with sqlite3.connect(self.server.data_dir / "jobs.db") as conn:
            conn.execute(
                "INSERT OR REPLACE INTO jobs "
                "(id, created_at, mode, params, status, progress, outputs) "
                "VALUES (?,?,?,?,?,?,?)",
                (job_id, time.time(), mode, json.dumps(params, ensure_ascii=False),
                 "done", 1.0,
                 json.dumps([{"kind": "video", "file": "videos/fake.mp4",
                              "name": "fake.mp4"}], ensure_ascii=False)))

    def _click_continue(self, job_id: str) -> None:
        """走真实的「接续」链路（取上下文 -> 回填 -> 设 current）。"""
        self.page.evaluate(
            """async (jid) => {
                 const r = await fetch('/api/jobs/' + jid + '/continuation');
                 const d = await r.json();
                 if (!d.ok) throw new Error(d.error || '取接续上下文失败');
                 await window.Continuation.start(
                   { id: jid, mode: d.mode, status: 'done', video_name: 'p.mp4' });
               }""",
            job_id)
        self.page.wait_for_timeout(2200)

    def _switch_state(self) -> dict:
        """读开关的可见性 + 上下文状态。"""
        return self.page.evaluate("""() => {
          const row = document.getElementById('vidTwopassAllowContinuationRow');
          const cur = window.Continuation && window.Continuation.state
            ? window.Continuation.state().current : null;
          const st = window.VIDEO_PANEL && window.VIDEO_PANEL.state
            ? window.VIDEO_PANEL.state : {};
          return {
            row_exists: !!row,
            row_hidden: row ? row.hidden : null,
            has_current: !!cur,
            twopass_enabled: !!(st.twopass && st.twopass.enabled),
          };
        }""")

    def _reset(self) -> None:
        self.page.evaluate(
            "() => { if (window.Continuation) window.Continuation.clear(); }")
        self.page.wait_for_timeout(300)

    def _asset_refs_for(self, mode: str) -> list:
        """按模式给一份**服务端认**的素材引用。"""
        if mode == "flf":
            return [{"asset_id": "probe-a1", "slot": "first_frame",
                     "ordinal": 0}]
        return [{"asset_id": "probe-a1", "slot": "ref_image", "ordinal": 0}]

    # ------------------------------------------------------------ 用例
    def test_switch_appears_for_all_modes_without_assets(self):
        """**核心回归**：三种模式、**无素材**、上一段开了二采 -> 开关必须显形。

        【为什么无素材这一档最关键】有素材时 `applyAssetRefs` 会顺带
        重渲染面板，开关**碰巧**显形 —— 所以「有素材」那一档在修复前
        也是绿的，**抓不到**本缺陷。只有「无素材」这一档会暴露。
        """
        for mode in MODES:
            with self.subTest(mode=mode, asset_refs=False):
                self._reset()
                job_id = f"swv_{mode}_noasset_0001"
                self._seed_job(job_id, mode, twopass_enabled=True,
                               asset_refs=None)
                self._click_continue(job_id)
                state = self._switch_state()
                self.assertTrue(state["has_current"],
                                f"{mode}: 接续上下文应当就绪")
                self.assertTrue(state["twopass_enabled"],
                                f"{mode}: 上一段开了二采 -> 面板上二采应当是开的")
                self.assertFalse(
                    state["row_hidden"],
                    f"{mode}（无素材）：点「接续」后「接续段也用二采」开关"
                    f"必须显形 —— 接续上下文已就绪，开关却不见了")

    def test_switch_appears_for_all_modes_with_assets(self):
        """有素材时也必须显形（这一档修复前就是绿的 —— 防止回归）。"""
        for mode in MODES:
            with self.subTest(mode=mode, asset_refs=True):
                self._reset()
                job_id = f"swv_{mode}_asset_0001"
                self._seed_job(job_id, mode, twopass_enabled=True,
                               asset_refs=self._asset_refs_for(mode))
                self._click_continue(job_id)
                state = self._switch_state()
                self.assertFalse(
                    state["row_hidden"],
                    f"{mode}（有素材）：点「接续」后「接续段也用二采」开关必须显形")

    def test_switch_stays_hidden_when_twopass_off(self):
        """上一段没用二采 -> 面板二采是关的 -> 开关**不**该出现。

        （普通段本来就能用二采；只有「接续段 + 开了二采」才需要那个放行口。）
        """
        for mode in MODES:
            with self.subTest(mode=mode):
                self._reset()
                job_id = f"swv_{mode}_tpoff_0001"
                self._seed_job(job_id, mode, twopass_enabled=False,
                               asset_refs=None)
                self._click_continue(job_id)
                state = self._switch_state()
                self.assertFalse(state["twopass_enabled"],
                                 f"{mode}: 上一段没用二采 -> 面板二采应当是关的")
                self.assertTrue(
                    state["row_hidden"],
                    f"{mode}：二采没开时「接续段也用二采」开关不该出现")

    def test_switch_hides_again_after_clear(self):
        """清除接续上下文后，开关必须**重新藏起来**。

        【为什么要这条】修「显形」的同时不能引入「不消失」——
        没有接续上下文时那个开关没有意义（普通段本来就能用二采）。
        """
        self._reset()
        job_id = "swv_clear_0001"
        self._seed_job(job_id, "flf", twopass_enabled=True, asset_refs=None)
        self._click_continue(job_id)
        self.assertFalse(self._switch_state()["row_hidden"],
                         "清除前开关应当是显形的")

        self._reset()
        state = self._switch_state()
        self.assertFalse(state["has_current"], "清除后不应再有接续上下文")
        self.assertTrue(state["row_hidden"],
                        "清除接续上下文后，「接续段也用二采」开关必须重新隐藏")

    def test_switch_hides_again_after_reuse_clears_context(self):
        """「复用参数」清掉上下文后，开关也必须重新藏起来。"""
        self._reset()
        job_id = "swv_reuse_0001"
        self._seed_job(job_id, "r2v", twopass_enabled=True, asset_refs=None)
        self._click_continue(job_id)
        self.assertFalse(self._switch_state()["row_hidden"],
                         "清除前开关应当是显形的")

        cleared = self.page.evaluate(
            "() => window.Continuation.clearForReuse('测试')")
        self.assertTrue(cleared, "clearForReuse 应当返回 true（确实清掉了）")
        self.page.wait_for_timeout(400)
        state = self._switch_state()
        self.assertFalse(state["has_current"], "复用参数后不应再有接续上下文")
        self.assertTrue(state["row_hidden"],
                        "复用参数清掉上下文后，「接续段也用二采」开关必须重新隐藏")

    def test_no_js_errors_during_the_flow(self):
        """整条流程 0 条 JS 报错（资源 404 不算 JS 错误）。"""
        js_errors = [e for e in self.errors
                     if "Failed to load resource" not in e]
        self.assertEqual(js_errors, [], f"控制台有 JS 报错: {js_errors}")


if __name__ == "__main__":
    unittest.main()
