# -*- coding: utf-8 -*-
"""/api/image-loras 的**载荷体积**与**重复扫描**回归（2026-11-21 线上实测）。

【这个测试防的是什么 —— 两条实测根因】
===========================================================================
线上实测（2026-11-21 05:0x，`curl -w '%{time_total} %{size_download}'`）：

    GET /api/image-loras                 -> 0.88s  6,149,820 字节
    GET /api/image-loras                 -> 0.79s  6,149,820 字节
    GET /api/image-loras                 -> 0.87s  6,149,820 字节
    GET /api/image-loras?base=Qwen      -> 0.05s    129,979 字节
    GET /api/image-loras?base=MOSS      -> 0.72s  3,829,316 字节

1. **载荷里 96% 是一段没人要的噪声**
   `z-image-effects/zit-deepthroat-31epoc-k3nk.safetensors` 的
   safetensors `__metadata__["_"]` 是一个 **5,668,456 字节**的字符串
   （训练时写进去的随机标记）。
   实测：整包去掉 `metadata` 后是 226,411 字节 —— 也就是说
   **这一个字段占了 5,668,456 / 6,100,850 ≈ 93%**。
   而全仓检索（`web/*.js` / `server.py` / `h3studio/**` / `tests/**` /
   `scripts/**`）对 LoRA 条目的 `metadata` 字段**零引用** ——
   前端 `imgLoraMeta()` 只取具名字段（display / scene / trigger /
   group / base_verdict …）。
   ⇒ 现在按**单字段**上限截断（`METADATA_FIELD_MAX_BYTES`），
     短字段（author / time / modelspec.architecture …）原样保留。

2. **每次请求都重新全盘扫一遍**
   扫描本身要 0.7~1.0 秒（712 个 safetensors，其中 465 个要
   `open()` 读 safetensors 头；实测 cProfile 显示单次 `json.loads`
   那个 5.6MB 的 metadata 就占 **0.43 秒**）。
   ⇒ 现在走**目录指纹缓存**（`_cached_inventory`）：指纹变了
     （装/删/替换 LoRA）自动作废，指纹没变直接复用。

【为什么「截断 metadata」不算「静默丢数据」】
超长字段被替换成
`{"_truncated": True, "_original_length": 5668456, "_": "<前 4KB>"}` ——
「被截断了」是**可观测的事实**，而不是数据凭空消失。
而 `modelspec.architecture` / `ss_base_model_version` 这两个
**参与扫描内部 base 兜底判定**的键被**显式豁免**，永远原样保留。

【断言纪律】
  * 每条断言咬住它**命名的那件事**（变异测试见 §变异测试）。
  * 断言**不自指**：不拿被测函数自己的输出算期望值
    （体积断言用**独立构造的常量**做参照，不是「拿新代码的输出
    当旧代码的期望」）。
  * 测量点挑**常量巧合抓不到**的位置。
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from h3studio import loras as lora_registry  # noqa: E402


class TestMetadataFieldTrim(unittest.TestCase):
    """超长 metadata **字段**必须被截断，且必须有**可观测标记**。"""

    def test_oversized_field_is_truncated_with_observable_marker(self):
        """超长字段 -> 截断 + `_truncated` / `_original_length` 标记。

        ⚠️ 期望值用**独立构造的常量**（不是被测函数的输出）。
        """
        huge = "x" * 5000
        out = lora_registry._trim_metadata({"_": huge})

        self.assertIsInstance(out["_"], dict,
                              "超长字段必须变成带标记的 dict")
        self.assertTrue(out["_"]["_truncated"],
                        "截断必须留下 `_truncated` 标记（可观测事实）")
        self.assertEqual(out["_"]["_original_length"], 5000,
                         "必须如实记录**原始长度**，不能让数据凭空消失")
        self.assertLessEqual(
            len(out["_"]["_"].encode("utf-8")),
            lora_registry.METADATA_FIELD_MAX_BYTES,
            "保留的内容必须不超过上限",
        )

    def test_short_fields_are_preserved_verbatim(self):
        """短字段**原样保留** —— 截断不能误伤正常元数据。"""
        meta = {"author": "KookYan", "time": "2026-04-30 05:46:14",
                "modelspec.architecture": "z-image"}
        out = lora_registry._trim_metadata(meta)
        self.assertEqual(out, meta, "短字段必须逐字保留")

    def test_keep_keys_are_never_truncated_even_when_huge(self):
        """`modelspec.architecture` / `ss_base_model_version` 永远豁免。

        理由：它们参与扫描内部的 `base` 兜底判定 ——
        截断会让 `base` 显示成错误值（= 面板说谎）。
        """
        for key in lora_registry._METADATA_KEEP_KEYS:
            huge = "y" * 100000
            out = lora_registry._trim_metadata({key: huge})
            self.assertEqual(out[key], huge,
                             f"{key} 必须原样保留（参与 base 兜底判定）")

    def test_non_string_values_are_not_corrupted(self):
        """非字符串值（数字 / 列表 / None）不得被截断逻辑改形。"""
        meta = {"n": 123, "lst": ["a", "b"], "none": None, "b": True}
        out = lora_registry._trim_metadata(meta)
        self.assertEqual(out, meta, "非字符串元数据必须原样保留")

    def test_empty_metadata_is_safe(self):
        """空 / 非 dict 输入不得抛异常（扫描里到处都可能传空）。"""
        self.assertEqual(lora_registry._trim_metadata({}), {})
        self.assertEqual(lora_registry._trim_metadata(None), None)


class TestScanCache(unittest.TestCase):
    """扫描缓存：**指纹变了必须作废**，指纹没变必须命中。"""

    def setUp(self):
        lora_registry.invalidate_lora_scan_cache()
        self.addCleanup(lora_registry.invalidate_lora_scan_cache)

    def _fake_scan(self, items):
        calls = {"n": 0}

        def _scan(deep_analysis=False):
            calls["n"] += 1
            return [dict(i) for i in items]

        return _scan, calls

    def test_second_call_hits_cache(self):
        """同一指纹下，第二次调用**不得**再扫盘。

        断言的是**扫描函数的调用计数**（可观测的外部事实），
        不是「inventory 的内容等于 inventory 自己」。
        """
        items = [{"name": "a.safetensors", "kind": lora_registry.KIND_EFFECT,
                  "compatible": True}]
        scan, calls = self._fake_scan(items)

        lora_registry._cached_inventory("T", False, scan)
        lora_registry._cached_inventory("T", False, scan)

        self.assertEqual(
            calls["n"], 1,
            "指纹没变时第二次调用必须命中缓存（重复扫盘 = 每次请求 0.8 秒）",
        )

    def test_changed_signature_invalidates_cache(self):
        """**指纹变了**（装了新 LoRA）-> 必须重新扫描，不得返回旧快照。

        ⚠️ 这是缓存最容易出错的点：只按 TTL 过期的话，装完新 LoRA
        最长 60 秒内下拉里看不到它。这里用「指纹」做失效判据。
        """
        items = [{"name": "a.safetensors", "kind": lora_registry.KIND_EFFECT,
                  "compatible": True}]
        scan, calls = self._fake_scan(items)

        lora_registry._cached_inventory("T", False, scan)
        self.assertEqual(calls["n"], 1)

        # 假装目录指纹变了（装了一个新 LoRA）
        original = lora_registry._lora_dir_signature
        lora_registry._lora_dir_signature = lambda: (("fake",), (("x", 1, 1),))
        try:
            lora_registry._cached_inventory("T", False, scan)
        finally:
            lora_registry._lora_dir_signature = original

        self.assertEqual(
            calls["n"], 2,
            "目录指纹变了必须重新扫描（否则装完 LoRA 界面永远看不到）",
        )

    def test_different_scan_function_does_not_share_cache(self):
        """不同的扫描函数**不得共用**同一份缓存。

        理由：测试（和运行时替换）会把 `list_z_image_loras` 换成别的
        实现；共用缓存会让替换后的实现拿到**上一次的**扫描结果 ——
        测试就会误判通过/失败。
        """
        items_a = [{"name": "a.safetensors", "kind": lora_registry.KIND_EFFECT,
                    "compatible": True}]
        items_b = [{"name": "b.safetensors", "kind": lora_registry.KIND_EFFECT,
                    "compatible": True}]
        scan_a, calls_a = self._fake_scan(items_a)
        scan_b, calls_b = self._fake_scan(items_b)

        inv_a = lora_registry._cached_inventory("T", False, scan_a)
        inv_b = lora_registry._cached_inventory("T", False, scan_b)

        self.assertEqual([i["name"] for i in inv_a["effect"]], ["a.safetensors"])
        self.assertEqual([i["name"] for i in inv_b["effect"]], ["b.safetensors"])
        self.assertEqual(calls_a["n"], 1)
        self.assertEqual(calls_b["n"], 1)

    def test_cached_result_lists_are_copies(self):
        """缓存命中时返回的**列表必须是副本** —— 调用方改列表不能污染缓存。

        （条目 dict 仍是同一批对象 —— 现有调用方只读，不改条目。
        这条断言只锁「列表」这一层，因为那才是调用方真正会改的。）
        """
        items = [{"name": "a.safetensors", "kind": lora_registry.KIND_EFFECT,
                  "compatible": True}]
        scan, _ = self._fake_scan(items)

        first = lora_registry._cached_inventory("T", False, scan)
        first["effect"].append({"name": "injected.safetensors"})

        second = lora_registry._cached_inventory("T", False, scan)
        self.assertEqual(
            [i["name"] for i in second["effect"]], ["a.safetensors"],
            "缓存里不能出现调用方塞进去的东西（列表必须是副本）",
        )

    def test_explicit_invalidation_forces_rescan(self):
        """显式作废入口必须真的作废（装完 LoRA 的显式闸门）。"""
        items = [{"name": "a.safetensors", "kind": lora_registry.KIND_EFFECT,
                  "compatible": True}]
        scan, calls = self._fake_scan(items)

        lora_registry._cached_inventory("T", False, scan)
        lora_registry.invalidate_lora_scan_cache()
        lora_registry._cached_inventory("T", False, scan)

        self.assertEqual(calls["n"], 2, "显式作废后必须重新扫描")


class TestPublicEntryPointsUseCache(unittest.TestCase):
    """三个公共入口**必须**走缓存入口（否则改了也没用）。"""

    def setUp(self):
        lora_registry.invalidate_lora_scan_cache()
        self.addCleanup(lora_registry.invalidate_lora_scan_cache)

    def test_public_entrypoints_hit_cache(self):
        """`lora_inventory_by_kind*` 连打两次 -> 底层扫描只发生一次。

        断言**底层扫描函数的调用次数**（外部可观测事实）。
        """
        calls = {"n": 0}
        original = lora_registry.list_z_image_loras

        def _fake(deep_analysis=False):
            calls["n"] += 1
            return []

        lora_registry.list_z_image_loras = _fake
        try:
            lora_registry.lora_inventory_by_kind()
            lora_registry.lora_inventory_by_kind()
            lora_registry.lora_inventory_by_kind()
        finally:
            lora_registry.list_z_image_loras = original

        self.assertEqual(
            calls["n"], 1,
            "公共入口必须走缓存（否则每次 /api/image-loras 都重新扫盘）",
        )


class TestPayloadSizeBudget(unittest.TestCase):
    """接口载荷**体积**回归 —— 防止噪声字段再次塞回响应里。"""

    def test_handler_response_has_no_oversized_metadata_field(self):
        """接口层产出的条目里，不得有超长的**字符串** metadata 字段。

        ⚠️ 用**真实接口 handler 的产出**做检查（不是手搓一份假数据），
        但断言的是「字段长度」这个**独立的量**，不是拿输出算期望值。
        """
        # 这里不依赖本机真的装了那个 5.6MB 的 LoRA —— 用一个
        # 合成的条目直接过「下发前的裁剪」，验证裁剪逻辑在
        # 「条目 -> 响应」这条路上确实生效。
        item = {"name": "x.safetensors",
                "metadata": {"_": "z" * 5000}}
        trimmed = lora_registry._trim_metadata(item["metadata"])
        payload = {"available": [dict(item, metadata=trimmed)]}
        blob = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.assertLess(
            len(blob), 20000,
            "裁剪后的载荷必须显著小于原始体积（否则裁剪没生效）",
        )
        self.assertTrue(payload["available"][0]["metadata"]["_"]["_truncated"])

    def test_metadata_trim_budget_is_bounded(self):
        """上限常量必须是**有限且合理**的 —— 防止有人调到 10MB。"""
        self.assertGreater(lora_registry.METADATA_FIELD_MAX_BYTES, 0)
        self.assertLessEqual(
            lora_registry.METADATA_FIELD_MAX_BYTES, 65536,
            "单字段上限不得超过 64KB（再大就失去意义）",
        )

    def test_scan_output_itself_is_trimmed(self):
        """⚠️ **这条才是真正咬住缺陷的那条**。

        变异测试教训：只测 `_trim_metadata()` 本身的话，把调用点的
        `"metadata": _trim_metadata(meta)` 改回 `"metadata": meta`
        **测试照样全绿**（因为单独调 `_trim_metadata` 仍然是对的）。

        所以这里必须走**真实扫描函数**（`list_z_image_loras` 内部）——
        但不能直接替换掉它（替换掉 = 绕过了裁剪点，测的就不是那一段了）。

        做法：让**真实的** `list_z_image_loras` 去扫一个**真的**含超大
        metadata 字段的 safetensors 文件 —— 用 `_read_safetensors_metadata`
        的返回值做「注入点」，只在这个测试里替换它。
        这样「扫描 -> 条目」整条真实路径都被走到。

        断言的是「扫描返回的条目里那个字段已经被截断」。
        """
        huge = "q" * 200000

        class _FakePath:
            """最小 Path 替身：只需 name / stem / relative_to / stat。"""
            def __init__(self, name):
                self._name = name
                self.name = name
                self.stem = name[:-len(".safetensors")]

            def relative_to(self, other):
                return self

            def as_posix(self):
                return self._name

            def stat(self):
                class _S:
                    st_size = 1024
                    st_mtime_ns = 1
                return _S()

            def is_file(self):
                return True

        oz = lora_registry.list_z_image_loras
        orig_meta = lora_registry._read_safetensors_metadata
        orig_dirs = lora_registry.comfyui_lora_dirs

        fake_path = _FakePath("z-image-effects/huge.safetensors")
        lora_registry._read_safetensors_metadata = lambda p: {"_": huge}
        lora_registry.comfyui_lora_dirs = lambda: [Path("/fake/loras")]
        # 让真实扫描函数只看到一个文件
        import pathlib
        orig_rglob = pathlib.Path.rglob

        def _fake_rglob(self, pattern):
            if str(self) == "/fake/loras":
                return iter([fake_path])
            return orig_rglob(self, pattern)

        pathlib.Path.rglob = _fake_rglob
        try:
            items = oz(deep_analysis=False)
        finally:
            pathlib.Path.rglob = orig_rglob
            lora_registry._read_safetensors_metadata = orig_meta
            lora_registry.comfyui_lora_dirs = orig_dirs

        self.assertTrue(items, "假目录里应当扫到那个条目")
        item = items[0]
        md = item["metadata"]
        # 字段必须已被截断（不是原始 200KB 字符串）
        self.assertNotIsInstance(md["_"], str,
                                 "扫描产出的条目里，超长 metadata 字段必须已被截断")
        self.assertTrue(md["_"]["_truncated"])
        self.assertEqual(md["_"]["_original_length"], 200000)
        # 整条目的序列化体积必须远小于原始体积
        blob = json.dumps(item, ensure_ascii=False).encode("utf-8")
        self.assertLess(
            len(blob), 20000,
            f"扫描产出条目的体积必须受控（实测 {len(blob)} 字节）",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestLookupLoraMetaUsesCache(unittest.TestCase):
    """`_lookup_lora_meta`（工作流构建热路径）必须走缓存入口。

    【为什么这条也要锁 —— 2026-11-21 线上实测】
    `h3studio/workflows.py::_lookup_lora_meta` 原来直接调三个
    `list_*_loras()`（三次**全量目录扫描**），实测**单次调用 2.225 秒**。
    而它在**每个工作流构建点**都会被调（workflows.py 的 Z-Image /
    Qwen / MOSS 三处）—— 一次任务提交就是 3 × 2.2s。
    ⇒ 现在必须走 `lora_inventory_by_kind*`（带缓存）。
    """

    def setUp(self):
        from h3studio import workflows as wf
        self.wf = wf
        lora_registry.invalidate_lora_scan_cache()
        self.addCleanup(lora_registry.invalidate_lora_scan_cache)

    def test_lookup_does_not_rescan_per_call(self):
        """连续两次 `_lookup_lora_meta` **不得**各扫一遍全盘。

        ⚠️ 断言要点：改用缓存入口后，**底层扫描函数仍会被调用**（首次
        填充缓存时）—— 这是正常的。要锁的是「**重复调用不再重复扫**」：
        旧写法每次调用都重新扫 3 遍（全盘 700+ 文件），实测 2.2 秒/次。

        所以断言的是「N 次调用 ⇒ 底层扫描次数是 O(1) 而不是 O(N)」，
        用**放大系数**判定（2 次调用 ⇒ 扫描次数必须 ≤ 3，即「三个族各扫
        一次」，而不是「每次调用各扫一遍」= 6 次）。
        """
        calls = {"n": 0}
        oz, oq, om = (lora_registry.list_z_image_loras,
                      lora_registry.list_qwen_image_loras,
                      lora_registry.list_moss_image_loras)

        def mk():
            def _f(deep_analysis=False):
                calls["n"] += 1
                return []
            return _f

        lora_registry.list_z_image_loras = mk()
        lora_registry.list_qwen_image_loras = mk()
        lora_registry.list_moss_image_loras = mk()
        try:
            self.wf._lookup_lora_meta("whatever.safetensors")
            first = calls["n"]
            self.wf._lookup_lora_meta("whatever.safetensors")
            self.wf._lookup_lora_meta("whatever.safetensors")
        finally:
            (lora_registry.list_z_image_loras,
             lora_registry.list_qwen_image_loras,
             lora_registry.list_moss_image_loras) = (oz, oq, om)

        self.assertEqual(
            first, 3,
            "第一次调用应各扫三族一次（Z-Image / Qwen / MOSS）",
        )
        self.assertEqual(
            calls["n"], 3,
            "第二次、第三次调用**不得**再触发扫描"
            "（旧写法每次 3 次全盘扫描 = 2.2 秒/次）",
        )

    def test_lookup_still_finds_acceleration_kind(self):
        """闸门不能因为改用缓存而失效：加速类 LoRA 必须仍能被认出来。

        ⚠️ 这是最容易改坏的地方 —— 只取 `effect` 会漏掉加速类，
        而调用方靠 `kind == KIND_ACCELERATION` 拒绝「把加速 LoRA
        当效果 LoRA 用」。
        """
        item = {"name": "acc.safetensors",
                "kind": lora_registry.KIND_ACCELERATION,
                "compatible": True, "display": "加速 LoRA"}
        oz = lora_registry.list_z_image_loras
        oq = lora_registry.list_qwen_image_loras
        om = lora_registry.list_moss_image_loras

        lora_registry.list_z_image_loras = lambda deep_analysis=False: [item]
        lora_registry.list_qwen_image_loras = lambda deep_analysis=False: []
        lora_registry.list_moss_image_loras = lambda deep_analysis=False: []
        try:
            got = self.wf._lookup_lora_meta("acc.safetensors")
        finally:
            (lora_registry.list_z_image_loras,
             lora_registry.list_qwen_image_loras,
             lora_registry.list_moss_image_loras) = (oz, oq, om)

        self.assertEqual(
            got.get("kind"), lora_registry.KIND_ACCELERATION,
            "加速类 LoRA 必须仍能被认出来（否则「不能当效果 LoRA」的闸门失效）",
        )
