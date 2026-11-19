# -*- coding: utf-8 -*-
"""
E2E / 脚本侧的**生成动作**公共助手。

【这个模块在防什么】
用户 2026-11-19 实测发现，端到端测试的生成动作有三处根本性问题：

  1. **4 步没配加速 LoRA** —— H3 的 4 步是**蒸馏档**，4 步必须配一枚
     加速 LoRA（ckpt850 / dmad / lightx2v_4step …）。没有它，跑的
     是一条「不存在的路径」：4 步采样一个非蒸馏权重，画面必然糊。
  2. **提示词是测试占位文本**（"e2e 第二段" / "XXXX"）—— 对模型毫无
     意义，模型只需要知道「当前要生成的内容是什么」。
  3. **写完提示词不增强就生成** —— 完全不符合既定使用流程。

【为什么要有这个共享模块】
上面三条要在**几十个**测试/脚本文件里同时成立。逐个文件手写
必然漂移（这正是缺陷产生的根因）。所以：
  * 提示词文案**只有一份**（`GEN_PROMPTS`）；
  * 「4 步 → 必须带哪枚加速 LoRA」的换算**只有一份**
    （`accel_for_steps`），而且它走**服务端实测清单**，不写死文件名；
  * 生成参数组装**只有一份**（`build_video_params`）。

【加速 LoRA 的选取口径 —— 必须可自由定义，不能写死】
`accel_for_steps` 优先读调用方传入的 `available`（服务端
`/api/video-options` 的实测清单：available / compatible /
recommended_steps）。**没有实测清单时**退到本模块的
`ACCEL_FALLBACK_KEYS`（按项目内既有的加速 LoRA 选项顺序），
但仍然要求该 key 的推荐步数与目标步数一致。

【为什么不能只依赖服务端清单】
E2E 隔离实例连的是**假 ComfyUI**，它的模型清单是空的
（`/models/loras` 没有真实权重）—— 于是下拉里一枚加速 LoRA 都选不了。
真跑线上时清单才是实的。所以两条路都得通，且都落到**同一套键**。
"""
from __future__ import annotations

import json
import re
import urllib.request
from typing import Any, Iterable

# ------------------------------------------------------------------
# 1. 提示词文案（**唯一真源**）
# ------------------------------------------------------------------
#
# 【硬规则】这里**不允许**出现 e2e / 第X段 / XXXX / test / 测试 等
# 占位描述。每条都是描述「主体 / 场景 / 镜头 / 风格」的中性提示词 ——
# 与用户真实在输入框里写的东西同构。
#
# 【断言会扫这里】tests/test_e2e_gen_flow_contract.py 的 A2 断言
# （提示词非空 + 不含占位词）直接读这个字典，所以**改这里等于改断言**。

