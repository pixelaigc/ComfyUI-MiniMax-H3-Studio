# -*- coding: utf-8 -*-
"""真实 ComfyUI 出图验证：官方 PE 扩写真的生效。

两段证据：
  A. **扩写** —— 真跑官方 PE checkpoint（PE-T2I 档），确认扩写产物来自
     官方 PE（不是模板模拟），并抓下 ComfyUI 实际执行的 PE 工作流 JSON。
  B. **出图** —— 用「增强」多选里官方 PE 那项套用的参数 + 同一份 system
     prompt 走一遍出图，抓出图工作流 JSON 与成图。

证据落到 pe-enhance-evidence/{gen,workflows}/。
"""
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, "/root/projects/h3studio-live")
from aiohttp import ClientSession, ClientTimeout   # noqa: E402

from h3studio import qwen_pe                      # noqa: E402
from h3studio import workflows as W               # noqa: E402

COMFY = "http://127.0.0.1:8188"
BASE = "/root/.hermes-web-ui/.ekko/workspace/default/" \
       "person-main-505e35f9-2826-4d4c-af53-d3dc5de6f957/pe-enhance-evidence"
GEN_DIR = Path(BASE) / "gen"
WF_DIR = Path(BASE) / "workflows"
GEN_DIR.mkdir(parents=True, exist_ok=True)
WF_DIR.mkdir(parents=True, exist_ok=True)

PROMPT = "一只橘猫坐在木质窗台上，午后阳光斜照，暖色调，浅景深"
PE_PARAMS = {"presence_penalty": 1.5, "max_new_tokens": 16256}


def show(title):
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


# ---------------------------------------------------------------- A. 真跑官方 PE
show("A. 真跑官方 PE 扩写（PE-T2I 档，官方 checkpoint）")

