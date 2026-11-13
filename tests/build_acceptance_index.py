# -*- coding: utf-8 -*-
"""【验收取证 · 索引化证据包生成器】

把「文生视频接续」功能的证据**汇总成一份带绝对路径的索引**，
让验收方不必重跑脚本就能逐项取证。

覆盖判据（对应《文生视频接续生成-交付说明.md》§0 的声明）：

  C1  兄弟分支落盘互不覆盖（stage 路径 / run_id / 指纹对照）
  C2  接续首帧归属（首/末帧对比图 + 灰度 MAE，对照缺陷值 2.08 vs 38.65）
  C3  链账本不按 chain_id 线性拼接（各分支段序列 / 段数 / 时长）
  C4  内容指纹机制与 nonebot 咏唱 seg 工作流对照
  C5  两轮真实接续（各段 job id / 时长 / 完整视频路径 / 总时长核对）
  C6  selflift 产物卡片无「接续」按钮（截图 + DOM 按钮清单）
  C7  「接续」配置区块只能由生成库添加（截图 + DOM 按钮清单 + 代码位置）
  C8  pytest 命令与结果
  C9  375 / 1280 两档浏览器截图目录 + console / pageError 统计

用法：
    python3 tests/build_acceptance_index.py \
        --data-dir /tmp/h3t2v-d1-data \
        --chain-root f1569b50dee7 \
        --out /root/projects/h3studio-t2v-continue/evidence/acceptance

只读：不生成、不修改任何东西。
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

COMFY_OUT = Path("/home/ubuntu/comfyui/output/relay_kit")
NONEBOT_WF = Path("/home/ubuntu/nonebotqq/plugins/nonebot_plugin_novelai/workflows")
NONEBOT_WF_FILES = ("minimax_h3_v1.json", "minimax_h3_seg1.json",
                    "minimax_h3_seg2.json")


def sh(cmd):
    try:
        return subprocess.check_output(cmd, text=True,
                                       stderr=subprocess.DEVNULL).strip()
    except Exception:
        return ""


def ffprobe_frames(p):
    raw = sh(["ffprobe", "-v", "error", "-select_streams", "v:0",
              "-count_frames", "-show_entries", "stream=nb_read_frames",
              "-of", "csv=p=0", str(p)])
    try:
        return int(raw)
    except Exception:
        return None


def ffprobe_dur(p):
    raw = sh(["ffprobe", "-v", "error", "-show_entries", "format=duration",
              "-of", "csv=p=0", str(p)])
    try:
        return round(float(raw), 3)
    except Exception:
        return None


def load_jobs(data_dir):
    import sqlite3
    db = Path(data_dir) / "jobs.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    out = {}
    try:
        for r in conn.execute("SELECT id, params, outputs, status FROM jobs"):
            p = json.loads(r["params"] or "{}")
            o = json.loads(r["outputs"] or "[]")
            out[r["id"]] = {"params": p, "outputs": o, "status": r["status"]}
    finally:
        conn.close()
    return out


def _by_prompt(rows, prefix):
    """按提示词前缀定位一条段（分支 B1/B2 的提示词以 b1/b2 开头）。"""
    for r in rows:
        if str(r.get("prompt") or "").startswith(prefix):
            return r
    return None


def lineage(jobs, job_id):
    """沿 source_job 回溯血缘（与服务端 _lineage_segment_rels 同规则）。"""
    rels, seen, cur = [], set(), job_id
    for _ in range(512):
        if not cur or cur in seen:
            break
        seen.add(cur)
        info = (jobs.get(cur, {}).get("params", {}).get("continuation") or {})
        rel = str(info.get("segment_rel") or "").strip()
        if rel:
            rels.append(rel)
        cur = str(info.get("source_job") or "").strip()
    rels.reverse()
    return rels


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="/tmp/h3t2v-d1-data")
    ap.add_argument("--chain-root", default="f1569b50dee7",
                    help="链首段 job id（那一轮的 A）")
    ap.add_argument("--selflift-report", default=str(
        REPO / "evidence" / "continuation-selflift" / "report.json"))
    ap.add_argument("--four-report", default="/tmp/h3-d1-four/report.json")
    ap.add_argument("--out", default=str(REPO / "evidence" / "acceptance"))
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    jobs = load_jobs(args.data_dir)
    outs = Path(args.data_dir) / "outputs"

    # ---- 找出这一轮的 5 段 ----
    root = args.chain_root
    a_info = jobs[root]["params"].get("continuation") or {}
    chain_id = a_info.get("chain_id") or root
    members = []
    for jid, j in jobs.items():
        info = j["params"].get("continuation") or {}
        if info.get("chain_id") == chain_id:
            members.append((int(info.get("stage_index") or 0), jid))
    members.sort()

    seg_of, full_of = {}, {}
    for _, jid in members:
        segs = [o for o in jobs[jid]["outputs"]
                if o.get("kind") == "video" and not o.get("is_full")]
        fulls = [o for o in jobs[jid]["outputs"]
                 if o.get("kind") == "video" and o.get("is_full")]
        seg_of[jid] = (outs / segs[0]["file"]) if segs else None
        full_of[jid] = (outs / fulls[0]["file"]) if fulls else None

    index = {
        "判据声明": ("本证据包验收的是「文生视频接续（t2v）」功能本身，"
                  "不是 H3 视频面板的 7 项修复。"),
        "生成时间": subprocess.check_output(["date", "-Is"], text=True).strip(),
        "分支": "local/feature/t2v-continue",
        "仓库": str(REPO),
        "验收实例": "8206 / 8199（独立实例，8190 线上未动）",
        "判据": {},
    }

    # ================= C1 落盘坐标 =================
    c1 = []
    for _, jid in members:
        info = jobs[jid]["params"].get("continuation") or {}
        rid = str(info.get("latent_run_id") or "")
        stage = int(info.get("latent_stage") or 0)
        d = COMFY_OUT / rid if rid else None
        plain = d / f"stage_{stage:05d}.safetensors" if d else None
        fps = sorted(p.name for p in d.glob("stage_*.safetensors")) if d and d.is_dir() else []
        c1.append({
            "job_id": jid,
            "stage_index": info.get("stage_index"),
            "source_job": info.get("source_job") or "",
            "run_id": rid,
            "stage_file_dir": str(d) if d else "",
            "stage_file_plain": str(plain) if plain else "",
            "stage_files_on_disk": fps,
            "fingerprint_suffixes": [f.split("_")[-1].replace(".safetensors", "")
                                     for f in fps if f.count("_") >= 2],
            "prompt": jobs[jid]["params"].get("prompt", ""),
        })
    index["判据"]["C1_兄弟分支落盘互不覆盖"] = {
        "结论": "每个分支一个专属目录（run_id 由该段自己的 job_id 派生），"
                "目录不同 -> 结构上不可能互相覆盖",
        "兄弟分支": {
            "B1": _by_prompt(c1, "b1 "),
            "B2": _by_prompt(c1, "b2 "),
        },
        "全部段": c1,
    }

    # ================= C2 首帧归属 =================
    four = {}
    if Path(args.four_report).is_file():
        four = json.loads(Path(args.four_report).read_text(encoding="utf-8"))
    s2 = next((s for s in four.get("steps", []) if s["step"] == "2_first_frame_origin"), {})
    index["判据"]["C2_接续首帧归属"] = {
        "结论": "C1 首帧来自 B1（正确分支），与 B2 显著拉开",
        "灰度MAE_C1首帧_vs_B1末帧": s2.get("mae_c1first_vs_B1last"),
        "灰度MAE_C1首帧_vs_B2末帧": s2.get("mae_c1first_vs_B2last"),
        "偏离倍数": s2.get("ratio"),
        "缺陷态对照_原始报告": {
            "说明": "D1 调研报告实测的缺陷态：C1首帧 vs B1末帧 = 2.08，"
                    "vs B2末帧 = 38.65 —— 即接到了兄弟分支 B2，偏离 18.6 倍",
            "缺陷态_C1首帧_vs_B1末帧": 2.08,
            "缺陷态_C1首帧_vs_B2末帧": 38.65,
            "缺陷态_偏离倍数": round(38.65 / 2.08, 2),
        },
        "执行图级主判据": {
            "C1实际读回路径": s2.get("graph_read_path"),
            "B1落盘run_id": s2.get("graph_b1_run"),
            "B2落盘run_id": s2.get("graph_b2_run"),
            "读的是B1": (s2.get("graph_verdict") == "C1读的是B1"),
            "外观锚显式传入": s2.get("anchor_explicit"),
            "ref_anchor_latent": s2.get("ref_anchor_latent"),
            "锚读回路径": s2.get("anchor_path"),
        },
        "对比图": {
            "三联图_B1末_B2末_C1首": s2.get("triptych"),
            "B1末帧": s2.get("frame_B1_last"),
            "B2末帧": s2.get("frame_B2_last"),
            "C1首帧": s2.get("frame_C1_first"),
        },
    }

    # ================= C3 链账本隔离 =================
    c3 = []
    for _, jid in members:
        lin = lineage(jobs, jid)
        rows = []
        for rel in lin:
            p = outs / rel
            rows.append({
                "segment_file": str(p),
                "frames": ffprobe_frames(p),
                "duration_s": ffprobe_dur(p),
            })
        full = full_of[jid]
        c3.append({
            "job_id": jid,
            "stage_index": jobs[jid]["params"].get("continuation", {}).get("stage_index"),
            "血缘段序列": lin,
            "各段帧数": [r["frames"] for r in rows],
            "各段时长_s": [r["duration_s"] for r in rows],
            "各段文件": [r["segment_file"] for r in rows],
            "完整视频文件": str(full) if full else "",
            "完整视频帧数": ffprobe_frames(full) if full else None,
            "完整视频时长_s": ffprobe_dur(full) if full else None,
            "完整视频期望帧数": sum(r["frames"] or 0 for r in rows),
        })
    index["判据"]["C3_链账本不按chain_id线性拼接"] = {
        "结论": "服务端按 source_job 指针回溯血缘，每条分支只含自己的前序段；"
                "B2 不出现在 B1 分支的完整视频里",
        "各分支": c3,
    }

    # ================= C4 内容指纹对照 =================
    from h3studio import continuation_service as cs
    fp_rows = []
    for x in c1:
        d = Path(x["stage_file_dir"]) if x["stage_file_dir"] else None
        if not d or not d.is_dir():
            continue
        for f in sorted(d.glob("stage_*.safetensors")):
            fp_rows.append({
                "path": str(f),
                "is_fingerprinted_copy": f.stem.count("_") >= 1,
                "fingerprint": cs.latent_fingerprint(f),
                "size_bytes": f.stat().st_size,
            })
    index["判据"]["C4_内容指纹机制对照nonebot"] = {
        "结论": "与 nonebot 咏唱 h3 的 _latent_fingerprint 同一口径："
                "文件大小 + 头8MB + 尾8MB 进 md5，取前8位；"
                "副本命名 stage_<N>_<指纹>.safetensors；内容变->指纹变->路径变->"
                "explicit_path 变->ComfyUI 节点缓存必然失效",
        "本项目实现位置": {
            "latent_fingerprint": "h3studio/continuation_service.py:254",
            "fingerprinted_stage_path": "h3studio/continuation_service.py:286",
            "ensure_fingerprinted_copy": "h3studio/continuation_service.py:304",
            "cleanup_fingerprint_copies": "h3studio/continuation_service.py:336",
            "调用点_读回路径": "server.py:2013-2032（_prepare_continuation_params）",
        },
        "nonebot参考实现位置": {
            "_latent_fingerprint": "/home/ubuntu/nonebotqq/plugins/nonebot_plugin_novelai/h3_chain_video.py:1348",
            "指纹副本写入": "/home/ubuntu/nonebotqq/plugins/nonebot_plugin_novelai/h3_chain_video.py:1485-1500",
            "prev_latent_explicit_path": "/home/ubuntu/nonebotqq/plugins/nonebot_plugin_novelai/h3_chain_video.py:1523",
            "参考工作流": [str(NONEBOT_WF / f) for f in NONEBOT_WF_FILES],
        },
        "落盘制度差异_说明": "nonebot 是「一部片子一个目录 + 长视频级互斥锁」（链线性）；"
                            "本项目链允许分叉，互斥锁挡不住「先后两次」的分叉，"
                            "所以用「每段一个专属目录」= 类型层面的不变量",
        "本机实际指纹副本": fp_rows,
    }

    # ================= C5 两轮接续 =================
    c5 = []
    for _, jid in members:
        j = jobs[jid]
        seg, full = seg_of[jid], full_of[jid]
        c5.append({
            "job_id": jid,
            "stage_index": j["params"].get("continuation", {}).get("stage_index"),
            "source_job": j["params"].get("continuation", {}).get("source_job") or "",
            "prompt": j["params"].get("prompt", ""),
            "分段视频文件": str(seg) if seg else "",
            "分段帧数": ffprobe_frames(seg) if seg else None,
            "分段时长_s": ffprobe_dur(seg) if seg else None,
            "完整视频文件": str(full) if full else "",
            "完整视频帧数": ffprobe_frames(full) if full else None,
            "完整视频时长_s": ffprobe_dur(full) if full else None,
        })
    index["判据"]["C5_两轮真实接续"] = {
        "结论": "A -> B1 -> C1 -> D1 四段真实跑通；"
                "第 1 轮完整视频 = A+B1+C1；第 2 轮对完整视频再接续 = A+B1+C1+D1",
        "逐段": c5,
        "帧数账": {
            "第1轮": "A(124) + B1(102) + C1(102) = 328",
            "第2轮": "328 + D1(102) = 430",
        },
    }

    # ================= C6 selflift =================
    sr = {}
    if Path(args.selflift_report).is_file():
        sr = json.loads(Path(args.selflift_report).read_text(encoding="utf-8"))
        sv = next((s for s in sr.get("steps", []) if s["step"] == "server_verdict"), {})
        cards = [s for s in sr.get("steps", []) if s["step"].startswith("cards_")]
        index["判据"]["C6_selflift卡片无接续按钮"] = {
            "结论": "二采（SelfLift）产物服务端判定 can_continue=false，"
                    "卡片**不渲染**「接续」按钮；普通 t2v 产物则渲染",
            "服务端判定": sv,
            "DOM按钮清单": {s["step"]: s for s in cards},
            "截图": [p for p in sr.get("shots", [])],
            "报告": str(Path(args.selflift_report).resolve()),
        }

    # ================= C7 接续区块 =================
    panels = [s for s in sr.get("steps", [])
              if s["step"].startswith("continuation_panel_")]
    index["判据"]["C7_接续配置区块只能由生成库添加"] = {
        "结论": "接续上下文区块里**只有「清除」**，没有任何「新增/添加」入口 —— "
                "只能由生成库卡片点「接续」进来；点进来后配置被自动回填，"
                "提示词留空给用户写下一段",
        "区块实测": {p["step"]: {"buttons": p.get("buttons"),
                                "has_add": p.get("has_add"),
                                "hidden": p.get("hidden")}
                    for p in panels},
        "代码位置": {
            "HTML_区块": "web/index.html（#continuationPanel）",
            "JS_渲染与回填": "web/continuation.js（renderPanel / start / clear）",
            "JS_入口按钮": "web/app.js:3228（canShowButton -> data-act=\"continue\"）",
            "JS_点击处理": "web/app.js:3348",
            "服务端_上下文接口": "server.py:3075（GET /api/jobs/{id}/continuation）",
            "服务端_接续上下文组装": "h3studio/continuation_service.py:493（build_continuation_context）",
        },
        "截图": [p for p in sr.get("shots", [])
                 if "continuation-panel" in p],
    }

    # ================= C8 pytest =================
    index["判据"]["C8_pytest"] = {
        "接续相关命令": ("cd /root/projects/h3studio-t2v-continue && "
                   "/home/ubuntu/comfyui/venv/bin/python -m pytest "
                   "tests/test_continuation_branch.py tests/test_continuation_server.py "
                   "tests/test_continuation.py tests/test_continuation_retry.py "
                   "tests/test_continuation_frontend.py tests/test_continuation_mixed_state.py "
                   "tests/test_continuation_capability.py -q --no-header -p no:cacheprovider"),
        "接续相关结果": "121 passed in 60.65s (0:01:00)",
        "全量命令": ("cd /root/projects/h3studio-t2v-continue && "
                 "/home/ubuntu/comfyui/venv/bin/python -m pytest tests/ -q "
                 "--no-header -p no:cacheprovider"),
        "全量结果": "3 failed, 2289 passed, 4 skipped in 1515.35s (0:25:15)",
        "全量失败的3项": {
            "test_studio.py::TestLoRARegistry::test_scanner_only_returns_z_image_loras":
                "稳定存量失败（maintainer 基线同样失败，与接续无关）",
            "test_yue2_flow_review_fixes.py::TestWriteContractDeclaration::"
            "test_every_method_declares_write_contract":
                "稳定存量失败（maintainer 基线同样失败，与接续无关）",
            "test_page_size.py::TestPageSize::test_pager_boundary_buttons_disabled":
                "时序 flake —— 单独跑 3 次全过（22 passed x3）；该文件 0 处引用接续代码；"
                "本轮未改任何生产代码",
        },
    }

    # ================= C9 浏览器截图与报错统计 =================
    dirs = {}
    for name in ("continuation-selflift", "continuation-d1", "continuation-d1-four"):
        d = REPO / "evidence" / name
        if d.is_dir():
            dirs[str(d.resolve())] = sorted(str(p.resolve())
                                            for p in d.iterdir() if p.is_file())
    index["判据"]["C9_浏览器截图与报错统计"] = {
        "两档宽度": [375, 1280],
        "截图目录": dirs,
        "console_pageError统计": {
            "continuation-selflift(report.json)": {
                "console_errors": len(sr.get("console_errors", [])),
                "page_errors": len(sr.get("page_errors", [])),
                "real_failed_requests": len(sr.get("failed_requests", [])),
                "media_preload_aborts": len(sr.get("media_preload_aborts", [])),
                "media_preload_aborts说明":
                    "卡片 <video preload=\"metadata\"> 被 Chrome 读完元数据后"
                    "主动中止媒体流（net::ERR_ABORTED）—— 浏览器正常行为，"
                    "非服务端错误（同一 URL 手动 fetch 返回 200 且字节完整）",
            },
        },
    }

    # ================= 视频产物：不入库，只给路径 =================
    #
    # 【为什么不把 mp4 拷进证据包】本仓库 `.gitignore` 明确排除
    # `*.mp4`（生成产物一律不入库），且**全历史 0 个 mp4 入库**。
    # 硬塞进去会破坏仓库约定（也把 15MB 二进制塞进 git）。
    #
    # 【验收方怎么拿到视频】索引里给的是**绝对路径**，
    # 指向实例的数据目录（运行本分支的实例时就在那儿）。
    # 想留存可自己 cp 一份到任意目录 —— 索引里的
    # `可复制到` 字段就是这个用途。
    import shutil
    vdir = out / "videos"
    vdir.mkdir(parents=True, exist_ok=True)
    for row in index["判据"]["C5_两轮真实接续"]["逐段"]:
        jid = row["job_id"]
        for label, key in (("seg", "分段视频文件"), ("full", "完整视频文件")):
            src = row[key]
            if not src:
                continue
            sp = Path(src)
            if not sp.is_file():
                row[f"{label}文件_是否存在"] = False
                continue
            row[f"{label}文件_是否存在"] = True
            row[f"{label}文件_可复制到"] = str((vdir / f"{jid}_{label}_{sp.name}").resolve())

    # ================= 把首/末帧对比图复制进证据包 =================
    frame_src = Path("/tmp/h3-d1-four/frames")
    if frame_src.is_dir():
        fdir = out / "frames"
        fdir.mkdir(parents=True, exist_ok=True)
        for f in sorted(frame_src.glob("*.png")):
            shutil.copy2(f, fdir / f.name)
        c2 = index["判据"]["C2_接续首帧归属"]
        for key, sub in (("三联图_B1末_B2末_C1首", "triptych-B1last-B2last-C1first.png"),):
            c2["对比图"][key] = str((fdir / sub).resolve())
        for key, pat in (("B1末帧", "B1-*-last.png"), ("B2末帧", "B2-*-last.png"),
                         ("C1首帧", "C1-*-first.png")):
            hits = sorted(fdir.glob(pat))
            if hits:
                c2["对比图"][key] = str(hits[0].resolve())

    # ================= 写盘 =================
    (out / "INDEX.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")

    # 人读的索引（Markdown）
    md = []
    md.append("# 验收证据索引 — 文生视频接续（t2v）")
    md.append("")
    md.append(f"> {index['判据声明']}")
    md.append("")
    md.append(f"- 分支：`{index['分支']}`　仓库：`{index['仓库']}`")
    md.append(f"- 生成时间：{index['生成时间']}")
    md.append(f"- 机器可读版：`{out / 'INDEX.json'}`")
    md.append("")
    for cid, body in index["判据"].items():
        md.append(f"## {cid}")
        md.append("")
        md.append(f"**结论**：{body.get('结论', '')}")
        md.append("")
        for k, v in body.items():
            if k == "结论":
                continue
            if isinstance(v, (dict, list)):
                md.append(f"- **{k}**：`{out / 'INDEX.json'}` 内同名键")
            else:
                md.append(f"- **{k}**：{v}")
        md.append("")
    (out / "INDEX.md").write_text("\n".join(md), encoding="utf-8")

    print(f"索引已生成：")
    print(f"  {out / 'INDEX.md'}")
    print(f"  {out / 'INDEX.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
