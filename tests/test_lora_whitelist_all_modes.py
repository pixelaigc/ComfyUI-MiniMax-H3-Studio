# -*- coding: utf-8 -*-
"""
C1 缺陷回归 —— LoRA 白名单闸门必须对**全部生成模式**生效（2026-11-13）。

【这个文件为什么存在】
上线前的复核实测发现：合并树里的 LoRA 白名单闸门**只对视频模式生效**
（t2v -> invalid_lora -> 落库清空），而 **t2i / t2i_qwen / t2a 三个模式
完全没有闸门**：

  * `agent_schema.validate_params` 写的是 `if allowed and str(lora) not in allowed`，
    而 `mode_loras()` 对图像 / 音乐模式返回**空列表** -> `allowed` 为空
    -> 整个校验分支被跳过 -> **任何**字符串都能过（返回 0 个问题）；
  * `server.prepare_job_params` 只对视频模式做白名单 -> 图像 / 音乐模式的
    `params["lora"]` **原样落库**（实测落库值 =
    "integrated_multimodal_description: [Shot 1] ..."）；
  * `asset_service.add_from_media_output` 又把 `params["lora"]` / `params["loras"]`
    **原文**写进 source_meta -> 前端素材抽屉把每个键原样印出来。

结果：Agent 产出的**图像 / 音频**条目仍会把提示词字段名当 LoRA 显示。

【三条测试纪律】
  1. 断言必须由「命名该行为的那条断言」抓住 —— 不能自指
     （不能拿被测函数自己的输出算期望值）。
  2. 每条用例都带「为什么这样断言」的注释。
  3. 除了「脏值被挡住」，**必须同时断言合法值不被误杀** ——
     否则一个「把所有 LoRA 都清空」的退化实现也能让本文件全绿。

运行：python3 -m pytest tests/test_lora_whitelist_all_modes.py -v
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from h3studio import agent_schema  # noqa: E402
from h3studio import asset_service  # noqa: E402
from h3studio import job_meta  # noqa: E402
import server  # noqa: E402


#: 用户 2026-11-13 实测看到的「假装成 LoRA 的提示词」。
#: 这是 H3 三段式提示词的**字段名**，不是 LoRA。
PROMPT_AS_LORA = (
    "integrated_multimodal_description: [Shot 1] Live-action, cinematic "
    "Chinese wuxia period film. At night in a moonlit bamboo forest..."
)

#: 本机真实存在的图像效果 LoRA（来自 loras.LORA_CATALOG）。
#: 用它做「不得误杀」的对照 —— 闸门必须放行真 LoRA。
REAL_IMAGE_LORA = "Kook_Zimage_瑶光.safetensors"

#: 不走 lora 字段的模式（图像 / 音乐）—— 需求明确点名要各有一条断言。
NON_VIDEO_MODES = ("t2i", "t2i_qwen", "t2a")


class TestLoraKeyWhitelist(unittest.TestCase):
    """job_meta.is_real_lora_key —— 白名单判定的单一口径。"""

    def test_real_lora_key_accepts_registered_video_lora(self):
        """视频加速 LoRA 的 key（workflows.LORAS）必须被认。"""
        # 为什么这样断言：ckpt850 是 workflows.LORAS 里的真实 key，
        # 落库闸门要靠它放行合法视频任务。
        self.assertEqual("ckpt850", job_meta.is_real_lora_key("ckpt850"))

    def test_real_lora_key_accepts_registered_image_lora(self):
        """图像效果 LoRA（loras.LORA_CATALOG）必须被认。"""
        # 为什么这样断言：图像模式的 LoRA 入口是 params["loras"]，
        # 它的 name 来自 LORA_CATALOG —— 闸门若不认它，所有图像 LoRA 全被清空。
        self.assertEqual(REAL_IMAGE_LORA,
                         job_meta.is_real_lora_key(REAL_IMAGE_LORA))

    def test_real_lora_key_rejects_prompt_text(self):
        """提示词字段名必须被拒。"""
        # 为什么这样断言：这是用户报的那个 BUG 的本体 ——
        # 一段提示词被当成 LoRA key。
        self.assertEqual("", job_meta.is_real_lora_key(PROMPT_AS_LORA))

    def test_real_lora_key_rejects_unknown_token(self):
        """未注册但「看起来像 key」的短串也必须被拒。"""
        # 为什么这样断言：光靠「有空格/超长」这种粗判不够 ——
        # 一个短的乱填 token 同样会冒充 LoRA，必须靠清单判定。
        self.assertEqual("", job_meta.is_real_lora_key("totally_not_a_lora"))

    def test_real_lora_key_accepts_real_lora_with_spaces(self):
        """**文件名带空格**的真实 LoRA 必须被认（判定顺序不能反）。

        【为什么这条必须存在】本机真实 LoRA 里有 23 个文件名带空格，例如
            "z-image-effects/CYBORG FACTORY 🦾 BY STX_epoch_13.safetensors"
            "moss-effects/moss Hsin2.safetensors"
        如果白名单判定把「不含空白」排在「查清单」**前面**，
        这 23 个真 LoRA 会被全部误杀 —— 用户的图像任务会突然
        「没有 LoRA 了」，而闸门看起来还在正常工作。

        所以：清单优先，形状判据只做兜底。
        """
        # 先从真实清单里挑一个带空格的名字（不写死，避免清单变化后失效）
        try:
            from h3studio import loras as lora_registry
        except Exception:
            self.skipTest("loras 模块不可导入")
        catalog = getattr(lora_registry, "LORA_CATALOG", {}) or {}
        spaced = [k for k in catalog
                  if any(ch.isspace() for ch in k) and len(k) <= 120]
        if not spaced:
            self.skipTest("当前清单里没有带空格的真实 LoRA")
        for name in spaced:
            with self.subTest(name=name):
                self.assertEqual(
                    name, job_meta.is_real_lora_key(name),
                    "带空格的真实 LoRA 不应被白名单误杀")


class TestValidateParamsCoversAllModes(unittest.TestCase):
    """agent_schema.validate_params —— 校验闸门必须覆盖全部模式。"""

    def test_video_mode_still_rejects_unknown_lora(self):
        """视频模式原有的拒绝行为不得被改坏。"""
        # 为什么这样断言：修「空 allowed 时空转」的 bug 时，
        # 最容易把视频模式的既有拒绝逻辑一起改掉 —— 这条是回归护栏。
        issues = agent_schema.validate_params("t2v", {"lora": "not_a_real_lora"})
        self.assertTrue(any(i.code == "invalid_lora" for i in issues),
                        f"视频模式应报 invalid_lora，实际 {[i.code for i in issues]}")

    def test_legal_lora_values_still_pass(self):
        """合法 LoRA 不得被误报（防止「一律拒绝」的退化实现）。"""
        # 为什么这样断言：闸门的第一条底线是**不破坏现有功能**。
        issues = agent_schema.validate_params("t2v", {"lora": "ckpt850"})
        self.assertEqual([], [i.code for i in issues])

    def test_non_video_mode_rejects_prompt_as_lora(self):
        """t2i / t2i_qwen / t2a：提示词字段名不得被当成合法 lora 放行。

        为什么这样断言：旧实现 `if allowed and ...` 在这些模式下
        `allowed` 为空 -> 整个分支跳过 -> 返回 0 个问题，
        脏值一路走到落库。
        """
        for mode in NON_VIDEO_MODES:
            with self.subTest(mode=mode):
                issues = agent_schema.validate_params(
                    mode, {"prompt": "x", "lora": PROMPT_AS_LORA})
                self.assertTrue(
                    any(i.code == "invalid_lora" for i in issues),
                    f"{mode} 应报 invalid_lora，实际 {[i.code for i in issues]}")

    def test_non_video_mode_rejects_prompt_in_loras_array(self):
        """t2i / t2i_qwen / t2a：`loras[].name` 里的提示词字段名也必须被拒。"""
        # 为什么这样断言：图像模式的 LoRA 入口是 params["loras"]
        # （不是 params["lora"]）—— 只堵 lora 字段会漏掉这条路径。
        for mode in NON_VIDEO_MODES:
            with self.subTest(mode=mode):
                issues = agent_schema.validate_params(
                    mode, {"prompt": "x",
                           "loras": [{"name": PROMPT_AS_LORA, "strength": 1.0}]})
                self.assertTrue(
                    any(i.code == "invalid_loras" for i in issues),
                    f"{mode} 应报 invalid_loras，实际 {[i.code for i in issues]}")

    def test_non_video_mode_accepts_real_image_lora(self):
        """t2i / t2i_qwen：真实图像 LoRA 不得被误报。"""
        # 为什么这样断言：防止闸门退化成「图像模式一律拒绝 loras」。
        for mode in ("t2i", "t2i_qwen"):
            with self.subTest(mode=mode):
                issues = agent_schema.validate_params(
                    mode, {"prompt": "x",
                           "loras": [{"name": REAL_IMAGE_LORA, "strength": 1.0}]})
                self.assertEqual([], [i.code for i in issues])


class TestPrepareJobParamsCoversAllModes(unittest.TestCase):
    """server.prepare_job_params —— 落库闸门必须覆盖全部模式。"""

    def _prepare(self, mode, params):
        """跑一遍归一，返回**就地修改后**的 params。"""
        params = dict(params)
        try:
            server.prepare_job_params(mode, params)
        except server.JobValidationError:
            # 有些模式需要参考图/音频 —— 那些参数不影响 lora 闸门，
            # 但为了不因缺素材而误判，这里只关心 lora 字段是否被清洗。
            pass
        return params

    def test_non_video_mode_lora_prompt_is_dropped(self):
        """t2i / t2i_qwen / t2a：提示词字段名不得留在落库的 params.lora。"""
        # 为什么这样断言：落库闸门是 C1 的**第一道**——
        # 脏值一旦进库，后续任何读取方都可能把它当 LoRA 显示。
        for mode in NON_VIDEO_MODES:
            with self.subTest(mode=mode):
                params = self._prepare(
                    mode, {"prompt": "x", "lora": PROMPT_AS_LORA})
                stored = params.get("lora")
                self.assertNotEqual(
                    PROMPT_AS_LORA, stored,
                    f"{mode} 落库后 params.lora 仍含提示词字段名: {stored!r}")
                self.assertIsNone(
                    stored,
                    f"{mode} 的脏 lora 应被清空（视为没传），实际 {stored!r}")

    def test_non_video_mode_loras_prompt_is_dropped(self):
        """t2i / t2i_qwen / t2a：`loras` 数组里的提示词项不得留在落库结果里。"""
        # 为什么这样断言：loras 数组是图像模式的**真正** LoRA 入口，
        # 漏掉它 = 图像模式的 BUG 根本没修。
        for mode in NON_VIDEO_MODES:
            with self.subTest(mode=mode):
                params = self._prepare(mode, {
                    "prompt": "x",
                    "loras": [{"name": PROMPT_AS_LORA, "strength": 1.0}]})
                stored = params.get("loras") or []
                names = [str(i.get("name") or "") for i in stored
                         if isinstance(i, dict)]
                self.assertNotIn(
                    PROMPT_AS_LORA, names,
                    f"{mode} 落库后 params.loras 仍含提示词字段名: {names}")
                self.assertEqual(
                    [], names,
                    f"{mode} 的脏 loras 项应被整项丢掉，实际 {names}")

    def test_video_mode_lora_whitelist_still_works(self):
        """视频模式：合法值放行、脏值丢弃、none 保留。"""
        # 为什么这样断言：闸门扩到全模式时，视频是**唯一**已有正确行为的模式，
        # 这三条是它的回归护栏。
        params = self._prepare("t2v", {"prompt": "x", "lora": "ckpt850"})
        self.assertEqual("ckpt850", params.get("lora"))

        params = self._prepare("t2v", {"prompt": "x", "lora": PROMPT_AS_LORA})
        self.assertIsNone(params.get("lora"))

        # "none" 是「明确不用加速 LoRA」的合法语义，必须保留
        params = self._prepare("t2v", {"prompt": "x", "lora": "none"})
        self.assertEqual("none", params.get("lora"))

    def test_image_mode_real_lora_survives_land_gate(self):
        """t2i：真实图像 LoRA 不得被落库闸门误杀。"""
        # 为什么这样断言：这是「闸门不能过度」的正面断言 ——
        # 一个「把所有 loras 清空」的实现会让脏值测试全绿，却破坏功能。
        params = self._prepare("t2i", {
            "prompt": "x",
            "loras": [{"name": REAL_IMAGE_LORA, "strength": 1.0}]})
        stored = params.get("loras") or []
        names = [str(i.get("name") or "") for i in stored if isinstance(i, dict)]
        self.assertIn(REAL_IMAGE_LORA, names,
                      f"真实图像 LoRA 不应被落库闸门清掉，实际 {names}")


class TestJobMetaSummaryCoversAllModes(unittest.TestCase):
    """job_meta.summarize —— 显示层闸门必须覆盖全部模式。

    【为什么显示层要单独测】即使落库闸门生效，**历史库里已经躺着**脏值
    （修复前的任务）。显示层不挡，这些老条目仍会显示提示词当 LoRA。
    """

    def test_non_video_mode_lora_prompt_not_shown(self):
        """t2i / t2i_qwen / t2a：params.lora 里的提示词字段名不得显示。"""
        for mode in NON_VIDEO_MODES:
            with self.subTest(mode=mode):
                info = job_meta.summarize(
                    mode, {"prompt": "x", "lora": PROMPT_AS_LORA})
                self.assertEqual(
                    "", info.get("lora_label"),
                    f"{mode} 的 lora_label 不应显示提示词: "
                    f"{info.get('lora_label')!r}")

    def test_non_video_mode_loras_prompt_not_shown(self):
        """t2i / t2i_qwen / t2a：loras[].name 里的提示词字段名不得显示。"""
        for mode in NON_VIDEO_MODES:
            with self.subTest(mode=mode):
                info = job_meta.summarize(mode, {
                    "prompt": "x",
                    "loras": [{"name": PROMPT_AS_LORA, "strength": 1.0}]})
                self.assertEqual(
                    "", info.get("lora_label"),
                    f"{mode} 的 lora_label 不应显示提示词: "
                    f"{info.get('lora_label')!r}")

    def test_real_image_lora_still_shown(self):
        """t2i：真实图像 LoRA 仍要正常显示（不得被闸门误杀）。"""
        info = job_meta.summarize("t2i", {
            "prompt": "x",
            "loras": [{"name": REAL_IMAGE_LORA, "strength": 1.0}]})
        self.assertTrue(info.get("lora_label"),
                        "真实图像 LoRA 应有显示文案")
        self.assertIn("瑶光", info.get("lora_label"))

    def test_video_lora_still_shown(self):
        """t2v：合法加速 LoRA 仍要正常显示。"""
        info = job_meta.summarize("t2v", {"model": "lora", "lora": "ckpt850"})
        self.assertEqual("ckpt850", info.get("lora_label"))


class TestResolveJobLora(unittest.TestCase):
    """job_meta.resolve_job_lora —— 素材库入库端要用的唯一取值入口。"""

    def test_image_mode_reads_loras_array(self):
        """图像模式：真实效果 LoRA 应被解析出来。"""
        resolved = job_meta.resolve_job_lora("t2i", {
            "loras": [{"name": REAL_IMAGE_LORA, "strength": 1.0}]})
        self.assertIsNotNone(resolved["loras"])
        self.assertEqual(REAL_IMAGE_LORA, resolved["loras"][0]["name"])

    def test_image_mode_drops_prompt_lora(self):
        """图像模式：提示词字段名必须被丢掉。"""
        resolved = job_meta.resolve_job_lora("t2i", {
            "loras": [{"name": PROMPT_AS_LORA, "strength": 1.0}]})
        self.assertIsNone(resolved["loras"])

    def test_music_mode_has_no_lora(self):
        """音乐模式：没有 LoRA 概念。"""
        resolved = job_meta.resolve_job_lora("t2a", {"lora": PROMPT_AS_LORA})
        self.assertIsNone(resolved["lora"])
        self.assertIsNone(resolved["loras"])
        self.assertEqual([], resolved["labels"])

    def test_video_explicit_none_has_no_lora(self):
        """t2v + lora="none" -> 明确不用加速 LoRA。"""
        resolved = job_meta.resolve_job_lora("t2v", {"lora": "none"})
        self.assertIsNone(resolved["lora"])

    def test_video_without_lora_field_uses_workflow_default(self):
        """t2v 没记 lora -> 工作流默认 ckpt850（确实挂上了）。"""
        # 为什么这样断言：工作流是 `params.get("lora", "ckpt850")`，
        # 显示层若说「没有 LoRA」就是**说谎**（工作流明明挂了）。
        resolved = job_meta.resolve_job_lora("t2v", {"aspect": "16:9"})
        self.assertEqual("ckpt850", resolved["lora"])


class TestAssetLibrarySourceMeta(unittest.TestCase):
    """asset_service —— 入库时 source_meta 不得带脏 LoRA。

    【为什么这条单独测】素材库抽屉把 source_meta 的**每个键原样印出来**
    （web/assets.js 的 metaRows 过滤只排除 attention / attention_label）。
    所以入库时写进去的原文 == 抽屉里显示的原文 —— 入库端不挡就等于没修。
    """

    def _build_source_meta(self, mode, params):
        """跑一次入库，抠出真正写进 source_meta 的内容。

        为什么用真实入库而不是直接调内部函数：
        入库路径上还有一层「哪些字段被搬进 source_meta」的映射
        （add_from_media_output -> register_asset），
        只测内部函数可能测不到真正的搬运结果。

        实现方式：临时替换 `asset_service.register_asset`，
        截获它收到的 `source_meta` 参数 —— 那就是最终落库的内容。
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outputs = root / "outputs"
            (outputs / "vid").mkdir(parents=True)
            src = outputs / "vid" / "sample.mp4"
            src.write_bytes(b"\x00" * 16)

            captured = {}

            def _fake_register(conn, **kwargs):
                captured["source_meta"] = kwargs.get("source_meta")
                return {"asset": {"id": "probe_asset"}}

            original = asset_service.register_asset
            asset_service.register_asset = _fake_register
            try:
                # 注意：真实调用方（asset_api）传进来的 job["params"]
                # 是**已解析的 dict**（json.loads(row["params"])），
                # 不是 JSON 字符串 —— 这里必须跟真实形状一致。
                job = {"id": "probe_job", "mode": mode, "params": dict(params)}
                asset_service.add_from_media_output(
                    None,
                    job=job,
                    output={"file": "vid/sample.mp4", "kind": "video"},
                    batch=None,
                    outputs_dir=outputs,
                )
            except Exception:
                # 入库可能因缺产物元信息而失败 —— 但 source_meta
                # 在 register_asset 之前就算好了，抓到即可。
                pass
            finally:
                asset_service.register_asset = original

            return captured.get("source_meta")

    def test_source_meta_lora_is_not_prompt(self):
        """t2i / t2i_qwen / t2a：source_meta.lora 不得是提示词字段名。"""
        for mode in NON_VIDEO_MODES:
            with self.subTest(mode=mode):
                meta = self._build_source_meta(
                    mode, {"prompt": "x", "lora": PROMPT_AS_LORA})
                if meta is None:
                    self.skipTest("入库替身未捕获到 source_meta（接口变更）")
                stored = meta.get("lora")
                self.assertNotEqual(
                    PROMPT_AS_LORA, stored,
                    f"{mode} 的 source_meta.lora 仍是提示词字段名: {stored!r}")

    def test_source_meta_loras_have_no_prompt(self):
        """t2i / t2i_qwen / t2a：source_meta.loras 里不得含提示词字段名。"""
        for mode in NON_VIDEO_MODES:
            with self.subTest(mode=mode):
                meta = self._build_source_meta(mode, {
                    "prompt": "x",
                    "loras": [{"name": PROMPT_AS_LORA, "strength": 1.0}]})
                if meta is None:
                    self.skipTest("入库替身未捕获到 source_meta（接口变更）")
                items = meta.get("loras") or []
                names = [str(i.get("name") or "") for i in items
                         if isinstance(i, dict)]
                self.assertNotIn(
                    PROMPT_AS_LORA, names,
                    f"{mode} 的 source_meta.loras 仍含提示词字段名: {names}")


if __name__ == "__main__":
    unittest.main()
