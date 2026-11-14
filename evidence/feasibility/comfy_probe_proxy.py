#!/usr/bin/env python3
"""
ComfyUI 观测代理 —— 原样透明转发 + 只记录 POST /prompt。

用途：让「独立验收实例」把提交给 ComfyUI 的 workflow 图落盘成 JSON，
从而证明 UI 参数是否真的进入了 ComfyUI 图（而不是只改了前端 UI 状态）。

约束（安全红线）：
  - **不修改**转发内容，二进制原样透传到真实 ComfyUI（127.0.0.1:8188）
  - **不碰** 8188 本身，不改 ComfyUI 任何文件，不改共享服务
  - 只在 /prompt 上额外落盘一个 JSON 副本
"""
import json
import os
import sys
import time
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout, web

UPSTREAM = os.environ.get("PROBE_UPSTREAM", "http://127.0.0.1:8188")
LOG_DIR = Path(os.environ.get("PROBE_LOG_DIR",
                              "/root/projects/h3studio-feasibility/evidence/feasibility/comfy-submissions"))
LOG_DIR.mkdir(parents=True, exist_ok=True)

RECORDS = []
_HOP_BY_HOP = {"content-encoding", "content-length", "transfer-encoding", "connection"}


async def handle(request: web.Request) -> web.StreamResponse:
    path = request.rel_url
    method = request.method
    body = None
    if method in ("POST", "PUT", "PATCH"):
        body = await request.read()

    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in _HOP_BY_HOP and k.lower() != "host"}

    timeout = ClientTimeout(total=None, sock_connect=30, sock_read=600)
    async with ClientSession(timeout=timeout) as sess:
        url = UPSTREAM + str(path)
        if body is not None:
            upstream = await sess.request(method, url, data=body, headers=headers)
        else:
            upstream = await sess.request(method, url, headers=headers)
        payload = await upstream.read()
        out_headers = {k: v for k, v in upstream.headers.items()
                       if k.lower() not in _HOP_BY_HOP}

    if path.path == "/prompt" and method == "POST":
        rec = {
            "seq": len(RECORDS) + 1,
            "ts": time.time(),
            "iso": time.strftime("%Y-%m-%d %H:%M:%S"),
            "upstream_status": upstream.status,
            "upstream_response": payload.decode("utf-8", "replace")[:2000],
            "request_body": body.decode("utf-8", "replace") if body else None,
        }
        try:
            parsed = json.loads((body or b"").decode("utf-8"))
        except Exception:
            parsed = None
        rec["workflow"] = parsed.get("prompt") if isinstance(parsed, dict) else None
        rec["extra_data"] = parsed.get("extra_data") if isinstance(parsed, dict) else None
        out = LOG_DIR / f"prompt-{rec['seq']:03d}.json"
        out.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
        rec["saved_to"] = str(out)
        with (LOG_DIR / "index.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "seq": rec["seq"], "iso": rec["iso"], "status": rec["upstream_status"],
                "upstream_response": rec["upstream_response"][:300],
                "saved_to": str(out),
                "graph_node_count": len(rec["workflow"]) if isinstance(rec.get("workflow"), dict) else None,
            }, ensure_ascii=False) + "\n")
        RECORDS.append(rec)
        print(f"[probe] POST /prompt #{rec['seq']} -> {upstream.status} saved={out}", flush=True)

    return web.Response(status=upstream.status, body=payload, headers=out_headers)


async def probe_index(request: web.Request) -> web.Response:
    return web.json_response({
        "upstream": UPSTREAM,
        "prompt_submissions": len(RECORDS),
        "records": [
            {"seq": r["seq"], "iso": r["iso"], "status": r["upstream_status"],
             "saved_to": r.get("saved_to"),
             "graph_nodes": len(r["workflow"]) if isinstance(r.get("workflow"), dict) else None}
            for r in RECORDS
        ],
    })


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8211
    app = web.Application(client_max_size=512 * 1024 * 1024)
    app.router.add_get("/__probe/index", probe_index)
    app.router.add_route("*", "/{tail:.*}", handle)
    print(f"[probe] 观测代理 {UPSTREAM} -> :{port}  日志 {LOG_DIR}", flush=True)
    web.run_app(app, host="127.0.0.1", port=port, print=None)
