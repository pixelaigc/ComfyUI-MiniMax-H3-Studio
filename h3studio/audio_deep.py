# -*- coding: utf-8 -*-
"""
深度音频分析后端 —— 把《音频分析方案.md》实测完成的编排脚本接进素材库。

**不重写算法**：本模块只做三件事
  1. 以子进程方式调用 /root/projects/audio-analysis/analyze_audio.py，
     拿到它输出的固定结构 JSON；
  2. 把该 JSON 压成素材库「分析文本记录」能承载的形态（中文文本 + extra 附加数据）；
  3. 保证源文件字节不变（脚本本身只读原文件，本模块再做一次前后校验）。

运行环境（硬性）：
  必须用 ComfyUI 的 venv —— /home/ubuntu/comfyui/venv/bin/python3。
  系统 hermes venv 没有 torch / transformers / funasr / demucs。

为什么走子进程而不是 import：
  素材库跑在 Studio 自己的 Python 环境里，而深度分析依赖 ComfyUI venv 里的
  torch/transformers/funasr/demucs/essentia。跨 venv 无法 import。
  analyze_audio.py 本身已经把「每阶段独立 try/except、单阶段失败不阻塞其它」
  的容错做好了，输出 JSON 是契约 —— 所以这里只当消费者，不当算法实现者。

集成契约（与素材库其余部分一致）：
  * 结果写入 asset_analyses.text（与图片/视频的分析文本**同一处存储**）；
  * 结构化数据写入 asset_analyses.extra，供后续检索与二次使用；
  * 修订历史沿用 asset_analyses.revision 机制，不另起版本表。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

# ------------------------------------------------------------------ 常量

#: 深度分析编排脚本（由另一任务实测产出）
#: 默认值；实际取值用 script_path() 读环境变量，方便测试注入假脚本。
ANALYZER_SCRIPT = Path(os.environ.get(
    "H3_AUDIO_ANALYZER_SCRIPT",
    "/root/projects/audio-analysis/analyze_audio.py"))

#: 乐器切换点脚本
SWITCH_SCRIPT = Path(os.environ.get(
    "H3_AUDIO_SWITCH_SCRIPT",
    "/root/projects/audio-analysis/detect_instrument_switches.py"))

#: ComfyUI venv 的 python（torch/transformers/funasr/demucs 都在里面）
DEFAULT_PYTHON = os.environ.get(
    "H3_AUDIO_ANALYZER_PYTHON",
    "/home/ubuntu/comfyui/venv/bin/python3")

#: 分析阶段。bpm 快，clap/vad/demucs 慢。
ALL_STAGES = ("bpm", "instruments", "vocals", "asr", "llm")

#: 单次分析的墙钟上限（秒）。超时判失败，避免后台任务永久挂住。
DEFAULT_TIMEOUT = 1800

#: 深度分析各阶段的显存需求（MiB）——用来挑一张够空的卡。
#: **键名必须与 analyze_audio.py 的 --stages 取值一致**：
#: bpm | instruments | vocals | asr | llm
#: （clap / ast / vad / separation 是 instruments / vocals 内部的子引擎，
#:   不是顶层阶段名，不能拿来匹配）
STAGE_VRAM_MB = {
    "bpm": 0,          # librosa + essentia，纯 CPU
    "instruments": 4000,   # CLAP(~1G) + AST(~1G) 两个模型同时在显存里
    "vocals": 4000,        # demucs htdemucs ~3.5G（silero 很小）
    "asr": 4000,           # faster-whisper large-v3 ~4G / medium ~1.5G
    "llm": 0,          # 走 H3 Studio 的 HTTP 代理，不占本地 GPU
}

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class DeepAudioError(RuntimeError):
    """深度音频分析失败。message 面向用户，直接可读。"""

    def __init__(self, message, code="audio_analysis_failed", detail=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.detail = detail or {}


# ------------------------------------------------------------------ 环境自检


def python_path() -> str:
    """ComfyUI venv 的 python。每次读环境变量，便于测试注入。"""
    return os.environ.get("H3_AUDIO_ANALYZER_PYTHON", DEFAULT_PYTHON)


def script_path() -> Path:
    """深度分析编排脚本路径。每次读环境变量，便于测试注入假脚本。"""
    return Path(os.environ.get("H3_AUDIO_ANALYZER_SCRIPT", str(ANALYZER_SCRIPT)))


def check_environment() -> dict:
    """
    自检运行环境可用性。返回
      {"ok": bool, "python": str, "script": str, "reason": str}
    素材库在启动时调用一次，把结果暴露给前端（用户能看到「音频深度分析不可用」的原因）。
    """
    py = python_path()
    script = script_path()
    if not os.path.exists(py):
        return {"ok": False, "python": py, "script": str(script),
                "reason": f"找不到 ComfyUI venv 的 python：{py}"
                          "（深度音频分析必须在 ComfyUI venv 里跑）"}
    if not script.is_file():
        return {"ok": False, "python": py, "script": str(script),
                "reason": f"找不到音频分析编排脚本：{script}"}
    if not os.access(script, os.R_OK):
        return {"ok": False, "python": py, "script": str(script),
                "reason": f"音频分析编排脚本不可读：{script}"}
    return {"ok": True, "python": py, "script": str(script), "reason": ""}


# ------------------------------------------------------------------ GPU 选择


def list_gpus() -> list[dict]:
    """
    列出本机 GPU 的 uuid / 空闲显存 / 总量。

    用 nvidia-smi 查。查不到就返回空列表（调用方退回 CPU）。
    """
    if not shutil.which("nvidia-smi"):
        return []
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,uuid,memory.free,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=20,
            creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    gpus = []
    for line in out.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 4:
            continue
        try:
            gpus.append({
                "index": int(parts[0]),
                "uuid": parts[1],
                "free_mb": float(parts[2]),
                "total_mb": float(parts[3]),
            })
        except ValueError:
            continue
    return gpus


def pick_gpu(stages=None, min_free_mb: int = 0) -> dict | None:
    """
    挑一张**空闲显存够用**的卡，返回 {"uuid": ..., "index": ...}。

    为什么必须按 UUID 指定：
      方案笔记实测记录 —— GPU0(4090D) 常被 Strata 占 46.8G，torch 的设备号
      与 nvidia-smi 的序号并不一致（CUDA_VISIBLE_DEVICES 认 UUID）。
      直接用 cuda:0 会 OOM。

    规则：
      1. 需要跑 GPU 的阶段（clap/ast/asr/separation）才挑卡；
      2. 只在 free >= 需求 + 余量 的卡里选空闲最多的；
      3. 一张都不够 -> 返回 None，调用方退回 CPU（慢但不会失败）。
    """
    stages = list(stages or ALL_STAGES)
    need = 0
    for stage in stages:
        need = max(need, STAGE_VRAM_MB.get(stage, 0))
    if need <= 0:
        return None

    gpus = list_gpus()
    if not gpus:
        return None

    # 留 2G 余量，避免刚好卡在临界点上被别的进程挤爆
    threshold = need + 2048
    candidates = [g for g in gpus if g["free_mb"] >= threshold]
    if not candidates:
        return None
    best = max(candidates, key=lambda g: g["free_mb"])
    return {"uuid": best["uuid"], "index": best["index"],
            "free_mb": best["free_mb"], "need_mb": need}


def gpu_selection_hint(stages=None) -> str:
    """给用户看的选卡说明（不选卡时说明为什么退回 CPU）。"""
    gpus = list_gpus()
    if not gpus:
        return "未检测到 NVIDIA GPU，深度分析将全程使用 CPU（较慢但可用）"
    stages = list(stages or ALL_STAGES)
    need = max([STAGE_VRAM_MB.get(s, 0) for s in stages] or [0])
    picked = pick_gpu(stages)
    if picked:
        return (f"选用 GPU {picked['index']}（{picked['uuid']}，"
                f"空闲 {picked['free_mb'] / 1024:.1f}G，需求约 {need} MiB）")
    free_desc = "，".join(f"GPU{g['index']} 空闲 {g['free_mb'] / 1024:.1f}G"
                          for g in gpus)
    return (f"没有空闲显存足够的 GPU（需要约 {need + 2048} MiB；当前 {free_desc}），"
            "本次退回 CPU 执行（更慢但结果一致）")


# ------------------------------------------------------------------ 源文件完整性


def file_signature(path: Path) -> dict:
    """源文件指纹：md5 + size + mtime。用于证明分析前后原文件未被改动。"""
    import hashlib

    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    st = path.stat()
    return {"md5": h.hexdigest(), "size": st.st_size, "mtime": int(st.st_mtime)}


# ------------------------------------------------------------------ 调用编排脚本


def run_analyzer(audio_path: Path, stages=None, asr_model="medium",
                 device="auto", use_llm=True, studio_url=None,
                 timeout=DEFAULT_TIMEOUT, max_seconds=None,
                 no_separation=False, extra_args=None) -> dict:
    """
    调用 analyze_audio.py，返回它输出的 JSON dict。

    **只读原文件**：脚本的重采样全在内存里做，demucs 只统计不落盘。
    本函数在调用前后各取一次源文件指纹做比对，不一致就报错。

    失败抛 DeepAudioError（带可读原因）。
    """
    env = check_environment()
    if not env["ok"]:
        raise DeepAudioError(env["reason"], code="audio_env_unavailable")

    audio_path = Path(audio_path)
    if not audio_path.is_file():
        raise DeepAudioError(f"音频文件不存在：{audio_path}",
                             code="audio_file_missing")

    stages = list(stages or ALL_STAGES)
    before = file_signature(audio_path)

    # 输出写到临时目录，不污染素材库目录，也不碰源文件所在目录
    tmpdir = tempfile.mkdtemp(prefix="h3_audio_", dir=tempfile.gettempdir())
    out_json = Path(tmpdir) / "result.json"
    stderr_log = Path(tmpdir) / "stderr.log"

    cmd = [python_path(), str(script_path()), str(audio_path),
           "--out", str(out_json),
           "--stages", ",".join(stages),
           "--device", device,
           "--asr-model", asr_model]
    if use_llm and "llm" in stages:
        cmd.append("--llm")
    if studio_url:
        cmd += ["--studio-url", studio_url]
    if max_seconds:
        cmd += ["--max-seconds", str(max_seconds)]
    if no_separation:
        cmd.append("--no-separation")
    if extra_args:
        cmd += list(extra_args)

    # ---- GPU 选卡：device=auto 时按空闲显存挑一张够用的卡，按 UUID 指定
    run_env = dict(os.environ)
    gpu_note = ""
    if device == "auto":
        picked = pick_gpu(stages)
        if picked:
            run_env["CUDA_VISIBLE_DEVICES"] = picked["uuid"]
            gpu_note = (f"GPU{picked['index']} {picked['uuid']} "
                        f"(空闲 {picked['free_mb'] / 1024:.1f}G)")
        else:
            # 没有够空的卡 -> 明确退回 CPU，别让 torch 自己撞 cuda:0
            cmd[cmd.index("--device") + 1] = "cpu"
            gpu_note = "CPU（没有空闲显存足够的 GPU）"
    elif device == "cuda":
        picked = pick_gpu(stages)
        if picked:
            run_env["CUDA_VISIBLE_DEVICES"] = picked["uuid"]
            gpu_note = f"GPU{picked['index']} {picked['uuid']}（按空闲显存选择）"

    t0 = time.time()
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            env=run_env, creationflags=CREATE_NO_WINDOW)
        stderr_log.write_text(proc.stderr or "", encoding="utf-8")
    except subprocess.TimeoutExpired as exc:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise DeepAudioError(
            f"音频深度分析超时（>{timeout}s）。可减少分析阶段或缩短音频。",
            code="audio_analysis_timeout",
            detail={"timeout": timeout,
                    "stderr": (exc.stderr or "")[-2000:]
                    if isinstance(exc.stderr, str) else ""}) from exc
    except OSError as exc:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise DeepAudioError(f"无法启动音频分析进程：{exc}",
                             code="audio_analysis_spawn_failed") from exc

    elapsed = round(time.time() - t0, 2)

    if proc.returncode != 0 and not out_json.is_file():
        shutil.rmtree(tmpdir, ignore_errors=True)
        tail = (proc.stderr or "")[-1500:]
        raise DeepAudioError(
            f"音频分析脚本执行失败（退出码 {proc.returncode}）：{tail.strip()[-500:]}",
            code="audio_analysis_failed",
            detail={"returncode": proc.returncode, "stderr": tail})

    if not out_json.is_file():
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise DeepAudioError("音频分析脚本没有产出 JSON 结果",
                             code="audio_analysis_no_output",
                             detail={"stderr": (proc.stderr or "")[-2000:]})

    try:
        payload = json.loads(out_json.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise DeepAudioError(f"音频分析结果不是合法 JSON：{exc}",
                             code="audio_analysis_bad_json") from exc
    finally:
        stderr_text = ""
        if stderr_log.is_file():
            stderr_text = stderr_log.read_text(encoding="utf-8", errors="replace")
        shutil.rmtree(tmpdir, ignore_errors=True)

    # ---- 源文件未被改动的硬校验
    after = file_signature(audio_path)
    source_untouched = (before["md5"] == after["md5"]
                        and before["size"] == after["size"]
                        and before["mtime"] == after["mtime"])
    payload["_source_signature_before"] = before
    payload["_source_signature_after"] = after
    payload["_source_untouched_verified"] = source_untouched
    payload["_elapsed_seconds"] = elapsed
    payload["_command"] = " ".join(cmd)
    payload["_gpu"] = gpu_note
    payload["_stderr_tail"] = stderr_text[-1500:]

    if not source_untouched:
        # 这是严重问题：分析过程动了源文件。明确报错，不静默吞掉。
        raise DeepAudioError(
            "检测到分析过程修改了源音频文件，已中止。"
            f"（md5 {before['md5'][:8]}… -> {after['md5'][:8]}…）",
            code="audio_source_mutated",
            detail={"before": before, "after": after})

    return payload


def collect_stage_errors(payload: dict) -> list[str]:
    """
    收集**各阶段内部**的失败。

    analyze_audio.py 的顶层 errors[] 只记录「阶段函数抛异常」的情况；
    而阶段函数内部自己 try/except 掉的失败（例如 GPU OOM 导致 AST 加载失败）
    只写在 stage["error"] 里，不进 errors[]。

    实测踩过：GPU0 被占满时 asr/separation/ast 三个阶段全部失败，
    但 payload["errors"] 仍然是空数组 —— 不显式收集就会误报「全绿」。
    """
    problems: list[str] = []
    bpm = payload.get("bpm") or {}
    if bpm.get("error"):
        problems.append(f"bpm: {bpm['error']}")

    inst = payload.get("instruments") or {}
    if inst.get("error"):
        problems.append(f"instruments: {inst['error']}")
    for engine_name, engine in (inst.get("engines") or {}).items():
        if isinstance(engine, dict) and engine.get("error"):
            problems.append(f"instruments.{engine_name}: {engine['error']}")

    voc = payload.get("vocals") or {}
    if (voc.get("vad") or {}).get("error"):
        problems.append(f"vocals.vad: {voc['vad']['error']}")
    if (voc.get("separation") or {}).get("error"):
        problems.append(f"vocals.separation: {voc['separation']['error']}")

    asr = payload.get("asr") or {}
    if asr.get("error"):
        problems.append(f"asr: {asr['error']}")

    llm = payload.get("llm") or {}
    if llm.get("error"):
        problems.append(f"llm: {llm['error']}")

    # 顶层 errors[]（阶段函数抛异常）也一并算上，去重
    for item in payload.get("errors") or []:
        text = str(item)
        if text not in problems:
            problems.append(text)
    return problems


# ------------------------------------------------------------------ 结果 → 中文文本


def _fmt_ts(value) -> str:
    """秒 → mm:ss.s"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    m, s = divmod(v, 60)
    return f"{int(m):02d}:{s:05.2f}"


