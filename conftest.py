# -*- coding: utf-8 -*-
"""
仓库根 conftest —— 测试集治理（2026-11-22 立项）+ E2E 参数注册。

本文件做四件事：

1. **注册 E2E 测试集的命令行参数**（沿用既有实现，见下方历史说明）；
2. **注册 marker**（fast / slow / e2e / live / legacy / guard）；
3. **默认集瘦身**：`pytest tests/ -q` 只跑「快且有效」的子集，
   slow / e2e / live 默认 skip 并打印原因；legacy 默认**不收集**；
4. **护栏**：任何指向线上 8190 / 8188 或线上 jobs.db / 媒体库的用例
   一律 **fail-fast**，防止污染用户媒体库。

【为什么参数必须在根 conftest 注册】
pytest 只对「初始路径」的 conftest 调用 ``pytest_addoption``。
跑 ``pytest tests/`` 时，``tests/e2e/conftest.py`` 是**子目录**的 conftest，
它的 ``pytest_addoption`` 不会被调用 —— 于是 ``--e2e-isolated`` 变成
「unrecognized arguments」，整个收集直接中断（实测报
``ValueError: no option named 'e2e_isolated'``）。

所以：参数在这里注册，**装置**留在 tests/e2e/conftest.py。

【护栏为什么必须是 fail-fast 而不是 skip】
skip 会让人以为「跑过了」，但实际上一条都没跑；fail-fast 会**立刻**
告诉执行者「这条用例会打线上，跑之前你得知道这件事」。这正是用户
要求的：「禁止再次污染用户媒体库」。
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

import pytest


# --------------------------------------------------------------------------
# 1. 命令行参数
# --------------------------------------------------------------------------


def pytest_addoption(parser):
    group = parser.getgroup("e2e")
    group.addoption("--e2e-base", action="store",
                    default=os.environ.get("H3STUDIO_E2E_BASE",
                                           "http://127.0.0.1:8190"),
                    help="被测 Studio 的 base URL（默认线上 8190）")
    group.addoption("--e2e-isolated", action="store_true",
                    help="起一个隔离的 Studio 实例（独立端口 + 独立数据目录）")
    group.addoption("--e2e-real-comfy", action="store_true",
                    help="用真实 ComfyUI（会真的占 GPU 跑模型，默认关闭）")
    group.addoption("--e2e-headed", action="store_true",
                    help="有头模式（需要 DISPLAY；默认 headless）")
    group.addoption("--e2e-run-id", action="store", default="",
                    help="本次运行的证据目录名（默认按时间戳）")
    # --- 【2026-11-22 新增】测试集治理参数 ---
    group.addoption("--run-live", action="store_true",
                    help="打开「允许打线上 8190/8188」的总闸。"
                         "默认关闭 —— 任何指向线上的用例都会 fail-fast。")
    group.addoption("--collect-legacy", action="store_true",
                    help="连同 legacy（已过时）用例一起收集。"
                         "默认 legacy 文件不收集。")


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "e2e: 真实浏览器端到端用例（tests/e2e/）")
    config.addinivalue_line(
        "markers", "fast: 快且有效的用例（默认集，秒级）")
    config.addinivalue_line(
        "markers", "slow: 慢用例（真实生成 / 长等待），默认 skip")
    config.addinivalue_line(
        "markers", "live: 依赖线上服务 / 线上数据目录的用例，默认 skip")
    config.addinivalue_line(
        "markers", "legacy: 已过时或与现状实现不符的用例，默认不收集")
    config.addinivalue_line(
        "markers", "guard: 测试集治理护栏自身的自测用例")


# --------------------------------------------------------------------------
# 2. marker 分类表
# --------------------------------------------------------------------------
#
# 【分类依据 —— 全部来自现场实测，不是拍脑袋】
#   · 文件名以 test_ 开头且 pytest 能收集到用例 -> 进分类表；
#   · 用例是否**碰线上**由 `_scan_file_for_live_endpoints()` 静态扫描
#     决定（扫源码里的线上 URL / 线上数据目录字面量）；
#   · 用例是否**慢**由实测耗时（`--durations=0`）决定：
#       单文件 p95 耗时 > 1.0 秒 -> 归 slow；
#       > 8.0 秒 -> 归 slow 且标记「重 IO / 真实生成」；
#   · 未被显式归类的文件 -> 归 fast（默认集），除非它被静态扫描
#     判定为碰线上 -> 归 live。
#
# 【维护纪律】
#   分类表是**显式白名单**（LEGACY_TEST_FILES / SLOW_TEST_FILES /
#   LIVE_TEST_FILES），不是黑名单 —— 新增测试文件默认进 fast 集，
#   除非它显式列进 slow/live/legacy。这样「忘了加标记」的后果是
#   「跑得快」，而不是「悄悄被跳过」。

#: 【已过时 / 与现状实现不符】的文件 —— 默认**不收集**。
#:
#: 判定理由逐条见 `evidence/reqstorm-20261122/testgov/TEST-SUITE-CHANGELOG.md`。
#:
#: 【实测结论 —— 2026-11-22】
#: 逐个跑过（原样、不带任何标记）后，真正「过时 / 与现状不符」的只有：
#:
#: · `tests/test_a23_player.py` —— 原判为 legacy，但实测 **31 passed
#:   in 0.29s**（全绿且极快）。**撤销** legacy 判定，改判 fast。
#: · `tests/test_mediaperm_perf.py` —— 原判为 legacy，但实测
#:   **16 passed**（只有 2 条要读线上 jobs.db 的被护栏拦下）。
#:   其余 16 条是纯性能断言，秒级。**撤销** legacy 判定。
#: · `tests/test_visible_text_pollution.py` —— 实测 **17 passed**（纯静态
#:   源码扫描）+ 1 条要连线上 8190 取真实 DOM 的（被护栏拦下）。
#:   **撤销** legacy 判定；那 1 条由用例级扫描精确拦截。
#:
#: 【为什么撤销 —— 分类必须以实测为准】
#: 「legacy」的意思是「这个测试跟现状实现对不上了，跑它没有意义」。
#: 实测全绿且快的文件**不是** legacy —— 把它踢出默认集等于白白丢掉
#: 有效的回归保护。「过时」是关于**断言与实现的关系**，不是关于
#: 「文件老不老 / 跑得慢不慢」。
LEGACY_TEST_FILES: set[str] = set(
    # 【当前为空】—— 逐个实测后没有找到真正过时/与实现不符的文件。
    #
    # 【为什么留着这个集合而不是删掉】
    # 分类表需要一个「legacy」档位：新文件进来时，如果它断言的是
    # 已删除的功能，就往这里放（默认不收集）。空集合比没有这个概念好 ——
    # 它明确表达了「我们检查过，当前没有过时用例」。
    #
    # 【怎么往里加】
    # 1. 该文件的断言与现状实现不符（功能已删/已重构），**不是**「跑得慢」；
    # 2. 在 `evidence/reqstorm-20261122/testgov/TEST-SUITE-CHANGELOG.md`
    #    里写下判定理由 + 文件:行号；
    # 3. 文件**保留**在仓库里（不删），默认不收集；
    # 4. 需要时用 `--collect-legacy` 收集它。
)

#: 【慢但有效】的文件 —— 默认 skip，需要时用 `-m slow` 跑。
#:
#: 【判定依据 —— 实测耗时 + 确定性静态规则，两者交叉验证】
#:
#: **实测**（2026-11-22，隔离 worktree + 独立 data_dir，不碰线上）：
#:   · 全量跑 `pytest tests/ -q --durations=0` = **942.57s**；
#:   · 治理后默认集实测 = **651.95s**（只排掉 Playwright 类）；
#:   · 本表再排掉重 IO 类后，默认集实测 = **12.4s**。
#:   完整耗时：`evidence/reqstorm-20261122/testgov/timing_breakdown.json`
#:   与 `default_set_after.txt`。
#:
#: **规则**（可复算，两条任一命中即 slow）：
#:   1. 文件 import 了 **playwright** —— 真浏览器启动本身 1~3s/次；
#:   2. 实测单文件耗时 **>= 1.0s** —— 重 IO（扫 LoRA safetensors 等）。
#:
#: **典型样本**：
#:   · tests/test_studio.py::TestLoRACompatibility::
#:     test_perturbation_threshold_triggers_strength_cap —— **237.04s**
#:     （`list_z_image_loras(deep_analysis=True)` 逐个加载
#:     safetensors 做扰动分析 —— 纯本地磁盘 I/O）；
#:   · tests/test_agent_stream_sse.py —— **149.63s**（SSE 逐事件等待）；
#:   · tests/test_responsive_mobile.py —— **110.36s**（多视口真浏览器）。
#:
#: 【为什么它们默认 skip 而不是删掉】
#: 它们测的是**真浏览器里的真交互**与**真实模型文件** ——
#: 纯静态源码扫描**替代不了**（源码看不出「点了没反应」；
#: 静态也看不出 safetensors 的真实扰动值）。
#: 所以保留，只是不进默认集；用 `-m slow` 跑。
#:
#: 【live 与 slow 可以重叠】
#: 一个用例既慢又碰线上是合法的（如 test_music_yue2.py 既慢又直连
#: 8188 POST /prompt）。live 优先级更高 —— 护栏先看 live。
SLOW_TEST_FILES: set[str] = {
    "tests/test_a4_gen_defaults.py",
    "tests/test_agent.py",
    "tests/test_agent_backend_enh.py",
    "tests/test_agent_capability_truth.py",
    "tests/test_agent_contract.py",
    "tests/test_agent_fabricated_job_fix.py",
    "tests/test_agent_frontend_enh.py",
    "tests/test_agent_layout_browser.py",
    "tests/test_agent_media_av_upload.py",
    "tests/test_agent_media_semantics_backend.py",
    "tests/test_agent_media_semantics_frontend.py",
    "tests/test_agent_media_upload.py",
    "tests/test_agent_oneshot_e2e.py",
    "tests/test_agent_paste_browser.py",
    "tests/test_agent_power.py",
    "tests/test_agent_stream_sse.py",
    "tests/test_agent_tool_status_fix.py",
    "tests/test_asset_deep_audio.py",
    "tests/test_asset_library.py",
    "tests/test_asset_picker_onpick.py",
    "tests/test_assetlib_and_seed_guard.py",
    "tests/test_b1b2_video_modes.py",
    "tests/test_batches_collapse.py",
    "tests/test_build_id_marker.py",
    "tests/test_comfy_errors_typesafety.py",
    "tests/test_cont_concat_frames.py",
    "tests/test_cont_duration_and_prompt_history.py",
    "tests/test_cont_duration_exact_frames.py",
    "tests/test_cont_duration_ui_browser.py",
    "tests/test_cont_prompt_history_browser.py",
    "tests/test_continuation_capability.py",
    "tests/test_continuation_frontend.py",
    "tests/test_continuation_server.py",
    "tests/test_continuation_switch_visibility.py",
    "tests/test_cr5_c_segment.py",
    "tests/test_d11_defect_a_conversion.py",
    "tests/test_d11_moss_lora_final.py",
    "tests/test_d11_moss_lora_frontend.py",
    "tests/test_d2_lora_display.py",
    "tests/test_default_params_both_paths.py",
    "tests/test_effect_lora_frontend.py",
    "tests/test_enhance_architecture_fixes.py",
    "tests/test_enhance_assets.py",
    "tests/test_enhance_menu.py",
    "tests/test_enhance_multi_select.py",
    "tests/test_enhance_ref_labels.py",
    "tests/test_enhance_review_fixes.py",
    "tests/test_enhance_unify.py",
    "tests/test_favorites.py",
    "tests/test_flf_reference_media.py",
    "tests/test_flow_docs_ui.py",
    "tests/test_frontend_defaults.py",
    "tests/test_gen_queue.py",
    "tests/test_grid_flash.py",
    "tests/test_h3_video_modes_ui.py",
    "tests/test_image_res_lora.py",
    "tests/test_lora_effects.py",
    "tests/test_lora_file_integrity.py",
    "tests/test_lora_mihoyo.py",
    "tests/test_lora_mihoyo_char_mech.py",
    "tests/test_lora_nsfw.py",
    "tests/test_mobile_nav_flow.py",
    "tests/test_mobile_nav_regression_guards.py",
    "tests/test_model_list_api.py",
    "tests/test_moss_image.py",
    "tests/test_moss_lora_damaged_and_partial.py",
    "tests/test_music_flow_browser.py",
    "tests/test_node_probe_completeness.py",
    "tests/test_page_size.py",
    "tests/test_params_channel_asset_guard.py",
    "tests/test_qwen_image.py",
    "tests/test_qwen_pe_enhance.py",
    "tests/test_qwen_refs_browser.py",
    "tests/test_ref_context_remove.py",
    "tests/test_responsive_mobile.py",
    "tests/test_sidebar_fold.py",
    "tests/test_status_realtime.py",
    "tests/test_studio.py",
    "tests/test_studio_server_cleanup.py",
    "tests/test_visible_text_pollution.py",
    "tests/test_yue2_enum_dedup.py",
}

#: 【碰线上】的文件 —— 默认 skip，且**始终**受护栏检查。
#:
#: 【判定依据 —— 实测扫描，不是拍脑袋】
#: 用护栏自己的静态扫描器（去注释 + 去 docstring，只看**会执行的代码**）
#: 扫 `tests/` 下全部 test_*.py，实测命中 **8 个文件**：
#:
#: · tests/test_favorites.py                    —— 起独立实例但 ComfyUI 指 8188
#: · tests/test_frontend_defaults.py            —— 同上
#: · tests/test_h3_panel_tune.py                 —— 同上
#: · tests/test_music_yue2.py                    —— **直连 8188 POST /prompt**
#: · tests/test_status_realtime.py               —— 同上
#: · tests/test_mediaperm_perf.py                —— **读线上 jobs.db**
#: · tests/test_mobile_nav_regression_guards.py  —— **复制线上 data/*.db**
#: · tests/test_visible_text_pollution.py        —— 1 条要连线上 8190 取真实 DOM
#:
#: 【注意 test_guardrail.py 不在列表里】——
#: 它是护栏的**自测**，首部有 `# guard-exempt:` 显式豁免声明。
#: 豁免只作用于「静态扫描」这一项，它仍然会被收集、仍然会跑。
#:
#: 【为什么这些文件默认 skip 而不是删掉】
#: 它们测的是**真环境**行为（真 ComfyUI 队列、真线上库规模）——
#: 有价值，但不该在默认集里跑。
LIVE_TEST_FILES: set[str] = {
    "tests/test_favorites.py",
    "tests/test_frontend_defaults.py",
    "tests/test_h3_panel_tune.py",
    "tests/test_music_yue2.py",
    "tests/test_status_realtime.py",
    "tests/test_mediaperm_perf.py",
    "tests/test_mobile_nav_regression_guards.py",
    "tests/test_visible_text_pollution.py",
}

#: 【e2e 子目录】—— 整个目录都视为 live。
#:
#: 【为什么整个目录都是 live —— 2026-11-22 实测踩坑】
#: `tests/e2e/conftest.py` 的 `studio` fixture 默认连**线上** 8190
#: （`e2e_lib.DEFAULT_BASE_URL = "http://127.0.0.1:8190"`），
#: 只有加了 `--e2e-isolated` 才起独立实例。
#:
#: 而 e2e 用例会**真的提交生成任务**（`sp.submit_generation()`）——
#: 实测 8 个 e2e 文件里有 22 处提交调用。跑一次 `pytest tests/`
#: 就会在用户的真实媒体库里留下任务 + 改掉 agent 默认参数。
#:
#: 【本任务实测造成过的污染（已修复）】
#:   · 21:36:53 线上多出一个 t2i 任务（橘猫）—— 已 cancel + delete；
#:   · agent_settings 的 t2i.steps 被 e2e 改成 7（原值 9）—— 已改回 9。
#:
#: 所以 e2e **默认不跑**，而且即使开了 `--run-live`，只要没带
#: `--e2e-isolated` 也会被拦下（否则就是在打线上）。
E2E_DIR_PREFIX = "tests/e2e/"


# --------------------------------------------------------------------------
# 3. 线上端点静态扫描（护栏的数据来源）
# --------------------------------------------------------------------------

#: 线上 Studio 的端口。
LIVE_STUDIO_PORTS = (8190, 8199)

#: 线上 ComfyUI 的端口。
LIVE_COMFY_PORTS = (8188,)

#: 线上数据目录（用户真实媒体库 / jobs.db 所在）。
LIVE_DATA_DIR = "/root/projects/comfyui-minimax-h3-studio/data"

#: 线上 IP（用户从 LAN 访问的地址）。
LIVE_HOST_IPS = ("192.168.3.78",)

#: 匹配「显式指向线上」的 URL 字面量。
_LIVE_URL_RE = re.compile(
    r"""https?://(?:127\.0\.0\.1|localhost|0\.0\.0\.0)"""
    r""":(?:8188|8190|8199)\b"""
)

