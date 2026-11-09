# -*- coding: utf-8 -*-
"""MOSS-Image 文生图接入的回归测试（真实后端 + 真实浏览器）。

覆盖本轮接入挖出并写死的几个**真事实**（不测它们就会回归）：

1. **SHIFT 节点必须是 ModelSamplingSD3，不能是 ModelSamplingFlux**
   MOSS 的 model_type 是 FLOW（不是 FLUX）-> 采样类是 ModelSamplingDiscreteFlow，
   sigma 公式**带 multiplier=1000 这一层**（插件 loaders.py 里
   `sampling_settings = {"shift": 1.0, "multiplier": 1000}`）。
   ModelSamplingSD3 恰好继承 ModelSamplingDiscreteFlow 且 patch() 里
   multiplier 写死 1000 —— 与官方示例工作流一致（节点标题就叫
   "Official Flow sigma shift"）。
   用 ModelSamplingFlux（Qwen-Image 2.1 用的那个）会丢掉 multiplier 层
   -> sigma 表差 1000 倍 -> 出图退化成噪点。
   注意这与本项目 Qwen-Image 2.1 的取舍**正好相反**，因为两者 model_type 不同。

2. **latent 节点是 EmptyFlux2LatentImage（16 倍下采样）**
   插件 loaders.py 里 `latent_format = latent_formats.Flux2`，
   而 Flux2 的 `spacial_downscale_ratio = 16` -> 宽高必须是 16 的倍数。

3. **SLG（SkipLayerGuidanceDiTSimple）官方推荐 single_layers="22"**
   且该节点只改 uncond 分支；官方 USERGUIDE 说「关掉 SLG 会让画面非常写实，
   但部分细节会崩」。

4. **官方采样参数**：50 步 / cfg 3 / euler / sgm_uniform / shift 5。

5. **尺寸吸附规则**（官方 USERGUIDE + EmptyFlux2LatentImage 节点 schema）：
   - 非 16 倍数 -> 吸附到最近的 16 倍数，并给出**调整原因**
   - 总像素低于官方下限 0.25M -> 按比例放大并说明
   - 总像素高于官方直出上限 4M -> 按比例缩小并说明
   - 合法值 -> 原样通过，且**没有**调整原因

6. **LoRA 兼容性不能拿 Z-Image 的底模去比**
   MOSS 的底模是 diffusers 目录（没有单文件 UNET 可读），
   之前的 _list_loras_for_base 会把 None 传给 check_compatibility()，
   而那里默认 `unet_path or _z_image_unet_path()` —— 于是 MOSS 的 LoRA
   被拿去和 Z-Image 比键集，结论毫无意义。

7. **前端面板**：MOSS 的 SHIFT 量程必须能放下官方推荐的 5.0
   （Z-Image 用的 min=1/max=7 正好能放下，但 Qwen 用的 min=0/max=3 放不下）。

运行：python3 -m pytest tests/test_moss_image.py -v
需要：playwright（chromium）。缺失时整类 skip，不算失败。
"""
import json
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except Exception:                                   # pragma: no cover
    sync_playwright = None
    HAS_PLAYWRIGHT = False

from test_frontend_defaults import StudioServer  # noqa: E402

from h3studio import workflows as W                # noqa: E402
from h3studio import gen_types as GT               # noqa: E402
from h3studio import loras as lora_registry       # noqa: E402
from h3studio import enhancer as enh               # noqa: E402


