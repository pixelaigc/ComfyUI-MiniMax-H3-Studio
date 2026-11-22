# -*- coding: utf-8 -*-
"""A/B/C/D 四项的定向测试（对齐 nonebot 咏唱h3 的媒体上下文一致性）。

只测本次改动直接相关的行为，不碰全量套件。
- A: build_query() 的「素材位置」块含绝对路径
- B: 音频 librosa 数值特征
- C: Agent 路径传 asset_refs
- D: 视频回传整段 mp4 路径
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from h3studio import enhance_assets as ea  # noqa: E402
from h3studio import hermes_enhance as he  # noqa: E402


# ==================================================================== A 项


def test_a_media_paths_block_contains_absolute_paths():
    """A: 素材路径块逐条写出绝对路径（nonebot 4346-4361 同款格式）。"""
    spec = he.get_media_spec("video")
    q = he.build_query(
        spec, "镜头扫过桌面",
        media_paths={
            "images": ["/abs/ref1.png", "/abs/ref2.png"],
            "videos": ["/abs/clip.mp4"],
            "audios": ["/abs/bgm.mp3"],
        })
    assert "# 素材位置" in q
    assert "供 hermes 用 vision_analyze / ffprobe 读" in q
    assert "  - 参考图1: /abs/ref1.png" in q
    assert "  - 参考图2: /abs/ref2.png" in q
    assert "  - 参考视频1: /abs/clip.mp4" in q
    assert "  - 参考音频1: /abs/bgm.mp3" in q


def test_a_no_media_paths_no_block():
    """A: 没有媒体时**不出现**素材位置块（无素材面板提示词逐字不变）。"""
    spec = he.get_media_spec("image")
    q = he.build_query(spec, "一只猫")
    assert "# 素材位置" not in q
    assert "参考图1:" not in q


def test_a_never_base64_in_query():
    """安全口径：query 里**绝不**出现 base64 / data: 二进制。"""
    spec = he.get_media_spec("image")
    q = he.build_query(
        spec, "猫",
        media_paths={"images": ["/abs/a.png"], "videos": [], "audios": []})
    assert "base64" not in q
    assert "data:image" not in q


def test_a_synth_stage_keeps_forbid_read_image():
    """A + 合成阶段：路径在，但**禁止读图**指令也在（nonebot 4370-4377）。"""
    spec = he.get_media_spec("video")
    q = he.build_query(
        spec, "镜头",
        media_paths={"images": ["/abs/a.png"], "videos": [], "audios": []},
        enhancement_section="【已核实素材事实】\n- 立方体\n",
        draft_prompt="<初稿>")
    assert "  - 参考图1: /abs/a.png" in q
    assert "不要用 vision_analyze 读参考图" in q


def test_a_draft_stage_no_forbid_read_image():
    """初稿阶段**不**禁止读图 —— 阶段1 要让 hermes 自己去 vision_analyze。"""
    spec = he.get_media_spec("video")
    q = he.build_query(
        spec, "镜头",
        media_paths={"images": ["/abs/a.png"], "videos": [], "audios": []})
    assert "  - 参考图1: /abs/a.png" in q
    assert "不要用 vision_analyze 读参考图" not in q


def test_a_forbid_only_when_images_present():
    """nonebot 的判定是「合成阶段 + 有参考图」—— 没参考图就不禁。"""
    spec = he.get_media_spec("video")
    q = he.build_query(
        spec, "镜头",
        media_paths={"images": [], "videos": ["/abs/v.mp4"], "audios": []},
        enhancement_section="【已核实素材事实】\n",
        draft_prompt="<初稿>")
    assert "不要用 vision_analyze 读参考图" not in q


# ==================================================================== D 项


def test_d_collect_media_paths_returns_video_original():
    """D: collect_media_paths 返回**整段视频**原文件路径，不是抽帧。"""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        vid = td / "clip.mp4"
        vid.write_bytes(b"fake-mp4")
        img = td / "ref.png"
        img.write_bytes(b"fake-png")
        aud = td / "bgm.mp3"
        aud.write_bytes(b"fake-mp3")
        resolved = {"items": [
            {"kind": "image", "path": str(img)},
            {"kind": "video", "path": str(vid)},
            {"kind": "audio", "path": str(aud)},
        ]}
        out = ea.collect_media_paths(resolved)
        assert out["images"] == [str(img)]
        assert out["videos"] == [str(vid)]
        assert out["audios"] == [str(aud)]


def test_d_collect_media_paths_skips_missing_files():
    """存在性校验：文件不存在 / 相对路径 -> **不写进**媒体路径。"""
    resolved = {"items": [
        {"kind": "image", "path": "/nonexistent/xxx.png"},
        {"kind": "video", "path": "relative/clip.mp4"},
        {"kind": "audio", "path": ""},
    ]}
    out = ea.collect_media_paths(resolved)
    assert out == {"images": [], "videos": [], "audios": []}


def test_d_collect_media_paths_empty():
    assert ea.collect_media_paths({}) == {
        "images": [], "videos": [], "audios": []}
    assert ea.collect_media_paths({"items": []}) == {
        "images": [], "videos": [], "audios": []}


# ==================================================================== B 项


def test_b_audio_numeric_missing_file_degrades():
    """B: 音频文件不存在 -> 降级，**不抛异常**。"""
    out = ea.analyze_audio_numeric(Path("/nonexistent/a.wav"))
    assert out["ok"] is False
    assert "不存在" in out["reason"]


def test_b_audio_numeric_empty_file_degrades():
    """B: 空文件 -> 降级。"""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        path = Path(f.name)
    try:
        out = ea.analyze_audio_numeric(path)
        assert out["ok"] is False
        assert "为空" in out["reason"]
    finally:
        os.unlink(path)


def test_b_audio_numeric_real_analysis():
    """B: 真实音频 -> librosa 数值特征（120 BPM 的合成样本）。"""
    np = pytest.importorskip("numpy")
    sf = pytest.importorskip("soundfile")
    sr = 22050
    dur = 8.0
    t = np.linspace(0, dur, int(sr * dur), endpoint=False)
    sig = np.zeros_like(t)
    beat = 0.5  # 120 BPM
    for i in range(int(dur / beat)):
        start = int(i * beat * sr)
        ln = int(0.05 * sr)
        if start + ln < len(sig):
            env = np.exp(-np.linspace(0, 6, ln))
            sig[start:start + ln] += 0.5 * env * np.sin(
                2 * np.pi * 1800 * t[start:start + ln])
    for f, amp in ((110.0, 0.2), (130.81, 0.15), (164.81, 0.12)):
        sig += amp * np.sin(2 * np.pi * f * t)
    sig = np.clip(sig, -0.99, 0.99)

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "a.wav"
        sf.write(path, sig, sr)
        out = ea.analyze_audio_numeric(path)
        assert out["ok"] is True, out.get("reason")
        feats = out["features"]
        # BPM 应该在 120 附近（±30 容差）
        assert 90 <= feats["tempo"]["bpm"] <= 150
        # 调式必须被检出
        assert feats["key"]["pitch_class"]
        # 段落必须被切出来
        assert feats["structure"]["segment_count"] >= 1
        # 中文摘要 + JSON 块都要有
        assert "节奏" in out["summary_zh"]
        assert "音频数值化分析结果" in out["block"]
        assert "```json" in out["block"]


def test_b_describe_includes_audio_numeric():
    """B: describe() 把数值特征与降级明细写进给模型的文字上下文。"""
    resolved = {
        "notes": ["<Audio 1> = 参考音频「x」"],
        "text_context": "【<Audio 1> 的音频内容】原始描述",
        "audio_numeric_context": "【<Audio 1> 的音频数值特征】\n- 节奏: 117 BPM",
        "audio_numeric_degraded": [],
        "skipped": [],
        "items": [],
    }
    out = ea.describe(resolved)
    assert "原始描述" in out
    assert "117 BPM" in out


def test_b_describe_reports_degraded():
    """B: 数值分析失败必须**如实告知**（不能静默）。"""
    resolved = {
        "notes": [],
        "text_context": "",
        "audio_numeric_context": "",
        "audio_numeric_degraded": [
            {"asset_id": "a1", "name": "坏音频.wav", "reason": "解码失败"}],
        "skipped": [],
        "items": [],
    }
    out = ea.describe(resolved)
    assert "数值化分析未成功" in out
    assert "坏音频.wav" in out
    assert "解码失败" in out


# ==================================================================== C 项


def test_c_enhance_for_mode_accepts_asset_refs():
    """C: enhance_for_mode 接受 asset_refs（Agent 路径传素材）。"""
    import inspect
    from h3studio import agent_enhance
    sig = inspect.signature(agent_enhance.enhance_for_mode)
    assert "asset_refs" in sig.parameters


def test_c_try_hermes_enhance_accepts_asset_refs():
    """C: _try_hermes_enhance 接受 asset_refs。"""
    import inspect
    from h3studio import agent_enhance
    sig = inspect.signature(agent_enhance._try_hermes_enhance)
    assert "asset_refs" in sig.parameters


def test_c_agent_passes_asset_refs_to_enhance():
    """C: agent.py 的建任务路径把 asset_refs 传给增强管线。"""
    src = (ROOT / "h3studio" / "agent.py").read_text(encoding="utf-8")
    assert "asset_refs=asset_refs" in src


# ==================================================================== 端到端接线


def test_server_passes_media_paths_both_endpoints():
    """A/D: server 两个入口都要把 media_paths 传给 enhance()。"""
    src = (ROOT / "server.py").read_text(encoding="utf-8")
    assert src.count("media_paths=media_paths") >= 2
    assert src.count("media_paths=asset_media_paths") >= 1


def test_enhance_signature_has_media_paths():
    """A/D: enhance() 接受 media_paths。"""
    import inspect
    sig = inspect.signature(he.enhance)
    assert "media_paths" in sig.parameters
    assert "scan_images" in sig.parameters


def test_build_query_signature_has_media_paths():
    import inspect
    sig = inspect.signature(he.build_query)
    assert "media_paths" in sig.parameters
