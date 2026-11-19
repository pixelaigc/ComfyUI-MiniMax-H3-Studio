# -*- coding: utf-8 -*-
"""
c6 修复的**真实执行**测试：详情模态框里必须有「作者推荐参数」分组。

【为什么不用字符串断言】
「源码里有没有 '作者推荐参数' 这个字符串」这种断言抓不住逻辑退化 ——
把渲染函数改成返回空串、或把「作者未提供」换回原始哨兵值 `not_provided`，
字符串断言照样全绿，但页面上的东西是错的。
所以这里**真的把 web/app.js 的 detailRecommendHtml() 抠出来跑**（Node），
喂进真实形状的 job，断言**渲染出来的 HTML 文本**。

【覆盖】
  1. 推荐参数分组必须出现，且含 c6 要求的 5 类字段：
     推荐步数 / 推荐调度器 / 推荐采样器 / 音视频 shift / 推荐主模型；
  2. 作者未提供的项必须显式渲染中文「作者未提供」，
     **绝不能**把服务端哨兵值 `not_provided` 透给用户；
  3. 来源可追溯：分组里必须有来源说明（来源类型 / 出处）；
  4. 没有推荐数据时**不渲染空分组**；
  5. 接线正确：openDetailModal 必须调用它，且插在 #detailParams 的
     **兄弟 section**（不能塞进 .detail-kv 网格里）；
  6. 服务端未改：model_info 仍下发 model_recommendation / lora_recommendation /
     fixed_lora_recommendation。

运行：python3 -m pytest tests/test_c6_detail_recommend.py -v
"""
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

NODE = shutil.which("node") or shutil.which("nodejs")

JOB_META_JS = (REPO / "web" / "job-meta.js").read_text(encoding="utf-8")
_APP_JS = (REPO / "web" / "app.js").read_text(encoding="utf-8")
_INDEX_HTML = (REPO / "web" / "index.html").read_text(encoding="utf-8")
_JOB_META_PY = (REPO / "h3studio" / "job_meta.py").read_text(encoding="utf-8")


def _extract_function(src: str, name: str) -> str:
    """抠出一个顶层 function 的源码（括号配对扫描，不用正则）。"""
    match = re.search(r"function\s+" + re.escape(name) + r"\s*\(", src)
    if not match:
        raise AssertionError(f"找不到 function {name}")
    start = match.start()
    i = src.index("{", match.end() - 1)
    depth = 0
    in_str = None
    while i < len(src):
        ch = src[i]
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == in_str:
                in_str = None
        elif ch in ("'", '"', "`"):
            in_str = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
        i += 1
    raise AssertionError(f"function {name} 括号没配平")


_PRELUDE = """
global.window = global;
global.document = { querySelector: () => null, addEventListener: () => {} };
global.MODE_INFO = { t2v: { name: "文生视频", kind: "video" } };
function escapeHtml(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
"""