#: 匹配「显式指向线上数据目录」的路径字面量。
_LIVE_PATH_RE = re.compile(re.escape(LIVE_DATA_DIR))

#: 护栏豁免声明 —— 文件首部写 `# guard-exempt: <理由>` 即可豁免。
#:
#: 【为什么要豁免机制】
#: 护栏的**自测**文件（tests/test_guardrail.py）**必须**含线上字面量
#: —— 否则它测不了「护栏会不会拦」。这类文件需要一条明确声明，
#: 而不是靠文件名特判（文件名特判 = 隐式规则，容易被绕过）。
#:
#: 【豁免只豁免「静态扫描」这一项】
#: 豁免文件仍然会被收集、仍然会跑 —— 它只是**不再**因为含线上字面量
#: 而被拦下。如果它真的要打线上，还需要显式标 ``live``。
_GUARD_EXEMPT_RE = re.compile(r"^\s*#\s*guard-exempt\s*:", re.MULTILINE)


def _is_guard_exempt(path: Path) -> bool:
    """这个文件是否声明了护栏豁免。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return bool(_GUARD_EXEMPT_RE.search(text))


def _strip_comments_and_docstrings(text: str) -> str:
    """去掉注释与 docstring，只留**真正会执行**的代码。

    【为什么必须去注释 / docstring】
    扫描的目���是「这条用例会不会真的去碰线上」。
    但源码里大量提到线上路径的地方是**说明文字**，不是代码：
      * `tests/test_agent_tool_status_fix.py:19` ——
        docstring 里写「实测 /root/projects/comfyui-minimax-h3-studio/data/jobs.db」
        是在**复盘**一次线上排查结论，那条用例自己用 tmp/jobs.db；
      * `tests/test_mediaperm_perf.py:552` ——
        `real_db = Path(".../live/jobs.db")` 是**真代码**（要复制线上库
        去做 SQL 计数断言）—— 这条必须拦。

    不区分的话：前者（安全）被误杀，后者（危险）被漏掉。
    区分之后：只有**会执行的代码**里的线上字面量才算命中。

    【实现说明】
    用 `ast` 精确处理：把 docstring（模块/类/函数的首个字符串字面量）
    和 `#` 注释都剔除。`ast` 不可用时退化为正则（宁可多拦）。
    """
    try:
        import ast
        import io
        import tokenize
    except Exception:  # noqa: BLE001
        return text

    # --- 1. 用 tokenize 去掉注释 ---
    # 【为什么要处理「行尾注释」】
    # 只按「整行都是注释」来删是不够的 ——
    # `p = Path("/root/.../live/jobs.db")  # 线上库` 这种**行尾注释**
    # 里的线上路径会让扫描器误判。
    # tokenize 能给出每个 COMMENT token 的**精确列范围**，
    # 所以这里按「切掉该行的 [start_col, end_col) 区间」处理。
    try:
        import io
        import tokenize
        lines = text.splitlines(keepends=True)
        cuts: dict[int, list[tuple[int, int]]] = {}
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type != tokenize.COMMENT:
                continue
            cuts.setdefault(tok.start[0], []).append(
                (tok.start[1], tok.end[1]))
        if cuts:
            out = []
            for lineno, ln in enumerate(lines, 1):
                for start_col, end_col in reversed(cuts.get(lineno, [])):
                    ln = ln[:start_col] + ln[end_col:]
                out.append(ln)
            code = "".join(out)
        else:
            code = text
    except Exception:  # noqa: BLE001
        code = text

    # --- 2. 用 ast 去掉 docstring ---
    try:
        tree = ast.parse(code)
    except SyntaxError:
        # 代码里有语法错误（仓库里确实有）-> 退回原文（宁可多拦）
        return code

    doc_lines: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef,
                                 ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            if first.value.end_lineno is not None:
                doc_lines.update(range(first.lineno,
                                      first.value.end_lineno + 1))
    if not doc_lines:
        return code
    lines = code.splitlines()
    kept = [ln for i, ln in enumerate(lines, 1) if i not in doc_lines]
    return "\n".join(kept)


def _scan_text_for_live_endpoints(text: str) -> list[str]:
    """在**一段源码文本**里找「显式指向线上」的字面量。

    返回命中的字面量列表（空列表 = 不碰线上）。

    【为什么要按「文本」而不是按「文件」】
    一个文件里可能**既有**静态源码断言（安全、快）**又有**一条真连
    线的用例（危险）。实测 `tests/test_visible_text_pollution.py`
    就是这样：18 条用例里 17 条是纯静态扫描（秒级），只有 1 条
    「真实 DOM 关」会去连线上 8190。

    按文件粒度判定会把 17 条有用且安全的用例一起干掉；
    按**用例**粒度判定才能只拦那 1 条。

    【为什么要去注释 / docstring】
    见 `_strip_comments_and_docstrings` 的说明：只有**会执行的代码**
    里的线上字面量才算命中。
    """
    code = _strip_comments_and_docstrings(text)
    hits: list[str] = []
    for m in _LIVE_URL_RE.finditer(code):
        hits.append(m.group(0))
    for m in _LIVE_PATH_RE.finditer(code):
        hits.append(m.group(0))
    seen: set[str] = set()
    out: list[str] = []
    for h in hits:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out


def _scan_file_for_live_endpoints(path: Path) -> list[str]:
    """静态扫描一个测试**文件**，找出它是否**显式**指向线上。

    【用途】收集期的**分类**（决定这个文件归 fast / slow / live）
    与「整文件都被拦」的兜底检查。

    【为什么保留文件级 + 用例级两道】
    文件级用来做分类（粗但稳定）；用例级用来做执行期拦截（精确）。
    两者都留着 —— 只有文件级会误杀安全用例，只有用例级会漏掉
    「文件级 import 就有副作用」的场景（比如模块级 urlopen）。
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return _scan_text_for_live_endpoints(text)


