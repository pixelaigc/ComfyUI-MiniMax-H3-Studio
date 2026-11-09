# -*- coding: utf-8 -*-
"""Qwen-Image 2.1「图片编辑」参考图（素材库来源 + 占位标记 + 智能排序）的回归测试。

覆盖本轮需求的关键约束（不测就会回归）：

1. **参考图强制来自素材库**
   - 参考图没有 asset_id -> 被丢弃
   - i2i_qwen 提交时带本地文件 -> 服务端明确拒绝（不是静默忽略）

2. **占位标记用官方的 <imageN>，不是中文 <参考图N>**
   官方 PE-I2I system prompt 原文要求多图必须用 <image1>/<image2>；
   ComfyUI 的 qwen_image21.py 也用 "<image{n}>" 作视觉块前缀。
   用中文标签模型认不出来。

3. **占位序号与 ComfyUI image 插槽顺序严格一致**
   第 N 张参考图 -> images.image_N -> <imageN>，三者同序。

4. **移除后重新编号连续无空洞**
   删掉中间一张，剩下的是 1..N-1 连续，提示词里不能留悬空 <imageN>。

5. **智能排序可降级**
   use_model=False 或内部异常 -> 确定性基础排序（入库时间倒序），
   **不抛异常**。

6. **超限拦截**：>10 张报错；素材失效报错。

运行：python3 -m pytest tests/test_qwen_ref_assets.py -v
"""
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from h3studio import ref_images as RI   # noqa: E402
from h3studio import workflows as W     # noqa: E402


def make_ref(asset_id, desc="", tags=None, created_at=1000.0, source="analysis"):
    return {
        "asset_id": asset_id,
        "version": 1,
        "name": f"素材{asset_id}",
        "description": desc,
        "description_source": source,
        "tags": tags or [],
        "created_at": created_at,
        "preview_url": f"/api/assets/{asset_id}/preview",
        "missing": False,
    }


# ------------------------------------------------------------------ 归一化


class TestNormalize(unittest.TestCase):
    """参考图归一化：必须锚定素材库条目。"""

    def test_drops_entries_without_asset_id(self):
        """没有 asset_id 的条目一律丢弃 —— 参考图必须来自素材库。"""
        refs = RI.normalize_refs([
            {"file": "h3studio/local.png"},          # 裸路径（旧的本地通道）
            {"name": "只有名字"},
            make_ref("asset_a"),
        ])
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]["asset_id"], "asset_a")

    def test_description_source_defaults(self):
        refs = RI.normalize_refs([
            {"asset_id": "a", "description": "一只猫"},
            {"asset_id": "b"},
        ])
        self.assertEqual(refs[0]["description_source"], "analysis")
        self.assertEqual(refs[1]["description_source"], "empty")
        self.assertEqual(refs[1]["description"], "")

    def test_empty_source_clears_description(self):
        """标成 empty 的描述不能带着文字走（那是编造出来的）。"""
        refs = RI.normalize_refs([
            {"asset_id": "a", "description": "编的", "description_source": "empty"},
        ])
        self.assertEqual(refs[0]["description"], "")

    def test_missing_flag_preserved(self):
        refs = RI.normalize_refs([{"asset_id": "a", "missing": True}])
        self.assertTrue(refs[0]["missing"])


# ------------------------------------------------------------------ 占位标记


class TestPlaceholders(unittest.TestCase):
    """占位标记：官方 <imageN> 语义。"""

    def test_placeholder_format(self):
        self.assertEqual(RI.placeholder_for(0), "<image1>")
        self.assertEqual(RI.placeholder_for(2), "<image3>")

    def test_parse_placeholders(self):
        self.assertEqual(RI.parse_placeholders("<image1> 猫 <image2> 桌"), [1, 2])
        self.assertEqual(RI.parse_placeholders("没有标记"), [])
        self.assertEqual(RI.parse_placeholders("<image3> <image1>"), [1, 3])

    def test_renumber_does_not_chain(self):
        """1->2、2->3 必须不连环替换（否则 <image1> 会变成 <image3>）。"""
        text = "<image1> 和 <image2>"
        out = RI.renumber_placeholders(text, {1: 2, 2: 3})
        self.assertEqual(out, "<image2> 和 <image3>")

    def test_renumber_downward(self):
        out = RI.renumber_placeholders("<image1> <image2> <image3>", {1: 1, 2: 2, 3: 3})
        self.assertEqual(out, "<image1> <image2> <image3>")

    def test_strip_placeholders(self):
        self.assertEqual(RI.strip_placeholders("<image1> 猫 <image2> 桌").strip(), "猫 桌")


