# -*- coding: utf-8 -*-
"""三基座 **NSFW / 成人向效果类** LoRA 接入的回归测试。

覆盖本轮接入（2026-11-10）挖出并写死的几个**真事实**：

1. **加速 / 蒸馏类绝不进用户可选列表**
   —— 成人向 LoRA 里最容易混进来的是「8steps 合体加速版」
   （`moss_pussy_lora_v3.0_8steps*.safetensors` 内含 DMD2 加速）。
   本轮目录里 0 条加速类。

2. **文件名含 turbo 不等于加速类**
   `turbopussyzit-v2.safetensors` 是 NSFW 姿态/部位**效果** LoRA
   （base_model 标签 = Z-Image-Turbo）。关键词启发式会误伤 ——
   目录里显式声明 kind=effect，登记值优先于启发式。

3. **目录里的每条都必须在磁盘上真实存在**
   —— 目录条目若没落盘，不能出现在用户可选列表里。

4. **每条的基座归属必须对**
   —— 本轮实测两个条目键名根本不是 MOSS（Anima 键名 0/280、
   SD 键名 0/788），已剔除。测试锁死「目录里不出现这两条」。

5. **推荐强度口径**
   —— 作者明示具体值 -> 用作者值；作者明示区间 -> 取中值并写明；
   作者未给 -> 1.0 且 `strength_source` 必须标注是建议值。

6. **MOSS 用 LoraLoaderModelOnly（DiT），Z-Image / QI21 用 LoraLoader**
   —— MOSS 的文本编码器是 Qwen3.5-9B，LoRA 只训 transformer 侧，
   接 clip 反而会报 "lora key not loaded"。

运行：/home/ubuntu/comfyui/venv/bin/python -m pytest tests/test_lora_nsfw.py -v
"""
import json
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from h3studio import loras as lora_registry            # noqa: E402
from h3studio import workflows as W                     # noqa: E402
from h3studio import lora_catalog_nsfw as catalog       # noqa: E402

LORAS_ROOT = Path("/mnt/usb2t/comfyui-models/loras")

FAMILY_TO_DIR = {
    "Z-Image": "z-image-effects",
    "Qwen-Image 2.1": "qwen21-effects",
    "MOSS-Image": "moss-effects",
}

# 本轮实测：这两条键名根本不是 MOSS 基座，已剔除，不得进目录
REJECTED_BASE_MISMATCH = {
    "Anima-梦幻柔光（nai5其五）.safetensors",
    "KonyaKarasue-Lokr-NOOBEPS110-000040.safetensors",
}


