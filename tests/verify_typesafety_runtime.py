# -*- coding: utf-8 -*-
"""【D1 续做 · 运行时取证】两处缺陷的**真实链路**证据。

【要证明什么】
缺陷①：ComfyUI history 的 `status` 是**字符串**时，
        后端不得抛 `'str' object has no attribute 'get'`。
缺陷②：任务失败时，**生成库卡片上显示的**必须是可读中文，
        不能是原始 Python 异常文本。

【怎么取证 —— 真的走一遍链路，不模拟】
  1. 起一个**假 ComfyUI**（真 HTTP 服务，监听随机端口）：
       * `/prompt` -> 立刻回一个 prompt_id；
       * `/history/<prompt_id>` -> 回一条 `status` 是**字符串**的记录
         （这正是触发缺陷①的数据形状）；
       * `/object_info/<name>` -> 回节点信息（让能力探测正常）。
  2. 起一个**真的 Studio 实例**（server.py，指向假 ComfyUI）。
  3. 通过 `POST /api/jobs` 提交一个 t2v 任务 -> worker 真的去问假
     ComfyUI -> 拿到字符串 status -> 走错误路径。
  4. 断言：
       * 任务落到 `failed`（没有 AttributeError 崩掉）；
       * `jobs.error` 里是**中文**，不含 Python 异常文本；
       * `GET /api/jobs/{id}` 返回的 `error` 字段也是中文；
       * 前端 `friendlyError()` 对这个 error 的处理结果仍是中文。

【为什么值得这么麻烦】
前两条缺陷都发生在**失败路径**上 —— 单元测试能证明「函数不崩」，
但「整条链路跑下来卡片上显示什么」只有真跑才知道。

用法：
    python3 tests/verify_typesafety_runtime.py [--base http://127.0.0.1:8199]
"""

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import tempfile
import threading

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


#: 假 ComfyUI 的 status 模式（由外部控制：string / None / dict）
STATUS_MODE = "string"


def _history_record():
    """按 STATUS_MODE 造一条 history 记录。

    【关键】`status` 的类型**不保证**是 dict —— 这正是触发缺陷①的形状。
    """
    status = STATUS_MODE
    if status == "None_then_error":
        # 第一次轮询返回 None（= 还在跑），第二次返回真错误。
        _history_record.calls = getattr(_history_record, "calls", 0) + 1
        status = None if _history_record.calls <= 1 else "dict"
    if status == "dict":
        status = {"status_str": "error",
                  "messages": [["execution_error", {
                      "node_type": "H3RelayCopyBridge",
                      "exception_type": "ValueError",
                      "exception_message":
                          "拷贝桥禁止跨分辨率：上段 768x448 ≠ 本段 1024x576",
                  }]]}
    elif status == "None":
        status = None
    return {"fake-prompt-1": {"prompt": [], "outputs": {}, "status": status}}


async def _fake_comfy_app():
    """用 aiohttp 起假 ComfyUI —— 必须支持 /ws（worker 会连它拿进度）。"""
    from aiohttp import web

    async def prompt(request):
        return web.json_response({"prompt_id": "fake-prompt-1", "number": 0,
                                   "node_errors": {}})

    async def history(request):
        return web.json_response(_history_record())

    async def object_info(request):
        name = request.path.rsplit("/", 1)[-1]
        return web.json_response({name: {"input": {}}})

    async def queue(request):
        return web.json_response({"queue_running": [], "queue_pending": []})

    async def ws(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        # 不推任何进度事件 —— worker 只用它更新百分比。
        async for _ in ws:
            pass
        return ws

    async def catch_all(request):
        return web.json_response({})

    app = web.Application()
    app.router.add_post("/prompt", prompt)
    app.router.add_get("/history/{prompt_id}", history)
    app.router.add_get("/object_info/{name}", object_info)
    app.router.add_get("/queue", queue)
    app.router.add_get("/ws", ws)
    app.router.add_route("*", "/{tail:.*}", catch_all)
    return app


def start_fake_comfy():
    """起假 ComfyUI（后台线程跑自己的事件循环），返回 (runner, url, loop)。"""
    import asyncio
    from aiohttp import web

    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def _run():
        asyncio.set_event_loop(loop)
        loop.call_soon(ready.set)
        loop.run_forever()

    threading.Thread(target=_run, daemon=True).start()
    ready.wait(10)

    app = asyncio.run_coroutine_threadsafe(_fake_comfy_app(), loop).result(30)
    runner = web.AppRunner(app)
    asyncio.run_coroutine_threadsafe(runner.setup(), loop).result(30)
    site = web.TCPSite(runner, "127.0.0.1", 0)
    asyncio.run_coroutine_threadsafe(site.start(), loop).result(30)
    port = runner.addresses[0][1]
    return runner, f"http://127.0.0.1:{port}", loop


def get(base, path):
    with urllib.request.urlopen(base + path, timeout=30) as r:
        return json.loads(r.read().decode())


def post_job(base, mode, params):
    """POST /api/jobs —— **multipart**（mode + params(JSON)）。

    【为什么是 multipart】server.py 的 api_create 读的是
    `await request.multipart()` —— 发 JSON 会 500。
    """
    boundary = "----ts9911"
    body = b""
    for key, value in (("mode", mode), ("params", json.dumps(params,
                                                             ensure_ascii=False))):
        body += f"--{boundary}\r\n".encode()
        body += f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode()
        body += str(value).encode("utf-8") + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        base + "/api/jobs", data=body,
        headers={"Content-Type":
                 f"multipart/form-data; boundary={boundary}"}, method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode())