# ------------------------------------------------------------------ 描述注入


class TestReferenceBlock(unittest.TestCase):
    """描述注入：占位 + 描述，且 N=1 时不用标签（官方 PE-I2I 要求）。"""

    def test_block_includes_placeholder_and_description(self):
        refs = [make_ref("a", "一只橘猫侧坐在窗台，暖光"),
                make_ref("b", "一件浅蓝色牛仔衬衫")]
        block = RI.build_reference_block(refs)
        self.assertIn("<image1> 一只橘猫侧坐在窗台，暖光", block)
        self.assertIn("<image2> 一件浅蓝色牛仔衬衫", block)

    def test_single_image_has_no_tag(self):
        """官方 PE-I2I：单图输入不要用 <imageN> 标签，自然表述。"""
        refs = [make_ref("a", "一只橘猫侧坐在窗台")]
        block = RI.build_reference_block(refs)
        self.assertNotIn("<image1>", block)
        self.assertIn("一只橘猫侧坐在窗台", block)

    def test_missing_refs_excluded_from_block(self):
        refs = [make_ref("a", "猫"), {"asset_id": "b", "missing": True, "description": "狗"}]
        block = RI.build_reference_block(refs)
        self.assertIn("<image1>", block)
        self.assertNotIn("狗", block)

    def test_inject_prefixes_block(self):
        refs = [make_ref("a", "猫"), make_ref("b", "狗")]
        out = RI.inject_reference_block("把狗放进猫的场景", refs)
        self.assertTrue(out.startswith("<image1> 猫"))
        self.assertIn("把狗放进猫的场景", out)

    def test_inject_is_idempotent_with_user_written_placeholders(self):
        """用户自己写了占位 -> 不注入第二份，只做重编号。"""
        refs = [make_ref("a", "猫"), make_ref("b", "狗")]
        out = RI.inject_reference_block("保持 <image2> 不变", refs)
        self.assertEqual(out.count("<image2>"), 1)
        self.assertNotIn("<image1>", out)


# ------------------------------------------------------------------ 校验


class TestValidate(unittest.TestCase):
    """一致性校验：占位符 vs 图片数量、失效素材、超限。"""

    def test_ok_when_matching(self):
        refs = [make_ref("a", "猫"), make_ref("b", "狗")]
        prompt = "<image1> 猫\n<image2> 狗\n把狗放进猫场景"
        result = RI.validate_refs(refs, prompt=prompt)
        self.assertTrue(result["ok"], result["errors"])

    def test_missing_placeholder_blocks(self):
        refs = [make_ref("a", "猫"), make_ref("b", "狗")]
        result = RI.validate_refs(refs, prompt="只有 <image1> 猫")
        self.assertFalse(result["ok"])
        self.assertTrue(any("缺少" in e for e in result["errors"]), result["errors"])

    def test_no_placeholder_blocks_for_multi(self):
        refs = [make_ref("a", "猫"), make_ref("b", "狗")]
        result = RI.validate_refs(refs, prompt="把狗放进猫场景")
        self.assertFalse(result["ok"])

    def test_hole_in_placeholders_blocks(self):
        """<image1> <image3>（缺 image2）-> 必须拦下。"""
        refs = [make_ref("a"), make_ref("b"), make_ref("c")]
        result = RI.validate_refs(refs, prompt="<image1> <image3>")
        self.assertFalse(result["ok"])
        self.assertTrue(any("image2" in e for e in result["errors"]), result["errors"])

    def test_extra_placeholder_blocks(self):
        refs = [make_ref("a"), make_ref("b")]
        result = RI.validate_refs(refs, prompt="<image1> <image2> <image3>")
        self.assertFalse(result["ok"])

    def test_missing_asset_blocks(self):
        refs = [make_ref("a"), {"asset_id": "b", "missing": True, "name": "已删图"}]
        result = RI.validate_refs(refs, prompt="<image1> <image2>")
        self.assertFalse(result["ok"])
        self.assertTrue(any("已从素材库删除" in e for e in result["errors"]), result["errors"])

    def test_too_many_blocks(self):
        refs = [make_ref(f"a{i}") for i in range(11)]
        result = RI.validate_refs(refs, prompt="")
        self.assertFalse(result["ok"])
        self.assertTrue(any("最多 10 张" in e for e in result["errors"]), result["errors"])

    def test_too_few_blocks(self):
        result = RI.validate_refs([], prompt="")
        self.assertFalse(result["ok"])

    def test_single_image_tag_is_warning_not_error(self):
        """单图带标签只是警告（官方说不要用，但不该硬拦）。"""
        refs = [make_ref("a", "猫")]
        result = RI.validate_refs(refs, prompt="<image1> 猫")
        self.assertTrue(result["ok"], result["errors"])
        self.assertTrue(result["warnings"])


