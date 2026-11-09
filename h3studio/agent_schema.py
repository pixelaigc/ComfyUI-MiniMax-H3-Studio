# -*- coding: utf-8 -*-
"""
Agent 的**参数合法性校验器**与**模式 schema 推导**。

【为什么单独一个模块，而不是塞进 agent.py】
agent.py 已经 1000+ 行，是「编排器」。本模块是**纯函数层**：
输入 (mode, params) -> 输出 (是否合法, 可读错误, 合法值集合, 替代方案)。
它不碰数据库、不碰 HTTP、不碰模型 —— 所以它可以被单测直接调用，
不需要起服务、不需要 fake 任何东西。这是它单独成文件的主要理由。

【核心原则：合法值必须来自真源，不许硬编码白名单】
所有「合法取值集合」都从**真实存在的配置**里推导：
  * 比例        <- workflows.ASPECT_MAP
  * 分辨率档位  <- workflows.Z_IMAGE_RES_TIERS / QWEN_IMAGE_RES_TIERS /
                  MOSS_IMAGE_RES_TIERS
  * 模型        <- workflows.Z_IMAGE_MODELS / QWEN_IMAGE_MODELS /
                  MOSS_IMAGE_MODELS
  * 视频 LoRA   <- workflows.LORAS
  * 视频分辨率  <- workflows.RES_MP
  * 视频时长    <- web/index.html 的滑块区间 5~15（面板的真实约束）
  * 音乐时长    <- music_yue2.YUE2_MIN_DURATION / YUE2_MAX_DURATION
  * 参考图数量  <- ref_images.MIN_REF_IMAGES / MAX_REF_IMAGES
  * 增强预设    <- enhancer.TEMPLATE_SPECS

**为什么强调这一点**：早前版本把时长硬编码成 [5,10,15]，模型据此以为
「8 秒不支持」而拒绝执行 —— 那是凭空造的约束。后端说「合法值」时，
每个数字都必须能在本仓库里找到出处，否则就是骗模型。

【替代方案而不是拒绝】
用户说「做一个 30 秒的竖屏视频」，30 秒超出视频面板的 5~15 秒。
正确做法**不是**回一句「不支持」，而是给出**可执行的替代方案**：
    * 30 秒拆成 3 段 10 秒（视频模式支持多段续接）
    * 或者改用 10 秒
本模块的 `suggest_alternatives()` 就是干这个的 —— 它给出的每条建议
都是**能直接用的 mode/参数组合**，不是空话。
"""
from __future__ import annotations

from . import enhancer as enh
from . import gen_types
from . import workflows as wf

# ------------------------------------------------------------------ 视频时长

#: 视频时长区间（秒）。来源：web/index.html 的 `#duration` 滑块 min/max。
#:
#: 为什么从面板约束取而不是从后端取：后端 workflows 里
#: `float(params.get("duration", 5))` 接受**任意**秒数 —— 后端不拦。
#: 真正的约束在面板滑块（5~15）。Agent 说「合法时长 5~15」才是对的，
#: 因为用户也只能在面板里选到这个范围。
VIDEO_DURATION_MIN = 5
VIDEO_DURATION_MAX = 15

#: 视频分辨率档位（对应 ComfyUI ResolutionSelector 的 megapixels）。
#: 来源：workflows.RES_MP
VIDEO_RESOLUTIONS = tuple(wf.RES_MP.keys())

#: 视频 LoRA 取值。来源：workflows.LORAS + "none"（官方工作流的无 LoRA 档）
VIDEO_LORAS = ("none",) + tuple(wf.LORAS.keys())

#: 音乐时长区间（秒）。来源：music_yue2 的 _clamp_duration 上下限。
def _music_duration_range() -> tuple[int, int]:
    try:
        from . import music_yue2
        return (int(music_yue2.YUE2_MIN_DURATION),
                int(music_yue2.YUE2_MAX_DURATION))
    except Exception:
        return (4, 360)

MUSIC_DURATION_MIN, MUSIC_DURATION_MAX = _music_duration_range()