# ------------------------------------------------------------------
# 后端
# ------------------------------------------------------------------
class TestMossBackend(unittest.TestCase):
    """后端：工作流结构 / 参数落到请求 / 类型注册（不依赖浏览器）。"""

    def build(self, **overrides):
        params = {"job_id": "moss1", "prompt": "a lighthouse at dusk",
                  "width": 1024, "height": 1024}
        params.update(overrides)
        return W.build_moss_t2i(params)

    # ---- 结构 ----

    def test_builder_is_registered(self):
        """t2i_moss 必须真的注册到 BUILDERS（不是只在前端加了个入口）。"""
        self.assertIn("t2i_moss", W.BUILDERS)

    def test_gen_type_is_registered_as_image(self):
        """生成类型注册表要有 t2i_moss，且 media=image（生成库「图片」筛选要能筛到）。"""
        self.assertIn("t2i_moss", GT.registered_modes())
        info = GT.type_for_mode("t2i_moss")
        self.assertEqual(info["media"], "image")
        self.assertEqual(info["category"], "t2i")

    def test_shift_uses_model_sampling_sd3_not_flux(self):
        """SHIFT 必须走 ModelSamplingSD3（FLOW + multiplier=1000 的采样类）。

        用 ModelSamplingFlux 会丢掉 multiplier 层 -> sigma 表差 1000 倍 -> 噪点。
        这与 Qwen-Image 2.1 的取舍正好相反（那个是 FLUX + ModelSamplingFlux）。
        """
        wf = self.build()
        self.assertIn("moss:15", wf)
        self.assertEqual(wf["moss:15"]["class_type"], "ModelSamplingSD3")
        self.assertNotIn("ModelSamplingFlux", [v["class_type"] for v in wf.values()])

    def test_plugin_path_multiplier_1000_requires_sd3_node(self):
        """【关键区分】插件路径 multiplier=1000 -> 必须 ModelSamplingSD3。

        本机 ComfyUI 是 v0.37.0（落后 origin/master 200 commit），**没有原生
        MOSS 支持**，走的是 custom_nodes/moss-image 插件路径。插件的
        MOSSImageConfig 写的是：

            sampling_settings = {"shift": 1.0, "multiplier": 1000}

        而上游 ComfyUI origin/master 的**原生** MOSSImage 类是：

            sampling_settings = {"multiplier": 1.0, "shift": 5.0}

        -> 原生路径要 ModelSamplingAuraFlow（multiplier=1），
           插件路径必须 ModelSamplingSD3（multiplier=1000）。

        这不是观点差异，是**实测 A/B 判定**的（同种子同提示词）：
          ModelSamplingSD3(shift=5)      -> 连贯图像（灯塔）
          ModelSamplingAuraFlow(shift=5) -> 纯噪点
        详见《集成改动说明.md》§0.1。

        本测试把「插件路径的 multiplier 契约」钉死 —— 将来若有人把节点
        换成 AuraFlow，或上游改了插件的 multiplier，这个用例会立刻失败。
        """
        plugin = Path("/home/ubuntu/comfyui/custom_nodes/moss-image/loaders.py")
        if not plugin.is_file():
            self.skipTest("本机没装 moss-image 插件")
        text = plugin.read_text(encoding="utf-8")
        self.assertIn('"multiplier": 1000', text,
                      "插件的 sampling_settings 应为 multiplier=1000 —— "
                      "若上游改了这条，本测试的结论需要重新用 A/B 实测验证")
        wf = self.build()
        self.assertEqual(wf["moss:15"]["class_type"], "ModelSamplingSD3")
        self.assertNotIn("ModelSamplingAuraFlow",
                         [v["class_type"] for v in wf.values()])

    def test_shift_value_reaches_the_request(self):
        """SHIFT 值要真的落到 ModelSamplingSD3 节点上。"""
        wf = self.build(shift=6.5)
        self.assertAlmostEqual(float(wf["moss:15"]["inputs"]["shift"]), 6.5, places=3)

    def test_latent_node_is_flux2(self):
        """latent 节点必须是 EmptyFlux2LatentImage（Flux2 = 16 倍下采样）。"""
        wf = self.build()
        self.assertEqual(wf["moss:6"]["class_type"], "EmptyFlux2LatentImage")
        self.assertNotIn("EmptySD3LatentImage", [v["class_type"] for v in wf.values()])

    def test_slg_uses_official_layer_22(self):
        """SLG 用官方推荐的 single_layers="22"，且只改 uncond 分支。"""
        wf = self.build()
        self.assertEqual(wf["moss:13"]["class_type"], "SkipLayerGuidanceDiTSimple")
        self.assertEqual(wf["moss:13"]["inputs"]["single_layers"], "22")
        self.assertEqual(wf["moss:13"]["inputs"]["double_layers"], "")

    def test_slg_layer_is_overridable(self):
        """用户可以改 SLG 层（22/24/26 都有帮助，空 = 关）。"""
        wf = self.build(slg_single_layers="26")
        self.assertEqual(wf["moss:13"]["inputs"]["single_layers"], "26")
        wf = self.build(slg_single_layers="")
        self.assertEqual(wf["moss:13"]["inputs"]["single_layers"], "")

    def test_official_defaults(self):
        """官方 USERGUIDE：50 步 / cfg 3 / euler / sgm_uniform / shift 5。"""
        wf = self.build()
        ks = wf["moss:16"]["inputs"]
        self.assertEqual(ks["steps"], 50)
        self.assertAlmostEqual(float(ks["cfg"]), 3.0, places=3)
        self.assertEqual(ks["sampler_name"], "euler")
        self.assertEqual(ks["scheduler"], "sgm_uniform")
        self.assertAlmostEqual(float(wf["moss:15"]["inputs"]["shift"]), 5.0, places=3)

    def test_all_params_reach_the_workflow(self):
        """面板上的每个参数都要真的进到工作流里。"""
        wf = self.build(steps=60, cfg=4, sampler="dpmpp_2m_sde",
                        scheduler="simple", shift=7, seed=12345)
        ks = wf["moss:16"]["inputs"]
        self.assertEqual(ks["steps"], 60)
        self.assertAlmostEqual(float(ks["cfg"]), 4.0, places=3)
        self.assertEqual(ks["sampler_name"], "dpmpp_2m_sde")
        self.assertEqual(ks["scheduler"], "simple")
        self.assertEqual(ks["seed"], 12345)
        self.assertAlmostEqual(float(wf["moss:15"]["inputs"]["shift"]), 7.0, places=3)

    def test_model_path_points_to_real_base_dir(self):
        """MOSSImageHFLoader 的 model_path 必须指向真实存在的底模目录。"""
        wf = self.build()
        path = wf["moss:1"]["inputs"]["model_path"]
        self.assertTrue(Path(path).is_dir(), f"底模目录不存在：{path}")
        for sub in ("transformer", "text_encoder", "tokenizer", "vae"):
            self.assertTrue((Path(path) / sub).is_dir(),
                            f"底模缺 {sub} 子目录：{path}")

    def test_model_path_switches_with_version(self):
        """换模型版本 -> 换底模目录（Base / SFT 是两套独立权重）。"""
        base = self.build(model="moss_image_base")
        sft = self.build(model="moss_image_sft")
        self.assertNotEqual(base["moss:1"]["inputs"]["model_path"],
                            sft["moss:1"]["inputs"]["model_path"])

    def test_missing_model_dir_gives_clear_error(self):
        """底模目录缺失 -> 明确中文错误，不是让 ComfyUI 报看不懂的错。"""
        original = dict(W.MOSS_IMAGE_MODELS["moss_image_base"])
        W.MOSS_IMAGE_MODELS["moss_image_base"]["model_path"] = "/nonexistent/moss"
        try:
            with self.assertRaises(ValueError) as ctx:
                self.build()
            self.assertIn("缺少", str(ctx.exception))
        finally:
            W.MOSS_IMAGE_MODELS["moss_image_base"].update(original)

    def test_negative_prompt_reaches_negative_conditioning(self):
        """负面提示词要接到负向条件上（官方 USERGUIDE 给了一组实测有效标签）。"""
        wf = self.build(negative_prompt="worst quality, watermark")
        self.assertEqual(wf["moss:5"]["class_type"], "CLIPTextEncode")
        self.assertEqual(wf["moss:5"]["inputs"]["text"], "worst quality, watermark")
        self.assertEqual(wf["moss:16"]["inputs"]["negative"], ["moss:5", 0])

    def test_vae_comes_from_hf_loader(self):
        """VAE 必须来自同一个 HF 加载器（官方工作流就是这么接的）。"""
        wf = self.build()
        self.assertEqual(wf["moss:17"]["inputs"]["vae"], ["moss:1", 2])

    # ---- LoRA ----

    def test_lora_chain_uses_model_only_loader(self):
        """MOSS 的 LoRA 只能接模型侧 —— 文本编码器是 Qwen3.5-9B，接 clip 会 patch 不到。"""
        name = self.any_moss_lora()
        if not name:
            self.skipTest("本机没有 MOSS LoRA")
        wf = self.build(loras=[{"name": name, "strength": 0.8}])
        lora_nodes = {k: v for k, v in wf.items()
                      if v.get("class_type", "").startswith("LoraLoader")}
        self.assertEqual(len(lora_nodes), 1)
        node = list(lora_nodes.values())[0]
        self.assertEqual(node["class_type"], "LoraLoaderModelOnly")
        self.assertNotIn("clip", node["inputs"])
        self.assertAlmostEqual(float(node["inputs"]["strength_model"]), 0.8, places=3)
        # LoRA 要串在 SLG 之后、KSampler 之前
        self.assertEqual(node["inputs"]["model"], ["moss:13", 0])
        self.assertEqual(wf["moss:16"]["inputs"]["model"],
                         [list(lora_nodes.keys())[0], 0])

    def test_missing_lora_file_is_rejected(self):
        """本机不存在的 LoRA 必须报错，不能静默忽略。"""
        with self.assertRaises(ValueError) as ctx:
            self.build(loras=[{"name": "moss/does_not_exist.safetensors",
                               "strength": 1.0}])
        self.assertIn("不存在", str(ctx.exception))

    def test_zero_strength_lora_is_skipped(self):
        """强度 0 = 关闭该 LoRA，不该产生节点。"""
        name = self.any_moss_lora()
        if not name:
            self.skipTest("本机没有 MOSS LoRA")
        wf = self.build(loras=[{"name": name, "strength": 0}])
        lora_nodes = [v for v in wf.values()
                      if v.get("class_type", "").startswith("LoraLoader")]
        self.assertEqual(lora_nodes, [])

    def test_lora_trigger_is_merged_into_prompt(self):
        """LoRA 的触发词要自动追加到最终提示词。"""
        name = self.any_moss_lora()
        if not name:
            self.skipTest("本机没有 MOSS LoRA")
        wf = self.build(loras=[{"name": name, "strength": 1.0,
                                 "trigger": "moss_xvstyle"}])
        self.assertIn("moss_xvstyle", wf["moss:4"]["inputs"]["text"])

    def test_lora_trigger_is_filled_from_registry_when_frontend_omits_it(self):
        """前端没传 trigger 时，后端要从注册表回填。

        这是本轮修掉的真缺陷：_lookup_lora_meta() 之前只查 Z-Image 族的清单，
        导致 MOSS / Qwen 的 LoRA 查不到元信息 -> 上游标注的触发词不会自动追加。
        """
        items = {i["name"]: i for i in lora_registry.list_moss_image_loras()}
        xv = [n for n in items if "xvstyle" in n]
        if not xv:
            self.skipTest("本机没有 xvstyle LoRA")
        name = xv[0]
        self.assertTrue(items[name]["trigger"], "该 LoRA 应有触发词标注")
        # 不传 trigger（模拟前端漏传）
        wf = self.build(loras=[{"name": name, "strength": 0.7}])
        self.assertIn(items[name]["trigger"], wf["moss:4"]["inputs"]["text"],
                      "触发词应从注册表回填并追加进提示词")

    def test_acceleration_lora_is_rejected(self):
        """加速/蒸馏类 LoRA 不能作为 MOSS 的可选效果 LoRA。"""
        accel = "Z-Image-Fun-Lora-Distill-8-Steps-2603-ComfyUI.safetensors"
        if not lora_registry.resolve_lora(accel)[1]:
            self.skipTest("本机没有该加速 LoRA")
        with self.assertRaises(ValueError) as ctx:
            self.build(loras=[{"name": accel, "strength": 1.0}])
        self.assertIn("加速", str(ctx.exception))

    def test_cross_family_lora_is_rejected(self):
        """别的底模族的 LoRA（Z-Image 的）不能挂到 MOSS 上。

        不拒绝的话 ComfyUI 会静默丢弃（只打 "lora key not loaded" 的 WARNING），
        出图照跑但 LoRA 根本没生效 —— 静默失效比报错更难查。
        """
        other = "Kook_Zimage_瑶光.safetensors"
        if not lora_registry.resolve_lora(other)[1]:
            self.skipTest("本机没有该 Z-Image LoRA")
        with self.assertRaises(ValueError) as ctx:
            self.build(loras=[{"name": other, "strength": 1.0}])
        self.assertIn("MOSS", str(ctx.exception))

    def test_moss_family_detection(self):
        """MOSS 族判定：清单里的算、放在 moss/ 目录下的也算、外族的不算。"""
        items = lora_registry.list_moss_image_loras()
        if not items:
            self.skipTest("本机没有 MOSS LoRA")
        self.assertTrue(lora_registry.is_moss_family_lora(items[0]["name"]))
        self.assertFalse(lora_registry.is_moss_family_lora("Kook_Zimage_瑶光.safetensors"))
        self.assertFalse(lora_registry.is_moss_family_lora(""))

    def test_moss_lora_compatibility_is_not_compared_against_z_image(self):
        """MOSS 的 LoRA 兼容性不能拿 Z-Image 的 UNET 去比。

        底模是 diffusers 目录（没有单文件 UNET），之前 None 会被
        check_compatibility() 的默认参数兜成 Z-Image 的 UNET ——
        结论毫无意义但 verified 却是 True。
        """
        items = lora_registry.list_moss_image_loras()
        if not items:
            self.skipTest("本机没有 MOSS LoRA")
        for item in items:
            self.assertFalse(item["compat_verified"],
                             f"{item['name']} 不该被标成「已做权重比对」"
                             "（MOSS 底模没有单文件 UNET 可比）")

    def test_moss_lora_registry_is_separate_from_other_families(self):
        """MOSS 的 LoRA 不能混进 Z-Image / Qwen 的列表。"""
        moss = {i["name"] for i in lora_registry.list_moss_image_loras()}
        zimg = {i["name"] for i in lora_registry.list_effect_loras()}
        qwen = {i["name"] for i in lora_registry.list_qwen_effect_loras()}
        self.assertFalse(moss & zimg, "MOSS 的 LoRA 不能出现在 Z-Image 列表")
        self.assertFalse(moss & qwen, "MOSS 的 LoRA 不能出现在 Qwen 列表")

    def test_moss_lora_prefs_use_own_key(self):
        """MOSS 的 LoRA 配置要存自己的 key（底模不同，不能混）。"""
        self.assertEqual(lora_registry.pref_key_for_base("MOSS-Image"),
                         lora_registry.MOSS_PREF_KEY)
        self.assertNotEqual(lora_registry.MOSS_PREF_KEY,
                            lora_registry.DEFAULT_PREF_KEY)
        self.assertNotEqual(lora_registry.MOSS_PREF_KEY,
                            lora_registry.QWEN_PREF_KEY)

    def test_moss_recommended_strength_comes_from_upstream_metadata(self):
        """上游标注的推荐强度要保留（xvstyle 是 0.7，不能被默认值 1.0 覆盖）。"""
        items = {i["name"]: i for i in lora_registry.list_moss_image_loras()}
        xv = [n for n in items if "xvstyle" in n]
        if not xv:
            self.skipTest("本机没有 xvstyle LoRA")
        self.assertAlmostEqual(
            float(items[xv[0]]["recommended_strength"]), 0.7, places=3)

    # ---- 提示词增强 ----

    def test_enhancer_has_moss_template(self):
        """MOSS 要有自己的提示词增强模板，且只对 moss_image 模型族生效。"""
        specs = {s["id"]: s for s in enh.list_specs()}
        self.assertIn("moss-image", specs)
        self.assertEqual(specs["moss-image"]["families"], ["moss_image"])
        self.assertEqual(specs["moss-image"]["media"], "image")

    def test_enhancer_moss_mode_is_image_media(self):
        """t2i_moss 的 media 是 image（否则增强 Agent 选不出模板）。"""
        self.assertEqual(enh.MODE_MEDIA.get("t2i_moss"), "image")

    def test_enhancer_moss_model_family_registered(self):
        """两个 MOSS 模型版本都要映射到 moss_image 族。"""
        self.assertEqual(enh.model_family("moss_image_base"), "moss_image")
        self.assertEqual(enh.model_family("moss_image_sft"), "moss_image")

    def test_enhancer_template_file_exists(self):
        """模板正文要真的落盘（缺文件时增强会退回内置兜底）。"""
        path = enh.ASSETS_DIR / "moss-image.txt"
        self.assertTrue(path.is_file(), f"模板文件缺失：{path}")
        self.assertGreater(len(path.read_text(encoding="utf-8")), 200)

    # ---- 辅助 ----

    def any_moss_lora(self):
        items = lora_registry.list_moss_image_loras()
        return items[0]["name"] if items else None


