# -*- coding: utf-8 -*-
"""
A1 / A2 防回归断言 —— **脚本级静态扫描**。

【这两条在防什么（2026-11-19 用户实测反馈）】
  A1  「步数设为 4 步，却未启用加速 LoRA」
      —— 4 步是 H3 的**蒸馏档**，必须配一枚加速 LoRA。
      没有它，跑的就是一条「不存在的路径」。
  A2  「提示词输入框里塞的是『e2e 第二段』『XXXX』这类占位文本」
      —— 对模型毫无意义；模型只需要知道当前要生成的内容是什么。

【为什么做成静态扫描而不是逐个跑】
缺陷分散在**几十个**测试/脚本文件里。逐个跑一遍既慢又会漏；
而这两条的性质是「源码层面就能判定」—— 所以直接扫源码，
谁违规就报出**文件名 + 行号**，改起来有明确落点。

【扫描范围】
`tests/` 与 `scripts/` 下的 `.py`（含 `tests/e2e/`）。

【怎么判定「这个文件会真实触发生成」】
文件里出现下列任一标记即认为它会触发生成：
  * `/api/jobs`      —— 直接打接口提交任务
  * `genBtn` / `submit_generation` —— 走界面点「生成」
  * `create_job_record` —— 直接往任务库塞记录

【误报怎么排除】
  * **A2**：只**断言空提示词会被拒**的用例（`assertRaises` 包着的）
    不算违规 —— 那里空串是**被测行为**，不是提交内容。
    判据：空串字面量出现在 `assertRaises` / `pytest.raises` 块内。
  * **A1**：加速 LoRA 的值来自**变量**（例如 `case["accel"]`）时，
    源码层面无法判定 -> 只在该文件**另有**明确的加速 LoRA 键名常量时
    才判违规（否则它可能是故意遍历不同档位的用例）。

运行：python3 -m pytest tests/test_e2e_gen_flow_contract.py -v
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests" / "e2e"))

import gen_flow  # noqa: E402

#: 扫描的目录
SCAN_DIRS = ("tests", "scripts")

#: 「这个文件会真实触发生成」的标记
GEN_TRIGGER_MARKERS = (
    "/api/jobs",
    "genBtn",
    "submit_generation",
    "create_job_record",
)

#: 视频生成参数里「步数」的赋值形态
STEPS_RE = re.compile(r"""["']?steps["']?\s*[:=]\s*(\d+)""")

#: 加速 LoRA 的**取值**（键名或权重文件名）
ACCEL_VALUE_RE = re.compile(
    r"""["']?(?:accel_lora|accel_loras|fixed_accel_lora)["']?\s*[:=]\s*
        \[?\s*["']?([A-Za-z0-9_./]+)""", re.X)

#: 加速 LoRA 的取值是**变量**（`case["accel"]` / `cfg.accel_lora` …）
ACCEL_DYNAMIC_RE = re.compile(
    r"""["']?(?:accel_lora|accel_loras|fixed_accel_lora)["']?\s*[:=]\s*
        (?!["'\[])([A-Za-z_][A-Za-z0-9_.\[\]"']*)""", re.X)

#: 提示词字面量：`fill("#prompt", "...")` 或 `{"prompt": "..."}`
PROMPT_FILL_RE = re.compile(
    r"""fill\(\s*['"]#prompt['"]\s*,\s*(['"])(.*?)\1""", re.S)
PROMPT_DICT_RE = re.compile(
    r"""['"]prompt['"]\s*:\s*(['"])(.*?)\1""", re.S)

#: 视频模式的判据（参数里出现这些 key 才算视频生成参数）
VIDEO_PARAM_KEYS = ("model_key", "duration", "width", "height",
                    "gen_type", "continuation")

#: 「这里正在断言错误」的上下文（`assertRaises` / `pytest.raises`）
RAISES_RE = re.compile(r"assertRaises|pytest\.raises")


def _iter_source_files():
    for d in SCAN_DIRS:
        base = REPO / d
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if path.name.startswith("_"):
                # 临时探针（_probe_*.py）不参与扫描
                continue
            yield path


def _reads_generation(text: str) -> bool:
    return any(marker in text for marker in GEN_TRIGGER_MARKERS)


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _in_empty_prompt_assertion(text: str, pos: int) -> bool:
    """这个位置是不是「断言空提示词**不被记录**」的用例？

    判据：上下文里出现 `not_recorded` / `is_not_recorded` /
    `empty_prompt` 这类**方法名**，或者紧接着就是一条
    `assertEqual(self._count(...), 0)` 式的计数断言。
    """
    lines = text.splitlines()
    idx = text.count("\n", 0, pos)
    window = "\n".join(lines[max(0, idx - 6):idx + 6])
    if re.search(r"not_recorded|is_not_recorded|empty_prompt|not be recorded",
                 window):
        return True
    # 紧接着（往下 4 行内）有一条「计数为 0」的断言
    tail = "\n".join(lines[idx:idx + 5])
    return bool(re.search(r"assertEqual\s*\(\s*self\._count\([^)]*\)\s*,\s*0",
                          tail))


def _in_raises_block(text: str, pos: int) -> bool:
    """这个位置是不是在「断言会抛错」的块里？

    判据：从**最近**的 `assertRaises` / `pytest.raises` 出发，往后找
    最近的 `)` 结束（缩进回到同级或更浅）—— 太脆。
    改用**简单可靠**的判据：取该位置**往前 12 行**与**往后 3 行**，
    里面出现 `assertRaises` / `pytest.raises` 就认为是错误断言块。
    """
    lines = text.splitlines()
    idx = text.count("\n", 0, pos)
    window = lines[max(0, idx - 12):idx + 3]
    return any(RAISES_RE.search(line) for line in window)


# ------------------------------------------------------------------
# A1 —— 4 步必须带加速 LoRA
# ------------------------------------------------------------------

#: 已知的「加速 LoRA 键 / 权重文件名」-> 推荐步数。
#: 来源：项目内既有的加速 LoRA 清单（h3studio/video_models.py）。
ACCEL_RECOMMENDED_STEPS: dict[str, int] = {
    "ckpt850": 4,
    "v4step600": 4,
    "lightx2v_4step_768p": 4,
    "lightx2v_4step_sla_768p": 4,
    "dmad_4step_critic": 4,
    "lightx2v_4step_v01_comfy": 4,
    "lightx2v_sla_768p_root": 4,
    "lightx2v_8step": 8,
    # 权重文件名形态（老脚本里直接写文件名的）
    "minimax_h3_turbo_4step_ema_ckpt850": 4,
    "minimax_h3_turbo_v4_step600_ema": 4,
    "minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16": 4,
    "minimax_h3_fl2v_turbo_4step_v0.1_768p_sla_comfyui_bf16": 4,
    "dmad_minimax_h3_4step_full_critic_comfyui": 4,
    "minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16": 8,
}


def _accel_keys_in(text: str) -> set[str]:
    keys: set[str] = set()
    for m in ACCEL_VALUE_RE.finditer(text):
        raw = m.group(1)
        base = raw.rsplit("/", 1)[-1].replace(".safetensors", "")
        if not base:
            continue
        # 「取值是变量名」而不是档位键名（例如 `case["accel"]` ->
        # ACCEL_VALUE_RE 会把 `case` 误当成一个键）。
        if base in ("case", "cfg", "params", "out", "self", "item"):
            continue
        keys.add(base)
    return keys


def _has_dynamic_accel(text: str) -> bool:
    """加速 LoRA 的取值来自**变量**（源码层面判定不了具体档位）。"""
    for m in ACCEL_DYNAMIC_RE.finditer(text):
        value = m.group(1)
        # 排除掉「纯字面量」与常见的非档位变量名
        if value in ("case", "cfg", "params", "out", "self"):
            continue
        if "accel" in value.lower():
            return True
    # `case["accel"]` / `cfg["accel_lora"]` 这种**下标取值**同样是
    # 「档位由数据决定」—— 源码层面判定不了具体是哪一枚，
    # 交给运行时断言（这类文件通常是**档位矩阵**测试）。
    for m in re.finditer(r"""\[["']accel(?:_lora|_loras)?["']\]""", text):
        return True
    return False


def _is_video_param_block(text: str, pos: int) -> bool:
    """这个位置附近是不是**视频生成参数**？"""
    window = text[max(0, pos - 600):pos + 600]
    return any(key in window for key in VIDEO_PARAM_KEYS)


def collect_a1_violations() -> list[str]:
    """返回 A1 违规清单（每条一个可读的问题描述）。"""
    problems: list[str] = []
    for path in _iter_source_files():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        if not _reads_generation(text):
            continue
        rel = path.relative_to(REPO)
        accel_keys = _accel_keys_in(text)
        dynamic_accel = _has_dynamic_accel(text)
        for m in STEPS_RE.finditer(text):
            steps = int(m.group(1))
            if steps != 4:
                continue
            line = _line_of(text, m.start())
            if not _is_video_param_block(text, m.start()):
                continue
            if not accel_keys:
                if dynamic_accel:
                    # 取值来自变量 -> 源码层面判定不了，交给运行时断言
                    continue
                problems.append(
                    f"{rel}:{line} —— 视频生成参数 steps=4 但**没有任何加速 LoRA 字段**"
                    f"（4 步是蒸馏档，必须配加速 LoRA）")
                continue
            if not any(ACCEL_RECOMMENDED_STEPS.get(k) == 4
                       for k in accel_keys):
                problems.append(
                    f"{rel}:{line} —— 视频生成参数 steps=4 但加速 LoRA "
                    f"{sorted(accel_keys)} 里没有一枚是 4 步档")
    return problems


# ------------------------------------------------------------------
# A2 —— 提示词非空且不含测试占位词
# ------------------------------------------------------------------

def collect_a2_violations() -> list[str]:
    """返回 A2 违规清单。"""
    problems: list[str] = []
    for path in _iter_source_files():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        if not _reads_generation(text):
            continue
        rel = path.relative_to(REPO)
        for regex in (PROMPT_FILL_RE, PROMPT_DICT_RE):
            for m in regex.finditer(text):
                literal = m.group(2)
                line = _line_of(text, m.start())
                if not literal.strip():
                    # 空提示词：只有在「断言空提示词**不被记录/被拒**」
                    # 的块里才合法 —— 那里空串是**被测行为**，不是提交内容。
                    if _in_raises_block(text, m.start()) or \
                            _in_empty_prompt_assertion(text, m.start()):
                        continue
                    problems.append(
                        f"{rel}:{line} —— 提示词是空串（模型无从知道要生成什么）")
                    continue
                hits = gen_flow.has_placeholder(literal)
                if hits:
                    problems.append(
                        f"{rel}:{line} —— 提示词含测试占位词 {hits}："
                        f"{literal[:80]!r}")
    return problems


# ------------------------------------------------------------------
# A3 —— 生成请求的 prompt 等于增强后的 prompt（E2E 真跑）
# ------------------------------------------------------------------
# A3 是一条**真浏览器**用例，放在 tests/e2e/test_gen_flow_contract.py。
# 这里是它依赖的静态部分：确认「增强后的文本」与「提交时的文本」必须
# 是**同一份**，且不是原文。


def test_a1_four_steps_must_carry_accel_lora():
    """A1：4 步的视频生成参数必须带 4 步档的加速 LoRA。"""
    problems = collect_a1_violations()
    assert not problems, (
        "【A1 违规】4 步是蒸馏档，必须配加速 LoRA —— "
        f"发现 {len(problems)} 处：\n  " + "\n  ".join(problems))


def test_a2_prompts_are_meaningful_and_non_empty():
    """A2：提交用的提示词非空，且不含测试占位词。"""
    problems = collect_a2_violations()
    assert not problems, (
        "【A2 违规】提示词必须是对模型有意义的描述，"
        f"不得含 e2e / 第X段 / XXXX 等占位词 —— "
        f"发现 {len(problems)} 处：\n  " + "\n  ".join(problems))


def test_a3_shared_prompt_catalog_has_no_placeholders():
    """A3 的静态部分：共享提示词表本身必须干净且非空。"""
    assert gen_flow.GEN_PROMPTS, "共享提示词表是空的"
    for key, text in gen_flow.GEN_PROMPTS.items():
        assert text.strip(), f"提示词 {key} 是空串"
        hits = gen_flow.has_placeholder(text)
        assert not hits, f"提示词 {key} 含占位词 {hits}：{text[:80]!r}"


def test_a4_build_video_params_never_emits_bare_four_steps():
    """A4：共享参数组装器不允许产出「4 步 + 无加速 LoRA」的组合。"""
    with pytest.raises(AssertionError):
        gen_flow.build_video_params("一只橘猫在草地上奔跑", steps=4,
                                    accel_key="")
    # 正常路径：4 步必须自带一枚加速 LoRA
    params = gen_flow.build_video_params("一只橘猫在草地上奔跑", steps=4)
    assert params["steps"] == 4
    assert params["accel_lora"], "4 步却没有加速 LoRA"
    assert params["accel_loras"], "4 步却没有加速 LoRA 数组"
    assert params["accel_loras"][0]["key"] == params["accel_lora"]
    # 非蒸馏档（20 步）必须**报错**，不能静默生成一条错路径
    with pytest.raises(AssertionError):
        gen_flow.build_video_params("一只橘猫在草地上奔跑", steps=20)


def test_a5_accel_for_steps_picks_recommended_four_step_lora():
    """A5：加速 LoRA 的选取走**实测清单**，且挑的是推荐 4 步的那枚。"""
    catalog = [
        {"key": "speed_slider", "available": True, "compatible": True,
         "selectable": True, "recommended_steps": 0},
        {"key": "better_motion", "available": True, "compatible": True,
         "selectable": True, "recommended_steps": 20},
        {"key": "ckpt850", "available": True, "compatible": True,
         "selectable": True, "recommended_steps": 4,
         "recommended_strength": 1.0},
        {"key": "v4step600", "available": True, "compatible": True,
         "selectable": True, "recommended_steps": 4,
         "recommended_strength": 0.9},
    ]
    key, strength = gen_flow.accel_for_steps(4, catalog)
    assert key == "ckpt850", f"挑到了 {key}，期望清单里第一个 4 步档"
    assert strength == 1.0

    # 本机一枚 4 步档都没有 -> **报错**，不静默降级
    only_non_4step = [c for c in catalog if c["key"] != "ckpt850"
                      and c["key"] != "v4step600"]
    with pytest.raises(AssertionError):
        gen_flow.accel_for_steps(4, only_non_4step)

    # 没有实测清单 -> 退到分组兜底，且仍是 4 步档
    key2, strength2 = gen_flow.accel_for_steps(4, None)
    assert key2 in gen_flow.ACCEL_4STEP_KEYS
    assert strength2 == 1.0
    key3, _ = gen_flow.accel_for_steps(8, None,
                                        fallback=gen_flow.ACCEL_8STEP_KEYS)
    assert key3 in gen_flow.ACCEL_8STEP_KEYS


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