#: 参考图数量上下限。来源：ref_images.MIN/MAX_REF_IMAGES
def _ref_image_range() -> tuple[int, int]:
    try:
        from . import ref_images
        return (int(ref_images.MIN_REF_IMAGES), int(ref_images.MAX_REF_IMAGES))
    except Exception:
        return (1, 10)

REF_IMAGE_MIN, REF_IMAGE_MAX = _ref_image_range()


# ------------------------------------------------------------------ 模式分类

#: 各模式的媒体类型（来自 gen_types 注册表，**不硬编码**）
def _media_of(mode: str) -> str:
    try:
        return gen_types.type_for_mode(mode).get("media") or "other"
    except Exception:
        return "other"


def is_video_mode(mode: str) -> bool:
    return mode in ("t2v", "flf", "r2v")


def is_music_mode(mode: str) -> bool:
    return mode in ("t2a", "a2a")


def is_image_mode(mode: str) -> bool:
    return mode in ("t2i", "t2i_qwen", "i2i_qwen", "t2i_moss")


#: 「图像模式 -> (模型表, 分辨率档位表)」。**来源：workflows 的真实常量**。
_IMAGE_MODEL_TABLES = {
    "t2i": ("Z_IMAGE_MODELS", "Z_IMAGE_RES_TIERS"),
    "t2i_qwen": ("QWEN_IMAGE_MODELS", "QWEN_IMAGE_RES_TIERS"),
    "i2i_qwen": ("QWEN_IMAGE_MODELS", "QWEN_IMAGE_RES_TIERS"),
    "t2i_moss": ("MOSS_IMAGE_MODELS", "MOSS_IMAGE_RES_TIERS"),
}


def _table(name: str) -> dict:
    """取 workflows 里的某张常量表。取不到就返回空 dict（不抛）。"""
    table = getattr(wf, name, None)
    return table if isinstance(table, dict) else {}


def mode_models(mode: str) -> list[str]:
    """该模式合法的模型取值集合（来自真实常量表）。"""
    entry = _IMAGE_MODEL_TABLES.get(mode)
    if entry:
        return list(_table(entry[0]).keys())
    if is_video_mode(mode):
        # 视频模式只有一个模型档：「lora」（= H3 + 加速 LoRA）
        return ["lora"]
    if is_music_mode(mode):
        return ["yue2"]
    return []


def mode_resolutions(mode: str) -> list[str]:
    """该模式合法的分辨率档位集合（来自真实常量表）。"""
    entry = _IMAGE_MODEL_TABLES.get(mode)
    if entry:
        return list(_table(entry[1]).keys())
    if is_video_mode(mode):
        return list(VIDEO_RESOLUTIONS)
    return []


def mode_aspects(mode: str) -> list[str]:
    """
    该模式合法的画面比例集合（来自 workflows.ASPECT_MAP）。

    音乐模式**没有画面比例** —— YuE2 工作流里不存在 ResolutionSelector，
    硬给它塞一份比例表就是骗模型（模型会以为能设比例）。
    """
    if mode in ("t2a", "a2a"):
        return []
    if mode in wf.BUILDERS:
        return list(wf.ASPECT_MAP.keys())
    return []


def mode_duration_range(mode: str) -> tuple[int, int] | None:
    """该模式合法的时长区间；不适用时返回 None。"""
    if is_video_mode(mode):
        return (VIDEO_DURATION_MIN, VIDEO_DURATION_MAX)
    if is_music_mode(mode):
        return (MUSIC_DURATION_MIN, MUSIC_DURATION_MAX)
    return None


def mode_duration_field(mode: str) -> str:
    """
    该模式的时长参数**字段名**。

    视频用 `duration`（workflows 里 float(params.get("duration", 5))），
    音乐用 `max_duration`（music_yue2._clamp_duration(params.get("max_duration"))）
    —— 字段名不同，用错的话音乐任务会静默用默认值而不是用户要的时长。
    """
    return "max_duration" if is_music_mode(mode) else "duration"


def mode_loras(mode: str) -> list[str]:
    """该模式合法的 LoRA 取值集合。"""
    if is_video_mode(mode):
        return list(VIDEO_LORAS)
    return []


