# -*- coding: utf-8 -*-
"""
端到端实测 · 三段式 VLM 扫描：图像 / 视频 / 音频三条能力，
带参考图场景都能产出扫描结果并正确落点。

【实测口径】
  * **独立素材库**（ASSET_LIBRARY_ROOT 指向临时目录）——
    **不碰用户媒体库**，不往线上素材库写任何东西
  * **独立端口 + 独立临时 sqlite**（StudioServer 自带）
  * 真实 hermes CLI 调用 + 真实 VLM 调用（127.0.0.1:5000）
  * 参考图是**当场生成**的合成图（不引用用户素材）

【三段式怎么验】
  1. 阶段2：VLM 真的读了参考图 -> 产出扫描结果（物件/服装/光照/调色板）
  2. 阶段3：hermes 合成阶段收到「已核实素材事实」节 + 初稿
     -> 证据是 query 文件里出现该节，且产物里出现扫描出的物件
  3. 落点：增强后的提示词进工作流节点
     图像 CLIPTextEncode.text / 视频 MiniMaxH3ImageToVideo.prompt /
     音频 YuE2GenerateABC.style
"""
import asyncio
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

REPO = Path("/root/projects/h3studio-prompt-enhance")
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

OUT = Path("/root/.hermes-web-ui/.ekko/workspace/default/"
           "person-main-505e35f9-2826-4d4c-af53-d3dc5de6f957/"
           "evidence-hermes-scan-20261122")
OUT.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------- 隔离素材库
LIB = Path(tempfile.mkdtemp(prefix="h3studio-scan-lib-"))
os.environ["ASSET_LIBRARY_ROOT"] = str(LIB)


def make_probe_images():
    """生成 2 张合成参考图（不引用用户素材）。"""
    from PIL import Image, ImageDraw
    paths = []
    # 图1：桌面场景（书本 / 杯子 / 花瓶 / 台灯）
    img = Image.new("RGB", (768, 512), (32, 40, 60))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 370, 768, 512], fill=(104, 68, 42))
    d.rectangle([70, 230, 215, 370], fill=(214, 210, 200))   # 书本
    d.rectangle([220, 230, 350, 370], fill=(58, 92, 162))
    d.ellipse([420, 268, 528, 372], fill=(238, 238, 242))   # 陶瓷杯
    d.ellipse([443, 282, 505, 312], fill=(126, 92, 68))
    d.rectangle([596, 196, 660, 370], fill=(72, 142, 92))   # 花瓶
    d.ellipse([604, 166, 652, 212], fill=(198, 84, 86))     # 红花
    d.polygon([(600, 150), (690, 150), (668, 40), (622, 40)], fill=(240, 236, 214))  # 台灯罩
    p1 = LIB / "probe_desk.png"
    img.save(p1)
    paths.append(p1)
    # 图2：更简的场景（咖啡杯 + 笔记本）
    img2 = Image.new("RGB", (640, 480), (212, 200, 186))
    d2 = ImageDraw.Draw(img2)
    d2.rectangle([0, 300, 640, 480], fill=(150, 118, 84))
    d2.ellipse([120, 210, 230, 320], fill=(250, 248, 244))
    d2.ellipse([140, 226, 212, 262], fill=(84, 52, 34))
    d2.rectangle([300, 250, 560, 320], fill=(46, 54, 78))
    p2 = LIB / "probe_table.png"
    img2.save(p2)
    paths.append(p2)
    return paths