def _run_node(script: str, timeout: int = 60) -> dict:
    assert NODE is not None, "node 不可用"
    full = (_PRELUDE
            + "\n" + JOB_META_JS
            + "\n" + _extract_function(_APP_JS, "detailRecommendHtml")
            + "\n" + script)
    proc = subprocess.run(
        [NODE, "--input-type=commonjs", "-e", full],
        capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise AssertionError(f"node 执行失败:\n{proc.stdout}\n{proc.stderr}")
    import json as _json
    return _json.loads(proc.stdout.strip().splitlines()[-1])


#: 一条真实的 t2v 任务形状（取自线上 /api/jobs 的 model_info）。
_JOB = {
    "id": "testc6job0001",
    "mode": "t2v",
    "gen_type": "t2v",
    "created_at": 1794590000.0,
    "params": {"prompt": "一只纸船", "model_key": "fl2va_pruned_int8"},
    "gen_params": {"model": "MiniMax-H3 FL2VA（int8，默认档）", "steps": "4"},
    "model_info": {
        "model_label": "MiniMax-H3 FL2VA（int8，默认档）",
        "model_recommendation": {
            "steps": "not_provided", "scheduler": "not_provided",
            "sampler": "not_provided", "shift_video": "not_provided",
            "shift_audio": "not_provided", "base_model": "not_provided",
            "source_kind": "not_provided", "source": "", "note": "",
        },
        "lora_recommendation": {
            "steps": 4, "scheduler": "simple", "sampler": "not_provided",
            "accel_lora": "ckpt850", "accel_lora_strength": 1.0,
            "accel_loras": [{"key": "ckpt850", "strength": 1.0, "enabled": True}],
            "shift_video": 12.0, "shift_audio": 3.0,
            "base_model": "MiniMax-H3（bf16 / int8_convrot / pruned_int8）",
            "source_kind": "author",
            "source": "/home/ubuntu/custom_nodes/ComfyUI-MiniMax-H3-Turbo/README.md:76-88",
            "note": "作者未给具体采样器 -> 采样器标为作者未提供",
        },
        "fixed_lora_recommendation": {
            "steps": "not_provided", "scheduler": "not_provided",
            "sampler": "not_provided", "shift_video": "not_provided",
            "shift_audio": "not_provided", "base_model": "not_provided",
            "source_kind": "not_provided", "source": "", "note": "",
        },
    },
}


class TestC6DetailRecommend(unittest.TestCase):
    """c6：详情模态框内必须有推荐参数分组。"""

    def _html(self, job=None):
        payload = job if job is not None else _JOB
        return _run_node("""
            const job = JSON.parse(%s);
            const html = detailRecommendHtml(job);
            console.log(JSON.stringify({html: html, len: html.length}));
        """ % _js_str(payload))

    # ---- 1. 分组必须出现，且含 c6 要求的字段 ----

    def test_recommend_group_is_rendered(self):
        out = self._html()
        self.assertGreater(out["len"], 0, "详情区必须渲染出推荐参数分组")

    def test_group_contains_all_c6_required_fields(self):
        out = self._html()
        html = out["html"]
        for label in ("推荐步数", "推荐调度器", "推荐采样器",
                      "推荐视频 shift", "推荐音频 shift", "推荐主模型"):
            self.assertIn(label, html, f"推荐参数分组缺少 {label}")

    def test_provided_values_are_rendered(self):
        out = self._html()
        html = out["html"]
        # 加速 LoRA 这份是「作者提供」：步数 4 / 调度器 simple / shift 12 / shift 3
        self.assertIn("推荐步数：4", html)
        self.assertIn("推荐调度器：simple", html)
        self.assertIn("推荐视频 shift：12", html)
        self.assertIn("推荐音频 shift：3", html)
        self.assertIn("MiniMax-H3（bf16 / int8_convrot / pruned_int8）", html)

    # ---- 2. 作者未提供：中文显式标注，绝不透出 not_provided ----

    def test_missing_items_show_chinese_author_not_provided(self):
        out = self._html()
        html = out["html"]
        self.assertIn("作者未提供", html, "缺失项必须显式标注「作者未提供」")

    def test_never_leaks_raw_not_provided_sentinel(self):
        out = self._html()
        self.assertNotIn(
            "not_provided", out["html"],
            "服务端的原始哨兵值 not_provided 不能显示给用户"
            "（含来源类型那一行 —— recSource 在标签表没就位时会回退成哨兵值）")

    def test_source_kind_label_missing_still_shows_chinese(self):
        """标签表没就位时（/api/video-options 还没回来），来源类型也必须是中文。"""
        out = _run_node("""
            global.JOB_META_SOURCE_KIND_LABEL = {};
            const job = JSON.parse(%s);
            const html = detailRecommendHtml(job);
            console.log(JSON.stringify({html: html, len: html.length}));
        """ % _js_str(_JOB))
        self.assertNotIn("not_provided", out["html"],
                         "标签表未就位时来源类型也不能漏出哨兵值")
        self.assertIn("来源类型：作者未提供", out["html"])

    def test_missing_items_get_is_missing_class(self):
        out = self._html()
        self.assertIn("job-detail-recchip is-missing", out["html"],
                      "未提供的项必须带 is-missing 样式类")

    # ---- 3. 来源可追溯 ----

    def test_source_is_rendered(self):
        out = self._html()
        html = out["html"]
        self.assertIn("job-detail-source", html, "推荐参数必须带来源说明")
        self.assertIn("来源类型：", html)
        self.assertIn("README.md:76-88", html, "必须能追溯到具体出处")

    # ---- 4. 没有推荐数据就不渲染空分组 ----

    def test_no_recommend_data_renders_nothing(self):
        job = {"id": "x", "mode": "t2v", "model_info": {}}
        out = self._html(job)
        self.assertEqual(out["len"], 0, "没有推荐数据时不得渲染空分组")

    def test_missing_job_meta_renders_nothing(self):
        out = _run_node("""
            global.JOB_META = undefined;
            const html = detailRecommendHtml({model_info: {}});
            console.log(JSON.stringify({html: html, len: html.length}));
        """)
        self.assertEqual(out["len"], 0, "JOB_META 不可用时不得抛错")


def _json_dumps(obj) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False)


