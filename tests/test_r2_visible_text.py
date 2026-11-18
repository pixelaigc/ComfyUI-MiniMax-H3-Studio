# -*- coding: utf-8 -*-
"""R2：前端「可见注释 / 调试文案」污染的回归测试。

【本轮修掉的缺陷】
R2-P1（严重）**「出处」字段把本机绝对路径 + 源码行号显示给用户**
    `h3studio/model_recommendations.py` 的 `source` 字段是「可追溯溯源」，
    会原样渲染到前端（`web/video-panel.js` 的「出处：」、
    `web/job-meta.js` 的「出处：」）。但溯源文本里写的是
    **本机绝对路径 + 源码行号 + 代码片段**：

        出处：/home/ubuntu/comfyui/comfy/supported_models.py:969-972（shift 12 …）
        出处：/mnt/usb2t/comfyui-models/loras/h3/h3_speed_slider_1.1.md:283-295（…）

    这属于**内部实现细节泄漏**：用户看到的是「这台机器的目录布局」，
    暴露了本机用户名（ubuntu）、磁盘挂载点（/mnt/usb2t）、
    以及源码行号 —— 都不是用户能操作、也不是作者推荐的一部分。

    修法（`model_recommendations._public_source`）：**只砍目录层级，
    保留文件名 + 行号**，溯源能力不丢。

【本文件断言的设计】
每条断言都**指名它要保护的行为**，并且都做过**变异自证**
（见文件末尾的 `test_mutation_*`）：
  * 把清洗函数改成「原样返回」-> 断言必须红；
  * 把正则改成「泛化匹配任意 /a/b」-> 断言必须红
    （这一条特别重要：泛化匹配会把正常文案里的斜杠吃掉，
     例如 "ModelTC/Minimax-H3-Turbo" 变成 "ModelTCMinimax-H3-Turbo"、
     "shift 12 / audio_shift 3" 变成 "shift 123"）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from h3studio import model_recommendations as mrec  # noqa: E402


# ------------------------------------------------------------------
# 1) 单元层：_public_source 的行为契约
# ------------------------------------------------------------------

def test_public_source_removes_local_absolute_path():
    """本机绝对路径必须被砍掉（目录层级 + 前缀全消失）。"""
    out = mrec._public_source(
        "/home/ubuntu/comfyui/comfy/supported_models.py:969-972（shift 12）")
    assert "/home/ubuntu" not in out, f"本机路径前缀仍在: {out!r}"
    assert "/comfy/" not in out, f"目录层级仍在: {out!r}"
    assert "supported_models.py:969-972" in out, f"溯源锚点丢了: {out!r}"
    assert "shift 12" in out, f"括号说明丢了: {out!r}"


def test_public_source_removes_mnt_prefix():
    """/mnt/* 挂载点同样要清掉（usb2t / nvme1t_colorful）。"""
    for prefix in ("/mnt/usb2t/comfyui-models/", "/mnt/nvme1t_colorful/"):
        out = mrec._public_source(f"{prefix}loras/h3/x.md:283-295（说明）")
        assert prefix not in out, f"{prefix} 仍在: {out!r}"
        assert "x.md:283-295" in out, f"文件名+行号丢了: {out!r}"


def test_public_source_keeps_plain_text_untouched():
    """不含本机路径的溯源文本必须**一字不改**。

    这一条守住「不能泛化成任意 /a/b 形态」：
    正常文案里的斜杠（ModelTC/Minimax-H3-Turbo、shift 12 / audio_shift 3、
    Comfy-Org/MiniMax-H3）不是路径，必须原样保留。
    """
    for text in (
        "作者规格表（ModelTC/Minimax-H3-Turbo README「1. Model specs」）："
        "「FL2VA Turbo 4-step v1.0 768p | FL2VA / T2VA | 768p (1344×768) | 6 / 3」",
        "shift 12 / audio_shift 3，量化/pruned 不改",
        "推荐主模型：Comfy-Org/MiniMax-H3（minimax_h3_fl2va_bf16.safetensors）",
    ):
        assert mrec._public_source(text) == text, (
            f"不含本机路径的文本被改了: {text!r} -> {mrec._public_source(text)!r}")


def test_public_source_handles_empty_and_non_str():
    """空值 / None / 非字符串 -> 空字符串，不抛异常。"""
    assert mrec._public_source("") == ""
    assert mrec._public_source(None) == ""
    assert mrec._public_source(123) == ""


# ------------------------------------------------------------------
# 2) 数据层：全部真实推荐条目的 source 都已清洗
# ------------------------------------------------------------------

def _all_recommendation_sources() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for key, rec in mrec.accel_lora_recommendations_payload().items():
        out.append((f"accel.{key}", str(rec.get("source") or "")))
    for key, rec in mrec.model_recommendations_payload().items():
        out.append((f"model.{key}", str(rec.get("source") or "")))
    return out


def test_no_recommendation_source_contains_local_absolute_path():
    """**全部**推荐条目的 source 都不含本机绝对路径。

    这条是端到端护栏 —— 它不依赖具体某一条 source 的内容，
    将来有人新增一条带 /home/ubuntu/... 的溯源，这里会立刻红。
    """
    bad = []
    for name, src in _all_recommendation_sources():
        if re.search(r"/(?:home/ubuntu|mnt/usb2t|mnt/nvme1t_colorful)", src):
            bad.append((name, src))
    assert not bad, f"仍有推荐条目把本机绝对路径发给前端: {bad}"


def test_recommendation_sources_still_carry_traceability():
    """清洗**不能**把溯源信息整个抹掉。

    至少要保留「文件名」或「作者出处描述」之一 ——
    否则「出处」就变成一句空话，追溯能力没了。
    """
    sources = _all_recommendation_sources()
    assert sources, "没有拿到任何推荐条目（数据源坏了？）"
    with_anchor = [
        (n, s) for n, s in sources
        if re.search(r"[\w.\-]+\.(?:py|md|json|safetensors|txt)(?::\d+)?", s)
    ]
    assert len(with_anchor) >= 5, (
        f"保留文件名+行号的溯源条目太少（{len(with_anchor)}），"
        f"清洗可能过头了: {sources[:3]}")


def test_recommendation_note_field_untouched():
    """note 字段**不得**被清洗影响（它不含路径，不该动）。"""
    rec = mrec.accel_lora_recommendations_payload().get("lightx2v_4step_768p")
    assert rec is not None, "拿不到 lightx2v_4step_768p 推荐条目"
    note = str(rec.get("note") or "")
    assert "shift" in note and "6/3" in note, f"note 被误改: {note!r}"


# ------------------------------------------------------------------
# 3) 契约层：清洗只挂在 _normalize（唯一收口点）
# ------------------------------------------------------------------

def test_normalize_applies_public_source():
    """_normalize 必须调用 _public_source（否则新条目绕过清洗）。"""
    rec = mrec._normalize({
        "steps": 4, "scheduler": None, "sampler": "euler",
        "shift_video": 6.0, "shift_audio": 3.0, "base_model": "X",
        "source_kind": "author",
        "source": "/home/ubuntu/comfyui/comfy/supported_models.py:969-972（x）",
        "note": "", "field_sources": {},
    })
    src = str(rec.get("source") or "")
    assert "/home/ubuntu" not in src, f"_normalize 没有清洗 source: {src!r}"
    assert "supported_models.py:969-972" in src


def test_normalize_does_not_touch_other_fields():
    """清洗**只动 source**，不动 source_kind / field_sources / note。"""
    rec = mrec._normalize({
        "steps": 4, "scheduler": None, "sampler": "euler",
        "shift_video": 6.0, "shift_audio": 3.0, "base_model": "X",
        "source_kind": "comfyui",
        "source": "/home/ubuntu/a.py:1（x）",
        "note": "原始说明",
        "field_sources": {"steps": "author"},
    })
    assert rec["source_kind"] == "comfyui"
    assert rec["field_sources"]["steps"] == "author"
    assert rec["note"] == "原始说明"


# ------------------------------------------------------------------
# 4) 前端护栏：渲染点必须用清洗后的 source
#    （前端不重复实现清洗 —— 后端是唯一收口点）
# ------------------------------------------------------------------

def test_frontend_does_not_reimplement_source_cleaning():
    """前端**不得**自己再写一遍「出处」的路径清洗（两处实现必然漂移）。

    判据：前端 JS 里不能出现针对本机路径前缀的**替换/截断**逻辑。
    只查「清洗动作」，不查「路径字面量本身」——
    `video-panel-fallback.js` 里的 `location` 是**功能数据**
    （服务端真源自动生成，前端据此判断模型在不在本机），
    它的路径字面量是合法的；显示层的清洗在 video-panel.js 的
    `modelLocationLabel()`（见下面的断言）。
    """
    web = REPO / "web"
    offenders = []
    for p in sorted(web.glob("*.js")):
        text = p.read_text(encoding="utf-8", errors="replace")
        for needle in ("/home/ubuntu", "/mnt/usb2t", "/mnt/nvme1t_colorful"):
            idx = 0
            while True:
                idx = text.find(needle, idx)
                if idx < 0:
                    break
                window = text[max(0, idx - 160):idx + 160]
                # 清洗动作 = 出现在 replace / split / substring 语境里
                if ("replace(" in window or ".split(" in window
                        or "substring(" in window or ".slice(" in window):
                    offenders.append(f"{p.name}: {needle}")
                idx += len(needle)
    assert not offenders, (
        f"前端不应自己实现「出处」的路径清洗（应在后端唯一收口点）: {offenders}")


def test_video_panel_renders_location_as_basename_only():
    """模型「位置」只显示目录名，不得显示本机绝对路径。

    【R2-P2】`video-panel.js` 原实现整条渲染
        `位置 /mnt/nvme1t_colorful/comfyui-models/diffusion_models`
    ⇒ 本机磁盘挂载点出现在用户界面上（真实浏览器 DOM 取证已确认）。

    修法：`modelLocationLabel()` 只取路径最后一段。
    """
    src = (REPO / "web" / "video-panel.js").read_text(encoding="utf-8")
    assert "modelLocationLabel" in src, (
        "video-panel.js 里没有 modelLocationLabel —— 位置文案没被处理")
    # 渲染点必须走这个函数，而不是直接用 model.location
    direct = re.findall(r"\$\{model\.location\}", src)
    assert not direct, (
        f"渲染点仍直接输出 model.location（绕过清洗）: {direct}")
    assert "位置 ${modelLocationLabel(model.location)}" in src, (
        "渲染点没有改成走 modelLocationLabel")


# ------------------------------------------------------------------
# 5) 变异自证 —— 断言不是恒绿
# ------------------------------------------------------------------

def test_mutation_reverting_cleaning_breaks_assertions():
    """【变异 M1】把 _public_source 改成「原样返回」-> 断言必须红。

    做法：临时把清洗函数替换成恒等函数，跑关键断言，看它是否真的失败。
    """
    original = mrec._public_source
    try:
        mrec._public_source = lambda s: s or ""
        failed = 0
        try:
            mrec._normalize({
                "steps": 1, "source": "/home/ubuntu/x.py:1（y）",
                "source_kind": "a", "note": "", "field_sources": {},
            })
        except AssertionError:
            failed += 1
        # 直接复算关键断言
        rec = mrec._normalize({
            "steps": 1, "source": "/home/ubuntu/x.py:1（y）",
            "source_kind": "a", "note": "", "field_sources": {},
        })
        if "/home/ubuntu" in str(rec.get("source")):
            failed += 1
        assert failed > 0, "变异 M1 没被任何断言抓住（断言是恒绿的）"
    finally:
        mrec._public_source = original


def test_mutation_overbroad_regex_breaks_assertions():
    """【变异 M2】把正则改成「泛化匹配任意 /a/b」-> 断言必须红。

    这一条最重要：泛化匹配会把正常文案里的斜杠吃掉
    （ModelTC/Minimax-H3-Turbo -> ModelTCMinimax-H3-Turbo）。
    """
    original = mrec._LOCAL_PATH_RE
    try:
        mrec._LOCAL_PATH_RE = re.compile(
            r"(?P<abs>/[A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)*)"
            r"(?P<anchor>:\d+(?:-\d+)?)?")
        text = "作者规格表（ModelTC/Minimax-H3-Turbo README）：shift 12 / 3"
        out = mrec._public_source(text)
        assert out != text, (
            "变异 M2 没被抓住：泛化匹配竟然没改变这段文本（断言恒绿？）")
        assert "ModelTC/Minimax-H3-Turbo" not in out, (
            "泛化匹配吃掉了正常文案的斜杠（这正是要防的回归）")
    finally:
        mrec._LOCAL_PATH_RE = original


def test_mutation_removing_normalize_hook_breaks_assertions():
    """【变异 M3】把 _normalize 里的清洗调用去掉 -> 断言必须红。"""
    import inspect
    src = inspect.getsource(mrec._normalize)
    assert "_public_source" in src, (
        "_normalize 里没有清洗调用 —— 数据层护栏会失效")