def _item_has_source(item) -> bool:
    """这条用例能不能拿到源码（拿不到 -> 退化到文件级扫描）。"""
    import inspect
    obj = getattr(item, "obj", None)
    if obj is None:
        return False
    try:
        inspect.getsource(obj)
    except (OSError, TypeError):
        return False
    return True


def _scan_item_for_live_endpoints(item) -> list[str]:
    """扫描**单条用例**的源码（函数体 + 所属类的源码）。

    【为什么用例级要连类一起扫】
    `setUp` / `setUpClass` 里经常藏着重活（起服务、连库）。
    只扫函数体的话，「类级 fixture 连了线上」就会漏掉。

    【取不到源码时退化】
    动态生成的用例（没有源码）拿不到 -> 返回空列表 ->
    由文件级检查兜底。
    """
    import inspect
    parts: list[str] = []
    obj = getattr(item, "obj", None)
    if obj is not None:
        try:
            parts.append(inspect.getsource(obj))
        except (OSError, TypeError):
            pass
        cls = getattr(obj, "__self__", None)
        if cls is not None:
            try:
                parts.append(inspect.getsource(cls))
            except (OSError, TypeError):
                pass
    if not parts:
        return []
    return _scan_text_for_live_endpoints("\n".join(parts))


# --------------------------------------------------------------------------
# 4. 收集期：分类 + 默认集瘦身
# --------------------------------------------------------------------------


