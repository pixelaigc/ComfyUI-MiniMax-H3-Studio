# -*- coding: utf-8 -*-
"""
H3 Studio 资产素材库 —— 可插拔内容分析器。

设计：
  * 每种媒体类型一个 Analyzer（image / video / audio）。
  * Analyzer 只负责「准备要喂给模型的材料」（抽帧、波形、文本提示），
    真正的模型调用统一走 h3studio.llm.call_provider（复用本地模型 Provider 配置）。
  * **音频分析器是可插拔的**：默认实现只做「基础描述 + 预留扩展点」，
    深度分析（BPM/乐器/人声/ASR 带时间戳）由另一任务产出方案与脚本后接入。
    接入方式见 AudioAnalyzer.set_backend()。

多轮交互纠错：
  用户针对分析结果继续追问/纠正，模型基于图片 + 对话上下文重新给结论，
  每次都写入 asset_analyses 表（revision 递增），保留修订历史。
"""
import json
import subprocess
from pathlib import Path

from . import llm as llm_client
from . import media_probe
from .assets import LibraryError

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ------------------------------------------------------------------ 提示词

IMAGE_SYSTEM = """你是专业的美术/影视素材分析师。你要对中国用户给出的图片素材做**详尽**的内容描述，供后续生图、生视频、生音频时作为快捷引用。

要求：
1. **全中文**，尽量不遗漏任何细节。
2. 按固定小节组织，缺省的小节写「无」：
   【主体人物】【服装与配饰】【发型与妆容】【表情与神态】【动作与姿态】
   【场景与环境】【光线与色彩】【构图与镜头】【画面风格与质感】【文字与标识】【其他值得注意的细节】
3. 人物细节要精确到配饰形状与颜色（例如「胸口红色菱形挂件」而不是「有个挂件」）。
4. 只描述**画面里真实存在的**内容，不要脑补剧情与设定。
5. 不要输出英文段落，术语用中文（必要时括注英文原词）。
"""

VIDEO_SYSTEM = """你是专业的影视素材分析师。下面给你的是同一段视频按时间顺序抽取的多帧图片，以及每帧对应的时间点。

请基于这些帧做**详尽**的中文内容描述，供后续生图、生视频时作为快捷引用。

要求：
1. **全中文**，尽量不遗漏细节。
2. 按固定小节组织，缺省写「无」：
   【整体概述】【分镜与时间线】【主体人物】【服装与配饰】【发型与妆容】【表情与神态】
   【动作与运镜】【场景与环境】【光线与色彩】【画面风格与质感】【字幕与对白】【其他值得注意的细节】
3. 「分镜与时间线」必须标注每个镜头的时间点（如 `00:03 切到…`）。
4. 只描述帧里真实可见的内容，看不清就写「该帧无法辨认」。
5. 不要输出英文段落。
"""

AUDIO_SYSTEM = """你是专业的音频素材分析师。下面给你的是一段音频的基本技术信息。

请基于这些信息做中文描述，供后续生音频、配音、编曲时作为快捷引用。

要求：
1. **全中文**。
2. 按固定小节组织，缺省写「无（当前分析器未提供该维度）」：
   【整体概述】【时长与结构】【音色与质感】【节奏与速度】【空间与混响】【可能的内容推测】【可用于的创作场景】
3. 不要编造具体的歌词或对白内容 —— 没有 ASR 结果时如实说明。
"""

CORRECTION_TEMPLATE = """用户对上一版分析提出了纠正或追问：

「{question}」

请基于**同一份素材**（下方给出的图片/帧）与之前的分析结论，重新给出修正后的完整描述。

要求：
1. 采纳用户指出的正确信息（如「不是椭圆形，是菱形」），并把它写进对应的小节。
2. 保留之前版本里没有出错的细节，不要因为局部纠正就丢掉其它正确内容。
3. 仍然按与上一版相同的小节结构输出，全中文。
4. 不要输出「已根据您的纠正修改」这类元叙述，直接给最终描述。
"""


# ------------------------------------------------------------------ 分析器基类