def _label_cn(name: str) -> str:
    """乐器/情绪标签英译中，保留英文原名以便对照。"""
    table = {
        "piano": "钢琴", "acoustic guitar": "木吉他",
        "electric guitar": "电吉他", "bass guitar": "贝斯",
        "drum kit": "架子鼓", "strings section": "弦乐组",
        "violin": "小提琴", "cello": "大提琴",
        "synthesizer": "合成器", "electronic dance music": "电子舞曲",
        "orchestra": "管弦乐团", "saxophone": "萨克斯",
        "trumpet": "小号", "flute": "长笛", "percussion": "打击乐",
        "human singing voice": "人声演唱", "choir": "合唱",
        "spoken voice narration": "口播旁白",
        "happy upbeat music": "欢快", "sad melancholic music": "悲伤",
        "energetic dance music": " energetic 舞曲".strip(),
        "calm relaxing ambient music": "舒缓氛围",
        "epic cinematic music": "史诗影视",
        "romantic love song": "浪漫情歌", "tense suspense music": "悬疑紧张",
        "corporate background music": "商务背景乐",
    }
    cn = table.get(name)
    return f"{cn}（{name}）" if cn else name


def render_text(payload: dict) -> str:
    """
    把 analyze_audio.py 的 JSON 渲染成**全中文**的分析文本。

    这是素材库「分析文本记录」里正文的内容 —— 与图片/视频的分析文本同处存储，
    同样可以被检索、被交互纠错、被引用。
    """
    lines: list[str] = []
    media = payload.get("media") or {}
    errors = payload.get("errors") or []

    lines.append("## 一、音频基本信息")
    if media.get("container"):
        lines.append(f"- 容器格式：{media.get('container')}")
    if media.get("codec"):
        lines.append(f"- 音频编码：{media.get('codec')}")
    if media.get("duration"):
        lines.append(f"- 时长：{float(media['duration']):.2f} 秒"
                     f"（{_fmt_ts(media['duration'])}）")
    if media.get("sample_rate"):
        lines.append(f"- 采样率：{media.get('sample_rate')} Hz")
    if media.get("channels"):
        lines.append(f"- 声道数：{media.get('channels')}")
    if media.get("bit_rate"):
        lines.append(f"- 比特率：{int(media['bit_rate']) // 1000} kbps")
    if media.get("size_bytes"):
        lines.append(f"- 文件大小：{media['size_bytes'] / 1024 / 1024:.2f} MB")

    # ---- BPM
    bpm = payload.get("bpm") or {}
    lines.append("")
    lines.append("## 二、节拍与速度")
    if bpm.get("bpm"):
        ess = bpm.get("essentia") or {}
        lines.append(f"- 测得 BPM：{bpm['bpm']}")
        if ess.get("bpm"):
            lines.append(f"- essentia 交叉验证：{ess['bpm']}"
                         f"（与 librosa 相差 {ess.get('diff_vs_librosa')}）")
        cands = bpm.get("bpm_candidates") or []
        if cands:
            lines.append(f"- BPM 候选值：{', '.join(str(c) for c in cands)}"
                         "（存在半速/倍速歧义，实际节拍可能是候选值之一）")
        if bpm.get("beat_regularity") is not None:
            lines.append(f"- 节拍规整度：{bpm['beat_regularity']}（1.0 为完全均匀）")
        if bpm.get("bar_seconds_4_4"):
            lines.append(f"- 4/4 拍一小节时长：{bpm['bar_seconds_4_4']} 秒")
        if bpm.get("n_beats"):
            lines.append(f"- 检出节拍点：{bpm['n_beats']} 个")
    elif bpm.get("error"):
        lines.append(f"- 未能测得 BPM：{bpm['error']}")

    local = bpm.get("local_tempo") or {}
    if local:
        lines.append(f"- 局部速度曲线：{local.get('min')} ~ {local.get('max')} BPM"
                     f"（波动标准差 {local.get('std')}）")

    # ---- 乐器
    inst = payload.get("instruments") or {}
    lines.append("")
    lines.append("## 三、乐器与音色")
    summary = inst.get("instrument_summary") or []
    if summary:
        for item in summary[:8]:
            lines.append(f"- {_label_cn(item['name'])}：占比 {item.get('weight')}（得分 {item.get('score')}）")
    elif inst.get("error"):
        lines.append(f"- 乐器识别失败：{inst['error']}")

    moods = inst.get("mood_summary") or []
    if moods:
        lines.append("")
        lines.append("### 情绪倾向（零样本）")
        for item in moods[:5]:
            lines.append(f"- {_label_cn(item['name'])}：{item.get('score')}")

    clap = (inst.get("engines") or {}).get("clap") or {}
    ast = (inst.get("engines") or {}).get("ast") or {}
    if clap.get("n_frames"):
        lines.append(f"- CLAP 零样本逐窗识别：{clap['n_frames']} 帧，"
                     f"耗时 {clap.get('seconds')} 秒")
    if ast.get("n_frames"):
        lines.append(f"- AST(AudioSet 527) 逐窗识别：{ast['n_frames']} 帧，"
                     f"耗时 {ast.get('seconds')} 秒")
    if clap.get("frames"):
        lines.append("")
        lines.append("### 逐窗乐器/情绪（前 12 窗）")
        for fr in clap["frames"][:12]:
            names = "、".join(_label_cn(n) for n, _ in fr.get("instruments", [])[:3])
            mood = "、".join(_label_cn(n) for n, _ in fr.get("moods", [])[:2])
            lines.append(f"- {_fmt_ts(fr.get('start', 0))}：{names}"
                         + (f"；情绪 {mood}" if mood else ""))

    # ---- 人声
    voc = payload.get("vocals") or {}
    vad = voc.get("vad") or {}
    lines.append("")
    lines.append("## 四、人声检测与分离")
    if vad.get("voiced_ratio") is not None:
        lines.append(f"- 语音活动占比：{vad['voiced_ratio']}"
                     f"（{vad.get('engine')}，模型来源 {vad.get('model_source')}）")
        lines.append(f"- 语音累计时长：{vad.get('speech_seconds')} 秒")
        lines.append(f"- 平均语音概率：{vad.get('mean_prob')}")
        segs = vad.get("segments") or []
        if segs:
            lines.append(f"- 检出语音片段 {len(segs)} 段，前 20 段：")
            for a, b in segs[:20]:
                lines.append(f"    - {_fmt_ts(a)} → {_fmt_ts(b)}")
    elif vad.get("error"):
        lines.append(f"- 人声活动检测失败：{vad['error']}")

    sep = voc.get("separation") or {}
    if sep.get("stem_ratio"):
        lines.append("- 人声/伴奏分离能量占比（demucs htdemucs，仅统计不落盘）：")
        for k, v in sep["stem_ratio"].items():
            lines.append(f"    - {k}：{v}")
        lines.append(f"- 是否含人声：{'是' if sep.get('has_vocals') else '否'}")
    elif sep.get("error"):
        lines.append(f"- 人声分离失败：{sep['error']}")
    elif sep.get("skipped"):
        lines.append("- 人声分离：本次跳过（--no-separation）")

    # ---- ASR
    asr = payload.get("asr") or {}
    lines.append("")
    lines.append("## 五、语音识别（带时间戳）")
    if asr.get("segments"):
        lines.append(f"- 引擎：faster-whisper {asr.get('model')}，"
                     f"语言 {asr.get('language')}"
                     f"（置信度 {asr.get('language_probability')}）")
        lines.append(f"- 实时率 RTF：{asr.get('rtf')}（推理 {asr.get('infer_seconds')} 秒）")
        lines.append("")
        for seg in asr["segments"]:
            lines.append(f"- [{_fmt_ts(seg.get('start', 0))} → "
                         f"{_fmt_ts(seg.get('end', 0))}] {seg.get('text', '')}")
    elif asr.get("items"):
        lines.append(f"- 引擎：{asr.get('engine')}"
                     f"（{asr.get('infer_seconds')} 秒，RTF {asr.get('rtf')}）")
        for item in asr["items"]:
            lines.append(f"- {item.get('text', '')}")
    elif asr.get("error"):
        lines.append(f"- 语音识别失败：{asr['error']}")
    else:
        lines.append("- 本次未执行语音识别")

    # ---- LLM 补充
    llm = payload.get("llm") or {}
    lines.append("")
    lines.append("## 六、语义补充分析")
    if llm.get("analysis"):
        lines.append(llm["analysis"].strip())
    elif llm.get("error"):
        lines.append(f"- 语义补充分析不可用：{llm['error']}")
    else:
        lines.append("- 本次未启用大模型语义补充")

    # ---- 错误与完整性
    problems = collect_stage_errors(payload)
    lines.append("")
    lines.append("## 七、分析过程说明")
    lines.append(f"- 分析总耗时：{payload.get('total_seconds')} 秒"
                 f"（含各阶段）")
    if payload.get("_gpu"):
        lines.append(f"- 计算设备：{payload.get('_gpu')}")
    if payload.get("_source_untouched_verified"):
        sig = payload.get("_source_signature_before") or {}
        lines.append(f"- 源文件校验：分析前后 md5 一致（{sig.get('md5', '')[:8]}…），"
                     "未发生二次转码或写回")
    else:
        lines.append("- 源文件校验：未通过")
    if problems:
        lines.append(f"- 以下阶段失败（不影响其它阶段结果）：")
        for err in problems:
            lines.append(f"    - {err}")
    else:
        lines.append("- 所有阶段执行成功，无错误")

    return "\n".join(lines)


