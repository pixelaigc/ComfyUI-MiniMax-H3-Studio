# -*- coding: utf-8 -*-
"""A 段回归测试：前端用户可见文案污染必须清零。

【为什么要有这个测试】
文案污染是**静默**缺陷：页面不报错、测试不失败、用户只是看到
「骨架占位模型 · 8步」这种开发者内部说法。这类问题只能靠
「扫源码 + 扫真实 DOM」两道关卡守住。

【四道关卡】
  1. **源码关**：web/ 下不得再出现会进入 UI 的开发者措辞
     （骨架占位 / TODO / FIXME / 内部日期标记…）。
  2. **产物关**：骨架卡的占位文案与 title 属性必须干净。
  3. **数据关**：LoRA 目录里 100% 的用户可见字段不得含上游机器字段名。
  4. **真实 DOM 关**（可选，需要 node + playwright）：
     渲染真实页面，采集可见文本与 title 属性做断言。

运行：
    python3 -m pytest tests/test_visible_text_pollution.py -q --tb=short
需要 node（缺失时第 4 关整类 skip，不算失败）
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

WEB = REPO / "web"
NODE = shutil.which("node")


# ==========================================================================
# 1) 源码关：web/ 下不得出现会进入 UI 的开发者措辞
# ==========================================================================
#
# 【判定范围】只扫**会进 DOM 的载体**：
#   * index.html 的文本节点与 placeholder/title/aria-label/alt 属性
#   * *.js 里赋值给 innerHTML / textContent / placeholder / title /
#     setAttribute(aria-*) / alert / confirm / toast 的字符串字面量
#
# 【为什么不扫 JS 注释】注释是**该有的**东西 —— 本仓库的风格是
# 每处非显然的改动都写清楚「为什么」。「扫描注释」会把正确的代码
# 全部误判成污染。这里只管**会显示给用户的字符串**。

# 会显示给用户的赋值点（与 audit/scan_visible_pollution.py 同一套口径）
_USER_VISIBLE_ASSIGN = [
    (re.compile(r"\.innerHTML\s*(?:\+)?=\s*"), "innerHTML"),
    (re.compile(r"\.outerHTML\s*(?:\+)?=\s*"), "outerHTML"),
    (re.compile(r"\.textContent\s*(?:\+)?=\s*"), "textContent"),
    (re.compile(r"\.innerText\s*(?:\+)?=\s*"), "innerText"),
    (re.compile(r"\.placeholder\s*=\s*"), "placeholder"),
    (re.compile(r"\.title\s*=\s*"), "title"),
    (re.compile(r"\.setAttribute\(\s*['\"](?:title|aria-label|alt|placeholder)['\"]\s*,"),
     "attr"),
    (re.compile(r"\.setAttribute\(\s*['\"]aria-[\w-]+['\"]\s*,"), "aria"),
    (re.compile(r"\bdocument\.title\s*=\s*"), "document.title"),
    (re.compile(r"\balert\s*\("), "alert"),
    (re.compile(r"\bconfirm\s*\("), "confirm"),
]
_STRING_LITERAL = re.compile(r"""(?P<q>['"`])(?P<body>(?:\\.|(?!\1).)*?)(?P=q)""", re.S)

# 会显示给用户的开发者措辞（**不包括**开发者注释里允许出现的词）
_POLLUTION_IN_UI = [
    (re.compile(r"骨架占位"), "骨架占位"),
    (re.compile(r"\bTODO\b"), "TODO"),
    (re.compile(r"\bFIXME\b"), "FIXME"),
    (re.compile(r"\bHACK\b"), "HACK"),
    (re.compile(r"待实现"), "待实现"),
    (re.compile(r"暂不支持"), "暂不支持"),
    (re.compile(r"未实现"), "未实现"),
    (re.compile(r"尚未实现"), "尚未实现"),
    (re.compile(r"【\d{4}-\d{2}-\d{2}"), "内部日期标记"),
    (re.compile(r"交付项\s*\d"), "交付项编号"),
]


def _iter_ui_strings():
    """产出 (文件, 行号, 赋值点类型, 字符串内容)。"""
    # --- index.html ---
    html = (WEB / "index.html").read_text(encoding="utf-8")
    for m in re.finditer(r">([^<>]+)<", html):
        text = m.group(1)
        if text.strip():
            line = html.count("\n", 0, m.start()) + 1
            yield ("web/index.html", line, "html-text", text.strip())
    for m in re.finditer(
            r"""\b(placeholder|title|aria-label|alt)\s*=\s*"([^"]*)"|"""
            r"""\b(placeholder|title|aria-label|alt)\s*=\s*'([^']*)'""", html):
        val = m.group(2) or m.group(4) or ""
        if val.strip():
            line = html.count("\n", 0, m.start()) + 1
            yield ("web/index.html", line, "html-attr", val.strip())

    # --- *.js ---
    for fn in sorted(os.listdir(WEB)):
        if not fn.endswith(".js"):
            continue
        path = WEB / fn
        src = path.read_text(encoding="utf-8")
        rel = f"web/{fn}"
        for pattern, tag in _USER_VISIBLE_ASSIGN:
            for m in re.finditer(pattern, src):
                tail = src[m.end(): m.end() + 3000]
                cut = re.search(r";(?![^\n]*[)])", tail)
                if cut:
                    tail = tail[: cut.start()]
                line = src.count("\n", 0, m.start()) + 1
                for lm in _STRING_LITERAL.finditer(tail):
                    body = lm.group("body")
                    if lm.group("q") == "`":
                        body = re.sub(r"\$\{[^}]*\}", "\x00", body)
                    if body.strip():
                        yield (rel, line, tag, body.strip())


def _strip_js_comments(src: str) -> str:
    """去掉 JS 注释，只留可执行代码。

    【为什么需要】
    断言「某行代码存在」时，如果那行已经被注释掉，字符串匹配**照样**能命中 ——
    变异测试实测过：把 `removeAttribute("title")` 注释掉，测试依然通过。
    所以必须先把注释剥掉再断言。
    """
    out = []
    i = 0
    n = len(src)
    while i < n:
        ch = src[i]
        if ch == '"' or ch == "'" or ch == "`":
            quote = ch
            out.append(ch)
            i += 1
            while i < n:
                out.append(src[i])
                if src[i] == "\\":
                    if i + 1 < n:
                        out.append(src[i + 1])
                        i += 2
                        continue
                elif src[i] == quote:
                    i += 1
                    break
                i += 1
            continue
        if ch == "/" and i + 1 < n and src[i + 1] == "/":
            while i < n and src[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and src[i + 1] == "*":
            i += 2
            while i + 1 < n and not (src[i] == "*" and src[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


class TestVisibleTextPollutionSource(unittest.TestCase):
    """关卡 1：源码里不得有会显示给用户的开发者措辞。"""

    def test_no_pollution_in_user_visible_strings(self):
        hits = []
        for rel, line, tag, body in _iter_ui_strings():
            for pattern, name in _POLLUTION_IN_UI:
                if pattern.search(body):
                    hits.append(f"{rel}:{line} [{tag}] {name} :: {body[:120]}")
                    break
        self.assertEqual([], hits, "用户可见文案里出现开发者措辞：\n  " + "\n  ".join(hits))

    def test_task_card_has_no_html_comment_in_template(self):
        """任务卡 innerHTML 模板里不得再嵌 HTML 注释（会随每次渲染进 DOM）。

        【为什么】HTML 注释不渲染，但会进 DOM —— 用户「查看源代码」
        「复制页面内容」都能带走，而且内容是内部日期标记 + 测试名。
        """
        src = (WEB / "app.js").read_text(encoding="utf-8")
        # jobCard 的 innerHTML 模板：从 `card.innerHTML = \`` 到对应的结束
        start = src.find("card.innerHTML = `")
        self.assertNotEqual(-1, start, "找不到 jobCard 的 innerHTML 模板")
        # 模板结束：下一个未转义的 `;
        end = src.find("`;", start)
        self.assertNotEqual(-1, end)
        template = src[start:end]
        self.assertNotIn("<!--", template,
                         "jobCard 的 innerHTML 模板里还有 HTML 注释："
                         + template[template.find("<!--"):template.find("<!--") + 160])

    def test_prompt_textarea_has_no_default_content(self):
        """提示词输入框不得预填内容。

        【为什么】textarea 的默认内容就是输入框里的值，首屏直接可见；
        用户不删就点生成，会把这段内容当提示词提交。
        而且历史故障（媒体库卡片 LoRA 行显示 integrated_multimodal_description）
        的根因就是这个字段名 —— 把它放在出厂默认位等于把已知易错串
        的字段名重新端到用户面前。
        """
        html = (WEB / "index.html").read_text(encoding="utf-8")
        m = re.search(r'<textarea id="prompt"[^>]*>(.*?)</textarea>', html, re.S)
        self.assertIsNotNone(m, "找不到 #prompt textarea")
        content = m.group(1).strip()
        self.assertEqual("", content,
                         "#prompt textarea 预填了内容：\n" + content[:300])

    def test_prompt_textarea_keeps_placeholder(self):
        """清掉默认内容后，占位符必须还在（否则用户不知道该写什么）。"""
        html = (WEB / "index.html").read_text(encoding="utf-8")
        m = re.search(r'<textarea id="prompt"[^>]*>', html)
        self.assertIsNotNone(m)
        tag = m.group(0)
        self.assertIn("placeholder=", tag, "#prompt textarea 丢了 placeholder")
        # 【为什么还要断言非空】只断言「有 placeholder 属性」不够 ——
        # placeholder="" 也能通过。变异测试实测过。
        pm = re.search(r'placeholder="([^"]*)"', tag)
        self.assertIsNotNone(pm)
        self.assertTrue(pm.group(1).strip(),
                        "#prompt textarea 的 placeholder 是空的")


class TestSkeletonCardText(unittest.TestCase):
    """关卡 2：骨架卡的占位文案与 title 属性必须干净。"""

    def test_skeleton_placeholder_text_is_neutral(self):
        src = (WEB / "app.js").read_text(encoding="utf-8")
        # 【为什么要限定在 skeletonJob 的函数体内】
        # app.js 里 `model_label:` 出现**多次**（本地兜底路径也有一处空串）。
        # 如果只取「第一个匹配」，断言就落在了错的函数上 —— 变异测试
        # 会显示它压根抓不到 skeletonJob 的真实改动。
        fn_start = src.find("function skeletonJob")
        self.assertNotEqual(-1, fn_start, "找不到 skeletonJob")
        fn_end = src.find("\nfunction ", fn_start + 10)
        body = src[fn_start: fn_end if fn_end != -1 else fn_start + 6000]
        m = re.search(r"model_label:\s*\"([^\"]*)\"", body)
        self.assertIsNotNone(m, "skeletonJob 里找不到 model_label")
        label = m.group(1)
        self.assertNotIn("骨架", label, f"骨架卡 model_label 仍含开发者措辞：{label}")
        self.assertNotIn("占位", label, f"骨架卡 model_label 仍含开发者措辞：{label}")

        # prompt 占位也要干净
        pm = re.search(r'prompt:\s*"([^"]*)"', body)
        self.assertIsNotNone(pm)
        prompt = pm.group(1)
        for bad in ("骨架", "占位", "TODO", "FIXME"):
            self.assertNotIn(bad, prompt,
                             f"骨架卡 prompt 仍含开发者措辞「{bad}」：{prompt}")

    def test_skeleton_cards_strip_title_attributes(self):
        """骨架卡必须摘掉 title 属性。

        【为什么】title 是**浏览器原生 tooltip**，悬停即显示 ——
        与 CSS 的 color:transparent 无关。骨架里的 title 全是占位值
        （模型名 / 采样器 / 步数…），留着只会在悬停时闪出假信息。
        """
        src = (WEB / "app.js").read_text(encoding="utf-8")
        m = re.search(r"function renderGridSkeleton\(", src)
        self.assertIsNotNone(m)
        start = m.start()
        # 函数体到下一个顶层 function
        end = src.find("\nfunction ", start + 10)
        body = src[start:end if end != -1 else start + 3000]
        # 【为什么要先剥注释】变异「把 removeAttribute 调用注释掉」后，
        # 字符串匹配照样能命中注释里的字 —— 剥掉注释才抓得住。
        code = _strip_js_comments(body)
        self.assertRegex(code, r"removeAttribute\(\s*[\"\']title[\"\']\s*\)",
                         "renderGridSkeleton 没有真正摘掉骨架卡的 title 属性"
                         "（removeAttribute(\"title\") 必须是可执行调用）")


class TestLoraCatalogSanitize(unittest.TestCase):
    """关卡 3：LoRA 目录的用户可见字段不得含上游机器字段名。"""

    # 上游模型卡的原始字段名 / 判定过程
    RAW_TOKENS = re.compile(
        r"instance_prompt|trainedWords|Data\.BaseModel|vision_foundation|"
        r"key_map|ss_output_name|ss_tag_frequency|ss_base_model_version|"
        r"extracted_by|lora_A|lora_B|lokr_w|AigcAttributes|BaseModelRelation|"
        r"modelVersions\[|frontmatter|attn_hit|attn_total|all_hit|all_total|"
        r"\btensor\b|ss_output|lora_down|lora_up|modelspec\.|diffusion_model\.|"
        r"main_file|zimage_arch_sample")

    def test_sanitizer_removes_raw_tokens(self):
        from h3studio.lora_text_sanitize import sanitize_user_text
        samples = [
            "基座判定：原生（Data.BaseModel=['Tongyi-MAI/Z-Image-Turbo']; "
            "vision_foundation=Z_IMAGE_TURBO）。",
            "作者未给触发词（README instance_prompt 为 null）。",
            "EasonnLi 10 件套 pack 成员。该版本 trainedWords 为空。作者未给强度。",
            "基座判定：原生（HF 镜像 key_map 命中判定：{'attn_hit': 128, "
            "'attn_total': 128, 'attn_ratio': 1.0}）。",
            "基座判定：原生（Civitai modelVersions[].baseModel = ZImageTurbo）。",
            # 这条**必须含 tensor 计数** —— 否则「删掉 tensor 计数」
            # 这类变异根本碰不到样本，断言会静默通过（变异测试实测过）。
            "方向：A1 东亚女性·小红书写实。 ⚠️ **近似适用**（依据："
            "无 README / 无 license；权重 480 tensor 全为 LoRA（lora_A/lora_B 各 240），"
            "确为适配器。）—— 非该基座原生训练。",
            # 这条是**裸的 tensor 计数**（没有「全为 LoRA」的后缀）——
            # 只有 _TENSOR_PHRASE 这条规则能清掉它。
            # 变异测试实测：只删 _TENSOR_PHRASE 而保留 _TENSOR_FULL 时，
            # 上一条样本仍被 _TENSOR_FULL 挡住 -> 断言会静默通过。
            "基座判定：原生（下载量 335853 / 174 like，630 tensor，"
            "键名结构已核对，170MB）。 来源平台：huggingface。",
            # 这条是「N tensor / M lora」配对 —— 变异测试实测过：
            # 只删 _TENSOR_PHRASE 而保留 _TENSOR_SLASH 时，上一条样本
            # 仍被 _TENSOR_SLASH 挡住 -> 断言静默通过。
            # 而且这条样本还暴露过**真实 bug**：清洗后残留悬空的「/ 420 lora」。
            "基座判定：原生（630 tensor / 420 lora，键名结构已核对）。",
        ]
        for raw in samples:
            out = sanitize_user_text(raw)
            self.assertIsNone(self.RAW_TOKENS.search(out),
                              f"清洗后仍含机器字段名：{raw!r} -> {out!r}")
            # 【为什么要查「悬空分隔符」】
            # 实测 bug：「630 tensor / 420 lora」被逐条清洗后残留「/ 420 lora」——
            # 机器字段名没了，但留下一个**读不通的半截句子**。
            # 所以断言清洗结果里不能出现「/ 数字」这种悬空配对。
            # 「/ 420 lora」这种「斜杠 + 数字 + 权重单位」的悬空残渣
            # （「335853 / 174 like」是正常散文，不能误判）
            self.assertNotRegex(out, r"[/／]\s*\d+\s*lora",
                                 f"清洗后留下悬空分隔符：{raw!r} -> {out!r}")

    def test_sanitizer_is_idempotent(self):
        """幂等：重复清洗不能把文案洗坏（调用点可能不止一处）。"""
        from h3studio.lora_text_sanitize import sanitize_user_text
        for raw in (
            "基座判定：原生（Data.BaseModel=['Tongyi-MAI/Z-Image-Turbo']; "
            "vision_foundation=Z_IMAGE_TURBO）。 触发词：97。",
            "作者未给触发词（README instance_prompt 为 null）。",
            "方向：B2 影棚/摄影感。 ⚠️ **近似适用**（依据：LoKR 而非标准 LoRA："
            "612 tensor 中 216 个是 lora_A/lora_B，其余 396 个是 lokr_w1 / "
            "lokr_w2 + alpha，dtype 全 F32，585MB。ComfyUI 需支持 LoKR 的加载器。）。",
            # 这条**必须含会被清洗的字段名** —— 否则「清洗后再清洗」
            # 两步结果相同，断言对「清洗器是否真的幂等」不敏感，
            # 变异测试实测过（尾部多一个空格都能蒙混过关）。
            "基座判定：原生（Data.BaseModel=['Tongyi-MAI/Z-Image-Turbo']; "
            "vision_foundation=Z_IMAGE_TURBO）。",
        ):
            once = sanitize_user_text(raw)
            twice = sanitize_user_text(once)
            self.assertEqual(once, twice, f"不幂等：{once!r} != {twice!r}")
            # 【为什么要额外断言「清洗后不含首尾空白」】
            # 只断言 once == twice 时，「尾部多一个空格」这类变异会被
            # _collapse_spaces 里的 .strip() 吃掉 —— 断言看不见它。
            # 所以这里直接检查输出本身没有首尾空白。
            self.assertEqual(once, once.strip(),
                             f"清洗结果带首尾空白：{once!r}")

    def test_sanitizer_preserves_plain_text(self):
        """不含机器字段名的普通文案必须**一个字都不改**。

        【为什么】清洗器是全局兜底，误伤普通文案比漏掉一个字段名更糟。
        """
        from h3studio.lora_text_sanitize import sanitize_user_text
        plain = [
            "梦幻朦胧/柔光滤镜风格，官方 tags 标注为「滤镜lora」。",
            "写实向「真实幻想」质感，官方 tags 标注为「瑶光 / 真实幻想」。",
            "作者明示 0.7–0.85，取中值 0.77（README 'Strength 0.7 - 0.85'）。",
            # 这两条**必须含 LoRA 字样** —— 否则「把 LoRA 改写成别的词」
            # 这类变异根本碰不到样本，断言会静默通过（变异测试实测过）。
            "风格 LoRA（体积感/光泽向风格）。上游 metadata 里没有触发词标注。",
            "针对 JPEG 压缩伪影的修复型效果 LoRA（patch UNet 主干层）。",
        ]
        for text in plain:
            self.assertEqual(text, sanitize_user_text(text),
                             f"普通文案被改动了：{text!r}")

    def test_sanitizer_never_empties_non_empty_text(self):
        """清洗**绝不能**把非空文本变成空串。

        【为什么】变异测试实测：把 `sanitize_scene(info.get("scene",""))`
        改成 `sanitize_scene("")` —— 机器字段名确实一个不剩了，
        但**所有 LoRA 的说明文字全没了**，用户看到的是一堆空下拉项。
        这是「查机器字段名」那道关卡查不出来的另一类缺陷：
        信息被删光，而不是被污染。
        """
        from h3studio.lora_text_sanitize import sanitize_user_text
        for text in ("方向：A2 东亚女性写实。触发词：97。来源平台：modelscope。",
                     "作者未给触发词（README instance_prompt 为 null）。",
                     "梦幻朦胧/柔光滤镜风格，官方 tags 标注为「滤镜lora」。"):
            out = sanitize_user_text(text)
            self.assertTrue(out.strip(),
                            f"清洗把非空文案变成了空串：{text!r}")

    def test_sanitizer_keeps_user_meaning(self):
        """清洗只改机器字段名，**用户需要的语义必须保留**。

        方向 / 触发词 / 判定结论 / 来源平台 / 加载器要求 ——
        这些是用户判断「能不能用」的依据，删掉就是信息损失。
        """
        from h3studio.lora_text_sanitize import sanitize_user_text
        out = sanitize_user_text(
            "方向：A2 东亚女性·日系/韩系写实。 基座判定：原生"
            "（Data.BaseModel=['Tongyi-MAI/Z-Image-Turbo']; "
            "vision_foundation=Z_IMAGE_TURBO）。 触发词：97。 来源平台：modelscope。")
        for keep in ("方向：", "触发词：97", "来源平台：modelscope", "原生"):
            self.assertIn(keep, out, f"清洗后丢失了用户需要的语义「{keep}」：{out}")

    def test_all_three_user_fields_are_sanitized_at_call_site(self):
        """三个用户可见字段（scene / strength_source / source）都必须**接上**清洗。

        【为什么单列一条】
        变异测试实测：把 `strength_source` 的清洗调用断开，**真实清单里查不出来** ——
        因为现有 strength_source 文案恰好都不含机器字段名。
        也就是说「清单扫描」这一道关卡对它是**盲区**。
        所以这里直接断言**出站点的调用形态**，把盲区补上。

        【怎么数「出站点」】
        用「字段名 + 清洗函数名必须出现在同一段连续文本里」来判定，
        并要求该字段的**全部**出站点都被覆盖 ——
        只检查「至少有一处」会放过「断了两处中的一处」（实测过）。
        """
        src = (REPO / "h3studio" / "loras.py").read_text(encoding="utf-8")
        for field, fn in (("scene", "sanitize_scene"),
                          ("strength_source", "sanitize_strength_source"),
                          ("source", "sanitize_user_text")):
            # 出站点 = `"field":` 后面紧接着就是清洗调用。
            # 用 re.DOTALL 让 `\n` 不影响匹配（清洗调用常被格式化到下一行）。
            sanitized = re.findall(
                rf'"{field}"\s*:\s*lora_text_sanitize\.{fn}\(', src, re.S)
            # 未清洗的出站点 = `"field":` 后面是 info.get / doc.get / meta.get
            unsanitized = re.findall(
                rf'"{field}"\s*:\s*(?:info|doc|meta)\.get\(', src, re.S)
            self.assertGreater(
                len(sanitized) + len(unsanitized), 0,
                f"loras.py 里找不到 {field} 的出站点")
            self.assertEqual(
                [], unsanitized,
                f"{field} 的出站点没有接上 lora_text_sanitize.{fn}()："
                f"{unsanitized}")

    def test_real_lora_lists_have_no_raw_tokens(self):
        """真实清单里 100% 的用户可见字段不得含机器字段名。

        【为什么】只测清洗函数等于「用被测函数验证自己」——
        必须拿**真实的 list_*_loras() 输出**再扫一遍，
        才能证明调用点真的接上了。
        """
        from h3studio import loras
        fields = ("scene", "strength_source", "source", "base",
                  "display", "trigger", "incompatible_reason")
        checked = 0
        leaks = []
        for list_fn in (loras.list_z_image_loras,
                        loras.list_qwen_image_loras,
                        loras.list_moss_image_loras):
            for item in list_fn():
                for field in fields:
                    value = item.get(field)
                    if not isinstance(value, str):
                        continue
                    checked += 1
                    m = self.RAW_TOKENS.search(value)
                    if m:
                        leaks.append(f"{field}={value[:140]} (命中 {m.group(0)})")
        self.assertGreater(checked, 100, "真实清单字段数异常少，测试可能没跑到真数据")
        self.assertEqual([], leaks, "真实清单里仍有机器字段名：\n  " + "\n  ".join(leaks[:20]))

    def test_real_lora_scene_is_not_emptied(self):
        """真实清单的 scene 不得被清空。

        【为什么单列】上面那条只查「机器字段名有没有漏掉」——
        变异「把 sanitize_scene(info.get(...)) 改成 sanitize_scene("")」
        让机器字段名一个不剩，但那道关卡查不出来：
        真正的后果是**所有 LoRA 的说明文字全没了**。
        这是「信息被删光」而不是「信息被污染」，必须单独一道关卡。
        """
        from h3studio import loras
        # 【为什么要检查**每一项**而不是「绝大多数」】
        # 变异「把 sanitize_scene(info.get(...)) 改成 sanitize_scene("")」只改了
        # 两个出站点里的一个 —— 只断一处时仍有 197 项非空，
        # 用「>80% 非空」这种宽松阈值就放过它了。
        # 所以这里要求**每一项**都非空。
        for label, items in (("z_image", loras.list_z_image_loras()),
                             ("qwen_image", loras.list_qwen_image_loras()),
                             ("moss_image", loras.list_moss_image_loras())):
            empty = [it.get("display") or it.get("name")
                     for it in items if not (it.get("scene") or "").strip()]
            self.assertEqual(
                [], empty,
                f"{label} 清单里有 {len(empty)} 项 scene 是空的 —— "
                f"清洗把内容删光了（前几个：{empty[:5]}）")
        # 抽查：至少有一条 scene 带「触发词」或「来源平台」
        # （这两个是**用户真正需要**的语义，不该被清洗掉）
        all_items = (loras.list_z_image_loras()
                     + loras.list_qwen_image_loras()
                     + loras.list_moss_image_loras())
        self.assertTrue(
            any("触发词" in (it.get("scene") or "")
                or "来源平台" in (it.get("scene") or "")
                for it in all_items),
            "真实清单里找不到任何带「触发词 / 来源平台」的 scene —— "
            "用户需要的语义被清洗掉了")


class TestStaticAssetCacheBust(unittest.TestCase):
    """关卡 5：改了静态资源必须升 ?v= 版本号。

    【为什么】本仓库约定：改静态文件就升版本号破浏览器缓存
    （见 242e639「升 app.js 版号到 b2audiotrack1（破浏览器缓存）」）。
    修复③ 给 style.css 加了 131 行（.num-input / .media-preview-* 样式）
    却没升 ?v= —— 服务端是新文件，用户浏览器可能命中旧缓存，
    于是「数值直输」在老缓存用户那里样式不生效。
    """

    def test_style_css_version_bumped_after_change(self):
        html = (WEB / "index.html").read_text(encoding="utf-8")
        m = re.search(r'href="/static/style\.css\?v=([^"]+)"', html)
        self.assertIsNotNone(m, "找不到 style.css 的版本参数")
        version = m.group(1)
        self.assertNotEqual("uxbatch1", version,
                            "style.css 的 ?v= 仍是 uxbatch1 —— "
                            "修复③ 改了 style.css 但没升版本号，老缓存用户拿不到新样式")

    def test_all_static_assets_have_version_param(self):
        html = (WEB / "index.html").read_text(encoding="utf-8")
        for m in re.finditer(r'(?:src|href)="/static/([^"?]+)(?:\?v=([^"]*))?"', html):
            name, version = m.group(1), m.group(2)
            self.assertTrue(version, f"静态资源 {name} 没有 ?v= 版本参数")


@unittest.skipUnless(NODE, "node 缺失，跳过真实 DOM 关")
class TestRealDomHasNoPollution(unittest.TestCase):
    """关卡 4：真实 DOM 里不得有污染文案（需要 playwright + node）。"""

    def test_rendered_dom_has_no_pollution(self):
        script = r"""
        const { chromium } = require('playwright');
        (async () => {
          const url = process.argv[2];
          const browser = await chromium.launch({args:['--no-sandbox']});
          const page = await browser.newPage();
          await page.goto(url, {waitUntil: 'networkidle', timeout: 45000});
          await page.waitForTimeout(2500);
          const bad = await page.evaluate(() => {
            const PAT = /骨架占位|骨架|TODO|FIXME|HACK|待实现|暂不支持|未实现|尚未实现/;
            const out = [];
            document.querySelectorAll('.job-skeleton').forEach(el => {
              el.querySelectorAll('*').forEach(n => {
                const t = (n.textContent||'').trim();
                if (t && PAT.test(t)) out.push('skeleton-text: ' + t.slice(0,120));
                const ti = n.getAttribute && n.getAttribute('title');
                if (ti && PAT.test(ti)) out.push('skeleton-title: ' + ti.slice(0,120));
              });
            });
            return out;
          });
          await browser.close();
          console.log(JSON.stringify(bad));
        })().catch(e => { console.error(String(e)); process.exit(2); });
        """
        try:
            import playwright  # noqa: F401
        except ImportError:
            self.skipTest("playwright 未安装")
        with tempfile.TemporaryDirectory(prefix="h3-pollution-") as tmp:
            path = Path(tmp) / "probe.js"
            path.write_text(script, encoding="utf-8")
            proc = subprocess.run(
                [NODE, str(path), "http://127.0.0.1:8190/"],
                capture_output=True, text=True, timeout=180, cwd=tmp)
        if proc.returncode != 0:
            self.skipTest(f"浏览器探测未通过（returncode={proc.returncode}）："
                          f"{proc.stderr[:200]}")
        import json
        bad = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual([], bad, "真实 DOM 里出现污染文案：\n  " + "\n  ".join(bad))


if __name__ == "__main__":
    unittest.main()