def seed_asset_library(images):
    """
    在隔离素材库里塞一条图片素材（返回 asset_id）。

    【为什么用 asset_service.register_asset 而不是裸 SQL】
    裸 INSERT 要自己补 sha256 / origin / created_by 等 NOT NULL 列，
    容易与真实 schema 漂移（实测踩到 datatype mismatch）。
    走真实入库 API -> 顺带过一遍去重/元数据/缩略图逻辑，
    得到的素材与用户素材**结构完全一致**，实测才有意义。
    """
    from h3studio import assets as A
    from h3studio import asset_service
    # 【必须调 init_db】ensure_layout 只建目录 + 磁盘自检，
    # 建表在 init_db() 里（h3studio/assets.py:500-508）。
    report = A.init_db()
    if not report.get("ok"):
        raise RuntimeError(f"隔离素材库初始化失败：{report}")
    conn = A.connect()
    try:
        res = asset_service.register_asset(
            conn,
            source_path=Path(images[0]),
            filename="probe_desk.png",
            mime="image/png",
            name="E2E 扫描探针图",
            category="image",
            note="e2e probe",
            source="upload",
        )
        # 【必须自己 commit】register_asset 只写不提交（提交是调用方的事 ——
        # 见 h3studio/agent_tools.py:102 的用法）。不提交的话素材只活在
        # 连接里，**另开连接的 server 进程看不到**（实测报「素材不存在」）。
        conn.commit()
        return res["asset"]["id"]
    finally:
        conn.close()


# ---------------------------------------------------------------- 三条能力
CASES = [
    ("image", "t2i", "桌面静物场景"),
    ("video", "t2v", "镜头缓缓扫过桌面上的静物"),
    ("audio", "t2a", "一段温暖的钢琴曲"),
]