class TestMossSizeNormalization(unittest.TestCase):
    """尺寸吸附规则（官方 USERGUIDE + EmptyFlux2LatentImage 节点 schema）。"""

    def test_non_multiple_snaps_to_16(self):
        """非 16 倍数 -> 吸附，并说明原因。"""
        w, h, reasons = W.normalize_moss_image_size(2559, 1401)
        self.assertEqual((w, h), (2560, 1408))
        self.assertTrue(reasons, "必须给出调整原因（界面要显示给用户）")
        self.assertTrue(any("2559" in r for r in reasons))
        self.assertTrue(any("1401" in r for r in reasons))

    def test_valid_size_passes_through(self):
        """合法值原样通过，且没有调整原因。"""
        w, h, reasons = W.normalize_moss_image_size(1024, 1024)
        self.assertEqual((w, h), (1024, 1024))
        self.assertEqual(reasons, [], "合法值不该有任何调整原因")

    def test_below_official_min_is_scaled_up(self):
        """总像素低于官方下限 0.25M -> 按比例放大并说明。"""
        w, h, reasons = W.normalize_moss_image_size(16, 16)
        self.assertGreaterEqual(W.moss_image_megapixels(w, h),
                                W.MOSS_IMAGE_MIN_MP - 1e-6)
        self.assertTrue(any("下限" in r for r in reasons))

    def test_above_official_max_is_scaled_down(self):
        """总像素高于官方直出上限 4M -> 按比例缩小并说明。"""
        w, h, reasons = W.normalize_moss_image_size(4096, 4096)
        self.assertLessEqual(W.moss_image_megapixels(w, h),
                             W.MOSS_IMAGE_MAX_DIRECT_MP + 1e-6)
        self.assertTrue(any("上限" in r for r in reasons))

    def test_clamps_to_max_resolution(self):
        """单边超过 16384 -> 钳到上限。"""
        w, h, reasons = W.normalize_moss_image_size(20000, 1000)
        self.assertLessEqual(w, W.Z_IMAGE_SIZE_MAX)
        self.assertTrue(any("上限" in r for r in reasons))

    def test_rejects_non_positive(self):
        """非正数 -> 明确报错信息（不抛异常，界面要显示）。"""
        w, h, reasons = W.normalize_moss_image_size(0, -5)
        self.assertEqual((w, h), (0, 0))
        self.assertTrue(reasons)

    def test_size_multiple_is_16(self):
        """倍数必须是 16 —— Flux2 VAE 是 16 倍下采样。"""
        self.assertEqual(W.MOSS_IMAGE_SIZE_MULTIPLE, 16)
        self.assertEqual(W.Z_IMAGE_SIZE_MAX, 16384)

    def test_tier_dimensions_are_16_multiples(self):
        """各档位算出来的宽高都必须是 16 的倍数。"""
        for tier in W.MOSS_IMAGE_RES_TIERS:
            w, h = W.moss_image_dimensions("16:9", tier)
            self.assertEqual(w % 16, 0, f"{tier} 宽度不是 16 的倍数")
            self.assertEqual(h % 16, 0, f"{tier} 高度不是 16 的倍数")

    def test_official_tiers_within_recommended_range(self):
        """官方推荐区间是 1M~2M，档位表不能漏掉 1M 和 2M。"""
        self.assertIn("1mp", W.MOSS_IMAGE_RES_TIERS)
        self.assertIn("2k", W.MOSS_IMAGE_RES_TIERS)
        self.assertAlmostEqual(W.MOSS_IMAGE_RES_TIERS["1mp"], 1.0, places=3)
        self.assertAlmostEqual(W.MOSS_IMAGE_RES_TIERS["2k"], 4.0, places=3)