class Analyzer:
    """分析器基类。子类实现 prepare() 与 kind。"""

    kind = "generic"

    def prepare(self, asset: dict, version: dict, source: Path,
                options: dict) -> dict:
        """
        准备分析材料。

        返回 dict：
          images     喂给模型的图片路径列表
          prompt     本次给模型的 user 消息
          extra      附加信息（写进分析记录，供 UI 展示）
        """
        raise NotImplementedError

    def system_prompt(self) -> str:
        return "你是专业的素材分析师，请用中文详尽描述素材内容。"


class ImageAnalyzer(Analyzer):
    kind = "image"

    def system_prompt(self) -> str:
        return IMAGE_SYSTEM

    def prepare(self, asset, version, source, options):
        return {
            "images": [str(source)],
            "prompt": (
                f"请分析这张图片素材。\n"
                f"文件名：{asset.get('name') or source.name}\n"
                f"尺寸：{version.get('width') or '未知'}×{version.get('height') or '未知'}\n"
                f"格式：{version.get('container') or version.get('mime') or '未知'}\n\n"
                f"请按系统提示里的小节结构输出详尽的中文描述。"),
            "extra": {"width": version.get("width"), "height": version.get("height")},
        }


class VideoAnalyzer(Analyzer):
    kind = "video"

    def system_prompt(self) -> str:
        return VIDEO_SYSTEM

    def prepare(self, asset, version, source, options):
        interval = float(options.get("frame_interval")
                         or media_probe.DEFAULT_FRAME_INTERVAL)
        max_frames = int(options.get("max_frames") or media_probe.MAX_FRAMES)
        derived = options.get("derived_dir")
        if derived is None:
            return {"images": [], "prompt": "", "extra": {"error": "缺少派生目录"},
                    "frames": []}
        frames = media_probe.extract_frames(
            source, Path(derived), prefix="frame",
            interval=interval, max_frames=max_frames)
        timeline = "、".join(f"{f['index']}@{f['t']}s" for f in frames)
        duration = version.get("duration_sec")
        prompt = (
            f"请分析这段视频素材（共 {len(frames)} 帧）。\n"
            f"文件名：{asset.get('name') or source.name}\n"
            f"分辨率：{version.get('width') or '未知'}×{version.get('height') or '未知'}\n"
            f"时长：{duration if duration is not None else '未知'} 秒\n"
            f"帧率：{version.get('fps') or '未知'}\n"
            f"编码：{version.get('codec') or '未知'}\n"
            f"抽帧时间点：{timeline or '无'}\n\n"
            f"请按系统提示里的小节结构输出详尽的中文描述。")
        return {
            "images": [f["path"] for f in frames],
            "prompt": prompt,
            "frames": frames,
            "extra": {"frame_interval": interval, "max_frames": max_frames,
                      "frame_count": len(frames),
                      "timeline": timeline},
        }


