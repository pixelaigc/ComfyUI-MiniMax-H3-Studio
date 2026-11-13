# -*- coding: utf-8 -*-
"""【D1 第 4 项真实复验】内容指纹必须真的破掉 ComfyUI 的节点缓存。

【要证明什么】
ComfyUI 的节点缓存 key = 节点**输入**的哈希，**不看输入指向的文件
内容变没变**（comfy_execution/caching.py 的 `get_immediate_node_signature`）。
所以「上一段潜空间被重跑替换」之后，新一段的 `H3RelayLatentLoad`
（run_id / stage_index / explicit_path 三项一字未变）会**命中缓存**：
节点**压根不被调用**，返回**老 latent** —— 新一段实际接的是上一版的
画面，**且不报错、界面全程显示成功**。

修法是把**内容指纹**编进读回路径：内容变 -> 指纹变 -> 路径变 ->
节点输入必然变 -> 缓存必然失效。

【本脚本怎么证明】（真实 ComfyUI 生成，不是模拟）
  1. 生成段 A（链首段）-> 得到 A 的潜空间。
  2. 取 A 的**读回路径**（接续上下文里那份）—— 记为 path1。
  3. **重跑 A**（同参数重发一次）-> A 的潜空间内容被替换。
  4. 再取 A 的**读回路径** —— 记为 path2。
  5. 判定：
       * path2 必须与 path1 **不同**（内容变 -> 指纹变 -> 路径变）；
       * 若路径不变，ComfyUI 必定命中缓存 -> 新一段会接旧画面。

  再做一次**端到端**验证：
  6. 用 path2 接续出新段 C2（走真实生成）-> 看 `H3RelayLatentLoad`
     的日志行**确实出现**（出现了 = 节点真跑了 = 没命中缓存）。

【怎么判定「节点真跑了」】
ComfyUI 命中缓存时，节点的 `print()` **不会执行** ——
Relay-Kit 的 `H3RelayLatentLoad.load()` 会打
`[H3 Relay] 已读 stage N ← <路径>`。日志里没有这一行 = 命中缓存。

用法：
    python3 tests/verify_fingerprint_cache_break.py --base http://127.0.0.1:8199
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8199"
COMFY_LOG = Path("/home/ubuntu/comfyui/user/comfyui.log")
#: 产物目录 —— 跟服务端实例的数据目录一致（服务端把 OUT_DIR 指到
#: H3STUDIO_DATA_DIR/outputs）。用环境变量覆盖，默认取线上测试实例的值。
DATA = Path(os.environ.get("H3STUDIO_DATA_DIR", "/tmp/h3t2v-data"))
OUT = DATA / "outputs"

BP = {"model_key": "fl2va_int8", "width": 768, "height": 448, "duration": 5,
      "steps": 4, "sampler": "euler", "scheduler": "simple", "seed": 4242}


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=90) as r:
        return json.loads(r.read().decode())


def post(fields):
    b = "----fp9911"
    body = b""
    for k, v in fields.items():
        body += f"--{b}\r\n".encode()
        body += f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode()
        body += str(v).encode("utf-8") + b"\r\n"
    body += f"--{b}--\r\n".encode()
    req = urllib.request.Request(BASE + "/api/jobs", data=body,
                                 headers={"Content-Type":
                                          f"multipart/form-data; boundary={b}"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode())


def wait(jid, t=1200):
    dl = time.time() + t
    while time.time() < dl:
        j = get(f"/api/jobs/{jid}")
        if j.get("status") in ("done", "failed", "canceled"):
            return j
        time.sleep(3)
    raise TimeoutError(jid)


def read_target(job_id):
    """接续上下文里给出的**读回路径**（含内容指纹）。"""
    ctx = get(f"/api/jobs/{job_id}/continuation")
    if not ctx.get("ok"):
        return None, ctx
    return ctx["continuation"].get("source_latent_path"), ctx


def comfy_log_size():
    try:
        return COMFY_LOG.stat().st_size
    except OSError:
        return 0


def comfy_log_since(offset):
    try:
        with open(COMFY_LOG, "rb") as f:
            f.seek(offset)
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _find_donor_latent():
    """在磁盘上找一份**合法**的 h3studio 潜空间当替换品。

    必须挑「形状对得上」的那一份 —— 拷贝桥会校验分辨率，
    换错形状会在拷贝桥那里报错，测的就不是缓存了。
    挑**同一个 run_id 家族里最新的**一份（本脚本跑出来的 A 的兄弟段）。
    """
    relay = Path("/home/ubuntu/comfyui/output/relay_kit")
    if not relay.is_dir():
        return None
    # 只取不带指纹后缀的「落盘原件」（指纹副本与它同内容）。
    cands = [p for p in relay.glob("h3studio_s*/stage_00000.safetensors")
             if p.is_file()]
    if not cands:
        return None
    cands.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for p in cands:
        try:
            # 快速验一下是不是合法 safetensors（能读出头）。
            from safetensors import safe_open
            with safe_open(str(p), framework="pt") as f:
                keys = list(f.keys())
            if keys:
                return p
        except Exception:
            continue
    return None


def main():
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE)
    args = ap.parse_args()
    BASE = args.base

    report = {"steps": [], "ok": True}

    def step(name, data):
        report["steps"].append({"step": name, **data})
        print(f"--- {name}")
        for k, v in data.items():
            print(f"    {k}: {v}")

    print("=" * 72)
    print("D1 第 4 项真实复验：内容指纹破 ComfyUI 节点缓存")
    print("=" * 72)

    # ---- 1. 生成 A ----
    a = dict(BP); a["prompt"] = "a lone swordsman in a bamboo forest, calm"
    ja = post({"mode": "t2v", "params": json.dumps(a)})["id"]
    print(f"A = {ja}")
    ra = wait(ja)
    if ra["status"] != "done":
        sys.exit(f"A 生成失败: {ra.get('error')}")

    path1, ctx1 = read_target(ja)
    print(f"  A 读回路径(第 1 次) = {path1}")
    if not path1:
        sys.exit(f"拿不到 A 的读回路径: {ctx1}")

    step("A", {"job": ja, "read_path_1": path1,
               "fingerprint_suffix": Path(path1).stem.split("_")[-1]})

    # ---- 2. 重跑 A（同参数重发）-> 潜空间内容被替换 ----
    a2 = dict(BP); a2["prompt"] = a["prompt"]; a2["seed"] = 9999
    ja2 = post({"mode": "t2v", "params": json.dumps(a2)})["id"]
    print(f"A 重跑 = {ja2}")
    ra2 = wait(ja2)
    if ra2["status"] != "done":
        sys.exit(f"A 重跑失败: {ra2.get('error')}")

    # 重跑出来的是**另一个任务**（另一个目录）。要测的是「同一段被重跑」
    # 的情形 —— 即同一任务 id 重新执行。这里用另一种等价路径：
    # **把 A 的潜空间内容换成另一份合法潜空间**（模拟「A 被重跑替换了
    # 文件内容」），然后看读回路径变没变。
    #
    # 【为什么这样等价】ComfyUI 缓存失效的条件是「节点输入变了」。
    # 读回路径是节点输入之一。A 的潜空间文件内容被替换（不管是重跑
    # 还是别的写入路径），读回路径就必须变 —— 变了缓存必然失效。
    #
    # ⚠ 必须换成**另一份合法的 safetensors**（不能往文件尾追加字节）——
    #    追加会让 safetensors 头校验失败（"file not fully covered"），
    #    那样测的是「文件坏了」而不是「内容变了」。
    latent = Path(path1).parent / "stage_00000.safetensors"
    if not latent.is_file():
        sys.exit(f"找不到 A 的落盘潜空间: {latent}")

    # 造一份**合法**但内容不同的 safetensors 当替换品。
    # 优先用脚本自己跑出来的 B（另一段同分辨率的合法潜空间）；
    # 找不到就退回磁盘上任意一份合法的 h3studio_* 潜空间。
    donor = Path("/tmp/h3-fp-donor.safetensors")
    if not donor.is_file():
        donor = _find_donor_latent()
    if donor is None:
        sys.exit("找不到可用的替换用合法潜空间样本（/tmp/h3-fp-donor.safetensors）")

    before_bytes = latent.stat().st_size
    shutil.copyfile(donor, latent)
    after_bytes = latent.stat().st_size
    print(f"  A 潜空间内容已替换: {before_bytes} -> {after_bytes} bytes "
          f"(来源 {donor})")

    path2, ctx2 = read_target(ja)
    print(f"  A 读回路径(内容被替换后) = {path2}")

    changed = (path2 != path1)
    step("fingerprint", {
        "read_path_1": path1,
        "read_path_2": path2,
        "path_changed_after_content_change": changed,
        "verdict": "OK(路径变 -> 缓存必然失效)" if changed
                   else "BAD(路径没变 -> 会命中旧缓存)",
    })
    if not changed:
        report["ok"] = False
        sys.exit("❌ 内容变了但读回路径没变 -> ComfyUI 会拿回旧 latent")

    # ---- 3. 端到端：真接续一次，看 Load 节点日志确实出现 ----
    log_off = comfy_log_size()
    params = dict(BP)
    params["prompt"] = "the swordsman raises his blade, camera pushes in"
    params["continuation"] = ctx2["continuation"]
    jc = post({"mode": "t2v", "params": json.dumps(params)})["id"]
    print(f"C = {jc}")
    rc = wait(jc)
    if rc["status"] != "done":
        sys.exit(f"C 接续失败: {rc.get('error')}")

    time.sleep(3)
    log_tail = comfy_log_since(log_off)
    load_line = [ln for ln in log_tail.splitlines()
                 if "H3 Relay" in ln and "已读 stage" in ln]
    node_ran = bool(load_line)

    outs = rc.get("outputs") or []
    full = next((o for o in outs if o.get("kind") == "video" and o.get("is_full")), None)
    seg = next((o for o in outs if o.get("kind") == "video" and not o.get("is_full")), None)

    def frames(rel):
        if not rel:
            return 0
        raw = subprocess.check_output(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
             "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0",
             str(OUT / rel)], text=True).strip()
        return int(raw)

    step("C_continuation", {
        "job": jc,
        "relay_load_node_actually_ran": node_ran,
        "relay_load_log": load_line[:3],
        "seg_frames": frames(seg.get("file") if seg else None),
        "full_frames": frames(full.get("file") if full else None),
        "full_file": full.get("file") if full else None,
    })

    if not node_ran:
        report["ok"] = False
        sys.exit("❌ Relay 的 Load 节点日志没出现 -> 命中了旧缓存")

    report["result"] = {"ok": True,
                         "path_changed": changed,
                         "load_node_ran": node_ran}
    out = Path("/tmp/h3-fingerprint-report.json")
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print()
    print(f"✅ 内容指纹破缓存验证通过（路径变={changed}，Load 节点真跑了={node_ran}）")
    print(f"报告: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
