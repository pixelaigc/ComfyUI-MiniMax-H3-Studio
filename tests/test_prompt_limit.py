# -*- coding: utf-8 -*-
"""
「提示词长度上限 + 无限制」功能的单元测试。

【背景】
用户在「本地模型配置」里点二次增强，报「提示词过长（上限 4000 字）」，
要求把这个上限做成可配置项，并提供「无限制」选项。

【覆盖】
  1. 取值语义：unlimited / 4000 / 8000 / 20000 / 50000
  2. 老配置兼容：没配 -> 默认 4000；非法值 -> 4000；老 DB 迁移
  3. 存储层：normalize / insert / update / read 往返
  4. 调用链：三个入口（/api/enhance / /api/moss-layout / /api/qwen-pe/expand）
     都走同一套校验 —— 不能一处放开另一处还砍
  5. 输出预算：无限制时 max_tokens = -1（不设人为上限）
  6. 后端物理上限：如实探测并回传，不静默截断
"""
import importlib.util
import os
import sqlite3
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, rel):
    path = os.path.join(REPO, rel)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


prov = _load("prov_mod", os.path.join("h3studio", "providers.py"))
enh = _load("enh_mod", os.path.join("h3studio", "enhancer.py"))
llm_client = _load("llm_mod", os.path.join("h3studio", "llm.py"))


def _base_payload(**over):
    p = {"name": "本机 Qwen", "type": "openai",
         "base_url": "http://127.0.0.1:5000",
         "model": "qwen3.8-flash-next-iq3_xxs", "api_key": "",
         "multimodal": True, "enabled": True}
    p.update(over)
    return p


class TestPromptLimitValues(unittest.TestCase):
    """取值语义 —— 显式字符串，不靠魔法负数。"""

    def test_unlimited_is_explicit_string(self):
        self.assertEqual(prov.PROMPT_LIMIT_UNLIMITED, "unlimited")
        self.assertNotIn(0, prov.PROMPT_LIMIT_CHOICES)

    def test_default_is_4000(self):
        """默认沿用改动前的 4000 字上限，保证老配置兼容。"""
        self.assertEqual(prov.DEFAULT_PROMPT_LIMIT, "4000")
        self.assertEqual(prov.prompt_limit_max_chars(prov.DEFAULT_PROMPT_LIMIT), 4000)

    def test_unlimited_maps_to_none(self):
        """unlimited -> None（不限），不是 0、不是负数。"""
        self.assertIsNone(prov.prompt_limit_max_chars("unlimited"))

    def test_all_choices_resolvable(self):
        for v in prov.PROMPT_LIMIT_CHOICES:
            got = prov.prompt_limit_max_chars(v)
            if v == "unlimited":
                self.assertIsNone(got)
            else:
                self.assertEqual(got, int(v))

    def test_labels_exist_for_all_choices(self):
        for v in prov.PROMPT_LIMIT_CHOICES:
            self.assertIn(v, prov.PROMPT_LIMIT_LABELS)
            self.assertTrue(prov.PROMPT_LIMIT_LABELS[v])


class TestPromptLimitLegacyCompat(unittest.TestCase):
    """老配置升级不报错、行为不变。"""

    def test_missing_field_defaults_to_4000(self):
        d = prov.normalize_provider(_base_payload())
        self.assertEqual(d["prompt_limit"], "4000")

    def test_illegal_value_rejected(self):
        with self.assertRaises(ValueError):
            prov.normalize_provider(_base_payload(prompt_limit="999999"))
        with self.assertRaises(ValueError):
            prov.normalize_provider(_base_payload(prompt_limit="-1"))

    def test_none_and_empty_default(self):
        self.assertEqual(
            prov.normalize_provider(_base_payload(prompt_limit=None))["prompt_limit"],
            "4000")
        self.assertEqual(
            prov.normalize_provider(_base_payload(prompt_limit=""))["prompt_limit"],
            "4000")

    def test_normalize_limit_helper_tolerates_garbage(self):
        self.assertEqual(prov._normalize_limit(None), "4000")
        self.assertEqual(prov._normalize_limit(""), "4000")
        self.assertEqual(prov._normalize_limit("bogus"), "4000")
        self.assertEqual(prov._normalize_limit("UNLIMITED"), "unlimited")

    def test_old_db_migration_adds_column(self):
        """老库（没有 prompt_limit 列）启动后必须能补列且填默认值。"""
        tmp = tempfile.mkdtemp()
        db = os.path.join(tmp, "t.db")
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE model_providers ("
            " id TEXT PRIMARY KEY, name TEXT NOT NULL, type TEXT NOT NULL,"
            " base_url TEXT NOT NULL, llm_base_url TEXT DEFAULT '',"
            " use_proxy INTEGER DEFAULT 0, proxy_base_url TEXT DEFAULT '',"
            " model TEXT NOT NULL, api_key TEXT DEFAULT '',"
            " multimodal INTEGER DEFAULT 0, enabled INTEGER DEFAULT 1,"
            " created_at REAL)")
        conn.execute("INSERT INTO model_providers "
                     "(id,name,type,base_url,model,created_at) "
                     "VALUES ('p1','旧','openai','http://x','m',1.0)")
        conn.commit()
        conn.close()

        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        prov.ensure_tables(conn)
        cols = {r["name"] for r in
                conn.execute("PRAGMA table_info(model_providers)").fetchall()}
        self.assertIn("prompt_limit", cols)
        item = prov.get_provider_full(conn, "p1")
        self.assertEqual(item["prompt_limit"], "4000")
        conn.close()