class AudioAnalyzer(Analyzer):
    """
    音频分析器 —— **可插拔**。

    当前默认实现只做基础描述（技术信息 + 通用分析）。
    深度分析（BPM / 乐器 / 人声 / ASR 带时间戳）由独立方案产出后接入：

        from h3studio import analysis
        analysis.AudioAnalyzer.set_backend(MyDeepAudioBackend)

    backend 接口约定（鸭子类型）：
        class Backend:
            name: str                      # 展示用的分析器名
            def analyze(self, path, options) -> dict:
                # 返回 {"bpm":..., "key":..., "instruments":[...],
                #       "vocals":..., "asr":[{"start":..,"end":..,"text":..}],
                #       "summary": "...", ...}
    """
    kind = "audio"

    _backend = None

    def system_prompt(self) -> str:
        return AUDIO_SYSTEM

    @classmethod
    def set_backend(cls, backend) -> None:
        cls._backend = backend

    @classmethod
    def backend(cls):
        return cls._backend

    def prepare(self, asset, version, source, options):
        extra = {
            "duration_sec": version.get("duration_sec"),
            "sample_rate": version.get("sample_rate"),
            "channels": version.get("channels"),
            "codec": version.get("audio_codec") or version.get("codec"),
        }
        backend = self.backend()
        if backend is not None:
            try:
                result = backend.analyze(str(source), options) or {}
                extra.update(result)
                extra["backend"] = getattr(backend, "name", "custom")
            except Exception as exc:
                extra["backend_error"] = f"{type(exc).__name__} {exc}"[:200]

        lines = [
            f"文件名：{asset.get('name') or source.name}",
            f"时长：{version.get('duration_sec') if version.get('duration_sec') is not None else '未知'} 秒",
            f"音频编码：{version.get('audio_codec') or version.get('codec') or '未知'}",
            f"采样率：{version.get('sample_rate') or '未知'} Hz",
            f"声道数：{version.get('channels') or '未知'}",
            f"容器：{version.get('container') or '未知'}",
        ]
        for key, label in (("bpm", "BPM"), ("key", "调性"), ("instruments", "乐器"),
                           ("vocals", "人声"), ("structure", "结构")):
            value = extra.get(key)
            if value:
                lines.append(f"{label}：{value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)}")
        asr = extra.get("asr")
        if asr:
            rows = []
            for item in asr:
                if isinstance(item, dict):
                    rows.append(f"[{item.get('start', '?')}s-{item.get('end', '?')}s] {item.get('text', '')}")
                else:
                    rows.append(str(item))
            lines.append("语音识别（含时间戳）：\n" + "\n".join(rows))
        else:
            lines.append("语音识别：当前分析器未提供 ASR 结果（如需请接入深度音频分析器）")

        prompt = ("请分析这段音频素材。\n" + "\n".join(lines)
                  + "\n\n请按系统提示里的小节结构输出详尽的中文描述。")
        return {"images": [], "prompt": prompt, "extra": extra, "frames": []}


ANALYZERS = {
    "image": ImageAnalyzer(),
    "video": VideoAnalyzer(),
    "audio": AudioAnalyzer(),
}


def get_analyzer(kind: str) -> Analyzer:
    analyzer = ANALYZERS.get(kind)
    if analyzer is None:
        raise LibraryError(f"暂不支持对 {kind} 类型素材做内容分析", status=400)
    return analyzer


# ------------------------------------------------------------------ Provider 选择


async def pick_provider(conn, provider_id: str | None, require_multimodal: bool = True):
    """
    取分析用的 Provider：优先用调用方指定的，否则用「本地模型配置」里的默认多模态模型。

    复用 h3studio.providers（工作台已有的本地模型 Provider 配置面板），
    不另起一套配置。
    """
    from . import providers as prov

    if provider_id:
        provider = prov.get_provider_full(conn, provider_id)
        if not provider:
            raise LibraryError(f"指定的 Provider 不存在：{provider_id}", status=400)
        if not provider.get("enabled"):
            raise LibraryError(f"Provider「{provider['name']}」已停用", status=400)
        if require_multimodal and not provider.get("multimodal"):
            raise LibraryError(
                f"Provider「{provider['name']}」未标记多模态能力，无法读图。"
                "请在本地模型配置里勾选「多模态」，或改用默认多模态模型。", status=400)
        return provider

    provider = prov.resolve_default(conn, "default_multimodal", require_multimodal=True)
    if provider is None:
        provider = prov.resolve_default(conn, "default", require_multimodal=False)
    if provider is None:
        raise LibraryError(
            "还没有可用的本地模型 Provider。请到「本地模型配置」里添加一个"
            "（分析素材需要多模态模型），再回到素材库触发分析。", status=400)
    return provider


# ------------------------------------------------------------------ 分析执行