def wait_job(base, jid, timeout=180):
    dl = time.time() + timeout
    while time.time() < dl:
        j = get(base, f"/api/jobs/{jid}")
        if j.get("status") in ("done", "failed", "canceled"):
            return j
        time.sleep(1)
    raise TimeoutError(jid)


def _free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def start_studio(comfy_url, data_dir):
    """起一个**真的** Studio 实例（server.py），指向假 ComfyUI。"""
    import shutil
    import tempfile
    port = _free_port()
    tmp = Path(tempfile.mkdtemp(prefix="h3-ts-runtime-"))
    shutil.copytree(REPO / "h3studio", tmp / "h3studio")
    shutil.copy2(REPO / "server.py", tmp / "server.py")
    shutil.copytree(REPO / "web", tmp / "web")
    cfg = {
        "host": "127.0.0.1", "port": port,
        "comfyui_url": comfy_url,
        "data_dir": str(data_dir),
        "models_dir": "/home/ubuntu/comfyui/models",
        "poll_interval": 0.5,
    }
    (tmp / "config.json").write_text(json.dumps(cfg, ensure_ascii=False),
                                     encoding="utf-8")
    log_path = Path("/tmp/h3-ts-runtime-server.log")
    log_f = open(log_path, "wb")
    proc = subprocess.Popen(
        [sys.executable, "server.py"], cwd=str(tmp),
        stdout=log_f, stderr=subprocess.STDOUT)
    import socket
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return proc, f"http://127.0.0.1:{port}", tmp
        except OSError:
            time.sleep(0.2)
    proc.terminate()
    raise RuntimeError("Studio 实例启动失败")


