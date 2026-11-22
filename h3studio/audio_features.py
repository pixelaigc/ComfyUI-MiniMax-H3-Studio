"""audio_features.py -- 本地数值化音频分析（忠实移植 nonebot h3_audio_features.py）。

来源：/home/ubuntu/nonebotqq/plugins/nonebot_plugin_novelai/h3_audio_features.py
移植方式：逐字保留算法，只把 nonebot 的 logger 换成标准库 logging。
咏唱参考生音乐 (YuE2 Remix) 的**本地数值化**音频分析.

为什么有这个模块 (2026-11-16 willis 拍板):
  旧实现把音频渲染成 spectrogram PNG + chromagram PNG 两张图, 交给 hermes 用
  **视觉模型看图**去推断节奏/音高/段落/乐器. 实测 (插件专属 hermes home 日志):
      tool vision_analyze completed (77.02s)
      tool vision_analyze failed    (300.34s)   ← 辅助视觉超时, 回落本地 5000 也超时
      tool vision_analyze completed (112.06s)
  → 3 次视觉 ≈ 8 分钟纯等待, 把整轮 1800s 拖爆.

  音频本身**不需要多模态视觉**: BPM / 节拍 / 段落 / 调式 / 能量 / 频谱分布全是
  可用 librosa 直接算的数值. 图片只是音频的代理表示, 拿代理表示去问视觉模型,
  既是多余的中间层, 又是最大的不稳定源.

本模块的定位:
  - **唯一的音频分析入口** —— 插件内本地算完, 产出结构化 JSON + 中文摘要.
  - 产物**直接注入** hermes 主调用的提示词 → 主调用只做**一次纯文本推理**.
  - **不产生任何图片副任务**, 不触发 vision_analyze, 不走 auxiliary.vision.

依赖: librosa / numpy / scipy / sklearn (本机 nonebotqq venv 已全部具备, 零新增依赖).
所有函数在依赖缺失时**优雅降级** (返回 degraded 标记 + 空字段), 不抛异常.
"""
import logging
import math
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

import logging

logger = logging.getLogger(__name__)

# ==================== 常量 ====================

# 分析采样率. 22050 是 librosa 的默认分析采样率, 足够覆盖 BPM / 段落 / 能量,
# 且比原始 44.1k/48k 少一半以上的计算量. 单元测试里的预期 BPM 也是按这个定的.
ANALYSIS_SR = 22050

# 分析时长的硬上限 (秒). 超过就只分析前 N 秒 —— 目的是**绝不**让本地分析
# 变成新的阻塞点 (旧路径的教训: 任何无界等待都会把整轮拖爆).
ANALYSIS_MAX_SECONDS = 600.0

# 最短可分析时长 (秒). 太短的片段没有节拍/段落可言.
ANALYSIS_MIN_SECONDS = 1.0

# 段落最短时长 (秒). 比它短的段会并入相邻段 —— 避免把一个音轨切成一堆碎片.
SEGMENT_MIN_SECONDS = 3.0

# 段落数上限 / 下限 (按音频长度自适应).
SEGMENT_MAX = 12
SEGMENT_MIN = 1

# Krumhansl-Schmuckler 调式模板 (标准值).
KS_MAJOR = (6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88)
KS_MINOR = (6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17)

PITCH_CLASSES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")

# 频段划分 (Hz) —— 用于能量分布 + 乐器族推断.
BAND_EDGES: Tuple[Tuple[str, float, float], ...] = (
    ("sub_bass", 20.0, 60.0),
    ("bass", 60.0, 250.0),
    ("low_mid", 250.0, 800.0),
    ("mid", 800.0, 2500.0),
    ("high_mid", 2500.0, 6000.0),
    ("air", 6000.0, 16000.0),
)

# 人声频段 (Hz) —— 主唱声基频与共鸣腔大致落在这.
VOICE_BAND = (200.0, 4000.0)

# ==================== 依赖探测 ====================

NUMERIC_ANALYSIS_OK = False
_DEPS_ERROR = ""

try:
    import numpy as np
    import librosa
    import soundfile  # noqa: F401  (写 wav 用, 测试里要)
    NUMERIC_ANALYSIS_OK = True
except Exception as _e:  # pragma: no cover - 环境级缺依赖
    _DEPS_ERROR = f"{type(_e).__name__}: {_e}"
    logger.warning(f"[咏唱参考生音乐] 数值化音频分析依赖不可用: {_DEPS_ERROR}")


# ==================== 音频加载 (带缓存) ====================

_AUDIO_CACHE: "OrderedDict[Tuple[str, float, int], Tuple[Any, int]]" = OrderedDict()
_AUDIO_CACHE_MAX = 2


def _file_signature(audio_path: str) -> Tuple[str, float, int]:
    """(path, mtime, size) —— 文件变了缓存自动失效."""
    import os

    try:
        st = os.stat(audio_path)
        return (os.path.abspath(audio_path), float(st.st_mtime), int(st.st_size))
    except OSError:
        return (os.path.abspath(audio_path), 0.0, 0)


def load_audio(audio_path: str):
    """载入音频 → (y, sr). 单声道 + 重采样到 ANALYSIS_SR.

    缓存理由: 同一次任务里 BPM / 调式 / 段落 / 能量 / 能量边界要读**同一份**音频,
    每次 librosa.load 都是一次完整解码 (130s 音频 ≈ 1~2s), 不缓存会白跑 4~5 遍.
    """
    if not NUMERIC_ANALYSIS_OK:
        return None, 0
    key = _file_signature(audio_path)
    cached = _AUDIO_CACHE.get(key)
    if cached is not None:
        _AUDIO_CACHE.move_to_end(key)
        return cached
    try:
        y, sr = librosa.load(audio_path, sr=ANALYSIS_SR, mono=True)
    except Exception as e:
        logger.warning(f"[咏唱参考生音乐] 音频载入失败 ({audio_path}): {e}")
        return None, 0
    if y is not None and len(y) > 0:
        _AUDIO_CACHE[key] = (y, sr)
        _AUDIO_CACHE.move_to_end(key)
        while len(_AUDIO_CACHE) > _AUDIO_CACHE_MAX:
            _AUDIO_CACHE.popitem(last=False)
    return y, sr