# ------------------------------------------------------------------
# 前端（真实浏览器）
# ------------------------------------------------------------------
@unittest.skipUnless(HAS_PLAYWRIGHT, "需要 playwright（chromium）")
class TestMossFrontend(unittest.TestCase):
    """前端：导航入口 / 参数面板按 MOSS 切换 / 尺寸吸附提示。"""

    @classmethod
    def setUpClass(cls):
        cls.server = StudioServer(seed_jobs=0)
        assert cls.server.start(), "Studio 实例启动失败"
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(args=["--no-sandbox"])
        cls.ctx = cls.browser.new_context(viewport={"width": 1920, "height": 1080})
        cls.page = cls.ctx.new_page()
        cls.errors = []
        cls.page.on("pageerror", lambda e: cls.errors.append(str(e)))
        cls.page.goto("http://127.0.0.1:%d/" % cls.server.port,
                      wait_until="domcontentloaded")
        cls.page.wait_for_timeout(2500)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.stop()

    def options(self):
        import urllib.request
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.server.port}/api/image-options",
                timeout=10) as r:
            return json.loads(r.read().decode())

    def test_nav_entry_exists_under_t2i_group(self):
        """「文生图」分类下要有 MOSS-Image 入口。"""
        groups = self.page.evaluate("""() => {
          const out = [];
          document.querySelectorAll('.nav-group').forEach(g => {
            const items = [];
            let n = g.nextElementSibling;
            while (n && n.classList.contains('nav-item')) {
              items.push(n.getAttribute('data-mode'));
              n = n.nextElementSibling;
            }
            out.push({label: g.textContent.trim(), items: items});
          });
          return out;
        }""")
        by_label = {g["label"]: g["items"] for g in groups}
        self.assertIn("文生图", by_label)
        self.assertIn("t2i_moss", by_label["文生图"])
        self.assertIn("t2i", by_label["文生图"])
        self.assertIn("t2i_qwen", by_label["文生图"])

    def test_switch_to_moss_uses_official_defaults(self):
        """切到 MOSS：参数面板换成官方 USERGUIDE 的推荐值。"""
        self.page.evaluate(
            "() => document.querySelector('.nav-item[data-mode=\"t2i_moss\"]').click()")
        self.page.wait_for_timeout(1500)
        self.assertEqual(self.page.evaluate("() => state.mode"), "t2i_moss")
        moss = self.options().get("moss", {})
        models = moss.get("models", {})
        default_key = moss.get("default_model", "moss_image_base")
        m = models.get(default_key, {})
        s = self.page.evaluate("""() => ({
          steps: state.imgMoss.steps, cfg: state.imgMoss.cfg, shift: state.imgMoss.shift,
          sampler: state.imgMoss.sampler, scheduler: state.imgMoss.scheduler,
          model: state.imgMoss.model,
          stepsShown: document.querySelector('#imgStepsVal').textContent,
          shiftShown: document.querySelector('#imgShiftVal').textContent,
          shiftMin: document.querySelector('#imgShift').min,
          shiftMax: document.querySelector('#imgShift').max,
          cfgMin: document.querySelector('#imgCfg').min,
          cfgMax: document.querySelector('#imgCfg').max,
          slgVisible: getComputedStyle(document.querySelector('#imgSlgField')).display !== 'none',
          slgNote: document.querySelector('#imgSlgNote').textContent,
          peVisible: getComputedStyle(document.querySelector('#imgPeField')).display !== 'none',
          refVisible: getComputedStyle(document.querySelector('#imgRefField')).display !== 'none',
          loras: Array.from(document.querySelectorAll('#imgLoraSelect option')).map(o => o.textContent),
        })""")
        self.assertEqual(s["steps"], m.get("steps", 50))
        self.assertAlmostEqual(float(s["cfg"]), m.get("cfg", 3.0), places=3)
        self.assertAlmostEqual(float(s["shift"]), m.get("shift", 5.0), places=3)
        self.assertEqual(s["sampler"], m.get("sampler", "euler"))
        self.assertEqual(s["scheduler"], m.get("scheduler", "sgm_uniform"))
        self.assertEqual(s["model"], default_key)
        # 滑块显示的值必须与真实值一致（量程不能把它夹掉）
        self.assertEqual(s["stepsShown"], str(m.get("steps", 50)))
        self.assertAlmostEqual(float(s["shiftShown"]), m.get("shift", 5.0), places=3)
        # SHIFT 量程必须放得下官方推荐的 5.0
        self.assertLessEqual(float(s["shiftMin"]), m.get("shift", 5.0))
        self.assertGreaterEqual(float(s["shiftMax"]), m.get("shift", 5.0))
        # CFG 量程要放得下官方推荐 3.0~6.0
        self.assertLessEqual(float(s["cfgMin"]), 3.0)
        self.assertGreaterEqual(float(s["cfgMax"]), 6.0)
        # SLG 区块只在 MOSS 可见
        self.assertTrue(s["slgVisible"], "MOSS 应显示 SLG skip layer 设置")
        self.assertIn("22", s["slgNote"])
        # PE / 参考图是 Qwen 专属，MOSS 不显示
        self.assertFalse(s["peVisible"], "MOSS 不该显示 PE 开关")
        self.assertFalse(s["refVisible"], "MOSS 不该显示参考图区")
        # LoRA 列表是 MOSS 的那批
        self.assertTrue(any("MOSS" in t for t in s["loras"]),
                        f"LoRA 列表应含 MOSS 的条目，实际 {s['loras']}")

    def test_size_adjustment_is_shown_to_user(self):
        """手填非 16 倍数的宽高 -> 界面显示「输入值 → 实际使用值」及原因。"""
        self.page.evaluate(
            "() => document.querySelector('.nav-item[data-mode=\"t2i_moss\"]').click()")
        self.page.wait_for_timeout(1200)
        # 走与用户相同的路径：input 事件写入 state，再 change 触发吸附写回
        self.page.evaluate("""() => {
          const w = document.querySelector('#imgWidth');
          w.value = '2559';
          w.dispatchEvent(new Event('input', {bubbles: true}));
          w.dispatchEvent(new Event('change', {bubbles: true}));
          const h = document.querySelector('#imgHeight');
          h.value = '1401';
          h.dispatchEvent(new Event('input', {bubbles: true}));
          h.dispatchEvent(new Event('change', {bubbles: true}));
        }""")
        self.page.wait_for_timeout(600)
        s = self.page.evaluate("""() => ({
          width: state.imgMoss.width, height: state.imgMoss.height,
          rawWidth: state.imgMoss.rawWidth, rawHeight: state.imgMoss.rawHeight,
          adjustHidden: document.querySelector('#imgSizeAdjust').hidden,
          adjustText: document.querySelector('#imgSizeAdjust').textContent,
          hint: document.querySelector('#imgSizeHint').textContent,
        })""")
        self.assertEqual(s["width"], 2560)
        self.assertEqual(s["height"], 1408)
        self.assertEqual(s["rawWidth"], 2559)
        self.assertEqual(s["rawHeight"], 1401)
        self.assertFalse(s["adjustHidden"], "有调整时必须显示说明")
        self.assertIn("2559", s["adjustText"])
        self.assertIn("2560", s["adjustText"])
        self.assertIn("2560×1408", s["hint"])

    def test_moss_size_over_official_max_is_clamped_with_reason(self):
        """超过官方直出上限的尺寸 -> 被缩到 4MP 以内，并说明原因。"""
        self.page.evaluate(
            "() => document.querySelector('.nav-item[data-mode=\"t2i_moss\"]').click()")
        self.page.wait_for_timeout(1200)
        self.page.evaluate("""() => {
          const w = document.querySelector('#imgWidth');
          w.value = '4096';
          w.dispatchEvent(new Event('input', {bubbles: true}));
          w.dispatchEvent(new Event('change', {bubbles: true}));
          const h = document.querySelector('#imgHeight');
          h.value = '4096';
          h.dispatchEvent(new Event('input', {bubbles: true}));
          h.dispatchEvent(new Event('change', {bubbles: true}));
        }""")
        self.page.wait_for_timeout(600)
        s = self.page.evaluate("""() => ({
          width: state.imgMoss.width, height: state.imgMoss.height,
          adjustHidden: document.querySelector('#imgSizeAdjust').hidden,
          adjustText: document.querySelector('#imgSizeAdjust').textContent,
        })""")
        mp = s["width"] * s["height"] / (1024 * 1024)
        self.assertLessEqual(mp, 4.0 + 1e-6,
                             f"应缩到官方直出上限 4MP 以内，实际 {mp}MP")
        self.assertFalse(s["adjustHidden"], "有调整时必须显示说明")
        self.assertIn("上限", s["adjustText"])

    def test_zimage_mode_unchanged_after_moss(self):
        """切回 Z-IMAGE：参数仍是它自己的推荐值（SHIFT 3 / min=1）。"""
        self.page.evaluate(
            "() => document.querySelector('.nav-item[data-mode=\"t2i\"]').click()")
        self.page.wait_for_timeout(1200)
        s = self.page.evaluate("""() => ({
          shift: state.img.shift,
          shiftMin: document.querySelector('#imgShift').min,
          slgVisible: getComputedStyle(document.querySelector('#imgSlgField')).display !== 'none',
        })""")
        self.assertAlmostEqual(float(s["shift"]), 3.0, places=3)
        self.assertEqual(s["shiftMin"], "1")
        self.assertFalse(s["slgVisible"], "Z-IMAGE 不该显示 SLG 区块")

    def test_qwen_mode_unchanged_after_moss(self):
        """切到 Qwen：SHIFT 量程仍是 0~3（不能被 MOSS 的量程污染）。"""
        self.page.evaluate(
            "() => document.querySelector('.nav-item[data-mode=\"t2i_qwen\"]').click()")
        self.page.wait_for_timeout(1200)
        s = self.page.evaluate("""() => ({
          shift: state.imgQwen.shift,
          shiftMin: document.querySelector('#imgShift').min,
          shiftMax: document.querySelector('#imgShift').max,
          slgVisible: getComputedStyle(document.querySelector('#imgSlgField')).display !== 'none',
        })""")
        self.assertAlmostEqual(float(s["shift"]), 0.69, places=3)
        self.assertEqual(s["shiftMin"], "0")
        self.assertEqual(s["shiftMax"], "3")
        self.assertFalse(s["slgVisible"], "Qwen 不该显示 SLG 区块")

    def test_no_console_errors(self):
        self.assertEqual([e for e in self.errors if e.strip()], [])


if __name__ == "__main__":
    unittest.main()