GEN_PROMPTS: dict[str, str] = {
    # ---- 文生视频 ----
    "t2v_cat_desk": (
        "一只橘猫在洒满阳光的旧书桌上走过，镜头缓缓跟随，"
        "暖色逆光，浅景深，安静的生活气息。"
    ),
    "t2v_cat_grass": (
        "一只橘猫在洒满阳光的草地上小跑，低角度跟拍镜头，"
        "清晨薄雾，自然光影，写实风格。"
    ),
    "t2v_swordsman_bamboo": (
        "黑衣剑客背对镜头站在月光下的竹林中，剑未出鞘，"
        "中景缓慢推近，冷蓝色调，氛围沉静。"
    ),
    "t2v_swordsman_turn": (
        "白衣剑客在竹林中缓缓转身，剑身映出面部轮廓，"
        "竹叶纷飞，镜头轻微横移，暖冷光交错。"
    ),
    "t2v_swordsman_stand": (
        "两名剑客在竹林空地对峙，剑锋相接，"
        "镜头缓慢推近至中近景，雾气弥漫，紧张而克制。"
    ),
    "t2v_classroom": (
        "一位少女站在日式教室讲台前，薄荷绿短发，粉色背心，"
        "镜头缓慢推近，午后侧光，细腻光影。"
    ),
    "t2v_classroom_cont": (
        "少女缓步走上教室讲台，衣摆轻动，"
        "镜头跟随横移，窗外树影斑驳，柔和自然光。"
    ),
    "t2v_astronaut_dunes": (
        "一名宇航员独自走过火星红色沙丘，"
        "广角远景，低角度长镜头，黄昏逆光，尘土飞扬。"
    ),
    "t2v_dome_mars": (
        "火星地表上的半透明测地线穹顶建筑，"
        "广角静景，冷暖对比光线，远处沙丘层叠，寂静。"
    ),
    "t2v_red_panda_bamboo": (
        "一只红熊猫缓慢走过竹林，"
        "侧面跟拍，柔和漫射光，浅景深，安静自然。"
    ),
    "t2v_window_cat": (
        "一只猫在窗边打盹，暖色台灯，"
        "镜头缓慢推进，窗外虚化光斑，慵懒氛围。"
    ),
    "t2v_window_cat_look": (
        "猫睁开眼抬头看向窗外，"
        "镜头继续推进，暖光与冷色窗光交叠，细节柔和。"
    ),
    "t2v_head_turn": (
        "人物缓缓转头，露出微笑，"
        "近景，柔和侧光，背景虚化。"
    ),
    # ---- 文生图 ----
    "t2i_cat_grass": (
        "一只橘猫在洒满阳光的草地上奔跑，"
        "抓拍瞬间，动感姿态，暖色逆光，浅景深。"
    ),
    "t2i_red_apple": (
        "一只红苹果放在木质桌面上，"
        "影棚产品摄影，柔光箱布光，浅景深，干净背景。"
    ),
    "t2i_teapot": (
        "一只陶瓷茶壶放在亚麻桌布上，"
        "柔和窗光，自然质感，暖色调，安静静物。"
    ),
    # ---- 首尾帧 / 参考生视频 ----
    "flf_cat_window": (
        "橘猫从窗边跃上书桌，"
        "中景固定机位，午后暖光，窗外树影婆娑。"
    ),
    "flf_classroom_cont": (
        "少女转身面向镜头，眼神坚定，"
        "中近景，日式教室，窗光柔和，浅景深。"
    ),
    "r2v_ref_cat": (
        "参考图里的猫在庭院中缓步走过，"
        "侧面跟拍，午后暖光，真实毛发质感。"
    ),
    "r2v_classroom": (
        "教室里的日常，少女走上讲台，"
        "中景，柔和自然光，画面安静。"
    ),
    # ---- 音乐 ----
    "t2a_piano": (
        "舒缓的钢琴独奏，温暖的分解和弦，"
        "中慢速，适合作为背景配乐，情绪平和。"
    ),
}


def prompt_text(key: str) -> str:
    """按 key 取一条中性提示词。key 不存在时**报错**（不返回空串）。"""
    if key not in GEN_PROMPTS:
        raise KeyError(
            f"未知的提示词 key {key!r}；可用：{sorted(GEN_PROMPTS)}")
    return GEN_PROMPTS[key]


# ------------------------------------------------------------------
# 2. 加速 LoRA 的选取
# ------------------------------------------------------------------
#
#: 4 步档的加速 LoRA（项目内既有的加速 LoRA 选项，按推荐档排序）。
#: **键名**而不是权重文件名 —— 键名才是面板 / 服务端认的稳定标识。
ACCEL_4STEP_KEYS: tuple[str, ...] = (
    "ckpt850",                    # Turbo 4 步 · ckpt850
    "v4step600",                  # Turbo 4 步 · v4-step600
    "lightx2v_4step_768p",        # LightX2V 4 步（768p 档）
    "lightx2v_4step_sla_768p",    # LightX2V SLA 4 步（768p 档）
    "dmad_4step_critic",          # DMAD 4 步（critic 版）
    "lightx2v_4step_v01_comfy",   # LightX2V 4 步（v0.1 comfy 版）
    "lightx2v_sla_768p_root",     # LightX2V SLA 4 步（768p · 根目录版）
)

#: 8 步档的加速 LoRA（8 步是蒸馏档，也必须配加速 LoRA）。
ACCEL_8STEP_KEYS: tuple[str, ...] = ("lightx2v_8step",)