def clear_audio_cache() -> None:
    """清空音频缓存 (测试用)."""
    _AUDIO_CACHE.clear()


# ==================== 基础数值 ====================

def _to_db(x) -> float:
    """幅度 → dB (相对峰值 0 dB), 带下限保护."""
    try:
        x = float(x)
    except (TypeError, ValueError):
        return -120.0
    if x <= 1e-10:
        return -120.0
    return max(-120.0, 20.0 * math.log10(x))


def detect_tempo(y, sr) -> Dict[str, Any]:
    """BPM + 节拍数 + 节拍网格间隔. 用 librosa beat_track, 再用 onset 间隔做交叉校验.

    为什么不用 onset 间隔直接算: onset 的 IOI 有倍速/半速歧义 (120 BPM 的歌
    很容易被听成 60 或 240). beat_track 内部有先验, 更稳; 但它在某些
    电子/自由节奏素材上会偏, 所以再用 onset 的 IOI 直方图峰值做一次交叉检查.
    """
    out: Dict[str, Any] = {"bpm": 0.0, "beat_count": 0, "beat_interval_sec": 0.0,
                           "onset_bpm": 0.0, "method": "none"}
    if y is None or sr <= 0 or len(y) < sr // 2:
        return out
    try:
        tempo, beats = librosa.beat.beat_track(y=y, sr=sr)
        tempo = float(np.atleast_1d(tempo)[0])
        out["bpm"] = round(tempo, 2)
        out["beat_count"] = int(len(beats))
        if len(beats) > 1:
            out["beat_interval_sec"] = round(float(np.median(np.diff(beats))) * sr, 4)
        out["method"] = "librosa.beat_track"
    except Exception as e:
        logger.warning(f"[咏唱参考生音乐] beat_track 失败: {e}")

    # onset IOI 交叉校验
    try:
        onset_frames = librosa.onset.onset_detect(y=y, sr=sr, units="time")
        if len(onset_frames) > 3:
            ioi = np.diff(onset_frames)
            ioi = ioi[(ioi > 0.2) & (ioi < 2.0)]
            if len(ioi) > 0:
                # 直方图峰值 (0.01s bin)
                hist, edges = np.histogram(ioi, bins=120, range=(0.2, 2.0))
                peak = float(edges[int(np.argmax(hist))])
                if peak > 0:
                    out["onset_bpm"] = round(60.0 / peak, 2)
    except Exception as e:
        logger.debug(f"[咏唱参考生音乐] onset 交叉校验失败: {e}")
    return out


def detect_key(y, sr) -> Dict[str, Any]:
    """调式检测 —— Krumhansl-Schmuckler 模板相关, 24 个候选 (12 大调 × 12 小调).

    比旧实现更严谨: 旧实现先用 chroma.argmax() 定根音, 再只比较该根音的大/小模板,
    根音选错就没救了. 这里**遍历全部 24 个候选**取最大相关, 并给出 confidence.
    """
    out: Dict[str, Any] = {"root": "", "mode": "", "confidence": 0.0,
                           "pitch_class": "", "top_pitch_classes": []}
    if y is None or sr <= 0:
        return out
    try:
        chroma = librosa.feature.chroma_cqt(y=y, sr=sr, hop_length=512)
        mean_chroma = np.mean(chroma, axis=1)
        if not np.any(mean_chroma > 0):
            return out
        major = np.array(KS_MAJOR)
        minor = np.array(KS_MINOR)
        best = (None, None, -2.0)
        for shift in range(12):
            for name, profile in (("major", major), ("minor", minor)):
                cand = np.roll(profile, -shift)
                if np.std(cand) == 0 or np.std(mean_chroma) == 0:
                    continue
                corr = float(np.corrcoef(cand, mean_chroma)[0, 1])
                if corr > best[2]:
                    best = (name, shift, corr)
        mode, shift, corr = best
        if mode is None:
            return out
        out["root"] = PITCH_CLASSES[shift]
        out["mode"] = mode
        out["pitch_class"] = f"{out['root']} {mode}"
        out["confidence"] = round(max(0.0, corr), 4)
        # 能量最高的前 3 个音级 (给 hermes 看和声色彩)
        top = np.argsort(mean_chroma)[::-1][:3]
        out["top_pitch_classes"] = [
            {"pitch_class": PITCH_CLASSES[int(i)],
             "weight": round(float(mean_chroma[int(i)] / max(float(np.max(mean_chroma)), 1e-9)), 4)}
            for i in top
        ]
    except Exception as e:
        logger.warning(f"[咏唱参考生音乐] 调式检测失败: {e}")
    return out