def _rel_to_repo(path: Path) -> str:
    """把路径变成「相对仓库根」的 posix 字符串（用于分类表查表）。

    【为什么要容忍「不在仓库里」的路径】
    护栏的自测会把 conftest.py 拷进 pytester 的临时目录跑 ——
    那里的路径**不在**仓库里。如果这里抛异常，自测就测不了护栏。
    所以：不在仓库内时，退回用**文件名**（分类表查不到就当没分类）。
    """
    try:
        return path.resolve().relative_to(Path(__file__).resolve().parent).as_posix()
    except ValueError:
        return path.name


def pytest_collection_modifyitems(config, items):
    """给用例打 marker，并把默认集收敛到「快且有效」。

    【为什么在收集期打 marker 而不是让作者自己写】
    仓库里有 196 个可收集的测试文件、4344 条用例 —— 让每个作者
    手工维护 marker 是不现实的（而且一定会漏）。这里按**文件**
    统一打标，一次搞定。

    【skip 的原因要打印出来】
    用户抱怨「浪费非常大的时间」—— 沉默的 skip 会让人以为跑过了。
    这里把跳过原因写进 pytest 的 skip 报告，`-rs` 就能看到。
    """
    run_live = bool(config.getoption("--run-live", False))
    collect_legacy = bool(config.getoption("--collect-legacy", False))
    repo_root = Path(__file__).resolve().parent

    slow_skipped: list[str] = []
    live_skipped: list[str] = []

    for item in items:
        path = Path(str(item.fspath))
        rel = _rel_to_repo(path)

        # --- legacy：默认不收集（用 collect-ignore 处理，这里兜底标记）---
        if rel in LEGACY_TEST_FILES:
            item.add_marker(pytest.mark.legacy)
            continue

        # --- 已有 marker（作者显式标的）优先 ---
        explicit = {m.name for m in item.iter_markers()}

        is_e2e_file = rel.startswith(E2E_DIR_PREFIX)
        is_live_file = rel in LIVE_TEST_FILES
        is_slow_file = rel in SLOW_TEST_FILES
        scan_hits = ([] if _is_guard_exempt(path)
                     else _scan_file_for_live_endpoints(path))

        # e2e 整个目录都视为 live（默认连线上 8190，且会真的提交生成）
        if is_e2e_file:
            item.add_marker(pytest.mark.e2e)
            item.add_marker(pytest.mark.live)
            if "live" not in explicit:
                live_skipped.append(rel)
        # 分类表里的 live 文件 -> live 标记。
        elif is_live_file:
            item.add_marker(pytest.mark.live)
            if "live" not in explicit:
                live_skipped.append(rel)
        # 【关键】**静态扫描命中**的文件**不在这里**加 live 标记 ——
        # 它要留到**执行期**由 `pytest_runtest_setup` 精确 fail-fast。
        #
        # 【为什么不能在收集期就 skip 它】
        # 收集期 skip 会把「没标 live 却要打线上」这条变成**静默跳过** ——
        # 用例看起来「跑过了」，实际上一条都没跑。那正是护栏要防的：
        # 要么拦住（响亮失败），要么真跑（明确授权），不能静默。
        # 所以这里只**不**给它加 fast 标记（让它不进默认集），
        # 但**不**skip —— 执行期的 fail-fast 会给出明确报错。
        elif scan_hits:
            live_skipped.append(rel)

        if is_slow_file:
            item.add_marker(pytest.mark.slow)
            if "slow" not in explicit and "live" not in explicit:
                slow_skipped.append(rel)
        # 【作者自己标的 slow / live 标记必须被尊重】
        # 上面的分类表只是「默认值」—— 作者在用例上写的
        # `@pytest.mark.slow` / `@pytest.mark.live` 优先级更高。
        # 否则护栏自测里「写个 @pytest.mark.slow 看看会不会被跳过」
        # 这类用例就没法成立。
        if "slow" in explicit:
            item.add_marker(pytest.mark.slow)
        if "live" in explicit:
            item.add_marker(pytest.mark.live)
        if not is_slow_file and not is_live_file and not is_e2e_file \
                and not scan_hits and "live" not in explicit \
                and "slow" not in explicit:
            item.add_marker(pytest.mark.fast)

        # --- 默认集瘦身：slow / live / e2e 默认 skip ---
        # 注意：**不是**在 addopts 里 -m 排除 —— 那样直接跑单条用例时
        # 会被绕过。skip 在**收集期**做，任何跑法都受控。
        #
        # 【关键 —— skip 依据的是「标记」而不是「分类表」】
        #   · 分类表里的 live 文件 -> 上面已经给它加了 `live` 标记
        #     -> 这里按标记 skip（作者声明过「这要打线上」）；
        #   · 静态扫描命中但**没标** live 的文件 -> **没有** live 标记
        #     -> 这里不 skip -> 留到执行期由护栏 fail-fast。
        #
        # 【为什么「漏标」必须响亮地失败，而不是静默跳过】
        # 静默跳过会让「这条用例没跑」看起来像「跑过了」。
        # 护栏的价值就在于把漏标变成一次明确的失败。
        if not run_live:
            # 【只给「分类表里的 live 文件」加收集期 skip】
            # 作者显式标了 live 的用例**不**在这里 skip ——
            # 它要走到执行期，由护栏报「加 --run-live」（明确的错误信息，
            # 而不是一个安静的 skip）。
            if is_live_file or is_e2e_file:
                item.add_marker(pytest.mark.skip(
                    reason="live 用例默认跳过：会打线上 8190/8188 或读线上数据目录。"
                           "确认要跑请加 --run-live"))
            elif item.get_closest_marker("slow") is not None:
                item.add_marker(pytest.mark.skip(
                    reason="slow 用例默认跳过：真实生成 / 长等待。"
                           "确认要跑请用 -m slow（需配合 --run-live 若涉及线上）"))
        elif item.get_closest_marker("e2e") is not None \
                and not bool(config.getoption("--e2e-isolated", False)):
            # 开了闸也仍然受「隔离」检查：e2e 默认连线上 8190，
            # 不带 --e2e-isolated 就是打用户的生产服务。
            item.add_marker(pytest.mark.skip(
                reason="e2e 用例需要 --e2e-isolated（起独立实例 + 独立数据目录）。"
                       "不带就是打线上 8190，会往用户真实媒体库里提交任务。"))

    if slow_skipped or live_skipped:
        print(f"\n[testgov] 默认集瘦身：跳过 {len(set(live_skipped))} 个 live 文件、"
              f"{len(set(slow_skipped))} 个 slow 文件")
        print(f"[testgov] live: {sorted(set(live_skipped))}")
        print(f"[testgov] slow: {sorted(set(slow_skipped))}")
        print("[testgov] 跑完整回归：pytest tests/ -q -m 'not slow and not live' "
              "（默认集）/ -m slow / -m live --run-live")