def fetch_video_options(base_url: str,
                         model_key: str = "fl2va_pruned_int8",
                         timeout: float = 60.0) -> dict:
    """读服务端 /api/video-options —— 加速 LoRA 的**实测清单**。

    失败时返回空 dict（调用方据此退到兜底键）。
    """
    url = (f"{base_url.rstrip('/')}/api/video-options"
           f"?model={model_key}")
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return {}


def accel_catalog(base_url: str | None = None,
                  model_key: str = "fl2va_pruned_int8",
                  timeout: float = 60.0) -> list[dict]:
    """加速 LoRA 清单（服务端实测优先，取不到则空列表）。"""
    if not base_url:
        return []
    data = fetch_video_options(base_url, model_key, timeout)
    return list(data.get("accel_loras") or [])


def accel_for_steps(steps: int,
                    available: Iterable[dict] | None = None,
                    *,
                    fallback: Iterable[str] = ACCEL_4STEP_KEYS) -> tuple[str, float]:
    """挑一枚**推荐步数 == steps** 的加速 LoRA。

    返回 (key, strength)。挑不到就**抛错** ——
    绝不返回一个「跑得通但档位不对」的组合（那就是静默降级）。

    【available 的形状】服务端 /api/video-options 的 accel_loras 条目：
        {"key": ..., "available": bool, "compatible": bool,
         "selectable": bool, "recommended_steps": int, ...}

    【筛选顺序】
      1. 服务端实测清单里：available 且 compatible 且
         recommended_steps == steps 的第一枚；
      2. 没有实测清单（或清单里没有匹配的）-> 退到 `fallback` 键序列里
         第一个；**但此时无法核对推荐步数**，所以只接受 `fallback`
         本身已经按步数分好组的键（ACCEL_4STEP_KEYS / ACCEL_8STEP_KEYS）。
    """
    want = int(steps)
    if want <= 0:
        raise ValueError(f"步数必须是正整数，收到 {steps!r}")

    pool = list(available or [])
    if pool:
        for item in pool:
            key = str(item.get("key") or "").strip()
            if not key:
                continue
            if item.get("available") is False:
                continue
            if item.get("compatible") is False:
                continue
            if item.get("selectable") is False:
                continue
            if int(item.get("recommended_steps") or 0) != want:
                continue
            strength = item.get("recommended_strength")
            try:
                strength = float(strength)
            except (TypeError, ValueError):
                strength = 1.0
            return key, (strength if strength > 0 else 1.0)
        # 实测清单在，但没有一枚匹配 -> 报错（不静默换档）
        keys = sorted({str(i.get("key") or "") for i in pool})
        raise AssertionError(
            f"本机没有「推荐 {want} 步」的加速 LoRA —— "
            f"4/8 步是蒸馏档，必须配加速 LoRA。"
            f"服务端清单里的候选：{keys}。"
            f"（请在本机放好对应的加速权重，或改用别的步数。）")

    # 没有实测清单 -> 退到分组兜底
    for key in fallback:
        if not key:
            continue
        return key, 1.0
    raise AssertionError(
        f"没有可用于 {want} 步的加速 LoRA 兜底键；"
        f"请检查 ACCEL_4STEP_KEYS / ACCEL_8STEP_KEYS。")


# ------------------------------------------------------------------
# 3. 生成参数组装
# ------------------------------------------------------------------

#: 加速 LoRA 的推荐采样档（ckpt850 / dmad / lightx2v_4step 都是这一档）。
ACCEL_4STEP_SAMPLER = "euler"
ACCEL_4STEP_SCHEDULER = "beta"


