# -*- coding: utf-8 -*-
"""
Hermes 提示词增强通道（对齐 nonebot 插件「咏唱h3」的增强方式）。

【这个模块解决什么】
本工作台原本的提示词增强**只**走「本地 Provider + 模板」（h3studio/enhancer.py
+ h3studio/llm.py）：把 system prompt + user prompt 发给本地 llama.cpp / vLLM 端点。
用户要的是**和 nonebot 咏唱h3 一样**的做法 —— 用 hermes 调一个
「特定 profile + 特定提示词」，把用户的输入处理成专业完美的提示词。

本模块提供那条通道，并把**图像 / 视频 / 音频**三条能力统一接上去。

【与 nonebot 插件的对应关系（逐项）】
| 维度              | nonebot 咏唱h3                          | 本模块                                |
|-------------------|-----------------------------------------|---------------------------------------|
| 调用形态          | `hermes chat --query-file <f> ...`      | **完全相同**（同一个 CLI、同一组 flag） |
| profile 选择      | `HERMES_HOME=/root/.hermes/profiles/media`（反推链路）| `--profile <name>`（等价且更显式）  |
| skill 白名单      | `--skills <逗号分隔白名单>`              | **完全相同**                            |
| 输入组织          | 临时文件写 query，CLI `--query-file`     | **完全相同**                            |
| 其它 flag         | `--oneshot --cli --yolo`                 | **完全相同**                            |
| 输出解析          | 正则抠 `╭─ ⚕ Hermes ─╮ ... ╰─╯` 框     | **完全相同**（同一套正则 + 同样回退）  |
| 超时              | `subprocess.run(timeout=1800)`            | 1800s（与 nonebot 一致）               |
| 重试              | 规格校验不过 → 回灌 issues 再跑 1 次     | **完全相同**（最多 2 次）               |
| 失败回退          | 不编造兜底（safe_fallback_prompt）       | **完全相同**（不编造，只给安全骨架）    |
| 整体工作流        | 先增强 → 再进生成                       | **完全相同**（增强产物才进工作流）      |

【为什么不用 hermes api-server】
nonebot 插件的注释写得很清楚（h3_interactive.py:3719-3722）：
  "hermes api 虽然 OpenAI 兼容, 但客户端不能传 skill/toolsets, hermes 默认纯文本
   LLM, 不会调 h3-cn-prompt-director skill, 也不会 vision_analyze 读参考图.
   cli 模式 hermes 自己会调 skill, 自己 vision_analyze, 输出高质量 H3 prompt."
本模块沿用这个结论 —— **只走 CLI**。

【安全边界】
  * 只调用本机 `hermes` 可执行文件（路径可配，默认 /usr/local/bin/hermes）；
  * 只读素材（素材路径由服务端解析，外部无法指定任意路径）；
  * 不执行用户输入里的任何 shell（query 走临时文件 + `--query-file`，
    不经 shell）；
  * 失败时**如实报错**，绝不伪造「已增强」。
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

# ------------------------------------------------------------------ 默认配置

#: hermes 可执行文件（与 nonebot 插件一致用绝对路径，不依赖 PATH）
DEFAULT_HERMES_BIN = "/usr/local/bin/hermes"

#: 默认 profile。
#:
#: 【为什么选 media】nonebot 插件「咏唱反推」链路（h3_reverse_prompt.py:71）
#: 用的就是 `HERMES_HOME=/root/.hermes/profiles/media`，因为「图像/视频/音乐」
#: 这类多媒体创作 skill 都在 media profile 下（实测：
#: /root/.hermes/profiles/media/skills/{image,video,media}/）。
#: 本工作台的三条能力（图像/视频/音频）也正是多媒体创作 —— 所以默认同款。
DEFAULT_PROFILE = "media"

#: hermes 根目录（用于校验 profile 是否真实存在）
HERMES_ROOT = Path("/root/.hermes/profiles")

#: 三条能力各自的 skill 白名单 + 提示词模板。
#:
#: 【白名单是 nonebot 的同款做法】nonebot 咏唱h3（h3_interactive.py:3760-3764）：
#:     SKILL_ALLOWLIST = (
#:         "h3-video-prompt-director,h3-cn-prompt-director,"
#:         "h3-prompt-templates,h3-character-presets,h3-kinetic-subtitle,"
#:         "h3-item-strengthening"
#:     )
#: hermes 的 `--skills` 是**白名单**（不是黑名单）—— 写了什么就只加载什么，
#: 所以这里给三条能力各配一份「够用且对口」的白名单。
HERMES_MEDIA_SPECS: dict[str, dict] = {
    # ---- 图像 ----
    "image": {
        "media": "image",
        "label": "图像提示词（hermes · media profile）",
        "profile": DEFAULT_PROFILE,
        "skills": "h3-cn-prompt-director,h3-prompt-templates,image-prompt-templates",
        "system_prompt": (
            "你是一名专业的 AI 图像提示词工程师，专精为扩散模型（Z-Image / "
            "Qwen-Image / FLUX / MOSS-Image）撰写提示词。\n"
            "把用户的粗略描述扩写成一段**忠实于原意、细节饱满、可直接被扩散模型"
            "使用**的完整自然语言提示词。\n"
            "工作流程：\n"
            "1. 锁定用户提示词中不可变更的核心要素（主体、数量、动作、状态、"
            "专有名词、颜色、画面文字）；\n"
            "2. 判断是否需要「生成式推理」；\n"
            "3. 注入专业级的画面细节（环境、时间、天气、空间层次、构图）；\n"
            "4. 精确处理画面文字元素（一字不差转录并用英文双引号括起来）。\n"
            "你的最终描述必须客观、具象，严禁使用比喻、情感化修辞，"
            "也绝不包含「8K」「masterpiece」等质量元标签或绘制指令。\n"
            "仅严格输出最终的提示词，不要输出任何其他内容。"
        ),
        "user_template": "用户输入 prompt: {prompt}",
    },
    # ---- 视频 ----
    "video": {
        "media": "video",
        "label": "视频提示词（hermes · MiniMax-H3 官方三段式）",
        "profile": DEFAULT_PROFILE,
        # 与 nonebot 咏唱h3 的白名单**完全一致**（视频那条链路在 nonebot
        # 里就是这套 skill）。
        "skills": (
            "h3-video-prompt-director,h3-cn-prompt-director,"
            "h3-prompt-templates,h3-character-presets,h3-kinetic-subtitle,"
            "h3-item-strengthening"
        ),
        "system_prompt": (
            "你是一名 MiniMax H3 视频生成模型的 prompt 导演。\n"
            "把用户的简单描述 + 他提供的素材，展开成**一份符合官方 H3 规格的"
            "完整 prompt**。\n"
            "要求覆盖：\n"
            "1. 镜头语言（运镜/景别/视角）\n"
            "2. 主体细节（角色外貌/动作/表情/服装）\n"
            "3. 场景细节（环境/光照/天气/时间）\n"
            "4. 视觉风格（色调/质感/氛围）\n"
            "5. 动作时序（按时间顺序分解）\n"
            "输出格式：按 H3 官方 Context IR 结构成稿"
            "（integrated_multimodal_description / overall_soundscape / "
            "non_diegetic_music）。\n"
            "不要输出解释，仅输出最终 prompt。"
        ),
        "user_template": "用户输入 prompt: {prompt}",
    },
    # ---- 音频 ----
    "audio": {
        "media": "audio",
        "label": "音频提示词（hermes · 音乐/音效）",
        "profile": DEFAULT_PROFILE,
        "skills": "yue2-music,h3-prompt-templates,music3-caption-rewriter",
        "system_prompt": (
            "你是一位专业的 AI 音乐与音效提示词工程师。\n"
            "把用户的粗略描述扩写成一段可用于音乐/音效生成的完整提示词。\n"
            "需要覆盖：体裁、节奏、乐器、音色情绪、动态变化、混音质感、参考风格。\n"
            "你的最终描述必须客观、具象，严禁使用比喻、情感化修辞，"
            "也绝不包含「8K」「masterpiece」等质量元标签或绘制指令。\n"
            "仅严格输出最终的提示词，不要输出任何其他内容。"
        ),
        "user_template": "用户输入 prompt: {prompt}",
    },
}

#: 三条能力 -> media 键。mode -> media 的映射复用 enhancer.MODE_MEDIA
#: （那是本项目 media 维度的唯一真源）。
def media_for_mode(mode: str) -> str:
    from . import enhancer as enh
    return enh.MODE_MEDIA.get((mode or "").strip()) or ""


#: 默认超时（nonebot 咏唱h3 用 subprocess.run(timeout=1800)）
DEFAULT_TIMEOUT = 1800

#: 单次调用最多尝试次数（nonebot：attempts = 2）
MAX_ATTEMPTS = 2

#: hermes 输出里「最终答案框」的正则（与 nonebot h3_interactive.py:3807-3810 一致）
#:
#: 【与 nonebot 的唯一差别】收尾横线那一段放宽成 `─+[─=]*` ——
#: nonebot 用 `╰─[─=]+╯`（至少两根横线），真实的 hermes 输出是几十根横线，
#: 但短框（`╰─╯`）会漏匹配。这里放宽**只会多匹配、不会少匹配**，
#: 对 nonebot 的真实输出行为完全相同。
HERMES_BOX_RE = re.compile(
    r"╭─\s*⚕\s*Hermes\s*[─=]+╮\s*\n(.*?)\n╰─+[─=]*╯",
    re.DOTALL,
)

#: skill 名的合法字符（它会进 CLI argv，必须严控）
_SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

#: Write File("...") —— nonebot 的 fallback 1（h3_interactive.py:3818）
WRITE_FILE_RE = re.compile(r'Write File\("([^"]+)"\)')


class HermesEnhanceError(RuntimeError):
    """hermes 增强失败。**不编造** —— 调用方必须如实把原因报给用户。"""

    def __init__(self, message: str, *, reason: str = "", detail: str = ""):
        super().__init__(message)
        self.reason = reason
        self.detail = detail


@dataclass
class HermesResult:
    """一次 hermes 增强的结果。"""

    ok: bool
    #: 增强后的提示词（失败时为空串）
    enhanced: str = ""
    #: 原始提示词
    original: str = ""
    #: 用了哪个 profile
    profile: str = ""
    #: 用了哪些 skill（白名单）
    skills: str = ""
    #: 实际跑的 CLI 命令（去掉临时文件路径的敏感部分保留形状）
    command: str = ""
    #: 模型原始返回（Hermes 框内容）
    raw: str = ""
    #: 尝试次数
    attempts: int = 0
    #: 失败原因（reason 码 + 人类可读 detail）
    reason: str = ""
    detail: str = ""
    #: 校验 / 校验 issues
    issues: list = field(default_factory=list)
    #: 是否走了「不编造」的兜底
    degraded: bool = False


# ------------------------------------------------------------------ 配置解析


def resolve_config(saved: dict | None = None) -> dict:
    """
    读用户配置（存在 model_provider_prefs 的 "enhancer" key 里），
    合成 hermes 通道的运行配置。

    配置项（都可以在「本地模型配置」页改）：
      hermes.enabled        是否启用 hermes 增强通道（默认 **关**）
      hermes.binary        hermes 可执行文件路径
      hermes.profile       profile 名（默认 media）
      hermes.timeout       单次调用超时秒数
      hermes.skills.<media> 覆盖某条能力的 skill 白名单
      hermes.templates.<media> 覆盖某条能力的 system prompt
      hermes.user_templates.<media> 覆盖某条能力的 user 模板
    """
    saved = saved or {}
    cfg = saved.get("hermes") if isinstance(saved.get("hermes"), dict) else {}
    cfg = cfg or {}

    out = {
        "enabled": bool(cfg.get("enabled", False)),
        "binary": str(cfg.get("binary") or DEFAULT_HERMES_BIN),
        "profile": str(cfg.get("profile") or DEFAULT_PROFILE),
        "timeout": _safe_int(cfg.get("timeout"), DEFAULT_TIMEOUT),
    }
    if out["timeout"] <= 0:
        out["timeout"] = DEFAULT_TIMEOUT
    return out


def _safe_int(value, default: int) -> int:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def available_profiles() -> list[dict]:
    """
    列出本机真实存在的 hermes profile（供前端下拉）。

    只读目录 —— 不调 hermes CLI（那会启动一次完整会话，代价太大）。
    """
    out = []
    try:
        for child in sorted(HERMES_ROOT.iterdir()):
            if not child.is_dir():
                continue
            # profile 目录必须有 config.yaml 或 skills 才算真 profile
            if not (child / "config.yaml").exists() and not (child / "skills").exists():
                continue
            out.append({"id": child.name, "path": str(child)})
    except Exception:
        return out
    return out


def profile_exists(profile: str) -> bool:
    """profile 目录是否真实存在。"""
    if not profile:
        return False
    try:
        return (HERMES_ROOT / profile).is_dir()
    except Exception:
        return False


def sanitize_skills(raw: str) -> str:
    """
    规整 skill 白名单：只保留 [A-Za-z0-9_.-] 的名字，用逗号连接。

    【为什么必须过滤】skill 白名单会**直接进 CLI argv**
    （`hermes chat --skills <这里>`）—— 不夹一道，配置里一个空格或分号
    就能变成额外的 argv 元素。所以这里做**白名单过滤**（不是黑名单）：
    非法字符的整段名字直接丢掉，不做「修补」。

    依据：nonebot 插件的白名单是硬编码的常量；本工作台允许用户配置，
    所以必须自己夹。
    """
    if not raw:
        return ""
    parts = []
    for chunk in str(raw).split(","):
        name = chunk.strip()
        if not name:
            continue
        if not _SKILL_NAME_RE.match(name):
            # 非法名字整段丢弃（不修补 —— 修补会造出一个用户没写的 skill）
            continue
        parts.append(name)
    return ",".join(parts)


def get_media_spec(media: str, saved: dict | None = None) -> dict:
    """
    取某条媒体（image / video / audio）的 hermes 增强配置。

    用户可覆盖 skill 白名单 / system prompt / user 模板。
    """
    media = (media or "").strip().lower()
    base = HERMES_MEDIA_SPECS.get(media)
    if base is None:
        raise HermesEnhanceError(
            f"没有 {media} 这一条能力的 hermes 增强配置",
            reason="unknown_media", detail=f"media={media}")
    spec = dict(base)
    cfg = (saved or {}).get("hermes") if isinstance((saved or {}).get("hermes"), dict) else {}
    cfg = cfg or {}
    skills = (cfg.get("skills") or {}).get(media)
    if isinstance(skills, str) and skills.strip():
        cleaned = sanitize_skills(skills)
        # 全部被过滤掉 -> 回落到内置白名单（不能变成「没 skill」的裸调用）
        spec["skills"] = cleaned or base.get("skills", "")
    template = (cfg.get("templates") or {}).get(media)
    if isinstance(template, str) and template.strip():
        spec["system_prompt"] = template.strip()
    user_template = (cfg.get("user_templates") or {}).get(media)
    if isinstance(user_template, str) and user_template.strip():
        spec["user_template"] = user_template.strip()
    return spec


# ------------------------------------------------------------------ query 构造


def build_query(spec: dict, original: str, *, asset_context: str = "",
                prompt_history: list | None = None,
                duration_seconds: float | None = None) -> str:
    """
    构造发给 hermes 的任务描述。

    结构对齐 nonebot `_build_hermes_director_query`
    （h3_interactive.py:4237-4398）的 4 块结构：
      1. 详细任务
      2. 可参考的 skill 列表
      3. 用户的一句话要求
      4. 素材位置
    本模块在此之上多了「跨段提示词历史」与「时长」两块（nonebot 在
    `_build_hermes_director_query` 里也有 duration_seconds，见 4392 行）。
    """
    parts: list[str] = []
    original = (original or "").strip()

    # 1. 详细任务
    parts.append(
        "# 详细任务\n"
        + (spec.get("system_prompt") or "").strip()
    )

    # 2. 可参考的 skill
    skills = spec.get("skills") or ""
    if skills:
        parts.append(
            "\n# 可参考的 skill\n"
            "以下是 hermes 客户端已装好的 skill（本次已通过 --skills 白名单加载），"
            "你可以根据任务需要调用/参考：\n"
            + "\n".join(f"  - `{s.strip()}`" for s in skills.split(",") if s.strip())
            + "\n**用法**: 按上面「详细任务」的要求成稿，严格遵守「输出格式」。"
        )

    # 3. 用户的一句话要求
    user_template = spec.get("user_template") or "用户输入 prompt: {prompt}"
    parts.append("\n# 用户的一句话要求\n```\n"
                 + user_template.replace("{prompt}", original)
                              .replace("{original}", original)
                 + "\n```")

    # 3b. 跨段提示词历史（接续生成时带上此前各段）
    if prompt_history:
        lines = []
        for item in prompt_history:
            text = str((item or {}).get("prompt") or "").strip()
            if text:
                lines.append(f"  - 第 {item.get('stage')} 段：{text}")
        if lines:
            parts.append(
                "\n# 前情提要（此前各段的提示词，仅作上下文参考，"
                "本次输出**不要**重复罗列）\n" + "\n".join(lines))

    # 4. 素材位置（服务端解析出来的素材上下文）
    if asset_context:
        parts.append("\n# 素材上下文\n" + asset_context.strip())

    # 5. 时间轴事实
    if duration_seconds:
        parts.append(f"\n# 目标时长\n- 目标视频时长 {duration_seconds:g} 秒")

    # 6. 输出格式（硬约束）
    parts.append(
        "\n# 输出格式（硬约束，违反 = 任务失败）\n"
        "只输出**最终提示词正文**，不要输出任何解释、标题、markdown 代码块围栏、"
        "或「提示词：」这类前缀。不要复述本任务描述。"
    )
    return "\n".join(parts)


# ------------------------------------------------------------------ CLI 调用


def _extract_answer(stdout: str, stderr: str = "") -> str:
    """
    从 hermes CLI 输出里提取最终答案。

    解析顺序与 nonebot 插件**完全一致**（h3_interactive.py:3806-3835）：
      1. 正则抠 Hermes 框；多个框时取**最后一个**（含完整 prompt 的那个）；
         若某个框里含「00:00.000」或「可直接复制」，取其中最长的。
      2. fallback 1：stdout 里的 Write File("...") 路径 → 读文件
      3. fallback 2：rc == 0 且 stdout 非空 → 取尾部 2000 字符
      4. 都不行 → raise
    """
    matches = HERMES_BOX_RE.findall(stdout or "")
    if matches:
        text = matches[-1].strip()
        prompt_like = [t for t in matches
                       if "00:00.000" in t or "可直接复制" in t]
        if prompt_like:
            text = max(prompt_like, key=len).strip()
        return text

    # fallback 1: Write File 路径
    wf = WRITE_FILE_RE.search(stdout or "")
    if wf:
        path = Path(wf.group(1))
        try:
            if path.exists():
                content = path.read_text(encoding="utf-8", errors="ignore").strip()
                if content and len(content) > 100:
                    return content
        except Exception:
            pass

    # fallback 2: stdout 尾部
    if (stdout or "").strip():
        return stdout.strip()[-2000:]

    raise HermesEnhanceError(
        "hermes 没有返回可解析的内容",
        reason="empty_output",
        detail=f"stderr={(stderr or '')[:300]}")


def _run_cli(binary: str, profile: str, query_path: str, skills: str,
             timeout: int) -> tuple[str, str, int]:
    """
    跑一次 hermes CLI。**同步**（调用方用 asyncio.to_thread 包）。

    返回 (stdout, stderr, returncode)。
    抛 subprocess.TimeoutExpired / FileNotFoundError 由调用方处理。
    """
    import subprocess

    cmd = [binary]
    # `--profile` 是 hermes 的**全局** flag，必须放在子命令 `chat` **之前**
    # （hermes_cli/_parser.py: PRE_ARGPARSE_INHERITED_FLAGS）。
    if profile:
        cmd += ["--profile", profile]
    cmd += [
        "chat",
        "--query-file", query_path,
    ]
    if skills:
        cmd += ["--skills", skills]
    cmd += ["--oneshot", "--cli", "--yolo"]

    # 子进程环境：清掉可能继承来的桥接上下文（nonebot h3_reverse_prompt.py:640-652
    # 的同款处理），否则子 agent 会继承父 session 的平台上下文。
    env = os.environ.copy()
    for var in (
        "HERMES_SESSION_ID",
        "HERMES_UI_SESSION_ID",
        "HERMES_SESSION_PLATFORM",
        "HERMES_DELEGATED_CHILD_CONTEXT",
        "HERMES_SESSION_SCOPE_ID",
        "HERMES_SESSION_CHAT_ID",
        "HERMES_AGENT_BRIDGE_WORKER_PROFILE",
    ):
        env.pop(var, None)

    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout,
        env=env, stdin=subprocess.DEVNULL,
    )
    return proc.stdout or "", proc.stderr or "", proc.returncode


async def enhance(original: str, media: str, *, saved: dict | None = None,
                   asset_context: str = "", prompt_history: list | None = None,
                   duration_seconds: float | None = None,
                   validate: bool = True) -> HermesResult:
    """
    走 hermes CLI 做提示词增强（图像 / 视频 / 音频三条链路共用）。

    与 nonebot 咏唱h3 一致的流程：
      1. 构造 query（4~6 块结构）
      2. 写临时文件 → `hermes chat --query-file ... --oneshot --cli --yolo`
      3. 抠 Hermes 框拿答案
      4. 校验（可选）：不过就**回灌 issues** 重跑一次（最多 2 次）
      5. 全失败 → 抛 HermesEnhanceError（**不编造**）

    失败时抛异常（而不是返回假结果）—— 调用方负责降级并如实告知用户。
    """
    import subprocess

    original = (original or "").strip()
    if not original:
        raise HermesEnhanceError("提示词为空", reason="empty_prompt")

    cfg = resolve_config(saved)
    spec = get_media_spec(media, saved)

    binary = cfg["binary"]
    if not (os.path.isfile(binary) and os.access(binary, os.X_OK)):
        # 退回 PATH 查找（hermes 可能装在别处）
        found = shutil.which("hermes")
        if not found:
            raise HermesEnhanceError(
                f"找不到 hermes 可执行文件（配置路径：{binary}）",
                reason="hermes_not_found", detail=binary)
        binary = found

    profile = cfg["profile"]
    if not profile_exists(profile):
        raise HermesEnhanceError(
            f"hermes profile「{profile}」不存在（目录：{HERMES_ROOT / profile}）",
            reason="profile_not_found", detail=profile)

    query = build_query(spec, original, asset_context=asset_context,
                         prompt_history=prompt_history,
                         duration_seconds=duration_seconds)

    # ---- 写 query 临时文件（nonebot 同款：NamedTemporaryFile + delete=False）----
    fd, query_path = tempfile.mkstemp(prefix="h3studio_hermes_", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(query)
    except Exception as exc:
        try:
            os.unlink(query_path)
        except OSError:
            pass
        raise HermesEnhanceError(
            f"写 hermes query 文件失败：{exc}", reason="query_write_failed") from exc

    command = (f"hermes --profile {profile} chat --query-file <tmp>.txt "
               f"--skills {spec.get('skills', '')} --oneshot --cli --yolo")
    last_issues: list[str] = []
    attempt = 0

    try:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                stdout, stderr, rc = await asyncio.to_thread(
                    _run_cli, binary, profile, query_path,
                    spec.get("skills", ""), cfg["timeout"])
            except subprocess.TimeoutExpired:
                # 超时 = 硬失败（nonebot 也是直接走 fallback，不重试）
                raise HermesEnhanceError(
                    f"hermes 调用超时（>{cfg['timeout']}s）",
                    reason="timeout", detail=f"profile={profile}") from None
            except FileNotFoundError as exc:
                raise HermesEnhanceError(
                    f"hermes 可执行文件不存在：{binary}",
                    reason="hermes_not_found", detail=str(exc)) from exc

            try:
                text = _extract_answer(stdout, stderr)
            except HermesEnhanceError as exc:
                # 输出解析不出来 -> 带 stderr 上下文重试一次
                last_issues = [exc.detail or str(exc)]
                if attempt >= MAX_ATTEMPTS:
                    raise HermesEnhanceError(
                        f"hermes 调用失败：{exc}",
                        reason=exc.reason,
                        detail=(stderr or "")[:500]) from exc
                continue

            if not text or len(text.strip()) < 20:
                last_issues = [f"hermes 返回内容过短（{len(text or '')} 字符）"]
                if attempt >= MAX_ATTEMPTS:
                    raise HermesEnhanceError(
                        "hermes 返回内容过短，未做修改",
                        reason="too_short", detail=last_issues[0])
                continue

            if validate:
                issues = _validate(text, original, media)
                if issues:
                    last_issues = issues
                    if attempt >= MAX_ATTEMPTS:
                        raise HermesEnhanceError(
                            "hermes 输出未通过提示词校验（已重试 "
                            f"{MAX_ATTEMPTS} 次）",
                            reason="validation_failed",
                            detail="；".join(issues)[:500])
                    # 回灌 issues 重跑（nonebot h3_interactive.py:3859-3866 同款）
                    with open(query_path, "w", encoding="utf-8") as fh:
                        fh.write(query + _retry_note(issues))
                    continue

            return HermesResult(
                ok=True, enhanced=text.strip(), original=original,
                profile=profile, skills=spec.get("skills", ""),
                command=command, raw=text, attempts=attempt,
                issues=last_issues)

        # 【理论上到不了这里】循环内每条失败分支要么 raise、要么 continue，
        # 而最后一次 attempt 一定 raise。留这一行纯粹是为了满足类型检查，
        # 并且**保证**函数永远不会「静默返回 None」。
        raise HermesEnhanceError(
            "hermes 增强未产出结果",
            reason="no_result",
            detail="；".join(last_issues)[:500] if last_issues else "")
    finally:
        try:
            os.unlink(query_path)
        except OSError:
            pass


def _retry_note(issues: list) -> str:
    """校验不过时的回灌说明（nonebot h3_interactive.py:3859-3866 同款措辞）。"""
    return (
        "\n\n# 上一次输出未通过校验（这是第 2 次尝试，只输出修正后的最终提示词）\n"
        "以下问题必须全部修掉:\n"
        + "\n".join(f"- {x}" for x in issues)
        + "\n\n不要解释, 不要道歉, 直接重新输出完整的最终提示词。"
    )


def _validate(text: str, original: str, media: str) -> list[str]:
    """
    校验 hermes 的输出。

    用本项目已有的校验器（h3studio/skills/prompt-enhancer/scripts/validate.py）
    —— 同一套硬约束（禁质量词 / 禁权重语法 / 禁 thinking 残留 / 保留原文主体）。
    校验器缺失时**不阻断**（返回空 issues）。
    """
    try:
        from . import enhancer as enh
        spec = None
        try:
            spec = enh.get_spec({
                "image": "image", "video": "video-director",
                "audio": "yue2-style",
            }.get(media, "image"))
        except Exception:
            spec = None
        mode = (spec or {}).get("validate_mode", "pe")
        result = enh.validate_output(text, original, mode)
        if not result.get("ok", True):
            return list(result.get("errors") or [])
    except Exception:
        # 校验器缺失 / 崩了 -> 不阻断增强（增强本身更重要）
        return []
    return []
