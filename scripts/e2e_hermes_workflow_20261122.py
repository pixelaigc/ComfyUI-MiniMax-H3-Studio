# -*- coding: utf-8 -*-
"""
端到端实测 · 工作流落点：把 hermes 增强后的提示词**真正送进**
三条能力的工作流构建器，证明「先增强、再进生成」这一环闭合。

【口径】
  * 只调本仓库的工作流构建器（不发 ComfyUI 请求，不占 GPU）
  * 从构建出来的 workflow 里把**含增强提示词的文本节点**抠出来作为证据
"""
import json
import sys
from pathlib import Path

REPO = Path("/root/projects/h3studio-prompt-enhance")
sys.path.insert(0, str(REPO))

OUT = Path("/root/.hermes-web-ui/.ekko/workspace/default/"
           "person-main-505e35f9-2826-4d4c-af53-d3dc5de6f957/"
           "evidence-hermes-enhance-20261122")

from h3studio import workflows  # noqa: E402

# 三条能力的工作流构建器与它需要的最小参数集
CASES = [
    ("t2i", "image", {
        "job_id": "e2e_hermes_img", "prompt": "", "width": 1024, "height": 1024,
        "seed": 123456, "steps": 8, "cfg": 1.0, "model": "z_image_turbo_bf16",
    }),
    ("t2v", "video", {
        "job_id": "e2e_hermes_vid", "prompt": "", "width": 1280, "height": 720,
        "seed": 123456, "steps": 20, "cfg": 1.0, "shift": 5.0,
        "model_key": "lora", "duration": 5,
    }),
    ("t2a", "audio", {
        "job_id": "e2e_hermes_aud", "prompt": "", "checkpoint": "yue2_3b_bf16",
        "seed": 123456,
    }),
]


def find_prompt_nodes(wf, needle):
    """把「文本输入里含增强提示词」的节点抠出来。"""
    found = []
    probe = (needle or "").strip()[:60]
    for nid, node in (wf or {}).items():
        if not isinstance(node, dict):
            continue
        for key, val in (node.get("inputs") or {}).items():
            if isinstance(val, str) and probe and probe in val:
                found.append({"node": nid, "class_type": node.get("class_type"),
                              "input": key, "chars": len(val),
                              "head": val[:120]})
    return found


def main():
    evidence = {"cases": []}
    for mode, media, params in CASES:
        # 读回实测产生的增强提示词
        src = OUT / f"http-enhanced-{media}.txt"
        if not src.exists():
            src = OUT / f"enhanced-{media}.txt"
        prompt = src.read_text(encoding="utf-8").strip()
        params = dict(params)
        params["prompt"] = prompt

        builder = workflows.BUILDERS.get(mode)
        rec = {"mode": mode, "media": media,
               "prompt_source": str(src),
               "prompt_len": len(prompt),
               "prompt_head": prompt[:200]}
        if builder is None:
            rec["workflow"] = {"ok": False, "reason": f"没有 {mode} 的构建器"}
        else:
            try:
                wf = builder(params)
                nodes = find_prompt_nodes(wf, prompt)
                rec["workflow"] = {
                    "ok": bool(nodes),
                    "node_count": len(wf or {}),
                    "prompt_nodes": nodes[:6],
                    "reason": "" if nodes else "工作流里没找到含增强提示词的文本节点",
                }
            except Exception as exc:
                rec["workflow"] = {"ok": False,
                                   "reason": f"{type(exc).__name__}: {exc}"}
        evidence["cases"].append(rec)
        print(json.dumps(rec, ensure_ascii=False, indent=2)[:1200])
        print()

    (OUT / "evidence-workflow.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"证据：{OUT / 'evidence-workflow.json'}")


if __name__ == "__main__":
    main()