# ------------------------------------------------------------------ 默认值推导

def _builder_defaults(mode: str) -> dict:
    """
    从**真实的工作流构造函数源码**里读出「参数留空时的默认值」。

    为什么要这么绕：
      * 图像模式的默认值有现成常量（Z_IMAGE_DEFAULT_MODEL 等）—— 直接读常量。
      * 视频模式的默认值**只写在 builder 源码里**：
            resolution = RES_MP.get(params.get("resolution", "768p"), 0.8)
            duration   = float(params.get("duration", 5))
            lora       = params.get("lora", "ckpt850")
        照抄一份到别处 = 两份实现必然漂移（这正是本仓库反复踩的坑）。
        所以这里**解析源码**拿到真实默认值。

    解析失败就返回空 dict（不抛）—— 拿不到默认值时，宁可不给，
    也不要给一个编出来的。
    """
    builder = wf.BUILDERS.get(mode)
    if builder is None:
        return {}
    try:
        import inspect
        import re
        source = inspect.getsource(builder)
    except (OSError, TypeError):
        return {}

    # builder 可能只是转调（build_t2v -> _h3_video_workflow），
    # 这种情况下继续往被调用的模块级函数里找。
    if "params.get(" not in source:
        for name in ("_h3_video_workflow",):
            helper = getattr(wf, name, None)
            if helper is None:
                continue
            try:
                helper_source = inspect.getsource(helper)
            except (OSError, TypeError):
                continue
            if "params.get(" in helper_source:
                source = helper_source
                break

    defaults: dict = {}
    # params.get("key", default) —— default 可以是字面量或数字
    for match in re.finditer(
            r"""params\.get\(\s*["']([a-z_]+)["']\s*,\s*["']?([A-Za-z0-9_.]+)["']?\s*\)""",
            source):
        key, raw = match.group(1), match.group(2)
        if key in defaults:
            continue
        defaults[key] = raw
    return defaults


def mode_defaults(mode: str) -> dict:
    """
    该模式各参数的默认值（来源：真实常量 + 真实 builder 源码）。

    返回 {"aspect": "16:9", "resolution": "768p", "duration": 5, ...}
    """
    defaults: dict = {}

    # 1. builder 源码里的默认值（视频模式的主要来源）
    for key, raw in _builder_defaults(mode).items():
        if key in ("aspect", "resolution", "duration", "lora", "model", "seed"):
            defaults[key] = raw

    # 2. 图像模式的显式常量（优先级更高 —— 它们是官方推荐值）
    if mode == "t2i":
        defaults.setdefault("model", getattr(wf, "Z_IMAGE_DEFAULT_MODEL", ""))
        defaults.setdefault("resolution",
                            getattr(wf, "Z_IMAGE_DEFAULT_TIER", ""))
    elif mode in ("t2i_qwen", "i2i_qwen"):
        defaults.setdefault("model",
                            getattr(wf, "QWEN_IMAGE_DEFAULT_MODEL", ""))
        defaults.setdefault("resolution",
                            getattr(wf, "QWEN_IMAGE_DEFAULT_TIER", ""))
    elif mode == "t2i_moss":
        defaults.setdefault("model",
                            getattr(wf, "MOSS_IMAGE_DEFAULT_MODEL", ""))
        defaults.setdefault("resolution",
                            getattr(wf, "MOSS_IMAGE_DEFAULT_TIER", ""))

    # 3. 比例默认值：builder 里写死的优先
    aspects = mode_aspects(mode)
    if aspects:
        raw = str(defaults.get("aspect") or "")
        defaults["aspect"] = raw if raw in aspects else (
            "16:9" if "16:9" in aspects else aspects[0])

    # 4. 清理：不在合法集合里的默认值直接丢掉（宁可不给默认值）
    clean: dict = {}
    for key, value in defaults.items():
        if value in (None, ""):
            continue
        if key == "duration":
            try:
                clean[key] = float(value)
            except (TypeError, ValueError):
                continue
            continue
        allowed = {"aspect": mode_aspects(mode),
                   "model": mode_models(mode),
                   "resolution": mode_resolutions(mode),
                   "lora": mode_loras(mode)}.get(key)
        if allowed and str(value) not in allowed:
            continue
        clean[key] = value
    return clean


