# -*- coding: utf-8 -*-
"""Qwen-Image 2.1 接入的回归测试（真实后端 + 真实浏览器）。

覆盖本轮实测挖出并修复的三个真缺陷（不测它们就会回归）：

1. **SHIFT 节点选错 -> 出图是紫底绿斑的噪点**
   Qwen-Image 2.1 走 model_type=FLUX -> ModelSamplingFlux（sigma 公式
   flux_time_shift，没有 multiplier 这一层）。用 ModelSamplingSD3 覆盖时，
   它的 patch() 把 multiplier 写死 1000 -> sigma 表放大 1000 倍 -> 噪点。
   正确做法：ModelSamplingFlux + base_shift = max_shift = 用户值
   （mm=0、b=S -> shift 恒等于 S）。

2. **PE 编码器接错位置**
   PE 编码器（qwen3.5_qwen_image_2.1_pe_*）接的是 **TextGenerate**
   （提示词扩写节点），扩写后的文本再喂给 TextEncodeQwenImage21 的 prompt；
   TextEncodeQwenImage21 的 clip 输入接的是普通编码器 qwen3vl_8b_bf16。

   注意：不能用 TextGenerateLTX2Prompt —— 那是 LTX2 **视频**用的节点，
   它会强制套 Gemma 对话模板并塞入 LTX2 的视频提示词 system prompt，
   官方 Qwen PE 工作流用的是 TextGenerate + 官方 PE 自己的 system prompt。

3. **参考图的 COMFY_AUTOGROW_V3 API 格式**
   必须写成**扁平点号键** `"images.image_1": <值>, "images.image_2": ...`。
   写成嵌套的 `"images": {"image_1": <值>}` 不会报错，只会把参考图**静默丢弃**
   （实测出图尺寸 1024×1024，说明参考图没生效）；写成顶层扁平的 `"image_1"`
   则会被当成 execute() 的关键字参数，报
   TypeError: TextEncodeQwenImage21.execute() got an unexpected keyword
   argument 'image_1'。

运行：python3 -m pytest tests/test_qwen_image.py -v
需要：playwright（chromium）。缺失时整类 skip，不算失败。
"""
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
except Exception:                                   # pragma: no cover
    sync_playwright = None
    HAS_PLAYWRIGHT = False

from test_frontend_defaults import StudioServer  # noqa: E402


QWEN_MODELS = ("t2i_qwen", "i2i_qwen")


def seed_jobs(data_dir, count=6):
    conn = sqlite3.connect(data_dir / "jobs.db")
    now = time.time()
    for i in range(count):
        params = {"title": f"qwen 探针 {i}", "prompt": f"probe {i}",
                  "aspect": "16:9", "width": 1360, "height": 768,
                  "model": "qwen_image_2.1_bf16", "steps": 50, "cfg": 1,
                  "shift": 0.69, "sampler": "euler", "scheduler": "simple",
                  "use_pe": True, "batch_count": 1}
        conn.execute(
            "INSERT OR REPLACE INTO jobs (id, created_at, mode, params, status,"
            " progress, progress_text, outputs) VALUES (?,?,?,?,?,?,?,?)",
            (f"qwenprobe{i:03d}", now - i * 60, "t2i_qwen",
             json.dumps(params, ensure_ascii=False),
             "done", 1.0, "", "[]"))
    conn.commit()
    conn.close()