# ------------------------------------------------------------------ 移除重编号


class TestRemoveAndRenumber(unittest.TestCase):
    """移除参考图后重新编号，连续无空洞。"""

    def test_renumber_after_removing_middle(self):
        """删掉中间一张 -> 剩下的是 1..2 连续。"""
        refs = [make_ref("a", "猫"), make_ref("b", "狗"), make_ref("c", "鸟")]
        remaining = [r for i, r in enumerate(refs) if i != 1]
        prompt = "<image1> 猫 <image2> 狗 <image3> 鸟"
        # 移除后占位要重排：把「旧2、3」映射到「新1、2」
        mapping = {2: 1, 3: 2}
        new_prompt = RI.renumber_placeholders(prompt, mapping)
        self.assertEqual(RI.parse_placeholders(new_prompt), [1, 2])
        self.assertNotIn("<image3>", new_prompt)

        block = RI.build_reference_block(remaining)
        self.assertIn("<image1> 猫", block)
        self.assertIn("<image2> 鸟", block)
        self.assertNotIn("狗", block)

    def test_block_numbers_match_list_position(self):
        refs = [make_ref("a", "猫"), make_ref("b", "狗"), make_ref("c", "鸟")]
        block = RI.build_reference_block(refs)
        self.assertEqual(RI.parse_placeholders(block), [1, 2, 3])

    def test_no_dangling_after_remove(self):
        """移除后重新生成占位块，不留悬空标记。

        移除后只剩 1 张 -> 官方 PE-I2I 要求单图**不要**用 <imageN> 标签，
        所以块里不该再有 <image2>（也不能留 <image1>）。
        """
        refs = [make_ref("a", "猫"), make_ref("b", "狗")]
        refs = refs[:1]
        block = RI.build_reference_block(refs)
        self.assertNotIn("<image2>", block)
        self.assertEqual(RI.parse_placeholders(block), [])
        self.assertIn("猫", block)
        self.assertNotIn("狗", block)


# ------------------------------------------------------------------ 智能排序


class TestSmartSort(unittest.TestCase):
    """智能排序 + 降级路径。"""

    def test_scores_prefer_prompt_overlap(self):
        refs = [make_ref("a", "一只柴犬在雪山上", created_at=1000),
                make_ref("b", "一座哥特式教堂", created_at=1000)]
        result = RI.smart_sort(refs, prompt="把柴犬放到教堂前", use_model=True)
        self.assertEqual(result["refs"][0]["asset_id"], "a")
        self.assertTrue(result["ranking"][0]["signals"]["prompt_overlap"] > 0)

    def test_degrades_to_deterministic_order(self):
        """use_model=False -> 降级为入库时间倒序，且**不抛异常**。"""
        refs = [make_ref("a", "x", created_at=1000),
                make_ref("b", "y", created_at=3000),
                make_ref("c", "z", created_at=2000)]
        result = RI.smart_sort(refs, prompt="任意提示词", use_model=False)
        self.assertTrue(result["degraded"])
        self.assertEqual([r["asset_id"] for r in result["refs"]], ["b", "c", "a"])

    def test_basic_sort_is_stable(self):
        refs = [make_ref("a", created_at=1000), make_ref("b", created_at=1000)]
        ordered = RI.basic_sort(refs)
        self.assertEqual([r["asset_id"] for r in ordered], ["a", "b"])

    def test_sort_never_raises(self):
        """排序是可选功能 —— 任何异常都不能抛出去。"""
        class Boom(dict):
            def get(self, *a, **kw):
                raise RuntimeError("boom")
        bad = [Boom(asset_id="a")]  # type: ignore[arg-type]
        # 不抛异常即通过（smart_sort 内部兜底）
        result = RI.smart_sort(bad, prompt="x", use_model=True)
        self.assertEqual(len(result["refs"]), 1)

    def test_ranking_is_explainable(self):
        refs = [make_ref("a", "猫", tags=["动物"]), make_ref("b", "狗", tags=["动物"])]
        result = RI.smart_sort(refs, prompt="猫", use_model=True)
        for item in result["ranking"]:
            self.assertIn("signals", item)
            self.assertIn("score", item)


# ------------------------------------------------------------------ 工作流接线