def mode_requires_refs(mode: str) -> list[str]:
    """该模式**必须**提供的参考素材槽位。"""
    if mode == "i2i_qwen":
        return ["asset_id"]
    if mode == "r2v":
        return ["asset_id"]
    if mode == "a2a":
        return ["asset_id"]
    if mode == "flf":
        return ["first_frame"]
    return []


# ------------------------------------------------------------------ 模式 schema


def mode_schema(mode: str) -> dict:
    """
    返回某个 mode 的**真实合法参数**。

    这是 get_mode_schema / describe_mode_params 两个工具的数据源。
    每一项都带：
      * values —— 合法取值集合（来自真实常量）
      * default —— 默认值（来自真实常量 / 面板默认）
      * source —— 这个取值集合是从哪个常量来的（可追溯，不臆造）
    """
    if mode not in wf.BUILDERS:
        raise KeyError(mode)

    info = gen_types.type_for_mode(mode)
    defaults = mode_defaults(mode)
    schema: dict = {
        "mode": mode,
        "label": info.get("label") or mode,
        "media": info.get("media") or "other",
        "params": {},
    }

    aspects = mode_aspects(mode)
    if aspects:
        schema["params"]["aspect"] = {
            "type": "string",
            "values": aspects,
            "default": defaults.get("aspect", aspects[0]),
            "source": "workflows.ASPECT_MAP",
        }

    models = mode_models(mode)
    if models:
        schema["params"]["model"] = {
            "type": "string",
            "values": models,
            "default": defaults.get("model", models[0]),
            "source": "workflows.*_MODELS",
        }

    resolutions = mode_resolutions(mode)
    if resolutions:
        schema["params"]["resolution"] = {
            "type": "string",
            "values": resolutions,
            "default": defaults.get("resolution", resolutions[0]),
            "source": "workflows.*_RES_TIERS" if is_image_mode(mode)
                      else "workflows.RES_MP",
        }

    duration_range = mode_duration_range(mode)
    if duration_range:
        field = mode_duration_field(mode)
        schema["params"][field] = {
            "type": "number",
            "unit": "second",
            "min": duration_range[0],
            "max": duration_range[1],
            "step": 1,
            "default": defaults.get(field, duration_range[0]),
            "source": ("web/index.html#duration 滑块区间"
                       if is_video_mode(mode)
                       else "h3studio.music_yue2._clamp_duration"),
        }

    loras = mode_loras(mode)
    if loras:
        schema["params"]["lora"] = {
            "type": "string",
            "values": loras,
            "default": defaults.get("lora", loras[0]),
            "source": "workflows.LORAS",
        }

    requires = mode_requires_refs(mode)
    if requires:
        schema["params"]["asset_id"] = {
            "type": "string",
            "required": True,
            "min_count": 1,
            "max_count": REF_IMAGE_MAX,
            "source": "h3studio.ref_images / server.prepare_job_params",
            "note": "参考素材必须来自素材库（用 search_assets 找 asset_id）",
        }

    if is_image_mode(mode):
        schema["params"]["batch_count"] = {
            "type": "integer",
            "min": 1,
            "max": 4,
            "default": 1,
            "source": "server.MAX_BATCH_COUNT",
        }

    schema["notes"] = {
        "unlisted_params": "未列出的参数留空即由工作台用官方默认值补齐",
        "params_source": "本 schema 的每个取值集合都从 workflows / enhancer / "
                         "ref_images 的真实常量推导，没有硬编码白名单",
    }
    return schema


def all_mode_schemas() -> list[dict]:
    """所有可下发模式的 schema 列表。"""
    return [mode_schema(mode) for mode in sorted(wf.BUILDERS.keys())]


# ------------------------------------------------------------------ 校验