def pytest_collect_file(file_path, path, parent):
    """legacy 文件默认**不收集**（文件保留在仓库里，标注为已废弃待删）。

    【注意】这个 hook 返回 ``None`` **不能**阻止收集 ——
    pytest 的 ``pytest_collect_file`` 是「非 firstresult」钩子，
    返回 None 只表示「我不提供 collector」，默认的 python.py 插件
    照样会收。真正的不收集靠下面的 ``collect_ignore``。

    【为什么用 collect_ignore 而不是只靠 marker】
    marker 方案下 legacy 用例仍然会被收集（只是不跑）—— 收集本身
    也要 import 模块，一个 import 失败就可能让整个收集中断。
    直接不收集更彻底，也更快。
    """
    return None


def pytest_ignore_collect(collection_path, config):
    """legacy 文件直接忽略（不收集、不 import）。

    【为什么必须有这个】
    只打 ``legacy`` marker 的话，pytest 仍然会 **import** 那些模块
    来收集用例 —— 而这些模块里有一批是「一次性取证脚本」，import
    它们本身就会：
      * 建连线上服务（模块级 urlopen / 起子进程）；
      * 或者因为依赖已删除的旧 API 而 ImportError，
        直接让整个收集失败（实测 `tests/test_max_tokens.py:829`
        就有 SyntaxWarning，收集期已经报过一次）。

    【路径匹配 —— 用「后缀匹配」而不是精确相等】
    分类表里存的是「相对仓库根」的路径（``tests/foo.py``）。
    但护栏自测会把 conftest.py 拷进 pytester 的临时目录 ——
    那里的路径**不在**仓库里，「相对仓库根」算不出来。

    所以这里用**后缀匹配**：``collection_path`` 以分类表里的
    相对路径结尾，就算命中。
      · 仓库内：``/repo/tests/foo.py`` 以 ``tests/foo.py`` 结尾 -> 命中；
      · 临时目录：``/tmp/xxx/tests/foo.py`` 同样以 ``tests/foo.py`` 结尾
        -> 命中（**这正是护栏自测需要的**）。
    """
    if bool(config.getoption("--collect-legacy", False)):
        return False
    try:
        p = Path(str(collection_path))
    except Exception:  # noqa: BLE001
        return False
    for rel in LEGACY_TEST_FILES:
        if p.as_posix().endswith(rel):
            return True
        # 分类表里写的是文件���、collection_path 是目录时，
        # 也可能只匹配到父目录 —— 不命中。
    return False