class TestWorkflowWiring(unittest.TestCase):
    """参考图 -> 占位 -> ComfyUI image 插槽，三者同序。"""

    def build(self, n=3, **over):
        params = {
            "job_id": "probe", "prompt": "换装", "model": "qwen_image_2.1_bf16",
            "aspect": "1:1", "resolution": "1mp", "seed": 42,
            "ref_images": [f"h3studio/ref{i}.png" for i in range(n)],
            "use_pe": True,
        }
        params.update(over)
        return W.build_qwen_i2i(params)

    def test_image_slots_are_flat_dot_keys(self):
        """必须用扁平点号键 —— 嵌套字典会让参考图被 ComfyUI 静默丢弃。"""
        p = self.build(3)
        te = next(n for n in p.values() if n["class_type"] == "TextEncodeQwenImage21")
        for i in (1, 2, 3):
            self.assertIn(f"images.image_{i}", te["inputs"])
        self.assertNotIn("images", te["inputs"],
                         "嵌套的 images 字典会让 ComfyUI 静默丢弃参考图")

    def test_slot_order_matches_ref_order(self):
        """第 N 张参考图 -> images.image_N -> <imageN>。"""
        p = self.build(3)
        te = next(n for n in p.values() if n["class_type"] == "TextEncodeQwenImage21")
        for i in range(3):
            node_id = te["inputs"][f"images.image_{i + 1}"][0]
            load = p[node_id]
            self.assertEqual(load["class_type"], "LoadImage")
            self.assertEqual(load["inputs"]["image"], f"h3studio/ref{i}.png")

    def test_reference_images_also_feed_the_pe_node(self):
        """官方 PE-I2I 工作流：参考图也喂给 PE 扩写节点（PE 要能看图）。"""
        p = self.build(3)
        batch = next((n for n in p.values() if n["class_type"] == "BatchImagesNode"), None)
        self.assertIsNotNone(batch, "参考图没有喂给 PE 节点")
        self.assertEqual(batch["inputs"]["images.image0"], ["qw:ref0", 0])
        self.assertEqual(batch["inputs"]["images.image2"], ["qw:ref2", 0])
        pe = next(n for n in p.values() if n["class_type"] == "TextGenerate")
        batch_id = next(k for k, v in p.items() if v is batch)
        self.assertEqual(pe["inputs"]["image"], [batch_id, 0])

    def test_no_pe_no_batch_node(self):
        """关掉 PE 就不建 BatchImagesNode。"""
        p = self.build(2, use_pe=False)
        classes = {n["class_type"] for n in p.values()}
        self.assertNotIn("BatchImagesNode", classes)

    def test_t2i_has_no_reference_nodes(self):
        params = {"job_id": "probe", "prompt": "一只猫", "model": "qwen_image_2.1_bf16",
                  "aspect": "16:9", "resolution": "1mp", "seed": 42}
        p = W.build_qwen_t2i(params)
        classes = {n["class_type"] for n in p.values()}
        self.assertNotIn("BatchImagesNode", classes)
        self.assertNotIn("LoadImage", classes)


# ------------------------------------------------------------------ 服务端拒绝本地通道


class TestServerRejectsLocalUpload(unittest.TestCase):
    """i2i_qwen 的参考图必须来自素材库 —— 本地上传要被明确拒绝。"""

    def test_api_create_rejects_file_ref_for_i2i(self):
        import asyncio
        import server as srv

        class FakePart:
            def __init__(self, name, filename=None, data=b""):
                self.name = name
                self.filename = filename
                self._data = data

            async def text(self):
                return self._data.decode("utf-8")

            async def read_chunk(self, size):
                if not hasattr(self, "_sent"):
                    self._sent = True
                    return self._data
                return b""

        class FakeRequest:
            def __init__(self, parts):
                self._parts = parts

            async def multipart(self):
                async def gen():
                    for part in self._parts:
                        yield part
                return gen()

            async def json(self):
                return {}

        parts = [
            FakePart("mode", data=b"i2i_qwen"),
            FakePart("params", data=json.dumps({
                "prompt": "换装",
                "ref_images": [{"asset_id": "asset_a", "description": "猫"}],
            }).encode()),
            FakePart("file_ref", filename="local.png", data=b"\x89PNG fake"),
        ]
        req = FakeRequest(parts)
        # 自己建事件循环：别的测试可能已经关掉了当前线程的默认 loop
        # （asyncio.get_event_loop() 在 Py3.10+ 无运行中的 loop 时会抛错）
        loop = asyncio.new_event_loop()
        try:
            resp = loop.run_until_complete(srv.api_create(req))
        finally:
            loop.close()
        self.assertEqual(resp.status, 400)
        payload = json.loads(resp.body.decode())
        self.assertEqual(payload.get("code"), "ref_must_come_from_library")
        self.assertIn("素材库", payload.get("error", ""))


if __name__ == "__main__":
    unittest.main()