class TestQwenBackend(unittest.TestCase):
    """后端：构建器结构 / 参数落到请求 / 10 图校验（不依赖浏览器）。"""

    @classmethod
    def setUpClass(cls):
        from h3studio import workflows as W
        cls.W = W

    def build_t2i(self, **over):
        params = {"job_id": "probe", "prompt": "一只橘猫", "model": "qwen_image_2.1_bf16",
                  "aspect": "16:9", "resolution": "1mp", "seed": 42}
        params.update(over)
        return self.W.build_qwen_t2i(params)

    def build_i2i(self, n=10, **over):
        params = {"job_id": "probe", "prompt": "换头", "model": "qwen_image_2.1_bf16",
                  "aspect": "1:1", "resolution": "1mp", "seed": 42,
                  "ref_images": [f"h3studio/ref{i}.png" for i in range(n)]}
        params.update(over)
        return self.W.build_qwen_i2i(params)

    # ---------------------------------------------- 缺陷 1：SHIFT 节点
    def test_shift_uses_model_sampling_flux_not_sd3(self):
        """SHIFT 必须用 ModelSamplingFlux，不能用 ModelSamplingSD3。

        ModelSamplingSD3 的 patch() 把 multiplier 写死 1000，而
        Qwen-Image 2.1 走 ModelSamplingFlux（无 multiplier 那一层），
        套上去会让 sigma 表放大 1000 倍 -> 出图退化成噪点。
        """
        p = self.build_t2i(shift=0.69)
        classes = {n["class_type"] for n in p.values()}
        self.assertNotIn("ModelSamplingSD3", classes,
                         "不能用 ModelSamplingSD3 覆盖 SHIFT（multiplier 写死 1000）")
        node = next((n for n in p.values() if n["class_type"] == "ModelSamplingFlux"), None)
        self.assertIsNotNone(node, "缺少 ModelSamplingFlux 节点")
        # base_shift = max_shift = 用户值 -> shift 恒等于该值，与分辨率无关
        self.assertAlmostEqual(node["inputs"]["base_shift"], 0.69, places=4)
        self.assertAlmostEqual(node["inputs"]["max_shift"], 0.69, places=4)

    def test_shift_value_reaches_the_request(self):
        """用户调的 SHIFT 必须落到 ModelSamplingFlux 的输入里。"""
        for shift in (0.5, 0.69, 1.0, 2.5):
            p = self.build_t2i(shift=shift)
            node = next(n for n in p.values() if n["class_type"] == "ModelSamplingFlux")
            self.assertAlmostEqual(node["inputs"]["base_shift"], shift, places=4)
            self.assertAlmostEqual(node["inputs"]["max_shift"], shift, places=4)

    def test_default_shift_is_official_value(self):
        """默认 SHIFT = 0.69（模型内置 supported_models.py 的 sampling_settings）。"""
        p = self.build_t2i()
        node = next(n for n in p.values() if n["class_type"] == "ModelSamplingFlux")
        self.assertAlmostEqual(node["inputs"]["base_shift"], 0.69, places=4)

    # ---------------------------------------------- 缺陷 2：PE 接线
    def test_pe_encoder_feeds_prompt_expansion_node(self):
        """PE 编码器接 TextGenerateLTX2Prompt，不接 TextEncodeQwenImage21.clip。"""
        p = self.build_t2i()
        pe_loader = next(n for n in p.values()
                         if n["class_type"] == "CLIPLoader"
                         and "pe_t2i" in n["inputs"]["clip_name"])
        cond_loader = next(n for n in p.values()
                           if n["class_type"] == "CLIPLoader"
                           and n["inputs"]["clip_name"] != pe_loader["inputs"]["clip_name"])
        # PE 编码器 -> 扩写节点
        pe_id = next(k for k, n in p.items() if n is pe_loader)
        expand = next((k for k, n in p.items()
                       if n["class_type"] == "TextGenerate"), None)
        self.assertIsNotNone(expand, "缺少 TextGenerate（PE 扩写节点）")
        self.assertEqual(p[expand]["inputs"]["clip"], [pe_id, 0],
                         "PE 编码器应接扩写节点的 clip")
        # 条件编码器 -> TextEncodeQwenImage21.clip
        cond_id = next(k for k, n in p.items() if n is cond_loader)
        te = next(n for n in p.values() if n["class_type"] == "TextEncodeQwenImage21")
        self.assertEqual(te["inputs"]["clip"], [cond_id, 0],
                         "条件编码器应接 TextEncodeQwenImage21 的 clip")
        # 扩写后的文本 -> TextEncodeQwenImage21.prompt
        self.assertEqual(te["inputs"]["prompt"], [expand, 0],
                         "扩写节点输出应接 TextEncodeQwenImage21 的 prompt")

    def test_pe_t2i_and_i2i_use_different_encoders(self):
        """文生图用 pe_t2i，图片编辑用 pe_i2i —— 两个是不同的权重。"""
        t2i = self.build_t2i()
        i2i = self.build_i2i(n=1)
        t2i_clip = next(n["inputs"]["clip_name"] for n in t2i.values()
                        if n["class_type"] == "CLIPLoader" and "pe_" in n["inputs"]["clip_name"])
        i2i_clip = next(n["inputs"]["clip_name"] for n in i2i.values()
                        if n["class_type"] == "CLIPLoader" and "pe_" in n["inputs"]["clip_name"])
        self.assertIn("pe_t2i", t2i_clip)
        self.assertIn("pe_i2i", i2i_clip)
        self.assertNotEqual(t2i_clip, i2i_clip)

    def test_prompt_expansion_node_api_shape(self):
        """TextGenerate 的 API 格式：sampling_mode 是选项 key 字符串。"""
        p = self.build_t2i()
        expand = next(n for n in p.values() if n["class_type"] == "TextGenerate")
        self.assertEqual(expand["inputs"]["sampling_mode"], "on")
        for key in ("sampling_mode.temperature", "sampling_mode.top_k",
                    "sampling_mode.top_p", "sampling_mode.min_p",
                    "sampling_mode.repetition_penalty", "sampling_mode.seed"):
            self.assertIn(key, expand["inputs"], f"缺少 {key}")

    def test_use_pe_off_skips_expansion(self):
        """关掉 PE 开关 -> 不建扩写节点，提示词原样进模型。"""
        p = self.build_t2i(use_pe=False)
        classes = {n["class_type"] for n in p.values()}
        self.assertNotIn("TextGenerate", classes,
                         "关掉 PE 后不该有扩写节点")
        te = next(n for n in p.values() if n["class_type"] == "TextEncodeQwenImage21")
        self.assertIsInstance(te["inputs"]["prompt"], str,
                              "关掉 PE 后 prompt 应是原字符串")

    # ---------------------------------------------- 官方事实核对
    def test_tiers_stop_at_native_2k(self):
        """档位只到 2K —— 官方说 2.1 原生只支持到 2048×2048 直出。

        官方模板 note 原文：
          "Qwen Image 2.1 has native 2K (2048*2048) support for direct output."
        4K（8.3MP）超出原生能力，官方没有对应路径，列出来会崩图。
        """
        self.assertNotIn("4k", self.W.QWEN_IMAGE_RES_TIERS,
                         "2.1 原生只到 2K，不该列 4K 档")
        self.assertIn("2k", self.W.QWEN_IMAGE_RES_TIERS)
        self.assertLessEqual(self.W.QWEN_IMAGE_MAX_DIRECT_MP, 4.2,
                             "直出上限应对应 2K（1:1 时 2048×2048）")
        # 2K 档 1:1 应当正好是 2048×2048
        w, h = self.W.qwen_image_dimensions("1:1", "2k")
        self.assertEqual((w, h), (2048, 2048),
                         f"2K 档 1:1 应是 2048×2048，实际 {w}×{h}")

    def test_ref_image_resolution_matches_official(self):
        """参考图重采样目标边长：文生图 1024 / 图片编辑 2048（官方工作流的值）。"""
        t2i = self.build_t2i()
        te = next(n for n in t2i.values() if n["class_type"] == "TextEncodeQwenImage21")
        self.assertEqual(te["inputs"]["resolution"], 1024,
                         "文生图的参考图分辨率官方是 1024")
        i2i = self.build_i2i(n=1)
        te2 = next(n for n in i2i.values() if n["class_type"] == "TextEncodeQwenImage21")
        self.assertEqual(te2["inputs"]["resolution"], 2048,
                         "图片编辑的参考图分辨率官方是 2048（用 1024 会把参考图降采样过多）")

    def test_negative_prompt_only_effective_above_cfg1(self):
        """cfg=1 时负面提示词无效 —— 界面必须提示这一点。

        ComfyUI samplers.py:609-627：cfg=1 时 uncond 分支被跳过，
        negative_prompt 数学上无效。官方模板 note 也写着
        "negative_prompt: unused while cfg is 1."
        """
        m = self.W.QWEN_IMAGE_MODELS["qwen_image_2.1_bf16"]
        self.assertTrue(m["supports_negative"], "Qwen 非蒸馏，负面提示词本身是支持的")
        self.assertTrue(m.get("negative_effective_only_above_cfg1"),
                        "应标记「负面提示词只在 cfg>1 时生效」，界面据此提示")
        self.assertAlmostEqual(m["cfg"], 1.0, places=3,
                               msg="官方路径默认 cfg=1")

    # ---------------------------------------------- PE 双档参数
    def test_pe_presence_penalty_differs_by_tier(self):
        """PE-T2I 与 PE-I2I 的 presence_penalty 不可互换。

        官方 PE model card：
          PE-T2I  presence_penalty=1.5  max_new_tokens=16256
          PE-I2I  presence_penalty=0    max_new_tokens=24000
        填错不报错，只会让采样分布悄悄变 —— 所以档位必须随模式走。
        """
        t2i = self.build_t2i()
        i2i = self.build_i2i(n=2)
        pe_t2i = next(n for n in t2i.values() if n["class_type"] == "TextGenerate")
        pe_i2i = next(n for n in i2i.values() if n["class_type"] == "TextGenerate")
        self.assertAlmostEqual(pe_t2i["inputs"]["sampling_mode.presence_penalty"],
                               1.5, places=3)
        self.assertAlmostEqual(pe_i2i["inputs"]["sampling_mode.presence_penalty"],
                               0.0, places=3)
        self.assertEqual(pe_t2i["inputs"]["max_length"], 16256)
        self.assertEqual(pe_i2i["inputs"]["max_length"], 24000)

    def test_pe_max_length_uses_official_card_value(self):
        """max_length 不能压到 4096（官方模板的做法）—— 会让 PE 静默返回空串。

        官方文档原文警告：
          "When the reasoning block uses up the 4096 budget, it never closes
           and the enhancer returns an empty string."
        官方 ComfyUI 模板把 max_length 压到 4096，本面板用官方卡值。
        """
        params = self.W.QWEN_IMAGE_PE_PARAMS
        self.assertEqual(params["t2i"]["max_new_tokens"], 16256)
        self.assertEqual(params["i2i"]["max_new_tokens"], 24000)
        p = self.build_t2i()
        pe = next(n for n in p.values() if n["class_type"] == "TextGenerate")
        self.assertGreater(pe["inputs"]["max_length"], 4096,
                           "max_length 必须大于 4096，否则开 thinking 会静默返回空串")
        # thinking 必须开（官方 PE model card: thinking 必开）
        self.assertTrue(pe["inputs"]["thinking"], "官方 PE 的 thinking 必须开")

    def test_pe_uses_official_system_prompt(self):
        """PE 节点要带官方 PE 的 system prompt（不是模型内置模板）。

        官方工作流用 TextGenerate + PrimitiveStringMultiline 接的官方
        system prompt；本面板把两份官方 prompt 落盘到 skill assets 里。
        """
        p = self.build_t2i()
        pe = next(n for n in p.values() if n["class_type"] == "TextGenerate")
        sp = pe["inputs"].get("system_prompt", "")
        self.assertGreater(len(sp), 5000,
                           f"PE system prompt 长度异常：{len(sp)}（应为官方 PE 原文）")
        self.assertIn("Image Prompt Rewriting Expert", sp,
                      "应是官方 PE-T2I 的 system prompt")
        p2 = self.build_i2i(n=2)
        pe2 = next(n for n in p2.values() if n["class_type"] == "TextGenerate")
        sp2 = pe2["inputs"].get("system_prompt", "")
        self.assertNotEqual(sp, sp2, "文生图与编辑档的 system prompt 必须不同")

    def test_pe_node_is_textgenerate_not_ltx2(self):
        """不能用 TextGenerateLTX2Prompt —— 那是 LTX2 视频节点，会套错模板。"""
        p = self.build_t2i()
        classes = {n["class_type"] for n in p.values()}
        self.assertIn("TextGenerate", classes)
        self.assertNotIn("TextGenerateLTX2Prompt", classes,
                         "TextGenerateLTX2Prompt 会塞入 LTX2 视频提示词 system prompt")

    # ---------------------------------------------- 缺陷 3：参考图 API 格式
    def test_reference_images_use_flat_dotted_keys(self):
        """参考图必须写成**扁平点号键** "images.image_N"（COMFY_AUTOGROW_V3 的 API 格式）。

        实测结论（2026-11-08，同一张参考图 + resolution=1024，参考图 1360×768）：
          写法 A  "images.image_1": ["node",0]  -> 出图 1376×768（参考图生效）
          写法 B  "images": {"image_1": [...]} -> 出图 1024×1024（参考图被静默丢弃）

        ComfyUI 在 execution.py:296 用 build_nested_inputs() 依据 schema 算出的
        dynamic_paths，把**扁平点号键**还原成嵌套 dict 传给 execute()。
        写成嵌套字典不会报错、只会把参考图悄悄丢掉 —— 表现为「编辑结果与
        输入图无关、输出尺寸变成方图」。

        这条断言就是为了防止再退回嵌套字典写法（上一版就是栽在这里）。
        """
        p = self.build_i2i(n=10)
        te = next(n for n in p.values() if n["class_type"] == "TextEncodeQwenImage21")
        # 必须是扁平的点号键
        # 按槽位号排序（image_10 要排在 image_9 之后，不能用字符串排序）
        dotted = sorted([k for k in te["inputs"] if k.startswith("images.image_")],
                        key=lambda n: int(n.rsplit("_", 1)[-1]))
        self.assertEqual(dotted, [f"images.image_{i}" for i in range(1, 11)],
                         f"参考图应接成 images.image_1..images.image_10，实际 {dotted}")
        # 不能是嵌套字典（会被静默丢弃）
        self.assertNotIn("images", te["inputs"],
                         "不能用嵌套的 images 字典 —— 参考图会被静默丢弃")
        # 顶层也不能有扁平的 image_N（会被当成 execute() 的关键字参数）
        flat = [k for k in te["inputs"] if k.startswith("image_")]
        self.assertEqual(flat, [],
                         f"顶层不能有扁平的 {flat} —— ComfyUI 会报 unexpected keyword argument")
        # 每个点号键的值都要指向真正的 LoadImage 节点
        for i in range(1, 11):
            node_id = te["inputs"][f"images.image_{i}"][0]
            self.assertEqual(p[node_id]["class_type"], "LoadImage")

    def test_reference_images_are_wired_to_loadimage_nodes(self):
        """每个参考图槽位的值都要指向真正的 LoadImage 节点。"""
        p = self.build_i2i(n=3)
        te = next(n for n in p.values() if n["class_type"] == "TextEncodeQwenImage21")
        for key in [k for k in te["inputs"] if k.startswith("images.image_")]:
            ref = te["inputs"][key]
            self.assertIsInstance(ref, list)
            node_id = ref[0]
            self.assertIn(node_id, p, f"{key} 指向了不存在的节点")
            self.assertEqual(p[node_id]["class_type"], "LoadImage")

    def test_ref_images_accepts_string_and_dict_forms(self):
        """ref_images 两种形态都收：文件名列表 / 字典列表。"""
        p1 = self.build_i2i(n=2)
        p2 = self.W.build_qwen_i2i({
            "job_id": "p", "prompt": "x", "model": "qwen_image_2.1_bf16", "seed": 1,
            "ref_images": [{"file": "a.png"}, {"file": "b.png"}]})
        te1 = next(n for n in p1.values() if n["class_type"] == "TextEncodeQwenImage21")
        te2 = next(n for n in p2.values() if n["class_type"] == "TextEncodeQwenImage21")
        self.assertEqual(len([k for k in te1["inputs"] if k.startswith("images.image_")]), 2)
        self.assertEqual(len([k for k in te2["inputs"] if k.startswith("images.image_")]), 2)

    # ---------------------------------------------- 数量校验
    def test_ref_image_count_bounds(self):
        """参考图 1-10 张之外必须报错，且提示要说清上限。"""
        with self.assertRaises(ValueError) as ctx:
            self.build_i2i(n=0)
        self.assertIn("至少", str(ctx.exception))
        with self.assertRaises(ValueError) as ctx:
            self.build_i2i(n=11)
        msg = str(ctx.exception)
        self.assertIn("10", msg)
        self.assertIn("最多", msg)

    def test_max_ref_images_is_official_ten(self):
        """上限取 10 张：模型官方能力上限 10（官方 Blog / README）。

        ComfyUI 节点侧有 16 个 image 插槽（nodes_qwen.py 的 io.Autogrow），
        官方模板接前 10 个 —— 三者取「模型能力 10」与「节点 16 槽」的较小者 = 10。
        需求原文说「最大 9 张」，那是官方图片编辑模板的接线数，不是能力上限。
        """
        self.assertEqual(self.W.QWEN_IMAGE_MAX_REF_IMAGES, 10)
        self.assertEqual(self.W.QWEN_IMAGE_MIN_REF_IMAGES, 1)
        self.assertEqual(self.W.QWEN_IMAGE_NODE_REF_SLOTS, 16)
        self.assertLessEqual(self.W.QWEN_IMAGE_MAX_REF_IMAGES,
                             self.W.QWEN_IMAGE_NODE_REF_SLOTS,
                             "落地上限不能超过 ComfyUI 节点的实际槽位数")

    def test_ten_ref_images_are_wired(self):
        """10 张参考图要全部接进 images.image_1..images.image_10。"""
        p = self.build_i2i(n=10)
        te = next(n for n in p.values() if n["class_type"] == "TextEncodeQwenImage21")
        keys = sorted([k for k in te["inputs"] if k.startswith("images.image_")],
                      key=lambda n: int(n.rsplit("_", 1)[-1]))
        self.assertEqual(keys, [f"images.image_{i}" for i in range(1, 11)],
                         f"10 张参考图应接成 images.image_1..images.image_10，实际 {keys}")

    # ---------------------------------------------- 媒体库分类筛选
    def test_media_filter_includes_all_image_modes(self):
        """生成库「作品类型=图片」必须包含所有图像模式的记录。

        实测踩过的坑：服务端把 media=image 硬编码成 mode='t2i'，
        导致 t2i_qwen / i2i_qwen 的 9 条出图记录在「图片」筛选里一条都看不到。
        现在改成按生成类型注册表推导 —— 新增图像模式自动被纳入。
        """
        from h3studio import gen_types as G
        image_modes = {m for m in G.registered_modes()
                       if G.type_for_mode(m).get("media") == "image"}
        self.assertIn("t2i", image_modes)
        self.assertIn("t2i_qwen", image_modes)
        self.assertIn("i2i_qwen", image_modes)
        # 视频模式不该混进图片
        for m in G.registered_modes():
            if m.startswith(("t2v", "flf", "r2v", "longfilm")):
                self.assertNotIn(m, image_modes, f"{m} 是视频模式，不该算图片")

    def test_server_media_filter_is_not_hardcoded_to_t2i(self):
        """服务端不能再把 media=image 写死成 mode='t2i'。"""
        src = (REPO / "server.py").read_text(encoding="utf-8")
        self.assertNotIn('conditions.append("mode=\'t2i\'")', src,
                         "media=image 不能写死 mode='t2i' —— 会漏掉 Qwen 的出图记录")

    # ---------------------------------------------- 参数落到请求
    def test_all_params_reach_the_workflow(self):
        """分辨率/比例/步数/CFG/SHIFT/采样器/调度器 都要落到真实节点输入。"""
        p = self.build_t2i(aspect="9:16", resolution="1080p", steps=33, cfg=2.5,
                           shift=1.25, sampler="dpmpp_2m", scheduler="beta",
                           negative_prompt="模糊")
        ks = next(n for n in p.values() if n["class_type"] == "KSampler")
        self.assertEqual(ks["inputs"]["steps"], 33)
        self.assertAlmostEqual(ks["inputs"]["cfg"], 2.5, places=3)
        self.assertEqual(ks["inputs"]["sampler_name"], "dpmpp_2m")
        self.assertEqual(ks["inputs"]["scheduler"], "beta")
        lat = next(n for n in p.values() if n["class_type"] == "EmptyLatentImage")
        self.assertEqual(lat["inputs"]["width"] % 16, 0, "宽必须是 16 的倍数")
        self.assertEqual(lat["inputs"]["height"] % 16, 0, "高必须是 16 的倍数")
        te = next(n for n in p.values() if n["class_type"] == "TextEncodeQwenImage21")
        self.assertEqual(te["inputs"]["negative_prompt"], "模糊")

    def test_edit_uses_textencode_latent(self):
        """编辑场景 latent 用 TextEncode 输出的（尺寸跟随首张参考图）。"""
        p = self.build_i2i(n=2)
        ks = next(n for n in p.values() if n["class_type"] == "KSampler")
        te_id = next(k for k, n in p.items() if n["class_type"] == "TextEncodeQwenImage21")
        self.assertEqual(ks["inputs"]["latent_image"], [te_id, 2])
        self.assertNotIn("EmptyLatentImage", {n["class_type"] for n in p.values()})

    def test_t2i_uses_empty_latent(self):
        """文生图场景用 EmptyLatentImage。"""
        p = self.build_t2i()
        ks = next(n for n in p.values() if n["class_type"] == "KSampler")
        lat_id = next(k for k, n in p.items() if n["class_type"] == "EmptyLatentImage")
        self.assertEqual(ks["inputs"]["latent_image"], [lat_id, 0])

    def test_official_defaults(self):
        """官方推荐默认值：40 步 / cfg 1 / euler / simple / shift 0.69。

        步数取 40 的依据是官方 README 的 Default Parameters 表
        （num_inference_steps=40）；ComfyUI 官方模板取 25 是折中值。
        """
        p = self.build_t2i()
        ks = next(n for n in p.values() if n["class_type"] == "KSampler")
        self.assertEqual(ks["inputs"]["steps"], 40)
        self.assertAlmostEqual(ks["inputs"]["cfg"], 1.0, places=3)
        self.assertEqual(ks["inputs"]["sampler_name"], "euler")
        self.assertEqual(ks["inputs"]["scheduler"], "simple")

    # ---------------------------------------------- 模型版本 -> 步数联动
    def test_model_versions_have_linked_default_steps(self):
        """模型版本 -> 默认步数：基座 40 / Turbo 8（蒸馏）。

        Turbo 是 8 步蒸馏版，步数与固定 sigma 表绑定 —— 前端切版本时
        必须把步数同步成该版本的值，否则会出现「Turbo 跑 40 步」。
        """
        base = self.W.QWEN_IMAGE_MODELS["qwen_image_2.1_bf16"]
        turbo = self.W.QWEN_IMAGE_MODELS["qwen_image_2.1_turbo_bf16"]
        self.assertEqual(base["steps"], 40)
        self.assertEqual(turbo["steps"], 8)
        # 两档的其它推荐参数一致
        for key in ("cfg", "shift", "sampler", "scheduler"):
            self.assertEqual(base[key], turbo[key], f"{key} 两档应一致")

    def test_turbo_uses_manual_sigmas_not_plain_ksampler(self):
        """Turbo 必须走 ManualSigmas + SamplerCustomAdvanced。

        官方 Turbo 模板（image_qwen_image_2_1_turbo_t2i.json）用
        ManualSigmas 写死 8 步 sigma 表；只用 KSampler(steps=8) 会让
        调度器按 simple 重新分配 sigma，与蒸馏固化的 schedule 不符。
        """
        turbo = self.W.QWEN_IMAGE_MODELS["qwen_image_2.1_turbo_bf16"]
        if not self.W.qwen_unet_available(turbo["unet"]):
            self.skipTest("本机没有 Turbo 权重，无法构建 Turbo 工作流")
        p = self.build_t2i(model="qwen_image_2.1_turbo_bf16")
        classes = {n["class_type"] for n in p.values()}
        self.assertIn("ManualSigmas", classes, "Turbo 缺固定 sigma 表")
        self.assertIn("SamplerCustomAdvanced", classes, "Turbo 缺自定义采样节点")
        self.assertNotIn("KSampler", classes,
                         "Turbo 不该用普通 KSampler（步数与 sigma 表绑定）")
        sigmas = next(n for n in p.values() if n["class_type"] == "ManualSigmas")
        self.assertEqual(sigmas["inputs"]["sigmas"], self.W.QWEN_IMAGE_TURBO_SIGMAS)
        # 8 步 schedule = 9 个 sigma 点（首尾各一个）
        values = [v.strip() for v in sigmas["inputs"]["sigmas"].split(",")]
        self.assertEqual(len(values), 9, f"8 步应有 9 个 sigma，实际 {len(values)}")
        self.assertEqual(values[-1], "0", "最后一个 sigma 应为 0")
        # guider 必须接模型 + 正负条件 + cfg
        guider = next(n for n in p.values() if n["class_type"] == "CFGGuider")
        self.assertIn("model", guider["inputs"])
        self.assertIn("positive", guider["inputs"])
        self.assertIn("negative", guider["inputs"])
        self.assertAlmostEqual(guider["inputs"]["cfg"], 1.0, places=3)
        # 噪声节点带种子
        noise = next(n for n in p.values() if n["class_type"] == "RandomNoise")
        self.assertIn("noise_seed", noise["inputs"])

    def test_turbo_refuses_when_weight_missing(self):
        """Turbo 权重缺失时要给明确中文错误，而不是让 ComfyUI 报看不懂的错。"""
        turbo = self.W.QWEN_IMAGE_MODELS["qwen_image_2.1_turbo_bf16"]
        if self.W.qwen_unet_available(turbo["unet"]):
            self.skipTest("本机已有 Turbo 权重，跳过缺失分支")
        with self.assertRaises(ValueError) as ctx:
            self.build_t2i(model="qwen_image_2.1_turbo_bf16")
        self.assertIn("缺少", str(ctx.exception))
        self.assertIn(turbo["unet"], str(ctx.exception))

    def test_builders_registered(self):
        """两个模式都要注册进 BUILDERS（否则前端有入口但后端不认识）。"""
        for mode in QWEN_MODELS:
            self.assertIn(mode, self.W.BUILDERS)

    def test_gen_types_have_categories(self):
        """生成类型注册表要有「文生图」「图片编辑」两个分类。"""
        from h3studio import gen_types as G
        cats = {c["key"]: c["label"] for c in G.list_categories()}
        self.assertEqual(cats.get("t2i"), "文生图")
        self.assertEqual(cats.get("i2i"), "图片编辑")
        types = {t["key"]: t.get("category") for t in G.list_types()}
        self.assertEqual(types.get("t2i"), "t2i")
        self.assertEqual(types.get("t2i_qwen"), "t2i")
        self.assertEqual(types.get("i2i_qwen"), "i2i")

    def test_qwen_lora_registry(self):
        """Qwen LoRA 清单独立于 Z-Image，且能列出本机真实存在的文件。"""
        from h3studio import loras as L
        items = L.list_qwen_effect_loras()
        for item in items:
            self.assertEqual(item["kind"], "effect")
            self.assertTrue(Path(item["path"]).is_file(),
                            f"{item['name']} 的路径不存在")
        # 与 Z-Image 清单不重叠
        z_names = {i["name"] for i in L.list_effect_loras()}
        for item in items:
            self.assertNotIn(item["name"], z_names,
                             "Qwen 与 Z-Image 的 LoRA 清单不应重叠")