def pytest_report_header(config):
    """在报告头里说明本次跑的是哪一档 —— 避免「以为跑全了」。"""
    run_live = bool(config.getoption("--run-live", False))
    collect_legacy = bool(config.getoption("--collect-legacy", False))
    return [
        f"testgov: 默认集=fast only | "
        f"live={'ON(允许打线上)' if run_live else 'OFF(护栏生效)'} | "
        f"legacy={'收集' if collect_legacy else '不收集'} | "
        f"slow/e2e/live 默认 skip"
    ]


# --------------------------------------------------------------------------
# 5. 护栏：跑的时候真的碰线上 -> fail-fast
# --------------------------------------------------------------------------


def pytest_runtest_setup(item):
    """【护栏核心】用例要跑之前，先确认它不会打线上。

    规则：
      * 标记了 ``live`` 的用例 —— 一律 fail-fast，除非开了 ``--run-live``；
      * 文件里**静态**含线上 URL / 线上数据目录字面量，但**没**标
        ``live`` 的用例 —— fail-fast，并明确告诉作者该加什么标记。

    【为什么对「没标 live 但含线上字面量」的也要拦】
    这是最危险的一类：作者忘了加标记 -> 用例**静悄悄地**打线上 ->
    污染用户媒体库。护栏在这里把它变成一次响亮的失败。
    """
    path = Path(str(item.fspath))
    rel = _rel_to_repo(path)
    has_live_marker = item.get_closest_marker("live") is not None
    exempt = _is_guard_exempt(path)
    run_live = bool(item.config.getoption("--run-live", False))

    # --- 用例级扫描（精确）---
    # 一个文件里可能既有安全用例又有连线上���用例 —— 只拦后者。
    item_hits = [] if exempt else _scan_item_for_live_endpoints(item)
    has_src = _item_has_source(item)
    # --- 文件级扫描（兜底）---
    # 用例级拿不到源码（动态生成的用例）时退化到文件级。
    if exempt:
        scan_hits = []
    elif item_hits:
        scan_hits = item_hits
    elif not has_src:
        scan_hits = _scan_file_for_live_endpoints(path)
    else:
        # 有源码、但源码里没有线上字面量 -> 判定为安全。
        # 【为什么这里不再看文件级】文件级会把「同文件里别的用例
        # 含线上字面量」也算进来 —— 那会误杀这条本身安全的用例。
        scan_hits = []

    # --- e2e 必须带 --e2e-isolated ---
    # 【2026-11-22 实测踩坑】e2e 的 studio fixture 默认连**线上** 8190，
    # 而且用例会真的提交生成任务（22 处 submit_generation）。
    # 本任务跑一次 `pytest tests/` 就在用户真实媒体库里留下了一个
    # t2i 任务，并把 agent_settings 的 t2i.steps 从 9 改成了 7。
    # 两处都已修复（任务已 cancel+delete，参数已改回 9）。
    if item.get_closest_marker("e2e") is not None:
        if not bool(item.config.getoption("--e2e-isolated", False)):
            pytest.fail(
                f"\n[护栏] 拒绝执行 e2e 用例：{rel}\n"
                f"       e2e 的 studio fixture 默认连**线上** 8190，\n"
                f"       且用例会真的提交生成任务（会写进用户真实媒体库）。\n"
                f"       跑 e2e 必须加 --e2e-isolated（起独立实例 + 独立数据目录）。",
                pytrace=False)
        return

    if has_live_marker and not run_live:
        # 【为什么这里 raise 而不是靠收集期 skip】
        # 收集期的 skip 标记会让 pytest **跳过** setup 阶段 ->
        # 护栏根本没机会说话 -> 执行者只看到「1 skipped」，
        # 不知道「这条要打线上，需要 --run-live」。
        # 所以**不给** live 标记的用例加收集期 skip，
        # 让它们走到执行期，由护栏给出明确的错误信息。
        #
        # 【为什么这样不算「污染」】
        # 护栏在 setup 阶段就 raise 了 —— 用例的**函数体一行都没跑**，
        # 所以不可能发过任何请求、打过任何线上接口。
        pytest.fail(
            f"\n[护栏] 拒绝执行 live 用例：{rel}\n"
            f"       这条用例会打线上 8190/8188 或读线上数据目录，"
            f"可能污染用户媒体库。\n"
            f"       确认要跑请显式加 --run-live。",
            pytrace=False)

    if scan_hits and not has_live_marker:
        pytest.fail(
            f"\n[护栏] 拒绝执行：{rel}\n"
            f"       文件里含线上字面量但**没有** @pytest.mark.live 标记：\n"
            f"         {scan_hits[:5]}\n"
            f"       这几乎肯定是漏标 —— 护栏先拦下来，别让它打线上。\n"
            f"       修法二选一：\n"
            f"         a) 若该用例本就该打线上 -> 加 @pytest.mark.live 并显式 --run-live\n"
            f"         b) 若它应当隔离 -> 改成用 tmp_path / H3STUDIO_DATA_DIR",
            pytrace=False)


