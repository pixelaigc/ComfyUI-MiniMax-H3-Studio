# -*- coding: utf-8 -*-
"""文生视频「面板所选 == 实际下发 == 生成记录」端到端验证（真实提交）。

【这个脚本做什么】

真实地跑一遍**修复后**的服务端逻辑（不启 HTTP 服务，直接调
server.py 里的真实函数），走完整链路：

    面板参数
      -> normalize_t2v_params（服务端归一层）
      -> create_job_record（落库）
      -> build_workflow（工作流拼装）
      -> 断言 ComfyUI prompt JSON 里的 UNETLoader.unet_name /
         MiniMaxH3TurboLoRA.lora_name / strength
      -> row_to_job（读回任务记录）
      -> 断言任务记录里的 model_info

这**不是**干跑：归一层、工作流拼装、落库、读回都是**真实代码路径**，
数据库用的是临时目录（不碰线上 data/jobs.db）。

【为什么不启 HTTP 服务】

`POST /api/jobs` 的 handler 里有一段逻辑（多段上传 / 素材库引用）依赖
aiohttp 的 multipart 解析；本次修复**不涉及**那段。用真实函数直接调用
能覆盖本次改动的全部路径，且不会往线上队列里塞真任务。

【哪些是真实跑通、哪些是静态推断 —— 明确区分】

  * 真实跑通：参数归一 / 工作流拼装（产出真正的 ComfyUI prompt JSON）/
    落库 / 读回 / 摘要计算 / 前端渲染（Node 里真跑 app.js 的函数）。
  * 静态推断：**最终成片**（本脚本不出图）；线上 HTTP 传输层
    （multipart 编码）与浏览器 DOM 组装，由 tests/test_t2v_card_dom.py
    与仓库既有浏览器验收脚本覆盖。

运行：python3 tests/verify_t2v_meta_e2e.py
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
NODE = shutil.which("node") or shutil.which("nodejs")

#: 面板上会出现的几组真实选择（key 取自 h3studio/video_models.py 的实测清单）
CASES = [
    {
        "name": "混合版 + DMAD 4 步（critic 版）×1.00（用户报出的那个组合）",
        "model_key": "hybrid_fl2va_ref2va",
        "accel": "dmad_4step_critic",
        "strength": 1.0,
        "fixed": "ckpt850",
    },
    {
        "name": "纯 H3 剪枝 int8 + LightX2V 4 步（768p 档）×0.75",
        "model_key": "fl2va_pruned_int8",
        "accel": "lightx2v_4step_768p",
        "strength": 0.75,
        "fixed": "ckpt850",
    },
    {
        "name": "纯 H3 完整 int8 + 不额外挂加速 LoRA",
        "model_key": "fl2va_int8",
        "accel": None,
        "strength": None,
        "fixed": "ckpt850",
    },
    {
        "name": "剪枝 w4a8 + 速度滑杆 ×1.00",
        "model_key": "fl2va_pruned_w4a8",
        "accel": "speed_slider",
        "strength": 1.0,
        "fixed": "v4step600",
    },
]


def run_case(case: dict, data_dir: Path) -> dict:
    """跑一个用例，返回断言结果。"""
    sys.path.insert(0, str(REPO))
    os.environ["H3STUDIO_DATA_DIR"] = str(data_dir)
    for mod in [m for m in list(sys.modules) if m == "server" or m.startswith("h3studio")]:
        del sys.modules[mod]
    import server as srv  # noqa: E402

    # 建表（真实代码路径：server.init_db）
    srv.init_db()

    # ---- 1. 面板参数（前端 submitParams 的产物形状）----
    params = {
        "prompt": "验证用提示词：雨夜霓虹街道，镜头缓慢推进。",
        "model_key": case["model_key"],
        "width": 864,
        "height": 480,
        "duration": 5,
        "steps": 4,
        "sampler": "euler",
        "scheduler": "beta",
        "fixed_accel_lora": case["fixed"],
        "accel_loras": ([] if not case["accel"]
                        else [{"key": case["accel"],
                               "strength": case["strength"],
                               "enabled": True}]),
        "attention": "default",
        "twopass": {"enabled": False},
    }

    # ---- 2. 服务端归一层（真实代码路径）----
    normalized, error = srv.normalize_t2v_params(dict(params))
    assert not error, f"归一层报错：{error}"

    # ---- 3. 工作流拼装（真实代码路径）-> ComfyUI prompt JSON ----
    normalized["job_id"] = "verifyjob001"
    normalized["mode"] = "t2v"
    workflow = srv.build_workflow("t2v", normalized)

    unet = workflow["6"]["inputs"]["unet_name"]
    lora_nodes = {
        nid: node for nid, node in workflow.items()
        if node.get("class_type") == "MiniMaxH3TurboLoRA"
    }
    lora_names = [n["inputs"]["lora_name"] for n in lora_nodes.values()]
    lora_strengths = [n["inputs"]["strength"] for n in lora_nodes.values()]

    # ---- 4. 落库 + 读回（真实代码路径）----
    job_id = srv.create_job_record("t2v", dict(normalized))
    with sqlite3.connect(data_dir / "jobs.db") as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    job = srv.row_to_job(row)
    info = job["model_info"]

    from h3studio import video_models as VM
    expected_unet = VM.VIDEO_MODELS[case["model_key"]]["unet"]
    expected_fixed_name = VM.VIDEO_ACCEL_LORAS[case["fixed"]]["name"]
    if case["accel"]:
        expected_accel_name = VM.VIDEO_ACCEL_LORA_COMBOS[case["accel"]]["name"]
        expected_accel_label = VM.VIDEO_ACCEL_LORA_COMBOS[case["accel"]]["display"]
    else:
        expected_accel_name = None
        expected_accel_label = ""

    # ---- 断言 ----
    problems = []
    if unet != expected_unet:
        problems.append(f"prompt 里的 unet={unet} != 面板选的 {expected_unet}")
    if expected_fixed_name not in lora_names:
        problems.append(f"prompt 里没有固定加速 LoRA {expected_fixed_name}，实际 {lora_names}")
    if expected_accel_name:
        if expected_accel_name not in lora_names:
            problems.append(f"prompt 里没有用户选的加速 LoRA {expected_accel_name}，实际 {lora_names}")
        else:
            idx = lora_names.index(expected_accel_name)
            got = lora_strengths[idx]
            if abs(got - case["strength"]) > 1e-9:
                problems.append(f"加速 LoRA 强度 {got} != 面板选的 {case['strength']}")
    else:
        if len(lora_names) != 1:
            problems.append(f"未选加速 LoRA 时只应有固定那枚，实际 {lora_names}")

    if info["model_unet"] != expected_unet:
        problems.append(f"记录里的 model_unet={info['model_unet']} != {expected_unet}")
    if info["model_label"] != VM.VIDEO_MODELS[case["model_key"]]["label"]:
        problems.append(f"记录里的模型显示名={info['model_label']!r}")
    if info["lora_label"] != expected_accel_label:
        problems.append(f"记录里的 LoRA={info['lora_label']!r} != {expected_accel_label!r}")
    if info["lora_strength"] != case["strength"] and case["accel"]:
        problems.append(f"记录里的 LoRA 强度={info['lora_strength']} != {case['strength']}")
    if info["fixed_lora_name"] != expected_fixed_name:
        problems.append(f"记录里的固定 LoRA={info['fixed_lora_name']!r}")

    return {
        "name": case["name"],
        "job_id": job_id,
        "panel_model": VM.VIDEO_MODELS[case["model_key"]]["label"],
        "panel_accel": expected_accel_label or "（未选）",
        "prompt_unet": unet,
        "prompt_loras": lora_names,
        "prompt_strengths": lora_strengths,
        "record_model_label": info["model_label"],
        "record_model_unet": info["model_unet"],
        "record_lora_label": info["lora_label"],
        "record_lora_strength": info["lora_strength"],
        "record_fixed_lora": info["fixed_lora_name"],
        "problems": problems,
    }


def run_frontend_render(job_json: dict) -> dict:
    """用 Node 真跑 app.js 的前端函数，看它渲染成什么文字。"""
    app_js = (REPO / "web" / "app.js").read_text(encoding="utf-8")

    def extract(name):
        import re
        m = re.search(r"function\s+" + re.escape(name) + r"\s*\(", app_js)
        start = m.start()
        i = app_js.index("{", m.end() - 1)
        depth, in_str = 0, None
        while i < len(app_js):
            ch = app_js[i]
            if in_str:
                if ch == "\\":
                    i += 2
                    continue
                if ch == in_str:
                    in_str = None
            elif ch in ("'", '"', "`"):
                in_str = ch
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return app_js[start:i + 1]
            i += 1
        raise AssertionError(name)

    shim = """
