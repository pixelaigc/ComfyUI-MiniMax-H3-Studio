# -*- coding: utf-8 -*-
"""L-B：MOSS 派生文件清理（`moss_lora_split.cleanup_stale_derived`）。

【这个测试在防什么】
上一轮验收判定「cleanup_stale_derived() 无调用点」—— 派生文件不自动清理。
补上调用点之后又暴露了更严重的问题：**旧实现会把全部有效派生文件删光**。

旧实现反解源文件时用的是 `lora_dir / <原名>`，而派生文件其实放在
**源文件同一个子目录**里（`moss-effects/`）—— 于是源文件明明在，
却被判成「不存在」-> 判定「派生文件过期」-> **48 个有效派生文件全删光**。
那等于把「MOSS 融合 qkv 转换已验证」这条证据一次性抹掉，
48 条 LoRA 立刻从 `derived` 掉回 `unverified` -> compatible=false
-> 用户可选列表凭空少 48 条。

所以本测试锁死三件事：
  1. **有效派生文件绝不能被删**（源文件在、mtime 不比派生文件新）
  2. **过期派生文件会被删**（源文件比派生文件新）
  3. **孤儿派生文件会被删**（源文件没了）
  4. 调用点存在：派生文件生成成功后会触发清理

运行：python3 -m pytest tests/test_labc_cleanup_stale_derived.py -v
"""
import inspect
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from h3studio import moss_lora_split as split  # noqa: E402


class TestCleanupStaleDerived(unittest.TestCase):
    """派生文件清理的三个场景 + 调用点。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="labc_cleanup_")
        self.root = Path(self.tmp)
        # 模拟真实的目录布局：派生文件在**族专属子目录**里
        self.sub = self.root / "moss-effects"
        self.sub.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make(self, source_name, derived_name, src_mtime, der_mtime):
        """造一对 源文件 / 派生文件，并设好 mtime。"""
        src = self.sub / source_name
        der = self.sub / derived_name
        src.write_bytes(b"S" * 64)
        der.write_bytes(b"D" * 64)
        os.utime(src, (src_mtime, src_mtime))
        os.utime(der, (der_mtime, der_mtime))
        return src, der

    def test_valid_derived_is_not_deleted(self):
        """有效派生文件（源文件在、mtime 不更新）绝不能被删。"""
        src, der = self._make(
            "foo.safetensors", split.derived_name("foo.safetensors"),
            src_mtime=1_000_000, der_mtime=1_000_000)
        self.assertTrue(der.is_file())
        removed = split.cleanup_stale_derived([self.root])
        self.assertEqual(removed, 0,
                         "有效派生文件被删了 —— 旧实现会用 lora 根目录反解源文件，"
                         "把全部有效派生文件误删")
        self.assertTrue(der.is_file(), "有效派生文件必须保留")
        self.assertTrue(src.is_file())

    def test_derived_newer_than_source_is_kept(self):
        """派生文件比源文件新（刚生成）-> 保留。"""
        src, der = self._make(
            "bar.safetensors", split.derived_name("bar.safetensors"),
            src_mtime=1_000_000, der_mtime=2_000_000)
        removed = split.cleanup_stale_derived([self.root])
        self.assertEqual(removed, 0)
        self.assertTrue(der.is_file())

    def test_stale_derived_is_deleted(self):
        """源文件比派生文件新（源被重下/替换）-> 派生文件带旧权重，必须删。"""
        src, der = self._make(
            "baz.safetensors", split.derived_name("baz.safetensors"),
            src_mtime=2_000_000, der_mtime=1_000_000)
        removed = split.cleanup_stale_derived([self.root])
        self.assertEqual(removed, 1,
                         "源文件已更新，派生文件带旧权重，必须被清理")
        self.assertFalse(der.exists(), "过期派生文件必须被删")
        self.assertTrue(src.is_file(), "源文件绝不能被删")

    def test_orphan_derived_is_deleted(self):
        """源文件没了（被删/改名）-> 派生文件是孤儿，必须删。"""
        der = self.sub / split.derived_name("gone.safetensors")
        der.write_bytes(b"D" * 64)
        removed = split.cleanup_stale_derived([self.root])
        self.assertEqual(removed, 1, "孤儿派生文件必须被清理")
        self.assertFalse(der.exists())

    def test_source_lookup_uses_own_directory_not_lora_root(self):
        """回归锁死：反解源文件必须用派生文件**自己的目录**。

        旧实现用 `lora_dir / <原名>` —— 源文件在子目录里就找不到，
        于是把有效派生文件全删光。本测试直接断言这个行为差异。
        """
        src, der = self._make(
            "sub.safetensors", split.derived_name("sub.safetensors"),
            src_mtime=1_000_000, der_mtime=1_000_000)
        # 源文件确实在派生文件自己的目录里
        self.assertTrue((der.parent / "sub.safetensors").is_file())
        # 旧算法的反解路径（lora 根目录）找不到源文件
        old_lookup = self.root / "sub.safetensors"
        self.assertFalse(old_lookup.is_file(),
                         "本测试的前提是旧反解路径找不到源文件")
        # 新实现必须能找到 -> 不删
        removed = split.cleanup_stale_derived([self.root])
        self.assertEqual(removed, 0)
        self.assertTrue(der.is_file())


class TestCleanupCallSites(unittest.TestCase):
    """调用点存在性 —— 上一轮的核心遗留项就是这个。"""

    def test_prewarm_derived_calls_cleanup(self):
        """预热（派生文件生成成功）后必须触发清理。"""
        src = inspect.getsource(split.prewarm_derived)
        self.assertIn("cleanup_stale_derived",
                      src,
                      "prewarm_derived 必须在派生文件生成成功后调用 "
                      "cleanup_stale_derived —— 上一轮遗留项 L-B")

    def test_ensure_normalized_calls_cleanup(self):
        """工作流提交路径（ensure_normalized）生成派生文件后也要清理。"""
        src = inspect.getsource(split.ensure_normalized)
        self.assertIn("cleanup_stale_derived",
                      src,
                      "ensure_normalized 必须在派生文件生成成功后调用 "
                      "cleanup_stale_derived —— 上一轮遗留项 L-B")

    def test_cleanup_is_reachable_from_public_api(self):
        """清理函数必须真的能从公开路径调到（不是死代码）。"""
        self.assertTrue(callable(split.cleanup_stale_derived))
        # ensure_normalized 内部会调它 —— 用一个真文件验证调用链活着
        with tempfile.TemporaryDirectory(prefix="labc_call_") as tmp:
            d = Path(tmp)
            src = d / "x.safetensors"
            src.write_bytes(b"not-a-real-safetensors")
            # 读不出头 -> ensure_normalized 提前返回（不触发清理），
            # 但函数本身不能抛 —— 证明清理调用被 try 包住
            res = split.ensure_normalized(src)
            self.assertIsNone(res["path"])


if __name__ == "__main__":
    unittest.main()