def build_video_params(prompt: str,
                       *,
                       steps: int = 4,
                       accel_key: str | None = None,
                       accel_strength: float = 1.0,
                       model_key: str = "fl2va_pruned_int8",
                       width: int = 864,
                       height: int = 480,
                       duration: float = 5.0,
                       aspect: str = "16:9",
                       seed: int | None = None,
                       sampler: str | None = None,
                       scheduler: str | None = None,
                       extra: dict | None = None) -> dict:
    """组装一份**自洽**的视频生成参数。

    【核心不变量】steps 与加速 LoRA **成对**：
      * steps 是 4 或 8（蒸馏档）-> 必须带加速 LoRA；
      * 加速 LoRA 的推荐步数必须与 steps 一致。

    【为什么不能用「不挂加速 LoRA」+ 4 步】
    4 步是蒸馏档：不挂加速 LoRA 时 4 步采样一个非蒸馏权重，
    出来的画面必然糊 —— 那是**一条不存在的路径**，不能拿来当测试基线。

    【accel_key 的来源】
      * 显式传入 -> 用它（**键名**，不是权重文件名）；
      * 没传 -> 按 `ACCEL_4STEP_KEYS` / `ACCEL_8STEP_KEYS` 兜底，
        且步数不在 4/8 档时**报错**（不静默生成一条错路径）。

    【sampler / scheduler】
      给了就用；没给就按加速 LoRA 的推荐档（4 步蒸馏档 = euler + beta）。
    """
    text = (prompt or "").strip()
    if not text:
        raise ValueError("提示词不能为空")

    want = int(steps)
    if accel_key is None:
        if want == 4:
            accel_key = ACCEL_4STEP_KEYS[0]
        elif want == 8:
            accel_key = ACCEL_8STEP_KEYS[0]
        else:
            raise AssertionError(
                f"步数 {want} 不是本项目已知的蒸馏档（4 / 8 步）—— "
                f"蒸馏步数必须配加速 LoRA；请显式传 accel_key，"
                f"或改用标准多步采样（不传加速 LoRA 时步数应 >= 20）。")
    accel_key = str(accel_key).strip()
    if not accel_key:
        raise AssertionError(
            "4/8 步必须带加速 LoRA —— accel_key 不能为空。"
            "（不挂加速 LoRA 跑 4 步 = 4 步采样非蒸馏权重，画面必然糊。）")

    params: dict[str, Any] = {
        "prompt": text,
        "model_key": model_key,
        "width": int(width),
        "height": int(height),
        "aspect": aspect,
        "duration": float(duration),
        "steps": want,
        # 加速 LoRA 槽位：**键名** + 数组形态（与面板真实提交形状一致）
        "accel_lora": accel_key,
        "accel_lora_strength": float(accel_strength),
        "accel_loras": [{"key": accel_key,
                         "strength": float(accel_strength),
                         "enabled": True}],
        "sampler": sampler or ACCEL_4STEP_SAMPLER,
        "scheduler": scheduler or ACCEL_4STEP_SCHEDULER,
        "cfg": 1.0,
    }
    if seed is not None:
        params["seed"] = int(seed)
    if extra:
        params.update(extra)
    return params


# ------------------------------------------------------------------
# 4. 断言用的常量（测试与断言读同一份）
# ------------------------------------------------------------------

#: **测试占位词**黑名单。提示词里出现任何一个都算「对模型无意义」。
PLACEHOLDER_TOKENS: tuple[str, ...] = (
    "e2e",
    "xxxx",
    "test",
    "测试",
    "占位",
    "第一段",
    "第二段",
    "第三段",
    "第1段",
    "第2段",
    "第3段",
    "第1幕",
    "第2幕",
    "第3幕",
    "dummy",
    "placeholder",
    "foo",
    "bar",
)

#: **纯 ASCII 的占位词**必须按「词边界」匹配 ——
#: 否则 "foo" 会命中 "footage"、"bar" 会命中 "barley"，
#: "test" 会命中 "contest"/"latest" —— 全是误报，
#: 而误报会让断言失去可信度（每次都得人肉排除）。
#: 中文词没有词边界问题，按子串匹配即可。
_ASCII_PLACEHOLDERS = frozenset(
    token for token in PLACEHOLDER_TOKENS if token.isascii())


def has_placeholder(text: str) -> list[str]:
    """返回文本里命中的占位词列表（空列表 = 干净）。

    ASCII 占位词按**词边界**匹配（`\\bfoo\\b` 不会命中 "footage"）；
    中文占位词按子串匹配（「第二段」在任何位置都算占位）。
    """
    low = (text or "").lower()
    hits: list[str] = []
    for token in PLACEHOLDER_TOKENS:
        low_token = token.lower()
        if low_token in _ASCII_PLACEHOLDERS:
            # 词边界：前面不能是字母/数字，后面也不能是
            pattern = r"(?<![a-z0-9])" + re.escape(low_token) + r"(?![a-z0-9])"
            if re.search(pattern, low):
                hits.append(token)
        elif low_token in low:
            hits.append(token)
    return hits