def detect_energy(y, sr) -> Dict[str, Any]:
    """能量与动态 —— RMS 电平 + 趋势 + 峰均比 (crest) + 动态范围."""
    out: Dict[str, Any] = {"mean_db": -120.0, "peak_db": -120.0, "dynamic_range_db": 0.0,
                           "crest_db": 0.0, "trend": "unknown", "dynamic_class": "unknown",
                           "loudness_lufs_proxy": -120.0}
    if y is None or sr <= 0:
        return out
    try:
        rms = librosa.feature.rms(y=y, hop_length=512)[0]
        if len(rms) == 0:
            return out
        rms_db = librosa.amplitude_to_db(rms, ref=np.max)
        out["mean_db"] = round(float(np.mean(rms_db)), 2)
        out["peak_db"] = round(float(np.max(rms_db)), 2)
        out["dynamic_range_db"] = round(float(np.percentile(rms_db, 95) - np.percentile(rms_db, 10)), 2)
        peak_amp = float(np.max(np.abs(y))) if len(y) else 0.0
        rms_amp = float(np.mean(rms)) if len(rms) else 0.0
        if rms_amp > 1e-9 and peak_amp > 0:
            out["crest_db"] = round(_to_db(peak_amp) - _to_db(rms_amp), 2)
        # 响度代理 (不做完整 ITU-R BS.1770, 但相对关系可靠): RMS 的 dB 偏移 + 峰值修正
        out["loudness_lufs_proxy"] = round(float(out["mean_db"] - 3.0), 2)

        # 趋势: 前 1/3 vs 后 1/3 的中位电平差
        third = max(1, len(rms_db) // 3)
        head = float(np.median(rms_db[:third]))
        tail = float(np.median(rms_db[-third:]))
        diff = tail - head
        if diff > 2.0:
            out["trend"] = "builds_up"
        elif diff < -2.0:
            out["trend"] = "decays"
        else:
            out["trend"] = "flat"

        dr = out["dynamic_range_db"]
        if dr < 8.0:
            out["dynamic_class"] = "compressed"
        elif dr < 18.0:
            out["dynamic_class"] = "normal"
        else:
            out["dynamic_class"] = "wide"
    except Exception as e:
        logger.warning(f"[咏唱参考生音乐] 能量分析失败: {e}")
    return out


def detect_spectrum(y, sr) -> Dict[str, Any]:
    """频谱特征 —— 质心 / 85% 滚降 / 平坦度 / 频段能量分布."""
    out: Dict[str, Any] = {"centroid_hz": 0.0, "rolloff85_hz": 0.0, "flatness": 0.0,
                           "band_energy": {}, "band_energy_pct": {}, "brightness": "unknown"}
    if y is None or sr <= 0:
        return out
    try:
        S = np.abs(librosa.stft(y, n_fft=2048, hop_length=512))
        freqs = librosa.fft_frequencies(sr=sr, n_fft=2048)
        if S.size == 0:
            return out
        centroid = librosa.feature.spectral_centroid(S=S, sr=sr)[0]
        rolloff = librosa.feature.spectral_rolloff(S=S, sr=sr, roll_percent=0.85)[0]
        flatness = librosa.feature.spectral_flatness(S=S)[0]
        out["centroid_hz"] = round(float(np.mean(centroid)), 1)
        out["rolloff85_hz"] = round(float(np.mean(rolloff)), 1)
        out["flatness"] = round(float(np.mean(flatness)), 5)

        # 频段能量 (用 mel 频谱求和, 对数感知更接近听感)
        mel = librosa.feature.melspectrogram(y=y, sr=sr, n_fft=2048, hop_length=512, n_mels=128)
        mel_energy = mel.sum(axis=1)
        mel_freqs = librosa.mel_frequencies(n_mels=128, fmin=0, fmax=sr // 2)
        total = float(mel_energy.sum())
        bands: Dict[str, float] = {}
        for name, lo, hi in BAND_EDGES:
            mask = (mel_freqs >= lo) & (mel_freqs < hi)
            bands[name] = float(mel_energy[mask].sum())
        for name, val in bands.items():
            out["band_energy"][name] = round(val, 6)
            out["band_energy_pct"][name] = round(val / total, 4) if total > 0 else 0.0

        c = out["centroid_hz"]
        if c < 1200:
            out["brightness"] = "dark"
        elif c < 3200:
            out["brightness"] = "warm"
        else:
            out["brightness"] = "bright"
    except Exception as e:
        logger.warning(f"[咏唱参考生音乐] 频谱分析失败: {e}")
    return out


def detect_texture(y, sr) -> Dict[str, Any]:
    """质感 —— 打击/和声分解比例 + 起音密度 + 静音占比."""
    out: Dict[str, Any] = {"percussive_ratio": 0.0, "harmonic_ratio": 0.0,
                           "onset_rate_per_sec": 0.0, "onset_count": 0,
                           "silence_ratio": 0.0, "noisiness": "unknown"}
    if y is None or sr <= 0:
        return out
    try:
        S_complex = librosa.stft(y, n_fft=2048, hop_length=512)
        S_mag = np.abs(S_complex)
        H, P = librosa.decompose.hpss(S_complex)
        h_energy = float(np.sum(np.abs(H)))
        p_energy = float(np.sum(np.abs(P)))
        total = h_energy + p_energy
        if total > 0:
            out["harmonic_ratio"] = round(h_energy / total, 4)
            out["percussive_ratio"] = round(p_energy / total, 4)

        onsets = librosa.onset.onset_detect(y=y, sr=sr, units="time")
        out["onset_count"] = int(len(onsets))
        dur = len(y) / float(sr)
        if dur > 0:
            out["onset_rate_per_sec"] = round(len(onsets) / dur, 3)

        rms = librosa.feature.rms(y=y, hop_length=512)[0]
        if len(rms) > 0:
            rms_db = librosa.amplitude_to_db(rms, ref=np.max)
            out["silence_ratio"] = round(float(np.mean(rms_db < -45.0)), 4)

        # spectral_flatness 只接受**实数**幅度谱, 传复数 STFT 会抛异常
        flat = float(np.mean(librosa.feature.spectral_flatness(S=S_mag)[0]))
        if flat < 0.005:
            out["noisiness"] = "tonal"
        elif flat < 0.05:
            out["noisiness"] = "mixed"
        else:
            out["noisiness"] = "noisy"
    except Exception as e:
        logger.warning(f"[咏唱参考生音乐] 质感分析失败: {e}")
    return out


def detect_vocal_proxy(y, sr) -> Dict[str, Any]:
    """人声代理指标.

    严格说这是**启发式**, 不是人声检测器 —— 它只回答"人声频段里有没有稳定的
    和声成分", 让 hermes 在 vocal / vocal_color 字段上有个客观依据, 而不是纯猜.
    输出里明确带 confidence, prompt 里也要求 hermes 别把它当铁证.
    """
    out: Dict[str, Any] = {"voice_band_harmonic_ratio": 0.0, "voice_band_flatness": 0.0,
                           "likely_has_lead_vocal": False, "confidence": 0.0, "note": ""}
    if y is None or sr <= 0:
        return out
    try:
        S = np.abs(librosa.stft(y, n_fft=2048, hop_length=512))
        freqs = librosa.fft_frequencies(sr=sr, n_fft=2048)
        band_mask = (freqs >= VOICE_BAND[0]) & (freqs < VOICE_BAND[1])
        if not np.any(band_mask):
            return out
        band = S[band_mask, :]
        if band.size == 0:
            return out
        mag = band.sum(axis=0)
        # 人声频段的谱平坦度低 = 有明确的谐波峰 (乐音), 高 = 噪声
        denom = mag.mean() + 1e-12
        geo = float(np.exp(np.mean(np.log(mag + 1e-12))))
        out["voice_band_flatness"] = round(geo / denom, 5)
        # 谐波占比: 用 HPSS 的 harmonic 分量在频段的占比
        S_complex = librosa.stft(y, n_fft=2048, hop_length=512)
        H, _ = librosa.decompose.hpss(S_complex)
        Hm = np.abs(H)[band_mask, :]
        tot = float(np.sum(mag))
        if tot > 0:
            out["voice_band_harmonic_ratio"] = round(float(np.sum(Hm)) / tot, 4)
        f = out["voice_band_flatness"]
        r = out["voice_band_harmonic_ratio"]
        # 启发式评分
        score = 0.0
        if f < 0.02:
            score += 0.5
        elif f < 0.08:
            score += 0.25
        if r > 0.5:
            score += 0.3
        elif r > 0.3:
            score += 0.15
        score = max(0.0, min(1.0, score))
        out["confidence"] = round(score, 3)
        out["likely_has_lead_vocal"] = score >= 0.55
        out["note"] = "启发式指标, 非人声检测器; 只作为 vocal 字段的参考依据"
    except Exception as e:
        logger.warning(f"[咏唱参考生音乐] 人声代理分析失败: {e}")
    return out


# ==================== 段落切分 ====================

def _segment_count_for_duration(duration: float) -> int:
    """按长度估段落数: 每 ~22 秒一段, 夹在 [SEGMENT_MIN, SEGMENT_MAX].

    ⚠️ 只作为**凝聚聚类的初始猜测**, 最终段落数由 _detect_boundaries 的
    novelty 检测决定 —— 详见那里的说明.
    """
    if duration <= 0:
        return SEGMENT_MIN
    n = int(round(duration / 22.0))
    return max(SEGMENT_MIN, min(SEGMENT_MAX, n))


def _foote_novelty(feat: np.ndarray, width: int = 16) -> np.ndarray:
    """Foote novelty —— 自相似矩阵 (SSM) 的 checkerboard kernel 相关, 峰值即结构边界.

    实现 (标准 Foote 2000):
      1. 算 SSM[i,j] = 帧 i 与帧 j 的余弦相似度 (用已 L2 归一的特征直接点积);
      2. 对每个 i, 用 checkerboard kernel:
           kernel = +1 覆盖"未来窗口" [i, i+w)
                     -1 覆盖"过去窗口" [i-w, i)
           novelty[i] = Σ_j kernel[j] · SSM[i,j]
         → 未来与过去的相似度差, 差越大越是结构边界.

    ⚠️ 早期版本用 lag-embedding 构造 SSM, 但嵌入后帧数变成 n-2w, 索引却仍按 n 取,
       切片越界 → 空数组 → 均值 NaN → 整条 novelty 曲线废掉 (静默失效).
       现在直接对原始帧算 SSM, 维度天然对齐.
    """
    n = feat.shape[1]
    nov = np.zeros(n, dtype=np.float64)
    if n < 2 * width + 2:
        return nov
    # 特征已 L2 归一 (调用方保证), 点积即余弦相似度
    ssm = feat.T @ feat
    for i in range(width, n - width):
        past = float(np.mean(ssm[i, i - width:i]))      # 与过去窗口的平均相似度
        future = float(np.mean(ssm[i, i:i + width]))    # 与未来窗口的平均相似度
        nov[i] = future - past
    # NaN 防护 (极端输入下 SSM 可能出 nan)
    nov = np.nan_to_num(nov, nan=0.0, posinf=0.0, neginf=0.0)
    return nov


def _detect_boundaries(feat: np.ndarray, duration: float, sr: int):
    """段落边界检测 —— 返回 (边界帧数组, 方法名).

    策略 (由数据决定段落数, 不按时长猜):
      1. 算 Foote novelty 曲线;
      2. 取显著峰值当候选边界 (novelty > 均值 + 0.5*std, 且彼此间隔 ≥ 最小段长);
      3. 若候选太少 (<2 段), 用凝聚聚类在**估计**的 k 上兜底 (旧路径, 仍比纯猜好);
      4. 若候选太多 (>SEGMENT_MAX), 只保留最强的若干个.

    为什么要两段式:
      - 纯 novelty 对"能量突变"敏感, 对"和声不变但编曲变了"不敏感;
      - 纯凝聚聚类的 k 是人为定的, 会硬造出并不存在的段落.
      先用数据找证据 (novelty), 再用聚类补齐, 是两者取长.
    """
    n_frames = feat.shape[1]
    hop = 512
    min_seg_frames = max(2, int(SEGMENT_MIN_SECONDS * sr / hop))

    nov = _foote_novelty(feat)
    cand: List[int] = []
    if nov.size > 0 and float(np.max(nov)) > 0:
        mean, std = float(np.mean(nov)), float(np.std(nov))
        thresh = mean + 0.5 * std
        # 找局部极大值.
        # ⚠️ 必须是**严格**的局部极大 (nov[i] > nov[i-1] 且 nov[i] >= nov[i+1]),
        #    且要求相对全局峰值有足够 prominence ——
        #    否则一个平坦高原上的第一个点也会被当成边界 (实测会在 0.37s 切出
        #    一个假边界, 把音频头 0.4s 单独切一段).
        peak_val = float(np.max(nov))
        min_prominence = max(peak_val * 0.25, 1e-6)
        for i in range(1, len(nov) - 1):
            if nov[i] >= thresh and nov[i] > nov[i - 1] and nov[i] > nov[i + 1]:
                # prominence: 往两侧走, 直到 novelty 掉到 nov[i] - min_prominence
                left = i
                while left > 0 and nov[left] > nov[i] - min_prominence:
                    left -= 1
                right = i
                while right < len(nov) - 1 and nov[right] > nov[i] - min_prominence:
                    right += 1
                if nov[i] >= nov[left] and nov[i] >= nov[right]:
                    cand.append(i)
        # 贪心: 按 novelty 强度取, 且彼此间隔 ≥ min_seg_frames
        cand.sort(key=lambda i: nov[i], reverse=True)
        chosen: List[int] = []
        for i in cand:
            if all(abs(i - c) >= min_seg_frames for c in chosen):
                chosen.append(i)
            if len(chosen) >= SEGMENT_MAX - 1:
                break
        chosen.sort()
        cand = chosen

    if len(cand) >= 1:
        bounds = [0] + list(cand) + [n_frames]
        # 去重 + 保序
        clean: List[int] = []
        for b in bounds:
            if not clean or b > clean[-1]:
                clean.append(b)
        if len(clean) - 1 >= 2:
            return np.array(clean), "foote_novelty"

    # 兜底: 凝聚聚类 (k 由时长估计, 只在 novelty 找不到证据时用)
    k = _segment_count_for_duration(duration)
    if k < 2 or n_frames < 4:
        return np.array([0, n_frames]), "single_segment"
    try:
        boundaries = librosa.segment.agglomerative(feat, k)
        return boundaries, "agglomerative(chroma+rms)"
    except Exception as e:
        logger.warning(f"[咏唱参考生音乐] 凝聚聚类失败: {e}")
        return np.array([0, n_frames]), "single_segment"


def _chord_from_chroma(chroma_vec) -> str:
    """从一段的 chroma 均值估一个简易和弦标记 (root + 大/小/无三度)."""
    try:
        v = np.asarray(chroma_vec, dtype=float)
        if v.size != 12 or not np.any(v > 0):
            return ""
        root = int(np.argmax(v))
        rv = float(v[root])
        third_minor = float(v[(root + 3) % 12])
        third_major = float(v[(root + 4) % 12])
        fifth = float(v[(root + 7) % 12])
        quality = ""
        if third_major > rv * 0.35:
            quality = ""
        elif third_minor > rv * 0.35:
            quality = "m"
        if fifth > rv * 0.3:
            quality = quality or "5"
        return f"{PITCH_CLASSES[root]}{quality}"
    except Exception:
        return ""


def detect_structure(y, sr, duration: float) -> Dict[str, Any]:
    """段落切分 —— chroma + RMS 的凝聚聚类 (librosa.segment.agglomerative).

    失败时降级为按 RMS 阈值的静音切分, 再兜底为整段.
    """
    out: Dict[str, Any] = {"segment_count": 0, "segments": [], "method": "none"}
    if y is None or sr <= 0 or duration <= 0:
        return out
    try:
        chroma = librosa.feature.chroma_cqt(y=y, sr=sr, hop_length=512)
        rms = librosa.feature.rms(y=y, hop_length=512)[0]
        rms_db = librosa.amplitude_to_db(rms, ref=np.max) if len(rms) else np.array([])
        if len(chroma) < 4 or chroma.shape[0] != 12:
            raise ValueError("chroma 太短")

        # 特征矩阵: 12 维 chroma + 1 维电平 (各向 L2 归一, 让二者同权重)
        feat = np.vstack([
            chroma / (np.linalg.norm(chroma, axis=0, keepdims=True) + 1e-9),
            (rms_db / 40.0).reshape(1, -1),
        ])

        # 段落数**由数据决定**, 不是按时长猜.
        #
        # 旧做法 (duration/22) 有两个硬伤:
        #   1. 12s 的素材 → k=1 → 永远切不出段落 (本测试就是踩这个);
        #   2. 一首结构规整的 3 分钟歌和一段 30s 副歌, 猜出来的 k 一样 → 段落数是编的.
        # 现在改成: 先用 Foote novelty 找出**真实的**边界 (自相似矩阵的突变点),
        # 再用凝聚聚类在 novelty 约束下细化.
        boundaries, method = _detect_boundaries(feat, duration, sr)
        out["method"] = method

        segs: List[Dict[str, Any]] = []
        for i in range(len(boundaries) - 1):
            s, e = int(boundaries[i]), int(boundaries[i + 1])
            if e <= s:
                continue
            seg_chroma = chroma[:, s:e].mean(axis=1)
            seg_rms = float(np.mean(rms_db[s:e])) if len(rms_db) > s else -120.0
            start = float(librosa.frames_to_time(s, sr=sr, hop_length=512))
            end = float(librosa.frames_to_time(e, sr=sr, hop_length=512))
            segs.append({
                "index": i,
                "start_sec": round(start, 2),
                "end_sec": round(end, 2),
                "duration_sec": round(max(0.0, end - start), 2),
                "energy_db": round(seg_rms, 2),
                "chord": _chord_from_chroma(seg_chroma),
                "dominant_pitch_class": PITCH_CLASSES[int(np.argmax(seg_chroma))] if np.any(seg_chroma) else "",
            })
        # 合并过短的段
        segs = _merge_short_segments(segs, duration)
        # 段落能量分级 (相对全曲)
        if segs:
            levels = [s["energy_db"] for s in segs]
            lo, hi = min(levels), max(levels)
            span = max(hi - lo, 1e-6)
            for s in segs:
                ratio = (s["energy_db"] - lo) / span
                if ratio >= 0.66:
                    s["energy_level"] = "high"
                elif ratio >= 0.33:
                    s["energy_level"] = "mid"
                else:
                    s["energy_level"] = "low"
        out["segments"] = segs
        out["segment_count"] = len(segs)
    except Exception as e:
        logger.warning(f"[咏唱参考生音乐] 段落切分失败, 降级: {e}")
        out["method"] = "degraded_single_segment"
        out["segments"] = [{
            "index": 0, "start_sec": 0.0, "end_sec": round(duration, 2),
            "duration_sec": round(duration, 2), "energy_db": -120.0,
            "chord": "", "dominant_pitch_class": "", "energy_level": "unknown",
        }]
        out["segment_count"] = 1
    return out


def _merge_short_segments(segs: List[Dict[str, Any]], duration: float) -> List[Dict[str, Any]]:
    """把短于 SEGMENT_MIN_SECONDS 的段并入相邻段.

    合并规则:
      - 段太短 → 并入**前一段**;
      - 若是**第一段**且太短 → 并入**后一段** (开头半秒的碎片不该单独成段;
        旧实现会把它留成独立段, 实测在 12s 样本上切出 "(0.0, 0.37)" 这种假段);
      - 末尾太短的段 → 并回前一段.
    """
    if not segs:
        return segs
    merged: List[Dict[str, Any]] = []
    for s in segs:
        if merged and s["duration_sec"] < SEGMENT_MIN_SECONDS:
            prev = merged[-1]
            prev["end_sec"] = s["end_sec"]
            prev["duration_sec"] = round(prev["end_sec"] - prev["start_sec"], 2)
            if s.get("chord") and not prev.get("chord"):
                prev["chord"] = s["chord"]
            continue
        merged.append(dict(s))
    # 开头太短的段 → 并入后一段
    while len(merged) > 1 and merged[0]["duration_sec"] < SEGMENT_MIN_SECONDS:
        head = merged.pop(0)
        nxt = merged[0]
        nxt["start_sec"] = head["start_sec"]
        nxt["duration_sec"] = round(nxt["end_sec"] - nxt["start_sec"], 2)
    # 末尾太短的段 → 并回前一段
    while len(merged) > 1 and merged[-1]["duration_sec"] < SEGMENT_MIN_SECONDS:
        tail = merged.pop()
        prev = merged[-1]
        prev["end_sec"] = tail["end_sec"]
        prev["duration_sec"] = round(prev["end_sec"] - prev["start_sec"], 2)
    # 重排 index
    for i, s in enumerate(merged):
        s["index"] = i
    return merged


# ==================== 风格族推断 (替代"看图列乐器") ====================

def infer_style_hints(features: Dict[str, Any]) -> List[str]:
    """从数值特征推出**风格族**描述 —— 替代旧的"看 spectrogram PNG 列乐器".

    这里刻意只给"族/倾向"而不是具体乐器名: 数值特征能可靠区分
    "低频厚重 vs 高频明亮", 但**不能**可靠区分 "电钢琴 vs 钢片琴".
    给具体乐器名会是伪装成事实的猜测 —— 所以只给方向, 让 hermes 在这个方向上
    选合理的具体乐器, 并要求它在 extra 里说明依据.
    """
    hints: List[str] = []
    spec = features.get("spectrum") or {}
    tex = features.get("texture") or {}
    energy = features.get("energy") or {}
    tempo = features.get("tempo") or {}
    voice = features.get("vocal_proxy") or {}
    bands = spec.get("band_energy_pct") or {}

    perc = float(tex.get("percussive_ratio") or 0.0)
    harm = float(tex.get("harmonic_ratio") or 0.0)
    low = float(bands.get("sub_bass") or 0.0) + float(bands.get("bass") or 0.0)
    air = float(bands.get("air") or 0.0)
    bright = float(bands.get("high_mid") or 0.0) + air
    rate = float(tex.get("onset_rate_per_sec") or 0.0)
    centroid = float(spec.get("centroid_hz") or 0.0)

    if perc >= 0.42:
        hints.append(f"打击/节奏成分占比高 ({perc:.0%}) → 偏节奏驱动编配 (鼓组/打击乐是骨架)")
    elif perc <= 0.22:
        hints.append(f"打击成分占比低 ({perc:.0%}) → 偏旋律/和声织体 (节奏靠和声与音色而非鼓点)")
    if harm >= 0.55:
        hints.append(f"和声成分占比高 ({harm:.0%}) → 旋律性器乐 (钢琴/弦乐/合成器铺底) 主导")
    if low >= 0.40:
        hints.append(f"低频占比高 ({low:.0%}) → 低音与低音鼓厚重, 适合偏低沉/律动强的风格")
    elif low <= 0.20:
        hints.append(f"低频占比低 ({low:.0%}) → 编制偏轻, 低音可以更克制")
    if air < 0.02:
        hints.append(f"高频空气感很弱 ({air:.1%}) → 少镲片/明亮合成器, 避免写明亮的金属音色")
    elif bright >= 0.22:
        hints.append(f"中高频明亮 ({bright:.0%}) → 可用明亮音色 (吉他/键盘/铃类) 与空间混响")
    if centroid:
        if centroid < 1200:
            hints.append(f"谱质心偏低 ({centroid:.0f} Hz) → 音色厚重温暖")
        elif centroid >= 3200:
            hints.append(f"谱质心偏高 ({centroid:.0f} Hz) → 音色明亮通透")
    if rate >= 4.0:
        hints.append(f"起音密度高 ({rate:.1f}/秒) → 节奏密集/律动强")
    elif rate <= 1.0:
        hints.append(f"起音密度低 ({rate:.1f}/秒) → 节奏松弛/抒情")
    if energy.get("dynamic_class") == "compressed":
        hints.append("动态范围窄 (压得较紧) → 现代混音/母带偏响, 情绪表达靠音色不靠音量起伏")
    elif energy.get("dynamic_class") == "wide":
        hints.append("动态范围宽 → 段落强弱对比明显, 有明显的强弱起伏空间")
    if energy.get("trend") == "builds_up":
        hints.append("能量自前向后递增 → 有渐进/递进结构")
    elif energy.get("trend") == "decays":
        hints.append("能量自前向后递减 → 有收束/淡出结构")
    if voice.get("likely_has_lead_vocal"):
        hints.append(f"人声频段有稳定和声成分 (置信 {voice.get('confidence')}) → 很可能有主唱人声")
    bpm = float(tempo.get("bpm") or 0)
    if bpm >= 120:
        hints.append(f"偏快板 ({bpm:.0f} BPM) → 适合向上/热烈/律动型情绪")
    elif 0 < bpm < 90:
        hints.append(f"偏慢板 ({bpm:.0f} BPM) → 适合安静/深情/叙事型情绪")
    return hints


# ==================== 中文摘要 (给 hermes 读) ====================

_SECTION_LABEL = {
    "high": "强", "mid": "中", "low": "弱", "unknown": "未知",
}


def build_chinese_summary(features: Dict[str, Any]) -> str:
    """把数值特征压成一段中文摘要 —— 注入提示词, 供 hermes 纯文本推理."""
    lines: List[str] = []
    tempo = features.get("tempo") or {}
    key = features.get("key") or {}
    energy = features.get("energy") or {}
    spec = features.get("spectrum") or {}
    tex = features.get("texture") or {}
    struct = features.get("structure") or {}

    bpm = float(tempo.get("bpm") or 0)
    if bpm:
        lines.append(f"- 节奏: {bpm:.0f} BPM (librosa 节拍检测, 检出 {tempo.get('beat_count', 0)} 个节拍)"
                     f", 起音密度 {tex.get('onset_rate_per_sec', 0)}/秒")
    else:
        lines.append("- 节奏: 未检出稳定节拍 (纯音乐/自由节奏/素材过短)")
    if key.get("pitch_class"):
        lines.append(f"- 调式: {key['pitch_class']} (相关度 {key.get('confidence', 0):.2f}; "
                     f"主要音级 {', '.join(p['pitch_class'] for p in key.get('top_pitch_classes', [])[:3])})")
    else:
        lines.append("- 调式: 未能判定")

    segs = struct.get("segments") or []
    if segs:
        desc = "; ".join(
            f"{s['start_sec']:.0f}-{s['end_sec']:.0f}s {s.get('chord') or '和弦未知'}"
            f"/{_SECTION_LABEL.get(s.get('energy_level', 'unknown'), '未知')}能"
            for s in segs[:12]
        )
        lines.append(f"- 段落: 共 {struct.get('segment_count', len(segs))} 段 [{desc}]")
    else:
        lines.append("- 段落: 未切分")

    lines.append(f"- 动态: 平均电平 {energy.get('mean_db', 0):.1f} dB, 动态范围 "
                 f"{energy.get('dynamic_range_db', 0):.1f} dB ({energy.get('dynamic_class', 'unknown')}), "
                 f"整体走向 {energy.get('trend', 'unknown')}")
    bands = spec.get("band_energy_pct") or {}
    if bands:
        band_txt = ", ".join(f"{k} {float(v):.0%}" for k, v in bands.items())
        lines.append(f"- 频谱: 质心 {spec.get('centroid_hz', 0):.0f} Hz ({spec.get('brightness', 'unknown')}), "
                     f"频段占比 [{band_txt}]")
    lines.append(f"- 质感: 和声 {tex.get('harmonic_ratio', 0):.0%} / 打击 {tex.get('percussive_ratio', 0):.0%}, "
                 f"噪声感 {tex.get('noisiness', 'unknown')}, 静音占比 {tex.get('silence_ratio', 0):.0%}")
    voice = features.get("vocal_proxy") or {}
    lines.append(f"- 人声线索: 人声频段和声占比 {voice.get('voice_band_harmonic_ratio', 0):.0%}, "
                 f"置信 {voice.get('confidence', 0)} → "
                 f"{'疑似有主唱' if voice.get('likely_has_lead_vocal') else '未检出明显主唱'}"
                 f" ({voice.get('note', '')})")
    hints = features.get("style_hints") or []
    if hints:
        lines.append("- 风格族推断依据:")
        lines.extend(f"  · {h}" for h in hints)
    return "\n".join(lines)


# ==================== 对外主入口 ====================

def _json_default(obj):
    """JSON 兜底序列化 —— numpy 标量/数组 (float32 等) 必须显式转 Python 原生类型.

    没有这个, 特征里任何一个 numpy 残留都会让整个 JSON 块变成 "{}",
    主调用就拿不到数值细节 —— 这是必须堵死的静默失败.
    """
    if hasattr(obj, "item") and callable(getattr(obj, "item")):
        try:
            return obj.item()
        except Exception:
            pass
    if hasattr(obj, "tolist") and callable(getattr(obj, "tolist")):
        try:
            return obj.tolist()
        except Exception:
            pass
    return str(obj)


def analyze_audio_numeric(audio_path: str, duration: Optional[float] = None) -> Dict[str, Any]:
    """**唯一的**音频分析入口 —— 本地数值化, 不产生任何图片, 不触发视觉模型.

    返回结构:
      {
        "analysis_ok": bool,
        "degraded": [ ... 降级的字段名 ... ],
        "duration_sec": float,
        "tempo": {...}, "key": {...}, "energy": {...},
        "spectrum": {...}, "texture": {...}, "vocal_proxy": {...},
        "structure": {...},
        "style_hints": [ str, ... ],
        "summary_zh": str,
        "elapsed_sec": float,
      }
    """
    import time

    t0 = time.time()
    degraded: List[str] = []
    out: Dict[str, Any] = {
        "analysis_ok": False,
        "degraded": degraded,
        "duration_sec": 0.0,
        "tempo": {"bpm": 0.0},
        "key": {},
        "energy": {},
        "spectrum": {},
        "texture": {},
        "vocal_proxy": {},
        "structure": {"segment_count": 0, "segments": []},
        "style_hints": [],
        "summary_zh": "",
        "elapsed_sec": 0.0,
    }
    if not NUMERIC_ANALYSIS_OK:
        degraded.append(f"dependencies_unavailable({_DEPS_ERROR})")
        out["summary_zh"] = "(数值化分析不可用: 依赖缺失, 已降级为仅时长信息)"
        return out

    y, sr = load_audio(audio_path)
    if y is None or sr <= 0:
        degraded.append("audio_load_failed")
        out["summary_zh"] = "(数值化分析失败: 音频无法载入)"
        return out

    # 分析时长上限 —— 宁可少分析, 也不让本地分析成为新的阻塞点
    max_samples = int(ANALYSIS_MAX_SECONDS * sr)
    analyzed_sec = len(y) / float(sr)
    if len(y) > max_samples:
        y = y[:max_samples]
        analyzed_sec = ANALYSIS_MAX_SECONDS
        degraded.append(f"truncated_to_{int(ANALYSIS_MAX_SECONDS)}s")
    if analyzed_sec < ANALYSIS_MIN_SECONDS:
        degraded.append("too_short")
        out["duration_sec"] = round(analyzed_sec, 3)
        out["summary_zh"] = f"(素材仅 {analyzed_sec:.1f}s, 过短, 未做节拍/段落分析)"
        return out

    if duration is None:
        duration = analyzed_sec
    out["duration_sec"] = round(float(duration), 3)

    out["tempo"] = detect_tempo(y, sr)
    out["key"] = detect_key(y, sr)
    out["energy"] = detect_energy(y, sr)
    out["spectrum"] = detect_spectrum(y, sr)
    out["texture"] = detect_texture(y, sr)
    out["vocal_proxy"] = detect_vocal_proxy(y, sr)
    out["structure"] = detect_structure(y, sr, analyzed_sec)

    if not out["tempo"].get("bpm"):
        degraded.append("tempo")
    if not out["key"].get("pitch_class"):
        degraded.append("key")
    if not out["structure"].get("segments"):
        degraded.append("structure")

    out["style_hints"] = infer_style_hints(out)
    out["summary_zh"] = build_chinese_summary(out)
    out["analysis_ok"] = True
    out["elapsed_sec"] = round(time.time() - t0, 3)
    logger.info(
        f"[咏唱参考生音乐] 数值化音频分析完成: {duration:.1f}s 音频, "
        f"BPM {out['tempo'].get('bpm')}, 调式 {out['key'].get('pitch_class')}, "
        f"{out['structure'].get('segment_count')} 段, 耗时 {out['elapsed_sec']:.2f}s"
        + (f", 降级字段 {degraded}" if degraded else "")
    )
    return out


def features_to_prompt_block(features: Dict[str, Any]) -> str:
    """把数值特征渲染成**提示词里的文本块** (JSON + 中文摘要).

    主调用只读这个文本块 —— 不读图片, 不调视觉模型.
    """
    import json as _json

    try:
        payload = _json.dumps(features, ensure_ascii=False, indent=2, default=_json_default)
    except Exception as e:
        logger.warning(f"[咏唱参考生音乐] 特征 JSON 序列化失败: {e}")
        payload = "{}"
    summary = features.get("summary_zh") or ""
    return (
        "# 音频数值化分析结果 (程序本地用 librosa/ffprobe 直接算出, 是硬事实)\n"
        f"{summary}\n\n"
        "# 同一份结果的完整 JSON (数值细节, 需要时看这里)\n"
        "```json\n"
        f"{payload}\n"
        "```"
    )


__all__ = [
    "ANALYSIS_SR",
    "NUMERIC_ANALYSIS_OK",
    "analyze_audio_numeric",
    "build_chinese_summary",
    "clear_audio_cache",
    "detect_energy",
    "detect_key",
    "detect_spectrum",
    "detect_structure",
    "detect_tempo",
    "detect_texture",
    "detect_vocal_proxy",
    "features_to_prompt_block",
    "infer_style_hints",
    "load_audio",
]