async def main():
    from test_frontend_defaults import StudioServer
    from h3studio import hermes_enhance as he
    from h3studio import item_scan as isc

    images = make_probe_images()
    asset_id = seed_asset_library(images)
    print(f"隔离素材库: {LIB}")
    print(f"探针素材: {asset_id} -> {images[0]}")

    srv = StudioServer()
    srv.start()
    base = f"http://127.0.0.1:{srv.port}"
    evidence = {"port": srv.port, "library": str(LIB),
                "asset_id": asset_id, "cases": []}
    try:
        import urllib.error
        import urllib.request

        def post(path, payload, timeout=1200):
            req = urllib.request.Request(
                f"{base}{path}",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return resp.status, json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                return exc.code, json.loads(exc.read().decode("utf-8"))

        # ---- 通道默认启用（复核服务端下发）----
        with urllib.request.urlopen(f"{base}/api/enhancer", timeout=60) as resp:
            cfg = json.loads(resp.read().decode("utf-8"))
        evidence["hermes_config"] = {
            "enabled": cfg["hermes"]["enabled"],
            "is_default": cfg["hermes"]["is_default"],
            "default_enabled": cfg["hermes"]["default_enabled"],
            "profile": cfg["hermes"]["profile"],
        }
        print(f"\nhermes 通道: enabled={cfg['hermes']['enabled']} "
              f"is_default={cfg['hermes']['is_default']}")

        # ---- 阶段2 单测：直接跑 VLM 扫描（真实调用）----
        print("\n=== 阶段2 直接扫描 ===")
        t0 = time.time()
        scan = await isc.scan_reference([str(images[0])])
        elapsed = round(time.time() - t0, 1)
        if scan:
            strength = isc.build_strength_block(scan)
            sec = isc.render_enhancement_section(scan, strength)
            (OUT / "scan-strength.txt").write_text(strength["text"], encoding="utf-8")
            (OUT / "scan-section.txt").write_text(sec, encoding="utf-8")
            (OUT / "scan-raw.txt").write_text(scan.get("raw") or "", encoding="utf-8")
            evidence["stage2_direct"] = {
                "ok": True, "elapsed_sec": elapsed,
                "items": len(strength["items"]),
                "costume": len(strength["costume"]),
                "subject_words": strength["subject_words"],
                "background_words": strength["background_words"],
                "item_names": [i["name"] for i in strength["items"]],
                "strength_text": strength["text"],
            }
            print(f"  ok {elapsed}s  物件 {len(strength['items'])} 个 / "
                  f"主体 {strength['subject_words']} 词 / "
                  f"背景 {strength['background_words']} 词")
            print(f"  物件名: {[i['name'] for i in strength['items']]}")
        else:
            evidence["stage2_direct"] = {"ok": False, "elapsed_sec": elapsed}
            print(f"  FAIL 扫描失败 ({elapsed}s)")

        # ---- 三条能力：带参考图跑完整增强 ----
        for media, mode, prompt in CASES:
            print(f"\n=== [{media}] 带参考图增强 ===")
            t0 = time.time()
            status, body = post("/api/hermes-enhance", {
                "prompt": prompt,
                "media": media,
                "mode": "replace",
                "asset_refs": [{"asset_id": asset_id, "slot": "ref_image"}],
            })
            elapsed = round(time.time() - t0, 1)
            rec = {
                "media": media, "mode": mode, "prompt": prompt,
                "status": status, "elapsed_sec": elapsed,
                "engine": body.get("engine"),
                "stage": body.get("stage"),
                "profile": body.get("profile"),
                "command": body.get("command"),
                "enhanced_len": len(body.get("enhanced") or ""),
                "scan": body.get("scan"),
                "error": body.get("error"),
                "code": body.get("code"),
            }
            if status == 200:
                (OUT / f"enhanced-{media}.txt").write_text(
                    body["enhanced"], encoding="utf-8")
                rec["enhanced_head"] = body["enhanced"][:400]
                print(f"  ok {elapsed}s stage={body.get('stage')} "
                      f"产物 {rec['enhanced_len']} 字")
                scan_info = body.get("scan") or {}
                print(f"  扫描: items={scan_info.get('items')} "
                      f"costume={scan_info.get('costume')} "
                      f"主体词={scan_info.get('subject_words')}")
            else:
                print(f"  FAIL {status} {body.get('error')}")
            evidence["cases"].append(rec)

        # ---- 落点验证：增强后的提示词进工作流节点 ----
        print("\n=== 落点验证 ===")
        from h3studio import workflows
        from h3studio import music_yue2
        WF_CASES = [
            ("t2i", "image", {"job_id": "e2e_scan_img", "width": 1024,
                              "height": 1024, "seed": 1, "steps": 8, "cfg": 1.0,
                              "model": "z_image_turbo_bf16"}),
            ("t2v", "video", {"job_id": "e2e_scan_vid", "width": 1280,
                              "height": 720, "seed": 1, "steps": 20, "cfg": 1.0,
                              "shift": 5.0, "model_key": "lora", "duration": 5}),
            ("t2a", "audio", {"job_id": "e2e_scan_aud",
                              "checkpoint": "yue2_3b_bf16", "seed": 1}),
        ]
        lands = []
        for mode, media, params in WF_CASES:
            src = OUT / f"enhanced-{media}.txt"
            if not src.exists():
                continue
            prompt = src.read_text(encoding="utf-8").strip()
            p = dict(params)
            p["prompt"] = prompt
            try:
                wf = (music_yue2.build_t2a(p) if mode == "t2a"
                      else workflows.BUILDERS[mode](p))
            except Exception as exc:
                lands.append({"mode": mode, "ok": False,
                              "reason": f"{type(exc).__name__}: {exc}"})
                continue
            found = []
            probe = prompt[:60]
            for nid, node in (wf or {}).items():
                if not isinstance(node, dict):
                    continue
                for key, val in (node.get("inputs") or {}).items():
                    if isinstance(val, str) and probe and probe in val:
                        found.append({"node": nid,
                                      "class_type": node.get("class_type"),
                                      "input": key, "chars": len(val)})
            lands.append({"mode": mode, "media": media, "ok": bool(found),
                          "node_count": len(wf or {}), "prompt_nodes": found[:4]})
            print(f"  {mode}: {found[:2]}")
        evidence["workflow_landing"] = lands

    finally:
        srv.stop()

    (OUT / "evidence-scan.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n证据：{OUT / 'evidence-scan.json'}")
    # 清理隔离素材库
    shutil.rmtree(LIB, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
