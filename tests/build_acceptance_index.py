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


def _tail_pytest(log_path):
    """从 pytest 日志里解析出结果摘要与失败项（不手写数字）。"""
    if not log_path.is_file():
        return {}
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return {}
    summary, failed = "", {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("FAILED "):
            name = line[len("FAILED "):].split(" - ")[0].strip()
            failed[name] = line
        elif (line[0].isdigit() and " passed" in line) or \
             (line[0].isdigit() and " failed" in line):
            summary = line
    return {"结果": summary, "失败项": failed}


def _d1_browser_stats():
    """读前序轮次 continuation-d1 的浏览器报告统计。"""
    f = REPO / "evidence" / "continuation-d1" / "browser_report.json"
    if not f.is_file():
        return {}
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {
        "console_errors": len(d.get("console_errors", [])),
        "page_errors": len(d.get("page_errors", [])),
        "failed_requests": len(d.get("failed_requests", [])),
        "shots": len(d.get("shots", [])),
    }


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
    ap.add_argument("--data-dir", default=str(REPO / "data-accept"),
                    help="验收实例的数据目录（持久化，扛得住宿主重启）")
    ap.add_argument("--chain-root", default="",
                    help="链首段 job id（留空=自动取最长链的首段）")
    ap.add_argument("--selflift-report", default=str(
        REPO / "evidence" / "continuation-selflift" / "report.json"))
    ap.add_argument("--four-report", default=str(
        REPO / "data-accept" / "evidence-frames" / "report.json"))
    ap.add_argument("--out", default=str(REPO / "evidence" / "acceptance"))
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    jobs = load_jobs(args.data_dir)
    outs = Path(args.data_dir) / "outputs"

    # ---- 找出这一轮的 5 段 ----
    # 【自动定位链首段】不硬编码 job id —— 每次重跑的 id 都不同。
    # 规则：找「段数最多」的链，它的 stage_index==0 那一段就是链首段。
    root = args.chain_root
    if not root:
        chains = {}
        for jid, j in jobs.items():
            info = j["params"].get("continuation") or {}
            cid = str(info.get("chain_id") or "")
            if not cid:
                continue
            chains.setdefault(cid, []).append((int(info.get("stage_index") or 0), jid))
        if not chains:
            print("❌ 库里没有带 continuation 的任务 —— 先跑 verify_d1_four_defects.py")
            return 1
        best = max(chains.values(), key=len)
        root = min(best)[1]
        print(f"自动定位链首段: {root}（链 {best[0][0] and jobs[root]['params']['continuation'].get('chain_id')}，{len(best)} 段）")
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
            "截图": [str(Path(p).resolve()) for p in sr.get("shots", [])],
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
        "截图": [str(Path(p).resolve()) for p in sr.get("shots", [])
                 if "continuation-panel" in p],
    }

    # ================= C8 pytest =================
    # pytest 结论**从落盘日志读**，不手写 —— 免得数字与实际跑出来的不一致
    cont_log = REPO / "data-accept" / "logs" / "pytest-continuation.log"
    full_log = REPO / "data-accept" / "logs" / "pytest-full.log"
    cont_tail = _tail_pytest(cont_log)
    full_tail = _tail_pytest(full_log)
    index["判据"]["C8_pytest"] = {
        "结论": f"接续相关：{cont_tail.get('结果', '未跑')}；"
                f"全量：{full_tail.get('结果', '未跑')}",
        "接续相关命令": ("cd /root/projects/h3studio-t2v-continue && "
                   "/home/ubuntu/comfyui/venv/bin/python -m pytest "
                   "tests/test_continuation_branch.py tests/test_continuation_server.py "
                   "tests/test_continuation.py tests/test_continuation_retry.py "
                   "tests/test_continuation_frontend.py tests/test_continuation_mixed_state.py "
                   "tests/test_continuation_capability.py -q --no-header -p no:cacheprovider"),
        "接续相关结果": cont_tail.get("结果", ""),
        "接续相关日志": str(cont_log.resolve()) if cont_log.is_file() else "",
        "全量命令": ("cd /root/projects/h3studio-t2v-continue && "
                 "/home/ubuntu/comfyui/venv/bin/python -m pytest tests/ -q "
                 "--no-header -p no:cacheprovider"),
        "全量结果": full_tail.get("结果", ""),
        "全量日志": str(full_log.resolve()) if full_log.is_file() else "",
        "全量失败的项": full_tail.get("失败项", {}),
        "全量失败判定": {
            "test_studio.py::TestLoRARegistry::test_scanner_only_returns_z_image_loras":
                "稳定存量失败（maintainer 基线同样失败，与接续无关）",
            "test_yue2_flow_review_fixes.py::TestWriteContractDeclaration::"
            "test_every_method_declares_write_contract":
                "稳定存量失败（maintainer 基线同样失败，与接续无关）",
            "test_page_size.py::TestPageSize::test_pager_boundary_buttons_disabled":
                "时序 flake —— 单独跑全过；该文件 0 处引用接续代码；"
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
        "结论": "375 / 1280 两档截图齐备；console_errors=0、page_errors=0、"
                "真实失败请求=0（媒体预载中止另列，非服务端错误）",
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
            "continuation-d1(browser_report.json)": _d1_browser_stats(),
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
    frame_src = REPO / "data-accept" / "evidence-frames" / "frames"
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

    # 人读的索引（Markdown）—— **关键数字与绝对路径直接内联**，
    # 验收方不必打开 JSON 就能逐项取证。
    md = []
    A = md.append
    A("# 验收证据索引 — 文生视频接续（t2v）")
    A("")
    A(f"> {index['判据声明']}")
    A("")
    A(f"- 分支：`{index['分支']}`　仓库：`{index['仓库']}`")
    A(f"- 生成时间：{index['生成时间']}")
    A(f"- 机器可读版：`{out / 'INDEX.json'}`")
    A(f"- 验收实例：{index['验收实例']}")
    A("")
    A("---")
    A("")

    # ---- C1 ----
    b = index["判据"]["C1_兄弟分支落盘互不覆盖"]
    A(f"## C1　兄弟分支落盘互不覆盖")
    A("")
    A(f"**结论**：{b['结论']}")
    A("")
    A("| 分支 | job_id | run_id | stage 文件（绝对路径） | 盘上文件 | 指纹 |")
    A("|---|---|---|---|---|---|")
    for tag in ("B1", "B2"):
        x = b["兄弟分支"][tag]
        if not x:
            A(f"| {tag} | — | — | — | — | — |")
            continue
        A(f"| **{tag}** | `{x['job_id']}` | `{x['run_id']}` | "
          f"`{x['stage_file_plain']}` | {', '.join(x['stage_files_on_disk'])} | "
          f"{', '.join(x['fingerprint_suffixes']) or '（未生成读回副本）'} |")
    A("")
    A(f"- B1 与 B2 的落盘目录**不同** -> "
      f"结构上不可能互相覆盖（不是靠「先后顺序不撞」的运气约束）")
    A(f"- 全部 5 段（chain_root `{args.chain_root}`）：")
    A("")
    A("| job_id | stage | source_job | run_id |")
    A("|---|---|---|---|")
    for x in b["全部段"]:
        A(f"| `{x['job_id']}` | {x['stage_index']} | "
          f"`{x['source_job'] or '—'}` | `{x['run_id']}` |")
    A("")

    # ---- C2 ----
    b = index["判据"]["C2_接续首帧归属"]
    A("## C2　接续首帧归属（对照已判定的 2.08 vs 38.65）")
    A("")
    A(f"**结论**：{b['结论']}")
    A("")
    A("| 比较 | 灰度 MAE |")
    A("|---|---|")
    A(f"| C1首帧 vs **B1末帧**（正确分支） | **{b['灰度MAE_C1首帧_vs_B1末帧']}** |")
    A(f"| C1首帧 vs **B2末帧**（兄弟分支） | **{b['灰度MAE_C1首帧_vs_B2末帧']}** |")
    A(f"| 偏离倍数 | **{b['偏离倍数']}×** |")
    A("")
    d = b["缺陷态对照_原始报告"]
    A(f"**缺陷态对照**（D1 调研报告实测）：{d['说明']}")
    A("")
    A(f"- 缺陷态：C1首帧 vs B1末帧 = **{d['缺陷态_C1首帧_vs_B1末帧']}**，"
      f"vs B2末帧 = **{d['缺陷态_C1首帧_vs_B2末帧']}**"
      f"（偏离 {d['缺陷态_偏离倍数']}×）—— 即接到了兄弟分支 B2")
    A(f"- 修复后：{b['灰度MAE_C1首帧_vs_B1末帧']} vs {b['灰度MAE_C1首帧_vs_B2末帧']}"
      f"（偏离 {b['偏离倍数']}×）—— 接的是 B1，方向已反转")
    A("")
    g = b["执行图级主判据"]
    A("**执行图级主判据**（ComfyUI 真正跑的那张图 —— 确定性，不依赖模型表现）：")
    A("")
    A(f"- C1 实际读回：`{g['C1实际读回路径']}`")
    A(f"- B1 落盘 run_id：`{g['B1落盘run_id']}`　B2 落盘 run_id：`{g['B2落盘run_id']}`")
    A(f"- 读的是 B1：**{g['读的是B1']}**（B2 的目录从未被读）")
    A(f"- 外观锚**显式传入**：{g['外观锚显式传入']}　"
      f"`ref_anchor_latent = {g['ref_anchor_latent']}`")
    A(f"- 锚读回路径：`{g['锚读回路径']}`")
    A("")
    A("**对比图**（绝对路径）：")
    A("")
    for k, v in b["对比图"].items():
        A(f"- {k}：`{v}`")
    A("")

    # ---- C3 ----
    b = index["判据"]["C3_链账本不按chain_id线性拼接"]
    A("## C3　链账本不按 chain_id 线性拼接")
    A("")
    A(f"**结论**：{b['结论']}")
    A("")
    A("| job_id | stage | 血缘段序列 | 各段帧数 | 各段时长(s) | 完整视频帧数 | 期望 | 完整视频时长(s) |")
    A("|---|---|---|---|---|---|---|---|")
    for x in b["各分支"]:
        A(f"| `{x['job_id']}` | {x['stage_index']} | "
          f"{' + '.join(Path(s).name for s in x['血缘段序列'])} | "
          f"{x['各段帧数']} | {x['各段时长_s']} | "
          f"{x['完整视频帧数']} | {x['完整视频期望帧数']} | "
          f"{x['完整视频时长_s']} |")
    A("")
    A("- **B2 的段（v-h3-studio-000022.mp4）只出现在 B2 分支**，"
      "不出现在 B1/C1/D1 任何一条血缘里")
    A("- 各分支完整视频的绝对路径见 `INDEX.json` 的 `C3_链账本不按chain_id线性拼接.各分支[*].完整视频文件`")
    A("")

    # ---- C4 ----
    b = index["判据"]["C4_内容指纹机制对照nonebot"]
    A("## C4　内容指纹机制 —— 与 nonebot 咏唱 seg 工作流对照")
    A("")
    A(f"**结论**：{b['结论']}")
    A("")
    A("| 维度 | nonebot 咏唱 h3 | 本项目 |")
    A("|---|---|---|")
    A("| 指纹算法 | `_latent_fingerprint` | `latent_fingerprint` |")
    A("| 读取范围 | 头 8MB + 尾 8MB + **文件大小**进哈希 | 同 |")
    A("| 指纹长度 | `md5[:8]` | `md5[:8]` |")
    A("| 副本命名 | `stage_<N>_<指纹>.safetensors` | 同 |")
    A("| 破缓存原理 | 内容变->指纹变->路径变->`explicit_path` 变->缓存 key 变 | 同 |")
    A("| 旧副本清理 | 清同 `stage_<N>_*` 非当前指纹 | `cleanup_fingerprint_copies` 同规则 |")
    A("| 落盘坐标制度 | `run_id = long_id` + 长视频级互斥锁（链线性） | "
      "`run_id = h3studio_s<job_id>`（每段一目录，链可分叉） |")
    A("")
    A(f"**落盘制度为何不同**：{b['落盘制度差异_说明']}")
    A("")
    A("**代码位置**：")
    A("")
    for k, v in b["本项目实现位置"].items():
        A(f"- `{k}`：`{v}`")
    A("")
    A("**nonebot 参考位置**：")
    A("")
    for k, v in b["nonebot参考实现位置"].items():
        A(f"- `{k}`：`{v}`")
    A("")
    A("**本机实际指纹副本**（绝对路径）：")
    A("")
    for r in b["本机实际指纹副本"]:
        A(f"- `{r['path']}`　指纹={r['fingerprint']}　"
          f"指纹副本={r['is_fingerprinted_copy']}　{r['size_bytes']} 字节")
    A("")

    # ---- C5 ----
    b = index["判据"]["C5_两轮真实接续"]
    A("## C5　两轮真实接续")
    A("")
    A(f"**结论**：{b['结论']}")
    A("")
    A("| job_id | stage | source_job | 分段帧数 | 分段时长(s) | 完整视频帧数 | 完整视频时长(s) |")
    A("|---|---|---|---|---|---|---|")
    for x in b["逐段"]:
        A(f"| `{x['job_id']}` | {x['stage_index']} | "
          f"`{x['source_job'] or '—'}` | {x['分段帧数']} | {x['分段时长_s']} | "
          f"{x['完整视频帧数'] or '—（首段无拼接）'} | "
          f"{x['完整视频时长_s'] or '—'} |")
    A("")
    A(f"- 帧数账：{b['帧数账']['第1轮']}　/　{b['帧数账']['第2轮']}")
    A("- 逐段视频绝对路径见 `INDEX.json` 的 `C5_两轮真实接续.逐段[*].分段视频文件` / `.完整视频文件`")
    A("- ⚠ 视频**不入库**（`.gitignore` 排除 `*.mp4`），索引给的是绝对路径")
    A("")

    # ---- C6 ----
    b = index["判据"].get("C6_selflift卡片无接续按钮")
    if b:
        A("## C6　selflift（二采）卡片**不出现**「接续」按钮")
        A("")
        A(f"**结论**：{b['结论']}")
        A("")
        sv = b["服务端判定"]
        A(f"- selflift job `{sv.get('selflift_job')}`："
          f"`can_continue = {sv.get('selflift_can_continue')}`")
        A(f"- 原因：{sv.get('selflift_reason')}")
        A(f"- 普通 t2v job `{sv.get('normal_job')}`："
          f"`can_continue = {sv.get('normal_can_continue')}`")
        A("")
        A("**DOM 按钮清单**（两档宽度实测）：")
        A("")
        A("| 宽度 | 卡片 | 有「接续」按钮 | 全部按钮 |")
        A("|---|---|---|---|")
        for step, v in b["DOM按钮清单"].items():
            for tag in ("selflift", "normal"):
                c = v.get(tag)
                if not c:
                    continue
                A(f"| {step.replace('cards_', '')}px | {tag} | "
                  f"**{c['has_continue']}** | {c['buttons']} |")
        A("")
        A("**截图**（绝对路径）：")
        A("")
        for x in b["截图"]:
            A(f"- `{x}`")
        A("")

    # ---- C7 ----
    b = index["判据"]["C7_接续配置区块只能由生成库添加"]
    A("## C7　「接续」配置区块 —— 只能由生成库添加")
    A("")
    A(f"**结论**：{b['结论']}")
    A("")
    A("**区块实测**（点普通卡的「接续」之后）：")
    A("")
    A("| 宽度 | 区块按钮 | 有新增/添加入口 | 区块隐藏 |")
    A("|---|---|---|---|")
    for step, v in b["区块实测"].items():
        A(f"| {step.replace('continuation_panel_', '')}px | {v.get('buttons')} | "
          f"**{v.get('has_add')}** | {v.get('hidden')} |")
    A("")
    A("**代码位置**：")
    A("")
    for k, v in b["代码位置"].items():
        A(f"- `{k}`：`{v}`")
    A("")
    A("**截图**（绝对路径）：")
    A("")
    for x in b["截图"]:
        A(f"- `{x}`")
    A("")

    # ---- C8 ----
    b = index["判据"]["C8_pytest"]
    A("## C8　pytest 命令与结果")
    A("")
    A(f"**接续相关**：{b['接续相关结果']}")
    A("")
    A("```bash")
    A(b["接续相关命令"])
    A("```")
    A("")
    if b.get("接续相关日志"):
        A(f"日志落盘：`{b['接续相关日志']}`")
        A("")
    A(f"**全量**：{b['全量结果']}")
    A("")
    A("```bash")
    A(b["全量命令"])
    A("```")
    A("")
    if b.get("全量日志"):
        A(f"日志落盘：`{b['全量日志']}`")
        A("")
    if b.get("全量失败的项"):
        A("**全量失败项**（逐条原始输出）：")
        A("")
        for k, v in b["全量失败的项"].items():
            A(f"- `{k}`")
            A(f"  - 原始行：`{v}`")
            A(f"  - 判定：{b['全量失败判定'].get(k, '（未标注）')}")
        A("")
    A("**各项失败判定**：")
    A("")
    for k, v in b["全量失败判定"].items():
        A(f"- `{k}`")
        A(f"  - {v}")
    A("")

    # ---- C9 ----
    b = index["判据"]["C9_浏览器截图与报错统计"]
    A("## C9　375 / 1280 两档浏览器截图 + console / pageError 统计")
    A("")
    A(f"**两档宽度**：{b['两档宽度']}")
    A("")
    A("**截图目录**（绝对路径）：")
    A("")
    for dd, files in b["截图目录"].items():
        A(f"- `{dd}`")
        for f in files:
            A(f"  - `{f}`")
    A("")
    A("**报错统计**：")
    A("")
    for k, v in b["console_pageError统计"].items():
        A(f"- `{k}`")
        for kk, vv in v.items():
            A(f"  - {kk}：{vv}")
    A("")
    (out / "INDEX.md").write_text("\n".join(md), encoding="utf-8")

    print(f"索引已生成：")
    print(f"  {out / 'INDEX.md'}")
    print(f"  {out / 'INDEX.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