async def run_analysis(conn, asset: dict, version: dict, source: Path,
                       options: dict | None = None,
                       provider_id: str | None = None) -> dict:
    """
    执行一次分析（首轮）。

    返回 {"text":..., "revision":..., "provider":..., "frames":[...], "extra":{...]}
    失败时抛 LibraryError（带可读原因），调用方负责写失败记录。
    """
    options = options or {}
    kind = asset.get("category") if asset.get("category") in ANALYZERS else version.get("kind")
    if kind not in ANALYZERS:
        # 依据 mime 再兜一次
        mime = (version.get("mime") or "").lower()
        if mime.startswith("image/"):
            kind = "image"
        elif mime.startswith("video/"):
            kind = "video"
        elif mime.startswith("audio/"):
            kind = "audio"
        else:
            raise LibraryError(
                f"暂不支持对该类型素材做内容分析（{asset.get('category')}）", status=400)

    analyzer = get_analyzer(kind)
    provider = await pick_provider(conn, provider_id, require_multimodal=True)

    prepared = analyzer.prepare(asset, version, source, options)
    images = prepared.get("images") or []
    if kind in ("image", "video") and not images:
        raise LibraryError("未能准备分析用的图片（源文件可能已损坏或格式不支持）", status=400)

    messages = [
        {"role": "system", "content": analyzer.system_prompt()},
        {"role": "user", "content": prepared["prompt"]},
    ]
    try:
        text = await llm_client.call_provider(
            provider, messages, images=images,
            options={"temperature": 0.3, "max_tokens": 4096})
    except llm_client.ProviderError as exc:
        raise LibraryError(f"模型调用失败：{exc}", status=502, code="provider_error") from exc

    return {
        "text": text.strip(),
        "provider": provider,
        "kind": kind,
        "frames": prepared.get("frames") or [],
        "extra": prepared.get("extra") or {},
        "prompt": prepared["prompt"],
    }


async def run_correction(conn, asset: dict, version: dict, source: Path,
                         question: str, options: dict | None = None,
                         provider_id: str | None = None) -> dict:
    """
    多轮交互纠错：基于素材 + 历史分析结论重新给出修正版描述。

    历史上下文 = 该素材此前的所有分析记录（按 revision 升序），
    以 user/assistant 对话形式拼进 messages，模型据此保留未被纠正的细节。
    """
    question = (question or "").strip()
    if not question:
        raise LibraryError("请填写要纠正或追问的内容", status=400)

    options = options or {}
    kind = asset.get("category") if asset.get("category") in ANALYZERS else None
    if kind is None:
        mime = (version.get("mime") or "").lower()
        kind = ("image" if mime.startswith("image/") else
                "video" if mime.startswith("video/") else
                "audio" if mime.startswith("audio/") else None)
    if kind not in ANALYZERS:
        raise LibraryError("该类型素材暂不支持交互纠错", status=400)

    analyzer = get_analyzer(kind)
    provider = await pick_provider(conn, provider_id, require_multimodal=True)

    prepared = analyzer.prepare(asset, version, source, options)
    images = prepared.get("images") or []
    if kind in ("image", "video") and not images:
        raise LibraryError("未能准备分析用的图片（源文件可能已损坏或格式不支持）", status=400)

    # 历史对话：把上一版分析作为 assistant 结论，用户的新诉求作为 user 消息
    from . import asset_repo
    history = sorted(asset_repo.analyses_of(conn, asset["id"]),
                     key=lambda item: (item["revision"], item["id"]))
    messages = [{"role": "system", "content": analyzer.system_prompt()}]
    for item in history:
        if not item.get("text"):
            continue
        messages.append({"role": "user", "content": "请给出这个素材的详尽中文描述。"})
        messages.append({"role": "assistant", "content": item["text"]})
    messages.append({"role": "user",
                     "content": CORRECTION_TEMPLATE.format(question=question)})

    try:
        text = await llm_client.call_provider(
            provider, messages, images=images,
            options={"temperature": 0.3, "max_tokens": 4096})
    except llm_client.ProviderError as exc:
        raise LibraryError(f"模型调用失败：{exc}", status=502, code="provider_error") from exc

    return {
        "text": text.strip(),
        "provider": provider,
        "kind": kind,
        "frames": prepared.get("frames") or [],
        "extra": prepared.get("extra") or {},
        "prompt": question,
    }