def _js_str(obj) -> str:
    """把 Python 对象变成一段可安全嵌进 JS 的 JSON 字符串字面量。"""
    import json
    return json.dumps(json.dumps(obj, ensure_ascii=False), ensure_ascii=False)


class TestC6Wiring(unittest.TestCase):
    """接线与副作用约束（静态断言 —— 这些是结构性的，字符串断言足够）。"""

    def test_open_detail_modal_calls_the_helper(self):
        body = _extract_function(_APP_JS, "openDetailModal")
        self.assertIn("detailRecommendHtml(job)", body,
                      "openDetailModal 必须调用 detailRecommendHtml")

    def test_rec_block_is_sibling_section_not_inside_detail_params(self):
        body = _extract_function(_APP_JS, "openDetailModal")
        # 推荐参数区块必须插到 .detail-section 之后（兄弟节点），
        # 不能塞进 #detailParams（那是 .detail-kv 两列网格）
        self.assertNotIn('paramsBox.innerHTML = rows.join("") + recHtml', body,
                         "推荐参数不能塞进 #detailParams（.detail-kv 是两列网格）")
        self.assertIn("insertAdjacentElement", body,
                      "推荐参数必须作为兄弟 section 插入")
        self.assertIn("data-detail-rec", body,
                      "推荐参数区块必须带 data-detail-rec 标记（便于清理与断言）")

    def test_helper_uses_public_job_meta_api(self):
        body = _extract_function(_APP_JS, "detailRecommendHtml")
        self.assertIn("meta.recItems", body, "必须走 window.JOB_META.recItems")
        self.assertIn("meta.recSource", body, "必须走 window.JOB_META.recSource")
        self.assertNotIn("JOB_DETAIL", body,
                         "不得依赖 job-detail.js 的 IIFE 私有实现")

    def test_stale_block_is_removed_before_render(self):
        body = _extract_function(_APP_JS, "openDetailModal")
        self.assertIn("stale", body,
                      "重开别的任务时必须先清掉上一次的推荐参数区块")

    def test_server_side_untouched_still_ships_recommendations(self):
        """服务端**不用改**：它本来就在 model_info 里下发三份推荐数据。"""
        for key in ("model_recommendation", "lora_recommendation",
                    "fixed_lora_recommendation"):
            self.assertIn(key, _JOB_META_PY,
                          f"服务端应继续下发 {key}")

    def test_cache_busting_version_bumped(self):
        m = re.search(r'app\.js\?v=([A-Za-z0-9._-]+)', _INDEX_HTML)
        self.assertIsNotNone(m, "index.html 里应有 app.js?v= 版本号")
        assert m is not None          # 给 Pyright 收窄类型
        self.assertNotEqual(m.group(1), "h3image1",
                            "app.js 改了内容必须升 ?v= 版本号破缓存")

    def test_only_one_modal_is_opened_by_detail_button(self):
        """点「详情」只能弹 #detailModal，#jobDetailModal 必须保持 hidden。"""
        body = _extract_function(_APP_JS, "openDetailModal")
        self.assertIn('modal.hidden = false', body)
        # 只看**代码**（剔除注释）：注释里可以解释为什么不弹另一套
        code = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
        code = re.sub(r"//[^\n]*", "", code)
        self.assertNotIn("jobDetailModal", code,
                         "openDetailModal 的代码不得碰 #jobDetailModal")
        self.assertIn('"#detailModal"', code,
                      "openDetailModal 必须操作 #detailModal")


if __name__ == "__main__":
    unittest.main()