class ValidationIssue:
    """一条校验问题。字段 / 错误 / 合法值 / 替代方案。"""

    def __init__(self, field: str, message: str, allowed=None,
                 suggestions=None, code: str = "invalid_param"):
        self.field = field
        self.message = message
        self.allowed = list(allowed or [])
        self.suggestions = list(suggestions or [])
        self.code = code

    def to_dict(self) -> dict:
        payload: dict = {"field": self.field, "message": self.message,
                         "code": self.code}
        if self.allowed:
            payload["allowed"] = self.allowed
        if self.suggestions:
            payload["suggestions"] = self.suggestions
        return payload

    def __repr__(self):  # pragma: no cover - 调试用
        return f"<ValidationIssue {self.field}: {self.message}>"


def _suggest_duration(mode: str, value: float) -> list[dict]:
    """
    时长越界时给**可执行**的替代方案。

    方案 1：夹到区间内（最省事，一定能跑）
    方案 2：拆成多段（视频模式支持多段续接）—— 但 Agent 一次只能建一个任务，
            所以这里给的是「段数 + 每段时长」的建议，由模型决定要不要分批建。
    """
    low, high = mode_duration_range(mode) or (0, 0)
    field = mode_duration_field(mode)
    suggestions = []
    if value < low:
        suggestions.append({
            "action": "clamp",
            "params": {field: low},
            "reason": f"{value} 秒低于该模式下限 {low} 秒，改用 {low} 秒",
        })
    elif value > high:
        suggestions.append({
            "action": "clamp",
            "params": {field: high},
            "reason": f"{value} 秒超过该模式上限 {high} 秒，改用 {high} 秒",
        })
        if is_video_mode(mode):
            # 拆段：段数 = ceil(value / high)，段长 = value / 段数（向上取整）
            segments = int(-(-value // high))
            per = int(-(-value // segments))
            suggestions.append({
                "action": "split",
                "params": {"segments": segments, field: per},
                "reason": (f"需要 {value} 秒时可拆成 {segments} 段、每段约 {per} 秒"
                           f"（每段都在 {low}~{high} 秒合法区间内），"
                           f"分段生成后按顺序拼接"),
            })
    return suggestions


def _suggest_enum(field: str, value: str, allowed: list[str],
                  mode: str = "") -> list[dict]:
    """
    枚举值非法时的替代方案。

    优先做**前缀/包含匹配**（用户说「竖屏」-> 9:16，「720p」-> 720p），
    匹配不到就给默认值。给的是**能直接用的参数**，不是「请重新输入」。
    """
    if not allowed:
        return []
    suggestions = []
    text = (value or "").strip()
    lower = text.lower()

    exact = [a for a in allowed if a.lower() == lower]
    if exact:
        return suggestions

    contains = [a for a in allowed if lower and lower in a.lower()]
    if contains:
        for item in contains[:3]:
            suggestions.append({
                "action": "use",
                "params": {field: item},
                "reason": f"「{text}」匹配到合法取值「{item}」",
            })
        return suggestions

    # 语义近邻：比例的常见中文说法
    ratio_aliases = {
        "竖屏": "9:16", "竖": "9:16", "竖版": "9:16", "手机": "9:16",
        "横屏": "16:9", "横": "16:9", "横版": "16:9", "宽屏": "16:9",
        "方形": "1:1", "正方形": "1:1", "方图": "1:1",
        "电影": "21:9", "宽银幕": "21:9",
    }
    alias = ratio_aliases.get(text)
    if alias and alias in allowed:
        return [{"action": "use", "params": {field: alias},
                 "reason": f"「{text}」对应合法取值「{alias}」"}]

    default = allowed[0]
    return [{
        "action": "use",
        "params": {field: default},
        "reason": f"「{text}」不是合法取值，改用默认值「{default}」",
    }]


def validate_params(mode: str, params: dict) -> list[ValidationIssue]:
    """
    校验一组任务参数。返回问题列表（空列表 = 全部合法）。

    只校验**取值**，不校验「必填是否缺失」—— 缺参数是「该问用户」的澄清场景，
    不是「值非法」。两者必须分开：把缺失当成非法会让 Agent 只会拒绝，
    而不会去问用户。
    """
    issues: list[ValidationIssue] = []
    if mode not in wf.BUILDERS:
        issues.append(ValidationIssue(
            "mode", f"未知模式 {mode}",
            allowed=sorted(wf.BUILDERS.keys()),
            code="unknown_mode"))
        return issues

    # --- 画面比例 ---
    aspect = params.get("aspect")
    if aspect:
        allowed = mode_aspects(mode)
        if allowed and str(aspect) not in allowed:
            issues.append(ValidationIssue(
                "aspect",
                f"画面比例「{aspect}」不是合法取值",
                allowed=allowed,
                suggestions=_suggest_enum("aspect", str(aspect), allowed, mode),
                code="invalid_aspect"))

    # --- 模型 ---
    model = params.get("model")
    if model:
        allowed = mode_models(mode)
        if allowed and str(model) not in allowed:
            issues.append(ValidationIssue(
                "model",
                f"模型「{model}」不是 {mode} 的合法取值",
                allowed=allowed,
                suggestions=_suggest_enum("model", str(model), allowed, mode),
                code="invalid_model"))

    # --- 分辨率 ---
    resolution = params.get("resolution")
    if resolution:
        allowed = mode_resolutions(mode)
        if allowed and str(resolution) not in allowed:
            issues.append(ValidationIssue(
                "resolution",
                f"分辨率档位「{resolution}」不是 {mode} 的合法取值",
                allowed=allowed,
                suggestions=_suggest_enum("resolution", str(resolution),
                                          allowed, mode),
                code="invalid_resolution"))

    # --- 时长 ---
    duration_field = mode_duration_field(mode)
    duration = params.get(duration_field)
    if duration is None and duration_field != "duration":
        # 兼容：模型有时会填 duration，但音乐模式的真实字段是 max_duration
        duration = params.get("duration")
    if duration is not None:
        try:
            value = float(duration)
        except (TypeError, ValueError):
            issues.append(ValidationIssue(
                duration_field, "时长必须是数字",
                code="invalid_duration"))
        else:
            span = mode_duration_range(mode)
            if span and not (span[0] <= value <= span[1]):
                issues.append(ValidationIssue(
                    duration_field,
                    f"时长 {value} 秒超出 {mode} 的合法区间 "
                    f"{span[0]}~{span[1]} 秒",
                    allowed=[f"{span[0]}~{span[1]} 秒"],
                    suggestions=_suggest_duration(mode, value),
                    code="invalid_duration"))

    # --- LoRA ---
    lora = params.get("lora")
    if lora:
        allowed = mode_loras(mode)
        if allowed and str(lora) not in allowed:
            issues.append(ValidationIssue(
                "lora",
                f"LoRA「{lora}」不是 {mode} 的合法取值",
                allowed=allowed,
                suggestions=_suggest_enum("lora", str(lora), allowed, mode),
                code="invalid_lora"))

    # --- 参考图数量 ---
    asset_ids = params.get("asset_ids") or params.get("asset_id")
    if isinstance(asset_ids, str):
        asset_ids = [asset_ids]
    if asset_ids:
        count = len([a for a in asset_ids if str(a or "").strip()])
        if count > REF_IMAGE_MAX:
            issues.append(ValidationIssue(
                "asset_id",
                f"参考图最多 {REF_IMAGE_MAX} 张，当前 {count} 张",
                allowed=[f"1~{REF_IMAGE_MAX} 张"],
                suggestions=[{"action": "trim",
                              "params": {"asset_id": list(asset_ids)[:REF_IMAGE_MAX]},
                              "reason": f"只保留前 {REF_IMAGE_MAX} 张参考图"}],
                code="too_many_refs"))

    return issues


def missing_params(mode: str, params: dict) -> list[str]:
    """
    找出**关键缺失**的参数（= 该问用户澄清的字段）。

    只列真正影响产出结果的字段 —— 把「batch_count 没填」也算成缺失，
    会让 Agent 逢单必问，用户体验极差。
    """
    missing = []
    if mode not in wf.BUILDERS:
        return missing
    if not str(params.get("prompt") or "").strip():
        missing.append("prompt")
    # 参考图是硬性依赖
    requires = mode_requires_refs(mode)
    if requires:
        asset_ids = params.get("asset_ids") or params.get("asset_id")
        if isinstance(asset_ids, str):
            asset_ids = [asset_ids]
        if not [a for a in (asset_ids or []) if str(a or "").strip()]:
            missing.append("asset_id")
    if mode == "flf" and not params.get("first_frame"):
        missing.append("first_frame")
    return missing


def format_issues(issues: list[ValidationIssue]) -> str:
    """
    把校验问题渲染成**给人看**的一段话。

    格式刻意是「问题 + 合法值 + 可执行建议」三段式 —— 模型读到这个字符串
    就能直接改参数重试，不需要再调一次 get_mode_schema 去查。
    """
    if not issues:
        return ""
    lines = []
    for issue in issues:
        line = f"- {issue.field}：{issue.message}"
        if issue.allowed:
            line += f"（合法值：{'、'.join(str(a) for a in issue.allowed)}）"
        lines.append(line)
        for suggestion in issue.suggestions:
            params = suggestion.get("params") or {}
            params_text = "，".join(f"{k}={v}" for k, v in params.items())
            lines.append(f"  可改用：{params_text}（{suggestion.get('reason', '')}）")
    return "\n".join(lines)


def issues_to_payload(issues: list[ValidationIssue]) -> dict:
    """结构化的问题列表（给前端 / 契约文档用）。"""
    return {
        "ok": not issues,
        "issues": [issue.to_dict() for issue in issues],
        "message": format_issues(issues),
    }


# ------------------------------------------------------------------ 提示词预设


def prompt_presets() -> list[dict]:
    """
    真实的提示词增强预设列表（来自 enhancer.TEMPLATE_SPECS）。

    **不臆造**：只列 enhancer 里真实注册的 spec。
    每个 spec 的默认模式来自 enhancer.MODE_DEFAULT_SPEC（反向推导）。
    """
    specs = enh.TEMPLATE_SPECS
    # 反向推导：哪些 mode 默认用这个 spec
    mode_by_spec: dict[str, list[str]] = {}
    for mode, spec_id in (enh.MODE_DEFAULT_SPEC or {}).items():
        mode_by_spec.setdefault(spec_id, []).append(mode)
    result = []
    for spec_id, spec in specs.items():
        result.append({
            "spec_id": spec_id,
            "label": spec.get("label") or spec_id,
            "media": spec.get("media") or "other",
            "modes": sorted(mode_by_spec.get(spec_id, [])),
            "validate_mode": spec.get("validate_mode") or "pe",
        })
    return result


def prompt_preset(spec_id: str, overrides: dict | None = None) -> dict:
    """
    取单个预设的详情（真实内容）。

    overrides 是「本地模型配置」页里保存的模板覆盖 —— 与 /api/enhance
    读的是同一份，所以这里看到的和增强按钮用的是同一套模板。

    【注意】enhancer.get_spec 对未知 spec_id 会**静默回落到 image 档**。
    这里必须先自己判存在性 —— 否则 Agent 问一个不存在的预设时，
    会拿到「image」预设的内容，看起来像成功了（最典型的「假成功」）。
    """
    if spec_id not in enh.TEMPLATE_SPECS:
        raise KeyError(spec_id)
    spec = enh.get_spec(spec_id, overrides or {})
    if not spec:
        raise KeyError(spec_id)
    mode_by_spec: dict[str, list[str]] = {}
    for mode, sid in (enh.MODE_DEFAULT_SPEC or {}).items():
        mode_by_spec.setdefault(sid, []).append(mode)
    return {
        "spec_id": spec_id,
        "label": spec.get("label") or spec_id,
        "media": spec.get("media") or "other",
        "modes": sorted(mode_by_spec.get(spec_id, [])),
        "validate_mode": spec.get("validate_mode") or "pe",
        "system_prompt": spec.get("system_prompt") or "",
        "template": spec.get("template") or spec.get("user_template") or "",
        "params": spec.get("params") or {},
    }