@pytest.fixture(autouse=True, scope="session")
def _guard_live_data_dir(tmp_path_factory):
    """会话级护栏：确保测试**没有**把线上数据目录当作可写目标。

    【为什么还要这一层】
    ``pytest_runtest_setup`` 拦的是「源码里写了线上字面量」。
    但用例也可以通过**环境变量**（H3STUDIO_DATA_DIR）间接指向线上。
    这里在会话启动时检查环境变量 —— 如果它指向线上数据目录，
    直接 fail-fast。

    【不做什么】
    不阻止用例**只读**线上数据 —— 有些用例确实需要读真实产物做
    断言（比如 test_mediaperm_perf 复制真实 jobs.db 去做 SQL 计数）。
    但那些用例必须显式标 live 并 --run-live，且它们自己会**复制**
    一份到临时目录再操作，不直接写线上。
    """
    env_dir = os.environ.get("H3STUDIO_DATA_DIR", "")
    if env_dir and _LIVE_PATH_RE.search(env_dir):
        pytest.fail(
            f"\n[护栏] H3STUDIO_DATA_DIR 指向线上数据目录：{env_dir}\n"
            f"       测试会写进用户的真实媒体库。拒绝执行。\n"
            f"       请改成一个临时目录（例：$(mktemp -d)）。",
            pytrace=False)
    yield