class TestPromptLimitStorage(unittest.TestCase):
    """存储层往返。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.conn = sqlite3.connect(os.path.join(self.tmp, "t.db"))
        self.conn.row_factory = sqlite3.Row
        prov.ensure_tables(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_insert_update_read_roundtrip(self):
        data = prov.normalize_provider(_base_payload(prompt_limit="unlimited"))
        data["id"] = "p1"
        data["created_at"] = 1.0
        prov.insert_provider(self.conn, data)
        got = prov.get_provider_full(self.conn, "p1")
        self.assertEqual(got["prompt_limit"], "unlimited")

        data2 = prov.normalize_provider(_base_payload(prompt_limit="20000"))
        data2["id"] = "p1"
        prov.update_provider(self.conn, "p1", data2)
        self.assertEqual(
            prov.get_provider_full(self.conn, "p1")["prompt_limit"], "20000")

    def test_list_providers_exposes_limit(self):
        data = prov.normalize_provider(_base_payload(prompt_limit="8000"))
        data["id"] = "p2"
        data["created_at"] = 2.0
        prov.insert_provider(self.conn, data)
        items = prov.list_providers(self.conn)
        self.assertEqual(items[0]["prompt_limit"], "8000")


class TestUnlimitedOutputBudget(unittest.TestCase):
    """输出预算：无限制时不被我们自己的 ceiling 压住。"""

    def test_unlimited_output_uses_sentinel(self):
        params = enh.resolve_params(None, {}, {"id": "image", "validate_mode": "refine"},
                                    original="x" * 100, unlimited_output=True)
        self.assertEqual(params["max_tokens"], enh.UNLIMITED_MAX_TOKENS)
        self.assertLess(params["max_tokens"], 0)
        self.assertTrue(params["max_tokens_unlimited"])

    def test_limited_output_still_adaptive(self):
        params = enh.resolve_params(None, {}, {"id": "image", "validate_mode": "refine"},
                                    original="x" * 100, unlimited_output=False)
        self.assertGreater(params["max_tokens"], 0)
        self.assertFalse(params.get("max_tokens_unlimited"))

    def test_sentinel_is_native_llamacpp_convention(self):
        """-1 是 llama.cpp / Ollama 的原生「不限」表示，不是魔法负数。"""
        self.assertEqual(enh.UNLIMITED_MAX_TOKENS, -1)

    def test_timeout_handles_unlimited(self):
        """不限时超时按封顶算，不能因为负数算出怪值。"""
        self.assertEqual(llm_client.compute_timeout(-1), llm_client.MAX_TIMEOUT)

    def test_comfy_node_supports_native_unlimited(self):
        """
        ComfyUI 本地 LLM 节点的 max_tokens schema 实测是
            ["INT", {default:4096, min:-1, max:262144, step:64}]
        —— min 就是 -1，**节点原生支持「不限」**。
        所以不限时直接传 -1，不需要「传大值再让节点夹」的绕法。
        """
        self.assertEqual(llm_client.COMFY_NODE_MAX_TOKENS_MIN, -1)


class TestNoRemainingHardGuards(unittest.TestCase):
    """
    【关键回归】调用链上不允许再有硬编码的 4000 字守卫。

    改动前有 3 处 `if len(original) > 4000`，散在三个 endpoint。
    只在一处放开、另一处还砍 = 用户换个入口又看到同一个报错。
    """

    def _server_src(self):
        with open(os.path.join(REPO, "server.py"), encoding="utf-8") as f:
            return f.read()

    def test_no_hardcoded_4000_guard_left(self):
        src = self._server_src()
        self.assertNotIn("if len(original) > 4000:", src)
        # 也不该有别的字面量上限（避免以后又写死一个 8000/20000）
        import re
        self.assertIsNone(
            re.search(r"if len\(original\) > \d+:", src),
            "提示词长度上限必须来自 Provider 配置，不能再硬编码")

    #: 所有会吃「原始提示词」的入口 —— 都必须走统一的提示词长度校验。
    #: （rebase 到新 maintainer 后，入口从 3 个增加到 5 个：
    #:  多了 api_music_enhance 与 api_h3image_pe_expand）
    GUARDED_ENDPOINTS = ("api_enhance", "api_moss_layout", "api_qwen_pe_expand",
                         "api_music_enhance", "api_h3image_pe_expand")

    def test_all_guards_go_through_single_helper(self):
        src = self._server_src()
        # 长度校验只有这一个入口：1 处定义 + N 处调用（不含注释里的提及）
        calls = [ln for ln in src.splitlines()
                 if "_prompt_limit_guard(" in ln
                 and not ln.strip().startswith("#")
                 and not ln.strip().startswith("def ")]
        self.assertEqual(len(calls), len(self.GUARDED_ENDPOINTS),
                         "每个吃提示词的 endpoint 各调一次，实际：%s" % calls)
        # 且每个 endpoint 都调用了它
        for fn in self.GUARDED_ENDPOINTS:
            i = src.index("async def %s(" % fn)
            j = src.index("\nasync def ", i + 10)
            self.assertIn("_prompt_limit_guard(", src[i:j],
                          "%s 必须走统一的提示词长度校验" % fn)

    def test_guard_is_after_provider_resolution(self):
        """校验必须在 Provider 解析之后 —— 上限是按 Provider 配的。"""
        src = self._server_src()
        i = src.index("async def api_enhance(")
        j = src.index("\nasync def ", i + 10)
        body = src[i:j]
        self.assertLess(body.index("resolve_default"),
                        body.index("_prompt_limit_guard"))


if __name__ == "__main__":
    unittest.main()