def to_extra(payload: dict) -> dict:
    """
    结构化数据 → asset_analyses.extra。

    只保留有实用价值的字段，避免把上百个逐窗帧塞进 DB。
    完整原始 JSON 落在派生目录，供将来检索/二次分析使用。
    """
    media = payload.get("media") or {}
    bpm = payload.get("bpm") or {}
    inst = payload.get("instruments") or {}
    voc = payload.get("vocals") or {}
    vad = voc.get("vad") or {}
    sep = voc.get("separation") or {}
    asr = payload.get("asr") or {}
    llm = payload.get("llm") or {}

    asr_segments = []
    for seg in (asr.get("segments") or []):
        asr_segments.append({"start": seg.get("start"), "end": seg.get("end"),
                              "text": seg.get("text")})

    extra = {
        "analyzer": "audio-analysis/analyze_audio.py",
        "python": payload.get("_command", "").split(" ")[0] if payload.get("_command") else "",
        "gpu": payload.get("_gpu", ""),
        "source_untouched": bool(payload.get("_source_untouched_verified")),
        "source_md5": (payload.get("_source_signature_before") or {}).get("md5"),
        "elapsed_seconds": payload.get("_elapsed_seconds"),
        "total_seconds": payload.get("total_seconds"),
        "errors": collect_stage_errors(payload),
        "media": {k: media.get(k) for k in
                  ("container", "codec", "duration", "sample_rate", "channels",
                   "bit_rate", "size_bytes")},
        "bpm": {
            "bpm": bpm.get("bpm"),
            "essentia_bpm": (bpm.get("essentia") or {}).get("bpm"),
            "bpm_candidates": bpm.get("bpm_candidates"),
            "beat_regularity": bpm.get("beat_regularity"),
            "local_tempo": bpm.get("local_tempo"),
            "bar_seconds_4_4": bpm.get("bar_seconds_4_4"),
        },
        "instruments": {
            "summary": (inst.get("instrument_summary") or [])[:10],
            "moods": (inst.get("mood_summary") or [])[:6],
            "clap_frames": ((inst.get("engines") or {}).get("clap") or {}).get("n_frames"),
            "ast_frames": ((inst.get("engines") or {}).get("ast") or {}).get("n_frames"),
        },
        "vocals": {
            "voiced_ratio": vad.get("voiced_ratio"),
            "speech_seconds": vad.get("speech_seconds"),
            "vad_engine": vad.get("engine"),
            "segments": (vad.get("segments") or [])[:100],
            "stem_ratio": sep.get("stem_ratio"),
            "has_vocals": sep.get("has_vocals"),
        },
        "asr": {
            "engine": asr.get("engine"),
            "model": asr.get("model"),
            "language": asr.get("language"),
            "rtf": asr.get("rtf"),
            "full_text": asr.get("full_text"),
            "segments": asr_segments[:200],
        },
        "llm": {
            "model": llm.get("model"),
            "analysis": llm.get("analysis"),
        },
    }
    return extra


def analyze(audio_path: Path, options: dict | None = None) -> dict:
    """
    素材库调用的统一入口。返回
      {"text": 中文分析文本, "extra": 结构化数据, "raw": 原始 JSON}

    失败抛 DeepAudioError。
    """
    options = options or {}
    payload = run_analyzer(
        audio_path,
        stages=options.get("stages") or ALL_STAGES,
        asr_model=options.get("asr_model") or "medium",
        device=options.get("device") or "auto",
        use_llm=bool(options.get("use_llm", True)),
        studio_url=options.get("studio_url"),
        timeout=options.get("timeout") or DEFAULT_TIMEOUT,
        max_seconds=options.get("max_seconds"),
        no_separation=bool(options.get("no_separation", False)),
    )
    return {
        "text": render_text(payload),
        "extra": to_extra(payload),
        "raw": payload,
    }
