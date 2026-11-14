# -*- coding: utf-8 -*-
"""
媒体库卡片：LoRA 白名单 + 生成参数详情（C2 / C3 / C4 / C5）—— 回归测试。

【为什么要有这个文件】
用户 2026-11-13 实测报出两个缺陷：
  BUG 1：生成库卡片的「LoRA」行显示的根本不是该任务用过的 LoRA，
         甚至是一段提示词（"integrated_multimodal_description: ..."）。
  BUG 2：卡片不显示步数等参数，也没有地方看完整生成参数。

【测试的三条纪律】
  1. 断言必须由「命名该行为的那条断言」抓住 —— 不能自指
     （不能拿被测函数自己的输出算期望值）。
  2. 每条用例都带「为什么这样断言」的注释 —— 便于下一个人判断
     这条断言保护的是哪条行为。
  3. 前端逻辑用 Node 真跑（不是字符串断言）—— 见 test_frontend_* 用例。

运行：python3 -m pytest tests/test_media_card_detail.py -v
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from h3studio import agent_schema, job_meta  # noqa: E402

import unittest  # noqa: E402


#: 用户 2026-11-13 实测看到的「假装成 LoRA 的提示词」。
PROMPT_AS_LORA = (
    "integrated_multimodal_description: [Shot 1] Live-action, cinematic "
    "Chinese wuxia period film. At night in a moonlit bamboo forest..."
)


# ==================================================================
# BUG 1 · 第一层：落库端校验（agent_schema）
# ==================================================================
class TestLoraWhitelistOnWrite(unittest.TestCase):
    """Agent 不能把提示词塞进 lora 字段落库。"""

    def test_non_video_mode_rejects_arbitrary_lora_value(self):
        """图像 / 音乐模式传 lora -> 必须报 invalid_lora。

        旧实现 `if allowed and str(lora) not in allowed` 在这些模式上
        `allowed` 是空列表 -> 整个分支跳过 -> 任何字符串都能落库。
        """
        for mode in ("t2i", "t2i_qwen", "i2i_qwen", "t2i_moss", "t2a", "a2a"):
            issues = agent_schema.validate_params(mode, {"lora": PROMPT_AS_LORA})
            codes = [getattr(i, "code", None) for i in issues]
            self.assertIn(
                "invalid_lora", codes,
                f"{mode} 模式必须拒绝非法 lora 值（提示词冒充 LoRA）")

    def test_video_mode_still_rejects_unknown_lora_key(self):
        """视频模式的原有行为不能退化：不在白名单里的 key 仍要拦。"""
        issues = agent_schema.validate_params("t2v", {"lora": "not_a_real_lora"})
        codes = [getattr(i, "code", None) for i in issues]
        self.assertIn("invalid_lora", codes)

    def test_legal_lora_values_still_pass(self):
        """合法值必须放行 —— 否则是把功能改坏了。"""
        for mode in ("t2v", "flf", "r2v"):
            for value in ("ckpt850", "v4step600", "none"):
                issues = agent_schema.validate_params(mode, {"lora": value})
                self.assertEqual(
                    [], issues,
                    f"{mode} 的合法 lora 值 {value} 不该被拦")


# ==================================================================
# BUG 1 · 第二层：展示端白名单（job_meta）
# ==================================================================
class TestLoraWhitelistOnRead(unittest.TestCase):
    """服务端摘要只能把「真实存在的 LoRA」当成 LoRA 显示。"""

    def test_is_real_lora_key_accepts_known_keys(self):
        """真实存在的 LoRA key 必须被认出来。"""
        self.assertEqual("ckpt850", job_meta.is_real_lora_key("ckpt850"))
        self.assertEqual("v4step600", job_meta.is_real_lora_key("v4step600"))

    def test_is_real_lora_key_rejects_prompt(self):
        """提示词必须被判定为「不是 LoRA」。"""
        self.assertEqual("", job_meta.is_real_lora_key(PROMPT_AS_LORA))
        self.assertEqual("", job_meta.is_real_lora_key(
            "integrated_multimodal_description: [Shot 1] A shot"))

    def test_is_real_lora_key_rejects_unknown_short_token(self):
        """不存在的短标识符也要被拒（否则「随便一个词」就能冒充 LoRA）。"""
        self.assertEqual("", job_meta.is_real_lora_key("totally_not_a_lora"))

    def test_legacy_video_summary_drops_prompt_as_lora(self):
        """旧路径视频：params.lora 是提示词 -> 摘要里的 lora_label 必须为空。

        这是 BUG 1 在**展示端**的直接表现：
        旧实现 `out["lora_label"] = lora` 会把提示词原文印在卡片上。
        """
        info = job_meta.summarize("t2v", {"lora": PROMPT_AS_LORA})
        self.assertEqual(
            "", info["lora_label"],
            "提示词不能被当成 LoRA 显示")
        self.assertNotIn("integrated_multimodal", info["lora_label"])

    def test_legacy_video_summary_keeps_real_lora(self):
        """真实 LoRA 仍要正常显示（不能把功能改坏）。"""
        info = job_meta.summarize("t2v", {"lora": "ckpt850", "lora_strength": 1.0})
        self.assertEqual("ckpt850", info["lora_label"])
        self.assertEqual(1.0, info["lora_strength"])

    def test_image_mode_ignores_legacy_lora_field(self):
        """图像模式：params.lora 是遗留字段（工作流不读），不能当成 LoRA。

        图像工作流读的是 params["loras"]（效果 LoRA 数组）。
        旧前端无条件写 params.lora = "ckpt850"，
        所以图像任务的 params.lora 有值**不代表**用了 LoRA。
        """
        info = job_meta.summarize("t2i", {"lora": "ckpt850", "loras": []})
        self.assertEqual("", info["lora_label"])

    def test_resolve_job_lora_image_mode_reads_loras_array(self):
        """图像模式：只认 params.loras 数组里的效果 LoRA。"""
        resolved = job_meta.resolve_job_lora("t2i", {
            "lora": "ckpt850",
            "loras": [{"name": "Kook_Zimage_瑶光.safetensors", "strength": 0.8}],
        })
        self.assertIsNone(resolved["lora"])
        self.assertEqual(1, len(resolved["loras"]))
        self.assertEqual(0.8, resolved["strength"])

    def test_resolve_job_lora_music_mode_is_empty(self):
        """音乐模式没有 LoRA 概念 -> 全空。"""
        resolved = job_meta.resolve_job_lora("t2a", {"lora": PROMPT_AS_LORA})
        self.assertIsNone(resolved["lora"])
        self.assertIsNone(resolved["loras"])
        self.assertEqual([], resolved["labels"])

    def test_legacy_video_without_lora_field_uses_workflow_default(self):
        """老视频任务没记 lora -> 工作流默认挂 ckpt850（确实挂上了）。

        workflows._h3_video_workflow:344
            `lora = params.get("lora", "ckpt850")`
        workflows.py:371-374
            `if lora != "none": p["138"] = {... TurboLoRA ...}`

        所以显示「无」是错的 —— 那会让用户以为这条视频没挂加速 LoRA。
        """
        resolved = job_meta.resolve_job_lora("t2v", {"aspect": "16:9"})
        self.assertEqual("ckpt850", resolved["lora"])
        self.assertEqual(1.0, resolved["strength"])

    def test_legacy_video_explicit_none_has_no_lora(self):
        """明确写了 lora="none" -> 确实没有 LoRA。"""
        resolved = job_meta.resolve_job_lora("t2v", {"lora": "none"})
        self.assertIsNone(resolved["lora"])
        self.assertEqual([], resolved["labels"])

    def test_resolve_job_lora_video_panel_path(self):
        """视频新面板：读结构化的 accel_loras / fixed_accel_lora。"""
        resolved = job_meta.resolve_job_lora("t2v", {
            "model_key": "fl2va_pruned_int8",
            "accel_loras": [{"key": "dmad_4step_critic", "strength": 1.0,
                             "enabled": True}],
            "fixed_accel_lora": "ckpt850",
        })
        self.assertEqual("dmad_4step_critic", resolved["lora"])
        self.assertEqual(1.0, resolved["strength"])
        self.assertTrue(any("DMAD" in x for x in resolved["labels"]))


# ==================================================================
# C2 / C3 · 生成参数摘要（gen_params）
# ==================================================================
class TestGenParams(unittest.TestCase):
    """C2 卡片行 / C3 详情模态框的唯一真源。"""

    def test_missing_fields_are_empty_not_substituted(self):
        """缺失的字段必须是空串 —— 前端显示「—」，不拿别的字段顶替。

        具体：不拿 shift_video 顶 shift、不拿 max_duration 顶 duration。
        """
        gp = job_meta.gen_params("t2i", {
            "steps": 20,
            "sampler": "euler",
            "scheduler": "beta",
        })
        self.assertEqual("", gp["shift"])
        self.assertEqual("", gp["duration"])
        self.assertEqual("", gp["size"])

    def test_music_duration_uses_max_duration_field(self):
        """音乐时长字段名是 max_duration —— 这是它自己的语义，不是顶替。"""
        gp = job_meta.gen_params("t2a", {"max_duration": 60.0})
        self.assertEqual("60s", gp["duration"])

    def test_video_shift_uses_video_and_audio_fields(self):
        """视频 shift 分视频 / 音频两个字段，都要显示。"""
        gp = job_meta.gen_params("t2v", {"shift_video": 7.0, "shift_audio": 3.0})
        self.assertIn("7", gp["shift"])
        self.assertIn("3", gp["shift"])

    def test_lora_is_explicitly_none_when_absent(self):
        """没有 LoRA 时显式写「未使用 LoRA」—— 不是空串。

        【2026-11-15 文案变更】用户要求：「缺失时应显示『未使用 LoRA』
        而不是错名」。「无」容易被读成「数据缺失」，
        「未使用 LoRA」明确说的是「这条产物没有用 LoRA」。
        """
        gp = job_meta.gen_params("t2a", {})
        self.assertEqual("未使用 LoRA", gp["lora"])

    def test_lora_shows_name_and_strength(self):
        """有 LoRA 时显示名称 + 强度。"""
        gp = job_meta.gen_params("t2v", {"lora": "ckpt850", "lora_strength": 1.0})
        self.assertIn("ckpt850", gp["lora"])
        self.assertIn("1.00", gp["lora"])

    def test_prompt_as_lora_yields_none(self):
        """BUG 1 的端到端断言：提示词冒充 LoRA 时，gen_params.lora 必须是「未使用 LoRA」。

        【2026-11-15 文案变更】用户明确要求：「缺失时应显示『未使用 LoRA』
        而不是错名」。原来的「无」太短、也容易被读成「数据缺失」，
        这里改成明确表述。断言的**行为**没变（仍是「不显示假 LoRA」）。
        """
        gp = job_meta.gen_params("t2v", {"lora": PROMPT_AS_LORA})
        self.assertEqual("未使用 LoRA", gp["lora"])

    def test_seed_defaults_to_random(self):
        """没指定种子 -> 「随机」（这是真实语义，不是缺失）。"""
        gp = job_meta.gen_params("t2v", {})
        self.assertEqual("随机", gp["seed"])

    def test_size_prefers_actual_output_pixels(self):
        """尺寸优先用产物实际像素（与卡片显示口径一致）。"""
        gp = job_meta.gen_params(
            "t2v", {"width": 1344, "height": 760},
            resolution_info={"width": 1344, "height": 768})
        self.assertEqual("1344×768", gp["size"])

    def test_steps_present(self):
        """步数要能被取到（C2 要求）。"""
        gp = job_meta.gen_params("t2v", {"steps": 4})
        self.assertEqual("4", gp["steps"])


# ==================================================================
# 前端逻辑（Node 真跑，不是字符串断言）
# ==================================================================
NODE = "node"


def _run_node(script: str):
    """在临时目录里跑一段 Node 脚本，返回 stdout。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "probe.js"
        path.write_text(script, encoding="utf-8")
        proc = subprocess.run([NODE, str(path)], capture_output=True,
                              text=True, timeout=60)
        if proc.returncode != 0:
            raise AssertionError(
                f"node 脚本失败：\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
        return proc.stdout.strip()


#: app.js 里要单独取出来测的纯函数（不含 DOM 依赖）。
FRONTEND_FN_SOURCE = r"""
(function () {
  // 从 app.js 里摘出的纯逻辑（与 app.js 中的实现逐字一致）。
  function paramOrDash(value) {
    const text = String(value == null ? "" : value).trim();
    return text || "—";
  }

  function isLoraKeyLike(text) {
    const value = String(text == null ? "" : text).trim();
    if (!value) return false;
    if (value.length > 120) return false;
    return /^[A-Za-z0-9_.\/-]+$/.test(value);
  }

  function loraLabel(job) {
    const info = (job && job.model_info) || null;
    const parts = [];
    const mode = (job && job.mode) || "";
    const isVideo = mode === "t2v" || mode === "flf" || mode === "r2v"
                    || mode === "longfilm";
    const p = (job && job.params) || {};
    const structured = Array.isArray(p.accel_loras)
      && p.accel_loras.some(item => item && item.enabled !== false && item.key);
    if (info && info.lora_label) {
      if (!isVideo || structured || isLoraKeyLike(info.lora_label)) {
        parts.push(info.lora_strength != null
          ? `${info.lora_label} ×${Number(info.lora_strength).toFixed(2)}`
          : info.lora_label);
      }
    } else if (isVideo) {
      parts.push("空");
    }
    if (info && info.fixed_lora_label) {
      parts.push(`工作流固定：${info.fixed_lora_label}`);
    }
    return parts.join(" · ");
  }

  globalThis.__probe = { paramOrDash, isLoraKeyLike, loraLabel };
})();
"""


class TestFrontendPureFunctions(unittest.TestCase):
    """前端纯函数的真实行为（用 Node 跑，不是字符串匹配）。"""

    def _probe(self):
        return _run_node(FRONTEND_FN_SOURCE + r"""
const p = globalThis.__probe;
const out = {};
out.dashEmpty = p.paramOrDash("");
out.dashNull = p.paramOrDash(null);
out.dashValue = p.paramOrDash("4");
out.promptLike = p.isLoraKeyLike(
  "integrated_multimodal_description: [Shot 1] A cinematic shot.");
out.realKey = p.isLoraKeyLike("ckpt850");
out.displayName = p.isLoraKeyLike("DMAD 4 步（critic 版）");
out.videoWithPrompt = p.loraLabel({
  mode: "t2v",
  model_info: {lora_label: "integrated_multimodal_description: [Shot 1] xxx",
               lora_strength: 1.0},
});
out.videoWithReal = p.loraLabel({
  mode: "t2v",
  model_info: {lora_label: "ckpt850", lora_strength: 1.0},
});
out.imageWithReal = p.loraLabel({
  mode: "t2i",
  model_info: {lora_label: "瑶光（Kook Z-Image 真实幻想） ×0.80"},
});
console.log(JSON.stringify(out));
""")

    def test_missing_value_renders_dash(self):
        """缺失 -> 「—」（C2 硬要求：不得拿别的字段顶替）。"""
        data = json.loads(self._probe())
        self.assertEqual("—", data["dashEmpty"])
        self.assertEqual("—", data["dashNull"])
        self.assertEqual("4", data["dashValue"])

    def test_prompt_is_not_lora_key_like(self):
        """提示词不能通过前端兜底白名单。"""
        data = json.loads(self._probe())
        self.assertFalse(data["promptLike"])

    def test_real_lora_key_passes_frontend_fallback(self):
        """真实 LoRA key 必须能通过（不能把功能改坏）。"""
        data = json.loads(self._probe())
        self.assertTrue(data["realKey"])

    def test_video_card_with_poisoned_lora_shows_no_lora(self):
        """BUG 1 前端断言：卡片上不能显示那段提示词。"""
        data = json.loads(self._probe())
        self.assertNotIn("integrated_multimodal", data["videoWithPrompt"])

    def test_video_card_with_real_lora_shows_it(self):
        """视频卡片仍要显示真实 LoRA + 强度。"""
        data = json.loads(self._probe())
        self.assertIn("ckpt850", data["videoWithReal"])
        self.assertIn("1.00", data["videoWithReal"])


# ==================================================================
# 端到端：服务端 row_to_job 下发的 gen_params
# ==================================================================
class TestServerJobPayload(unittest.TestCase):
    """server.row_to_job 必须把 gen_params 一起下发。"""

    def test_row_to_job_includes_gen_params(self):
        """每个任务都要带 gen_params —— 前端只渲染它。"""
        sys.path.insert(0, str(ROOT))
        import server  # noqa: E402

        row = {
            "id": "testjob001",
            "created_at": 1700000000.0,
            "mode": "t2v",
            "status": "done",
            "progress": 1.0,
            "progress_text": "",
            "error": None,
            "started_at": 1700000000.0,
            "finished_at": 1700000010.0,
            "params": json.dumps({"lora": PROMPT_AS_LORA, "steps": 4}),
            "outputs": "[]",
            "session_id": "",
        }
        job = server.row_to_job(row)
        self.assertIn("gen_params", job)
        # 【2026-11-15 文案变更】「无」->「未使用 LoRA」（用户要求）。
        self.assertEqual("未使用 LoRA", job["gen_params"]["lora"])
        self.assertEqual("4", job["gen_params"]["steps"])
        # 老的 model_info 也要一起在（前端两处都用）
        self.assertIn("model_info", job)


if __name__ == "__main__":
    import unittest
    unittest.main(verbosity=2)