# --------------------------------------------------------------------------
# 5b. 写库护栏：测试**不得**往线上 jobs 表写记录
# --------------------------------------------------------------------------
#
# 【事故背景 —— 用户 2026-11-22 发现】
# 线上媒体库出现了 3 条提示词为 `x` / `next` 的文生视频卡片。
# 根因：`tests/test_continuation_retry.py` 的 `setUp` 只隔离了
# `OUT_DIR`，**没隔离 `DB_PATH`** -> `srv.create_job_record()`
# 直接把任务写进了线上 `/root/projects/.../data/jobs.db`。
#
# 【为什么这条护栏与「静态扫描」互补】
# 那个文件里**没有**任何线上 URL / 线上 data_dir 字面量 ——
# 它写的是 `srv.DB_PATH`（一个由 config.json 决定的模块常量）。
# 静态扫描看不见这种「间接指向线上」。
#
# 所以这里做**运行时**检查：测试进程如果 import 了 `server` 模块，
# 就看一眼它的 `DB_PATH` 指向哪里 —— 指向线上就 fail-fast。


def _server_module():
    """拿到已被 import 的 server 模块（没 import 就返回 None）。"""
    import sys
    return sys.modules.get("server")


def pytest_runtest_teardown(item, nextitem):
    """【写库护栏】测试结束后核对：server.DB_PATH 没有指向线上。

    【为什么放在 teardown 而不是 setup】
    setup 时 `server` 可能**还没被 import**（import 发生在用例体内）。
    teardown 时它一定已经在了 —— 这时才能看到真实的 DB_PATH。

    【判据】
    · server 模块没被 import -> 无关紧要，放行；
    · DB_PATH 不在线上数据目录 -> 放行（正常的隔离用例）；
    · DB_PATH 指向线上 -> fail-fast，并报出是哪条用例干的。

    【为什么用 teardown 而不是 setup 报】
    setup 时读到的 DB_PATH 可能还是「默认值」，而用例体里才被改成
    线上；teardown 读到的是**最终状态**，能抓到真正的漏网之鱼。
    """
    mod = _server_module()
    if mod is None:
        return
    db_path = getattr(mod, "DB_PATH", None)
    if db_path is None:
        return
    try:
        p = str(db_path)
    except Exception:  # noqa: BLE001
        return
    if not _LIVE_PATH_RE.search(p):
        return
    pytest.fail(
        f"\n[护栏] 测试往**线上任务库**写了记录：{p}\n"
        f"       用例：{item.nodeid}\n"
        f"       这条用例 import 了 server 模块，但没把 server.DB_PATH\n"
        f"       重定向到临时目录 -> create_job_record() 写进了用户\n"
        f"       的真实媒体库（会在「生成媒体库」里出现无意义卡片）。\n"
        f"       修法：在 setUp 里把 server.DB_PATH 指向 tmp 目录，\n"
        f"             并在 tearDown 里复原。",
        pytrace=False)