# 抓 ComfyUI 实际执行的工作流：/history 里带 prompt
async def run_pe():
    async with ClientSession(timeout=ClientTimeout(total=900)) as s:
        # 先把工作流写盘（证明我们送进去的东西长什么样）
        wf = qwen_pe.build_pe_workflow(PROMPT, False, None,
                                       pe_params=PE_PARAMS)
        (WF_DIR / "pe-expand-workflow.json").write_text(
            json.dumps(wf, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[i] PE 扩写工作流已写盘：{WF_DIR/'pe-expand-workflow.json'}")
        pe_node = wf["pe:gen"]["inputs"]
        print(f"[i] checkpoint        = {wf['pe:clip']['inputs']['clip_name']}")
        print(f"[i] max_length        = {pe_node['max_length']}")
        print(f"[i] presence_penalty  = {pe_node['sampling_mode.presence_penalty']}")
        print(f"[i] thinking          = {pe_node['thinking']}")
        print(f"[i] system_prompt 长度 = {len(pe_node['system_prompt'])} 字")

        t0 = time.time()
        res = await qwen_pe.run_pe_expansion(s, COMFY, PROMPT, False, None,
                                             pe_params=PE_PARAMS)
        print(f"[i] 耗时 {time.time()-t0:.1f}s")
        return res, wf

res, wf = asyncio.run(run_pe())
expanded = res["expanded"]
print(f"\n[OK] 扩写成功：{len(expanded)} 字（原文 {len(PROMPT)} 字）")
print(f"     档位 = {res['tier_label']} / {res['clip_name']}")
print(f"     套用参数 = {res.get('pe_params')}")
print(f"     前 300 字：{expanded[:300]}")
assert expanded != PROMPT, "扩写结果与原文相同 —— PE 没生效！"
assert len(expanded) > 200, "扩写结果过短"
# 官方 PE-T2I 的产物特征：英文单段观察者式描述
assert expanded[0].isalpha(), "官方 PE 产物应是英文描述开头"
(GEN_DIR / "pe-expanded-prompt.txt").write_text(expanded, encoding="utf-8")

# 从 ComfyUI history 里抓**实际执行**的 PE 工作流（证明 ComfyUI 跑的就是它）
async def fetch_history(prompt_id):
    async with ClientSession(timeout=ClientTimeout(total=60)) as s:
        async with s.get(f"{COMFY}/history/{prompt_id}") as r:
            return await r.json()

hist = asyncio.run(fetch_history(res["prompt_id"]))
entry = hist.get(res["prompt_id"], {})
actual_wf = entry.get("prompt", [])
if isinstance(actual_wf, list) and len(actual_wf) >= 3:
    actual_wf = actual_wf[2]
(WF_DIR / "pe-expand-workflow-actual-from-comfyui.json").write_text(
    json.dumps(actual_wf, ensure_ascii=False, indent=2), encoding="utf-8")
pe_actual = (actual_wf or {}).get("pe:gen", {}).get("inputs", {})
print(f"\n[OK] ComfyUI 实际执行的 PE 节点参数：")
print(f"     max_length       = {pe_actual.get('max_length')}")
print(f"     presence_penalty = {pe_actual.get('sampling_mode.presence_penalty')}")
print(f"     thinking         = {pe_actual.get('thinking')}")
assert pe_actual.get("max_length") == 16256, "ComfyUI 实际执行的 max_length 不对"
assert abs(float(pe_actual.get("sampling_mode.presence_penalty", 0)) - 1.5) < 1e-6

# ---------------------------------------------------------------- B. 真出图
show("B. 真实出图（走同一套 PE 接线 + 同一份 system prompt）")

params = {
    "job_id": "pe_multiselect_verify",
    "prompt": expanded,          # 用官方 PE 扩写结果当提示词
    "model": "qwen_image_2.1_bf16",
    "aspect": "16:9",
    "resolution": "1mp",
    "steps": 40,
    "cfg": 1.0,
    "shift": 0.69,
    "sampler": "euler",
    "scheduler": "simple",
    "seed": 20261109,
    "negative_prompt": "",
    "use_pe": True,                                   # 出图链路内部再走一次官方 PE
    "pe_params": PE_PARAMS,                           # 多选列表套用的档位参数
    "pe_system_prompt": qwen_pe.system_prompt_for(False),   # 同一份 system prompt
}
gen_wf = W.build_qwen_t2i(params)
(WF_DIR / "image-generation-workflow.json").write_text(
    json.dumps(gen_wf, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"[i] 出图工作流已写盘：{WF_DIR/'image-generation-workflow.json'}")
gen_pe = gen_wf["qw:26"]["inputs"]
print(f"[i] 出图工作流里的 PE 节点：max_length={gen_pe['max_length']}, "
      f"presence_penalty={gen_pe['sampling_mode.presence_penalty']}, "
      f"thinking={gen_pe['thinking']}")
assert gen_pe["max_length"] == 16256
assert abs(float(gen_pe["sampling_mode.presence_penalty"]) - 1.5) < 1e-6

# 用 ComfyUI 的 API 跑（带 client_id 便于取产物）
async def run_gen():
    payload = {"prompt": gen_wf, "client_id": "pe-multiselect-verify"}
    async with ClientSession(timeout=ClientTimeout(total=1800)) as s:
        async with s.post(f"{COMFY}/prompt", json=payload) as r:
            body = await r.json()
        pid = body.get("prompt_id")
        if not pid:
            raise SystemExit(f"[FAIL] ComfyUI 拒绝工作流：{json.dumps(body)[:500]}")
        print(f"[i] prompt_id = {pid}")
        deadline = time.time() + 1800
        while time.time() < deadline:
            await asyncio.sleep(3)
            async with s.get(f"{COMFY}/history/{pid}") as r:
                h = await r.json()
            e = h.get(pid)
            if not e:
                continue
            st = e.get("status") or {}
            if st.get("status_str") == "error":
                msgs = [str((x[1] or {}).get("exception_message", ""))
                        for x in (st.get("messages") or [])
                        if isinstance(x, list) and x and x[0] == "execution_error"]
                raise SystemExit("[FAIL] ComfyUI 执行失败：" + "; ".join(msgs)[:600])
            outs = e.get("outputs") or {}
            imgs = []
            for node_out in outs.values():
                for kind in ("images", "gifs"):
                    for im in (node_out.get(kind) or []):
                        imgs.append(im)
            if imgs:
                return pid, imgs, e
        raise SystemExit("[FAIL] 出图超时")

pid, imgs, entry = asyncio.run(run_gen())
print(f"\n[OK] 出图成功：prompt_id={pid}，产物 {len(imgs)} 张")

async def download():
    out = []
    async with ClientSession(timeout=ClientTimeout(total=300)) as s:
        for i, im in enumerate(imgs):
            name = im.get("filename")
            sub = im.get("subfolder", "")
            url = f"{COMFY}/view?filename={name}&subfolder={sub}&type=output"
            async with s.get(url) as r:
                data = await r.read()
            p = GEN_DIR / f"qwen21-pe-official-{i+1}.png"
            p.write_bytes(data)
            out.append(str(p))
            print(f"     → {p} ({len(data)} bytes)")
    return out

paths = asyncio.run(download())
# 记录 ComfyUI 实际执行的工作流
(WF_DIR / "image-generation-workflow-actual-from-comfyui.json").write_text(
    json.dumps((entry.get("prompt") or [])[2] if isinstance(entry.get("prompt"), list)
                and len(entry["prompt"]) >= 3 else entry.get("prompt"),
               ensure_ascii=False, indent=2), encoding="utf-8")

summary = {
    "pe_expansion": {
        "tier": res["tier"], "tier_label": res["tier_label"],
        "clip_name": res["clip_name"], "prompt_id": res["prompt_id"],
        "original_len": len(PROMPT), "expanded_len": len(expanded),
        "pe_params": res.get("pe_params"),
        "system_prompt_len": len(qwen_pe.system_prompt_for(False)),
    },
    "generation": {
        "prompt_id": pid, "images": paths,
        "params": {k: params[k] for k in
                   ("model", "aspect", "resolution", "steps", "cfg",
                    "shift", "seed", "use_pe", "pe_params")},
    },
    "workflow_files": sorted(str(p) for p in WF_DIR.glob("*.json")),
}
(Path(BASE) / "gen-summary.json").write_text(
    json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"\n[OK] 证据汇总 → {Path(BASE)/'gen-summary.json'}")