class TestNSFWCatalog(unittest.TestCase):
    def test_catalog_not_empty(self):
        self.assertGreater(len(catalog.NSFW_ALL_EFFECTS), 50,
                           "NSFW 目录条目数异常少")

    def test_all_entries_are_effect_kind(self):
        """目录里的每条都必须显式声明 kind=effect（不靠关键词启发式）"""
        for key, entry in catalog.NSFW_ALL_EFFECTS.items():
            self.assertEqual(entry.get("kind"), "effect",
                             f"{key} 的 kind 应为 effect，实际 {entry.get('kind')}")

    def test_no_acceleration_lora(self):
        """加速 / 蒸馏类绝不能进用户可选列表"""
        accel_markers = ("turbo_lora", "distill", "lightning", "dmd",
                         "speedup", "accel", "8steps", "4step", "lcm")
        offenders = []
        for key, entry in catalog.NSFW_ALL_EFFECTS.items():
            name = key.split("/", 1)[1].lower()
            # 「turbopussyzit-v2」这类是效果类，不能按 turbo 字样误伤
            for marker in accel_markers:
                if marker in name:
                    # 排除已知的效果类误伤：NSFW MASTER / pornmaster 等
                    if "nsfw_master" in name or "pornmaster" in name:
                        continue
                    offenders.append(f"{key} 含加速关键词 {marker}")
        self.assertEqual(offenders, [], f"目录里混进了加速类：{offenders}")

    def test_rejected_base_mismatch_absent(self):
        """基座不符（键名对不上底模）的条目不得出现在目录里"""
        for key in catalog.NSFW_ALL_EFFECTS:
            fname = key.split("/", 1)[1]
            self.assertNotIn(fname, REJECTED_BASE_MISMATCH,
                             f"{fname} 键名与底模不符（全层命中 0），不应入列")

    def test_files_exist_on_disk(self):
        """目录里的每条都必须真的在 ComfyUI 的 loras 目录里"""
        missing = []
        for key in catalog.NSFW_ALL_EFFECTS:
            fam_dir, fname = key.split("/", 1)
            path = LORAS_ROOT / fam_dir / fname
            if not path.is_file():
                missing.append(str(path))
        self.assertEqual(missing, [], f"目录条目未落盘：{missing}")

    def test_files_are_real_safetensors_with_lora_keys(self):
        """落盘的文件必须含 lora / lokr / hada 键（不是 HTML 登录页）"""
        import struct
        markers = (".lora_A.default.weight", ".lora_B.default.weight",
                   ".lora_A.weight", ".lora_B.weight",
                   ".lora_down.weight", ".lora_up.weight",
                   ".lokr_w1", ".lokr_w2", ".hada_w1", ".hada_w2")
        bad = []
        for key in catalog.NSFW_ALL_EFFECTS:
            fam_dir, fname = key.split("/", 1)
            path = LORAS_ROOT / fam_dir / fname
            try:
                with open(path, "rb") as fh:
                    n = struct.unpack("<Q", fh.read(8))[0]
                    header = json.loads(fh.read(n))
            except Exception as exc:
                bad.append(f"{fname}: 头部不可解析 {exc}")
                continue
            keys = [k for k in header if k != "__metadata__"]
            if not any(k.endswith(m) for k in keys for m in markers):
                bad.append(f"{fname}: 头部没有 LoRA 键")
        self.assertEqual(bad, [], f"以下文件不是真 LoRA 权重：{bad}")

    def test_strength_source_annotated(self):
        """推荐强度必须写明依据；作者未给的要标为建议值"""
        for key, entry in catalog.NSFW_ALL_EFFECTS.items():
            self.assertIn("strength_source", entry, f"{key} 缺 strength_source")
            src = entry["strength_source"]
            self.assertTrue(src.strip(), f"{key} 的 strength_source 为空")
            st = entry.get("recommended_strength")
            self.assertIsInstance(st, (int, float), f"{key} 缺 recommended_strength")
            self.assertGreater(float(st or 0), 0.0,
                               f"{key} 的 recommended_strength 应 > 0")
            if "未给" in src or "未明示" in src:
                self.assertIn("建议值", src,
                              f"{key}: 作者未给强度时必须标注为建议值")

    def test_family_prefix_matches_base(self):
        """目录前缀 -> base 字段必须一致（前缀是族归属的唯一真相源）"""
        prefix_base = {
            "z-image-effects/": "Z-Image-Turbo",
            "qwen21-effects/": "Qwen-Image 2.1",
            "moss-effects/": "MOSS-Image",
        }
        for key, entry in catalog.NSFW_ALL_EFFECTS.items():
            for prefix, base in prefix_base.items():
                if key.startswith(prefix):
                    self.assertEqual(entry["base"], base,
                                     f"{key} 的 base 与前缀不符")
                    break
            else:
                self.fail(f"{key} 没有族专属前缀")

    def test_discovered_by_registry(self):
        """目录里的条目必须能被 registry 扫到（且标成 effect）"""
        for fam, lister in (("Z-Image", lora_registry.list_z_image_loras),
                            ("Qwen-Image 2.1", lora_registry.list_qwen_image_loras),
                            ("MOSS-Image", lora_registry.list_moss_image_loras)):
            items = lister()
            by_name = {i["name"]: i for i in items}
            mine = {k: v for k, v in catalog.NSFW_ALL_EFFECTS.items()
                    if k.startswith(FAMILY_TO_DIR[fam] + "/")}
            self.assertGreater(len(mine), 0, f"{fam} 目录为空")
            for key in mine:
                self.assertIn(key, by_name,
                              f"{key} 未被 registry 扫到（文件不在扫描目录？）")
                self.assertEqual(by_name[key]["kind"], "effect",
                                 f"{key} 在 registry 里不是 effect")

    def test_catalog_entries_have_complete_display_metadata(self):
        """目录条目必须带完整的中文显示名与场景说明（界面要显示这些）"""
        incomplete = []
        for key, entry in catalog.NSFW_ALL_EFFECTS.items():
            if not (entry.get("display") or "").strip():
                incomplete.append(f"{key} 缺 display")
            if not (entry.get("scene") or "").strip():
                incomplete.append(f"{key} 缺 scene")
            if not (entry.get("source") or "").strip():
                incomplete.append(f"{key} 缺 source")
        self.assertEqual(incomplete, [],
                         f"目录条目缺界面需要的字段：{incomplete[:10]}")

    def test_moss_uses_model_only_loader(self):
        """MOSS 链必须用 LoraLoaderModelOnly（DiT），不能用带 clip 的 LoraLoader"""
        import inspect
        src = inspect.getsource(W._moss_lora_chain)
        self.assertIn("LoraLoaderModelOnly", src)
        self.assertNotIn('"clip"', src,
                         "MOSS 的 LoRA 权重只有 transformer 侧，接 clip 会静默失效")

    def test_zimage_and_qwen_use_lora_loader_with_clip(self):
        """Z-Image / QI21 必须用 LoraLoader（model + clip 都接）"""
        import inspect
        for fn in (W._z_image_lora_chain, W._qwen_lora_chain):
            src = inspect.getsource(fn)
            self.assertIn('"class_type": "LoraLoader"', src)
            self.assertIn('inputs["clip"]', src)

    def test_accel_lora_not_in_nsfw_catalog(self):
        """工作流固定的加速 LoRA 不许出现在 NSFW 目录里"""
        accel = getattr(W, "Z_IMAGE_ACCEL_LORA", {}) or {}
        accel_values = {v for v in accel.values() if v}
        for key in catalog.NSFW_ALL_EFFECTS:
            self.assertNotIn(key.split("/", 1)[1], accel_values,
                             f"{key} 与工作流固定的加速 LoRA 重名")


if __name__ == "__main__":
    unittest.main()