@unittest.skipUnless(HAS_PLAYWRIGHT, "未安装 playwright")
class TestQwenFrontend(unittest.TestCase):
    """前端：两个分类入口可见可切换 + 参数面板按模式切。"""

    @classmethod
    def setUpClass(cls):
        cls.server = StudioServer(seed_jobs=0)
        assert cls.server.start(), "Studio 实例启动失败"
        seed_jobs(cls.server.data_dir)
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(args=["--no-sandbox"])
        cls.ctx = cls.browser.new_context(viewport={"width": 1920, "height": 1080})
        cls.page = cls.ctx.new_page()
        cls.errors = []
        cls.page.on("pageerror", lambda e: cls.errors.append(str(e)))
        cls.page.goto("http://127.0.0.1:%d/" % cls.server.port,
                      wait_until="domcontentloaded")
        cls.page.wait_for_function(
            "() => document.querySelectorAll('#jobsGrid .job-card').length > 0",
            timeout=20000)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.stop()

    def recommended_max_ref_images(self):
        """服务端下发的参考图上限。"""
        import urllib.request
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.server.port}/api/image-options",
                timeout=10) as r:
            data = json.loads(r.read().decode())
        return int(data.get("qwen", {}).get("max_ref_images", 9))

    def recommended_steps(self):
        """服务端下发的推荐步数（官方有多个出处，测试跟随注册表）。"""
        import urllib.request
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.server.port}/api/image-options",
                timeout=10) as r:
            data = json.loads(r.read().decode())
        models = data.get("qwen", {}).get("models", {})
        return int(models["qwen_image_2.1_bf16"]["steps"])

    def nav_groups(self):
        return self.page.evaluate("""() => {
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

    def test_two_categories_visible(self):
        """「文生图」「图片编辑」两个分类入口都要在侧栏可见。"""
        groups = {g["label"]: g["items"] for g in self.nav_groups()}
        self.assertIn("文生图", groups, f"缺少「文生图」分类，实际 {list(groups)}")
        self.assertIn("图片编辑", groups, f"缺少「图片编辑」分类，实际 {list(groups)}")
        self.assertIn("t2i", groups["文生图"])
        self.assertIn("t2i_qwen", groups["文生图"])
        self.assertIn("i2i_qwen", groups["图片编辑"])

    def test_switch_to_qwen_t2i(self):
        """切到 Qwen 文生图：参数面板换成 Qwen 的推荐值。"""
        self.page.evaluate(
            "() => document.querySelector('.nav-item[data-mode=\"t2i_qwen\"]').click()")
        self.page.wait_for_timeout(1200)
        self.assertEqual(self.page.evaluate("() => state.mode"), "t2i_qwen")
        s = self.page.evaluate("""() => ({
          steps: state.imgQwen.steps, cfg: state.imgQwen.cfg, shift: state.imgQwen.shift,
          sampler: state.imgQwen.sampler, scheduler: state.imgQwen.scheduler,
          model: state.imgQwen.model,
          shiftShown: document.querySelector('#imgShiftVal').textContent,
          stepsShown: document.querySelector('#imgStepsVal').textContent,
          peFieldExists: !!document.querySelector('#imgPeField'),
          peCheckboxExists: !!document.querySelector('#imgUsePe'),
          refVisible: getComputedStyle(document.querySelector('#imgRefField')).display !== 'none',
          loras: Array.from(document.querySelectorAll('#imgLoraSelect option')).map(o => o.textContent),
        })""")
        self.assertEqual(s["steps"], self.recommended_steps())
        self.assertAlmostEqual(float(s["cfg"]), 1.0, places=3)
        self.assertAlmostEqual(float(s["shift"]), 0.69, places=3)
        self.assertEqual(s["sampler"], "euler")
        self.assertEqual(s["scheduler"], "simple")
        self.assertEqual(s["model"], "qwen_image_2.1_bf16")
        # 滑块显示的 SHIFT 必须与真实值一致（量程不能把它夹掉）
        self.assertAlmostEqual(float(s["shiftShown"]), 0.69, places=3)
        self.assertEqual(s["stepsShown"], str(self.recommended_steps()))
        # 官方 PE 已并入「增强」下拉菜单 -> 面板上**不再有**独立 PE 区块/勾选框；
        # 文生图不显示参考图区
        self.assertFalse(s["peFieldExists"],
                         "官方 PE 已并入增强下拉，不该再有独立 PE 区块")
        self.assertFalse(s["peCheckboxExists"],
                         "官方 PE 已并入增强下拉，不该再有独立 PE 勾选框")
        self.assertFalse(s["refVisible"])
        # LoRA 列表是 Qwen 的那两个
        self.assertTrue(any("Qwen" in t for t in s["loras"]),
                        f"LoRA 列表应含 Qwen 的条目，实际 {s['loras']}")

    def test_switch_to_qwen_i2i(self):
        """切到 Qwen 图片编辑：参考图区出现；官方 PE 已并入增强下拉（无独立勾选框）。"""
        self.page.evaluate(
            "() => document.querySelector('.nav-item[data-mode=\"i2i_qwen\"]').click()")
        self.page.wait_for_timeout(1200)
        self.assertEqual(self.page.evaluate("() => state.mode"), "i2i_qwen")
        # 参考图状态搬到 web/qwen-refs.js（window.QwenRefs）——
        # 参考图强制来自素材库，state.imgQwen.refs 已移除。
        s = self.page.evaluate("""() => ({
          refVisible: getComputedStyle(document.querySelector('#imgRefField')).display !== 'none',
          peFieldExists: !!document.querySelector('#imgPeField'),
          hint: document.querySelector('#imgRefHint').textContent,
          note: document.querySelector('#imgRefNote').textContent,
          max: window.QwenRefs ? window.QwenRefs.getRefs().length : -1,
          addBtn: document.querySelector('#imgRefAdd').textContent,
          hasFileInput: !!document.querySelector('#imgRefInput'),
        })""")
        self.assertTrue(s["refVisible"], "图片编辑应显示参考图区")
        self.assertFalse(s["peFieldExists"],
                         "官方 PE 已并入增强下拉，不该再有独立 PE 区块")
        # 参考图上限以服务端下发的为准（官方节点支持 16 个槽位，
        # 面板取官方工作流用量对应的值）
        max_ref = self.recommended_max_ref_images()
        self.assertIn(str(max_ref), s["hint"])
        self.assertIn("最多", s["note"])
        # 参考图**必须来自素材库** -> 入口是「从素材库添加」，没有本地上传通道
        self.assertIn("素材库", s["addBtn"])
        self.assertFalse(s["hasFileInput"],
                         "图片编辑不该再有本地参考图上传通道（参考图强制来自素材库）")
        self.assertEqual(s["max"], 0)

    def test_zimage_mode_unchanged(self):
        """切回 Z-IMAGE：参数仍是它自己的推荐值（SHIFT 3 / min=1）。"""
        self.page.evaluate(
            "() => document.querySelector('.nav-item[data-mode=\"t2i\"]').click()")
        self.page.wait_for_timeout(1200)
        s = self.page.evaluate("""() => ({
          shift: state.img.shift,
          shiftMin: document.querySelector('#imgShift').min,
          shiftShown: document.querySelector('#imgShiftVal').textContent,
          peFieldExists: !!document.querySelector('#imgPeField'),
          refVisible: getComputedStyle(document.querySelector('#imgRefField')).display !== 'none',
          model: state.img.model,
        })""")
        self.assertAlmostEqual(float(s["shift"]), 3.0, places=3)
        self.assertEqual(s["shiftMin"], "1")
        # 显示精度按模式（Z-Image 1 位小数 / Qwen 2 位），值本身要对
        self.assertAlmostEqual(float(s["shiftShown"]), 3.0, places=3)
        self.assertFalse(s["peFieldExists"],
                         "官方 PE 已并入增强下拉，不该再有独立 PE 区块")
        self.assertFalse(s["refVisible"], "Z-IMAGE 不该显示参考图区")

    def test_no_console_errors(self):
        self.assertEqual([e for e in self.errors if e.strip()], [])


if __name__ == "__main__":
    unittest.main()