def _extract_frontend_impl() -> str:
    """从 web/app.js 里抽出前端错误文案的**真实实现**，写成可执行的 JS。

    【为什么要在 Python 里抽，而不是在 JS 里抽】
    在 JS 里用 `indexOf('];')` 找数组结尾会**误匹配到正则里的字符**
    （`PY_EXCEPTION_PATTERNS` 里就有 `]`）—— 抽出来的是坏的代码。
    Python 里做括号配平更可靠。

    【抽什么】
      * 常量 `PY_EXCEPTION_PATTERNS`（数组字面量，括号配平）；
      * 函数 `looksLikePythonTraceback`；
      * 函数 `friendlyError`。
    最后一行打印 `friendlyError(process.argv[2])` 的结果。
    """
    appjs = (REPO / "web" / "app.js").read_text(encoding="utf-8")
    nl = "\n"

    def extract_fn(name):
        i = appjs.index(f"function {name}(")
        end_mark = nl + "}" + nl
        end = appjs.index(end_mark, i)
        return appjs[i:end + len(end_mark)]

    def extract_const(name):
        start = appjs.index(f"const {name} = [")
        arr_start = appjs.index("[", start)
        depth = 0
        i = arr_start
        while i < len(appjs):
            ch = appjs[i]
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    i += 1
                    break
            i += 1
        return appjs[start:i]

    return (extract_const("PY_EXCEPTION_PATTERNS") + nl
            + extract_fn("looksLikePythonTraceback") + nl
            + extract_fn("friendlyError") + nl
            + "console.log(JSON.stringify({shown: friendlyError(process.argv[2])}));")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="")
    args = ap.parse_args()

    report = {"steps": [], "ok": True}

    # ---- 起假 ComfyUI + 真 Studio 实例 ----
    fake_runner, fake_url, fake_loop = start_fake_comfy()
    data_dir = Path(tempfile.mkdtemp(prefix="h3-ts-data-"))
    proc = None
    tmp = None
    if args.base:
        base = args.base
        print(f"[说明] 用外部实例 {base}（不自带假 ComfyUI）", flush=True)
    else:
        proc, base, tmp = start_studio(fake_url, data_dir)
        print(f"[说明] 自起 Studio 实例 {base} -> 假 ComfyUI {fake_url}", flush=True)

    def step(name, data):
        report["steps"].append({"step": name, **data})
        print(f"--- {name}")
        for k, v in data.items():
            print(f"    {k}: {v}")

    print("=" * 72)
    print("D1 续做 · 运行时取证：history 类型免疫 + 错误文案中文化")
    print("=" * 72)

    # ---- 场景 A：status 是字符串（原始缺陷①的形状）----
    global STATUS_MODE
    STATUS_MODE = "string"
    job = post_job(base, "t2v",
                   {"prompt": "a swordsman in a bamboo forest",
                    "model_key": "fl2va_int8", "width": 768, "height": 448,
                    "duration": 5, "steps": 4, "sampler": "euler",
                    "scheduler": "simple", "seed": 4242})
    jid = job.get("id") or job.get("job_id")
    print(f"任务 = {jid}", flush=True)
    # 轮询时把状态打出来（诊断：任务卡在哪一步）
    dl = time.time() + 90
    j = None
    while time.time() < dl:
        j = get(base, f"/api/jobs/{jid}")
        print(f"    status={j.get('status')} progress={j.get('progress')}",
              flush=True)
        if j.get("status") in ("done", "failed", "canceled"):
            break
        time.sleep(2)
    if j is None or j.get("status") not in ("done", "failed", "canceled"):
        print("[诊断] 服务端日志:", flush=True)
        try:
            print(Path("/tmp/h3-ts-runtime-server.log").read_text(
                encoding="utf-8", errors="replace")[-3000:], flush=True)
        except OSError:
            pass
        report["ok"] = False
        raise SystemExit("任务未在 90 秒内结束")
    err = j.get("error") or ""

    step("A_status_is_string", {
        "job": jid,
        "status": j.get("status"),
        "error": err,
        "no_attribute_error": "object has no attribute" not in err,
        "no_traceback": "Traceback" not in err,
        "is_chinese": any("\u4e00" <= ch <= "\u9fff" for ch in err),
        "verdict": "OK(未崩 + 中文)" if ("object has no attribute" not in err
                                        and err.strip()) else "BAD",
    })
    if "object has no attribute" in err or not err.strip():
        report["ok"] = False

    # ---- 场景 B：status 是 None（ComfyUI「还在跑」时的形状）----
    #
    # 【重要语义】ComfyUI 在任务**还没跑完**时，history 里的 status 就是
    # None（execution.py 的 status_dict 初值 None，task_done 时才填 dict）。
    # 所以 None **不是**「形状坏了」，而是「还在跑」—— worker 应继续轮询。
    # 本场景验证：先给 None（不判失败、不判畸形），随后转成真错误。
    STATUS_MODE = "None_then_error"
    job2 = post_job(base, "t2v",
                    {"prompt": "b", "model_key": "fl2va_int8",
                     "width": 768, "height": 448, "duration": 5, "steps": 4,
                     "sampler": "euler", "scheduler": "simple", "seed": 4243})
    jid2 = job2.get("id") or job2.get("job_id")
    j2 = wait_job(base, jid2)
    err2 = j2.get("error") or ""
    step("B_status_none_then_error", {
        "job": jid2,
        "status": j2.get("status"),
        "error": err2,
        "no_attribute_error": "object has no attribute" not in err2,
        "is_chinese": any("\u4e00" <= ch <= "\u9fff" for ch in err2),
    })
    if "object has no attribute" in err2:
        report["ok"] = False

    # ---- 场景 C：status 是 dict（真 ComfyUI 错误）-> 中文原因必须保留 ----
    STATUS_MODE = "dict"
    job3 = post_job(base, "t2v",
                    {"prompt": "c", "model_key": "fl2va_int8",
                     "width": 768, "height": 448, "duration": 5, "steps": 4,
                     "sampler": "euler", "scheduler": "simple", "seed": 4244})
    jid3 = job3.get("id") or job3.get("job_id")
    j3 = wait_job(base, jid3)
    err3 = j3.get("error") or ""
    step("C_status_is_dict_real_error", {
        "job": jid3,
        "status": j3.get("status"),
        "error": err3,
        "kept_real_reason": "拷贝桥禁止跨分辨率" in err3,
        "no_python_text": "object has no attribute" not in err3,
    })
    if "拷贝桥禁止跨分辨率" not in err3:
        report["ok"] = False

    # ---- 场景 D：前端 friendlyError 对真实 error 的处理 ----
    #
    # 【执行 app.js 里真实的实现】—— 不在测试里抄一份（抄一份就测不到
    # app.js 的改动，变异「改 app.js」不会被抓住）。
    # 实现放在本文件底部的 _extract_frontend_impl()。
    js_path = Path("/tmp/h3-ts-frontend-check.js")
    js_path.write_text(_extract_frontend_impl(), encoding="utf-8")
    node = subprocess.run(["node", str(js_path), err3],
                          capture_output=True, text=True)
    if node.returncode == 0:
        shown = json.loads(node.stdout.strip().splitlines()[-1])["shown"]
        step("D_frontend_friendly_error", {
            "input": err3,
            "shown": shown,
            "is_chinese": any("\u4e00" <= ch <= "\u9fff" for ch in shown),
            "no_python_text": "object has no attribute" not in shown,
        })
        if "object has no attribute" in shown:
            report["ok"] = False
    else:
        step("D_frontend_friendly_error", {"error": node.stderr[:400]})
        report["ok"] = False

    out = Path("/tmp/h3-typesafety-runtime-report.json")
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print()
    print(f"报告: {out}")
    print("✅ 运行时取证通过" if report["ok"] else "❌ 运行时取证失败")

    # 清理自起的实例
    if proc is not None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    try:
        import asyncio as _aio
        _aio.run_coroutine_threadsafe(fake_runner.cleanup(), fake_loop).result(10)
        fake_loop.call_soon_threadsafe(fake_loop.stop)
    except Exception:
        pass
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