const IMG_OPTIONS = { models: {} };
const MOSS_OPTIONS = { models: {} };
const QWEN_OPTIONS = { models: {} };
const VIDEO_OPTIONS = { models: {}, accel_lora_combos: [], fixed_accel_loras: [] };
function imgModelLabel(k) { return k || "—"; }
const MUSIC_MODEL_FALLBACK = {};
function musicModelLabel(k) { return k || "—"; }
function escapeHtml(s) { return String(s == null ? "" : s); }
global.window = global;
"""
    funcs = "\n".join(extract(n) for n in
                     ("jobModelInfo", "localModelInfo", "loraRowHtml", "modelLabel", "loraLabel"))
    script = f"""
{shim}
{funcs}
const job = {json.dumps(job_json)};
console.log(JSON.stringify({{
  model: modelLabel(job),
  lora: loraLabel(job),
  loraRow: loraRowHtml(loraLabel(job)),
}}));
"""
    proc = subprocess.run([NODE, "--input-type=commonjs", "-e", script],
                          capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise AssertionError(f"Node 失败:\n{proc.stderr}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def main() -> int:
    assert NODE is not None, "node 不可用"
    sys.path.insert(0, str(REPO))
    from h3studio import job_meta
    all_problems = []
    results = []
    tmp_root = Path(tempfile.mkdtemp(prefix="h3-t2v-meta-e2e-"))
    try:
        for index, case in enumerate(CASES):
            data_dir = tmp_root / f"case{index}"
            data_dir.mkdir(parents=True, exist_ok=True)
            result = run_case(case, data_dir)
            # ---- 5. 前端真渲染 ----
            with sqlite3.connect(data_dir / "jobs.db") as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute("SELECT * FROM jobs WHERE id=?",
                                   (result["job_id"],)).fetchone()
            stored_params = json.loads(row["params"])
            # 前端拿到的是**整个 job**（params 是它的子对象）——
            # 这里必须按真实形状组装，否则 modelLabel 读不到 job.params.steps。
            job_json = {
                "id": result["job_id"],
                "mode": "t2v",
                "params": stored_params,
                "model_info": job_meta.summarize("t2v", stored_params),
            }
            rendered = run_frontend_render(job_json)
            result["frontend_model"] = rendered["model"]
            result["frontend_lora"] = rendered["lora"]
            result["frontend_lora_row"] = rendered["loraRow"]
            result["frontend_lora_row_present"] = bool(rendered["loraRow"])

            # ---- 6. 前端渲染必须与服务端摘要一致 ----
            # 模型行：服务端 model_label + 步数（前端补的步数后缀）
            steps = stored_params.get("steps")
            expected_model = result["record_model_label"] + (
                f" · {steps}步" if steps else "")
            if rendered["model"] != expected_model:
                result["problems"].append(
                    f"前端模型行={rendered['model']!r} != 服务端摘要 {expected_model!r}")
            # LoRA 行：用户选的那枚（带强度）+ 工作流固定那枚
            user_part = (result["record_lora_label"] +
                         (f" ×{result['record_lora_strength']:.2f}"
                          if result["record_lora_strength"] is not None else "")
                         ) if result["record_lora_label"] else "空"
            fixed_part = VM_fixed(result)
            expected_lora = user_part + (f" · 工作流固定：{fixed_part}"
                                         if fixed_part else "")
            if rendered["lora"] != expected_lora:
                result["problems"].append(
                    f"前端 LoRA 行={rendered['lora']!r} != {expected_lora!r}")
            if not result["frontend_lora_row_present"]:
                result["problems"].append("前端没有渲染 LoRA 行")

            all_problems.extend(f"[{case['name']}] {p}" for p in result["problems"])
            results.append(result)
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    print("=" * 78)
    print("文生视频「面板所选 == 实际下发 == 生成记录 == 前端显示」端到端验证")
    print("=" * 78)
    for r in results:
        print(f"\n■ {r['name']}")
        print(f"  面板所选底模   : {r['panel_model']}")
        print(f"  面板所选加速LoRA: {r['panel_accel']}")
        print(f"  → ComfyUI prompt unet_name : {r['prompt_unet']}")
        print(f"  → ComfyUI prompt lora_name  : {r['prompt_loras']}")
        print(f"  → ComfyUI prompt strength  : {r['prompt_strengths']}")
        print(f"  → 落库记录 model_unet       : {r['record_model_unet']}")
        print(f"  → 落库记录 模型显示名        : {r['record_model_label']}")
        print(f"  → 落库记录 LoRA             : {r['record_lora_label']!r} "
              f"(强度 {r['record_lora_strength']})")
        print(f"  → 落库记录 固定加速LoRA      : {r['record_fixed_lora']}")
        print(f"  → 前端渲染 模型行            : {r['frontend_model']}")
        print(f"  → 前端渲染 LoRA行            : {r['frontend_lora']!r} "
              f"(行存在={r['frontend_lora_row_present']})")
        if r["problems"]:
            print("  ✗ 问题：")
            for p in r["problems"]:
                print(f"      - {p}")
        else:
            print("  ✓ 三者一致")

    print("\n" + "=" * 78)
    if all_problems:
        print(f"验证失败：{len(all_problems)} 项不一致")
        for p in all_problems:
            print("  - " + p)
        return 1
    print(f"验证通过：{len(results)}/{len(CASES)} 组用例，"
          "面板所选 / ComfyUI prompt / 落库记录 / 前端显示 四者完全一致")
    return 0


def VM_fixed(result: dict) -> str:
    """固定加速 LoRA 的显示名（从服务端摘要拿不到时用权重名兜底）。"""
    from h3studio import video_models as VM
    for key, meta in VM.VIDEO_ACCEL_LORAS.items():
        if meta["name"] == result["record_fixed_lora"]:
            return meta["display"]
    return ""


if __name__ == "__main__":
    sys.exit(main())
