# -*- coding: utf-8 -*-
"""
端到端实测 · HTTP 层：起一个**独立端口 + 独立临时 sqlite** 的 Studio 实例，
走 /api/hermes-enhance（图像 / 视频 / 音频各一次），并把增强后的提示词
**真正送进工作流构建器**，验证「先增强、再进生成」这一环。

【隔离口径】
  * 端口：临时分配（不是 8190）
  * 数据目录：临时目录（不是线上 jobs.db）
  * 不写用户媒体库
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path("/root/projects/h3studio-prompt-enhance")
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from test_frontend_defaults import StudioServer  # noqa: E402

OUT = Path("/root/.hermes-web-ui/.ekko/workspace/default/"
           "person-main-505e35f9-2826-4d4c-af53-d3dc5de6f957/"
           "evidence-hermes-enhance-20261122")
OUT.mkdir(parents=True, exist_ok=True)

import urllib.error  # noqa: E402
import urllib.request  # noqa: E402


def post(base, path, payload, timeout=900):
    req = urllib.request.Request(
        f"{base}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def get(base, path, timeout=60):
    with urllib.request.urlopen(f"{base}{path}", timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def main():
    srv = StudioServer()
    srv.start()
    base = f"http://127.0.0.1:{srv.port}"
    evidence = {"port": srv.port, "data_dir": str(srv.data_dir), "steps": []}
    try:
        # ---- 1. 打开 hermes 通道（PUT /api/enhancer 的 hermes 段）----
        status, body = get(base, "/api/enhancer")
        hermes_cfg = body["hermes"]
        evidence["hermes_config_before"] = hermes_cfg
        evidence["steps"].append({"step": "GET /api/enhancer", "status": status,
                                  "hermes_present": "hermes" in body,
                                  "media_specs": sorted(hermes_cfg["media_specs"].keys())})

        # 用 PUT 打开通道
        req = urllib.request.Request(
            f"{base}/api/enhancer",
            data=json.dumps({"hermes": {
                "enabled": True,
                "binary": "/usr/local/bin/hermes",
                "profile": "media",
                "timeout": 900,
            }}).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="PUT")
        with urllib.request.urlopen(req, timeout=60) as resp:
            put_status = resp.status
            put_body = json.loads(resp.read().decode("utf-8"))
        evidence["steps"].append({"step": "PUT /api/enhancer (enable hermes)",
                                  "status": put_status, "ok": put_body.get("ok")})

        # 复核
        status, body = get(base, "/api/enhancer")
        evidence["hermes_config_after"] = body["hermes"]
        evidence["steps"].append({"step": "GET /api/enhancer (recheck)",
                                  "status": status,
                                  "enabled": body["hermes"]["enabled"],
                                  "profile": body["hermes"]["profile"]})

        # ---- 2. /api/hermes-test 连通性 ----
        status, body = post(base, "/api/hermes-test", {"media": "video"}, timeout=300)
        evidence["hermes_test"] = {k: body.get(k) for k in
                                   ("ok", "binary", "binary_exists", "profile",
                                    "profile_exists", "skills", "returncode",
                                    "answer", "error")}
        evidence["steps"].append({"step": "POST /api/hermes-test", "status": status,
                                  "ok": body.get("ok")})

        # ---- 3. 三条能力各跑一次 ----
        cases = [
            ("image", "t2i", "一只橘猫坐在窗台上，午后阳光斜照进来"),
            ("video", "t2v", "一个少女在雨中的霓虹街道上奔跑，镜头从背后跟拍到正面特写"),
            ("audio", "t2a", "一段适合深夜驾驶的电子音乐，节奏舒缓，情绪略带忧郁"),
        ]
        results = []
        for media, mode, prompt in cases:
            status, body = post(base, "/api/hermes-enhance", {
                "prompt": prompt, "media": media, "mode": "replace",
            }, timeout=900)
            rec = {
                "media": media, "mode": mode, "prompt": prompt,
                "status": status,
                "engine": body.get("engine"),
                "profile": body.get("profile"),
                "skills": body.get("skills"),
                "command": body.get("command"),
                "attempts": body.get("attempts"),
                "enhanced_len": len(body.get("enhanced") or ""),
                "enhanced_head": (body.get("enhanced") or "")[:400],
                "error": body.get("error"),
                "code": body.get("code"),
            }
            if status == 200:
                (OUT / f"http-enhanced-{media}.txt").write_text(
                    body["enhanced"], encoding="utf-8")
                # ---- 4. 增强后的提示词真正送进工作流构建器 ----
                rec["workflow"] = _build_workflow(mode, body["enhanced"])
            results.append(rec)
            evidence["steps"].append({
                "step": f"POST /api/hermes-enhance ({media})", "status": status,
                "enhanced_len": rec["enhanced_len"]})
        evidence["results"] = results

        # ---- 5. 失败回退验证：通道关闭时必须如实报错 ----
        req = urllib.request.Request(
            f"{base}/api/enhancer",
            data=json.dumps({"hermes": {"enabled": False}}).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="PUT")
        with urllib.request.urlopen(req, timeout=60) as resp:
            resp.read()
        status, body = post(base, "/api/hermes-enhance",
                             {"prompt": "x", "media": "image"}, timeout=60)
        evidence["disabled_behavior"] = {"status": status,
                                         "code": body.get("code"),
                                         "error": body.get("error")}
        evidence["steps"].append({"step": "POST /api/hermes-enhance (disabled)",
                                  "status": status, "code": body.get("code")})

    finally:
        srv.stop()

    (OUT / "evidence-http.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(evidence, ensure_ascii=False, indent=2)[:4000])
    print(f"\n证据：{OUT / 'evidence-http.json'}")


def _build_workflow(mode, prompt):
    """把增强后的提示词送进工作流构建器，确认它真的落到提示词字段。"""
    try:
        from h3studio import workflows
        builder = workflows.BUILDERS.get(mode)
        if builder is None:
            return {"ok": False, "reason": f"没有 {mode} 的构建器"}
        params = {"prompt": prompt, "width": 1024, "height": 1024,
                  "seed": 123456, "model_key": "lora", "steps": 20,
                  "cfg": 1.0, "shift": 5.0}
        wf = builder(params)
        # 从工作流里把文本节点的输入抠出来，证明提示词真的进去了
        found = []
        for nid, node in (wf or {}).items():
            if not isinstance(node, dict):
                continue
            for key, val in (node.get("inputs") or {}).items():
                if isinstance(val, str) and prompt[:40] and prompt[:40] in val:
                    found.append({"node": nid, "input": key,
                                  "chars": len(val)})
        return {"ok": True, "node_count": len(wf or {}),
                "prompt_nodes": found[:5]}
    except Exception as exc:
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}


if __name__ == "__main__":
    main()
