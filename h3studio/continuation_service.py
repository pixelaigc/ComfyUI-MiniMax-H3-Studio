# -*- coding: utf-8 -*-
"""
接续生成的**服务层**：链的记账、潜空间落盘检查、接续上下文组装。

【为什么单独一层】
「能不能接续」这件事要同时看四样东西，散在各处就会自相矛盾：
  1. 任务模式（t2v / flf / r2v）；
  2. 任务参数（是不是二采）；
  3. 潜空间文件**在不在**（落盘可能被清理）；
  4. 链的段号关系（上一段是第几段）。
本模块把这四样收成**一个**判定 + **一个**上下文，server.py 只调它。

【链（chain）的概念】
一条链 = 一串连续的段。
  * chain_id   —— 链 id，同一条链的所有段共用（= Relay-Kit 的 run_id）
  * stage_index—— 段号，第 1 段 = 0
  * source_job —— 上一段的**任务 id**（拿它的潜空间当接力棒）

链 id 用「链首段的任务 id」派生，所以：
  * 同一条链的所有段都有同一个 chain_id（段号不同）；
  * 不同的链永远不会撞（不同任务 id）。
  * 用户对**完整视频**再点接续时，chain_id 不变、stage_index +1 ——
    这就是「无限接续」的实现方式。

【潜空间文件在哪】
Relay-Kit 的节点自己管落盘位置：
    ComfyUI output/relay_kit/<run_id>/stage_<N>.safetensors
本项目把 run_id 定成 `h3studio_<chain_id>`（加前缀避免和用户手工跑
Relay-Kit 建的目录撞车），段号就是 stage_index。

服务端要能反推这个路径 —— 因为「文件还在吗」必须由服务端回答
（前端看不到 ComfyUI 的磁盘）。
"""

from __future__ import annotations

import os
from pathlib import Path

from . import continuation as cont


# ------------------------------------------------------------------
# 落盘坐标（服务层的公开入口）
# ------------------------------------------------------------------
# 【为什么这里有一层薄包装】协议层（continuation.py）是**规则**的单一真源，
# 但调用方（server / 工作流拼装 / 测试）大多拿的是「任务参数」而不是
# 裸的 info 字典。这层包装把「怎么从参数拿到坐标」收在一处，
# 免得每个调用方各写一遍取字段的代码（取错字段 = 写到 A 目录读 B 目录）。
def chain_run_id(chain_id: str) -> str:
    """链 id -> Relay-Kit 的 run_id（**链首段**的落盘目录名）。

    ⚠️ 只用于链首段（第 0 段，外观锚）。第 1 段起用 segment_run_id。
    """
    return cont.chain_run_id(chain_id)


def segment_run_id(job_id: str) -> str:
    """一个段的 Relay-Kit run_id（落盘目录名）。

    每段一个目录 —— 目录名由**该段自己的任务 id** 派生，
    兄弟分支各写各的目录，结构上不可能互相覆盖。
    """
    return cont.segment_run_id(job_id)


# ------------------------------------------------------------------
# 落盘路径（ComfyUI output 目录）
# ------------------------------------------------------------------
def comfy_output_dir() -> Path:
    """ComfyUI 的 output 目录。

    【怎么找到它】ComfyUI 没有把 output 目录暴露成 API，但**工作目录**
    就是它的 base_path（`folder_paths.output_directory = base_path/output`）。
    进程 cwd 由 systemd 的 WorkingDirectory 决定。

    解析顺序：
      1. 环境变量 H3STUDIO_COMFY_OUTPUT（显式覆盖，测试用）；
      2. ComfyUI 进程的 cwd（从 /proc 找监听 8188 的进程）；
      3. 常见安装位置 /home/ubuntu/comfyui。
    都找不到就返回空路径 —— 调用方据此**明确报错**，不静默。
    """
    override = os.environ.get("H3STUDIO_COMFY_OUTPUT")
    if override:
        return Path(override)
    for candidate in _comfy_candidates():
        out = candidate / "output"
        if out.is_dir():
            return out
    return Path("/home/ubuntu/comfyui/output")


def _comfy_candidates() -> list[Path]:
    """可能的 ComfyUI 安装目录（按可信度排序）。"""
    found: list[Path] = []
    # 1. 从监听 ComfyUI 端口的进程 cwd 反推
    for proc_dir in _comfy_proc_cwds():
        found.append(proc_dir)
    # 2. 常见安装位置
    found.append(Path("/home/ubuntu/comfyui"))
    # 去重（保序）
    seen: set[str] = set()
    out: list[Path] = []
    for item in found:
        key = str(item)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _comfy_proc_cwds() -> list[Path]:
    """找监听 ComfyUI 端口的进程的 cwd。

    读 /proc/<pid>/cwd —— Linux 上不需要额外权限（root）。
    找不到就返回空列表（不是错误：可能是容器里没有 /proc）。
    """
    out: list[Path] = []
    pids = _comfy_pids()
    for pid in pids:
        try:
            cwd = Path(os.readlink(f"/proc/{pid}/cwd"))
        except (OSError, ValueError):
            continue
        if cwd.is_dir():
            out.append(cwd)
    return out


def _comfy_pids() -> list[int]:
    """监听 ComfyUI 端口的进程 id。

    用 `ss -ltnp` 解析 —— 不依赖 psutil 的 net_connections（那需要 root
    之外的额外能力，且在容器里常常拿不到 inode -> pid 的映射）。
    解析失败就退回「扫 /proc 找 cmdline 含 comfy 的进程」。
    """
    import subprocess

    port = str(_comfy_port())
    try:
        raw = subprocess.check_output(["ss", "-ltnp"], encoding="utf-8",
                                      timeout=10, stderr=subprocess.DEVNULL)
    except Exception:
        return _comfy_pids_by_proc()
    pids: list[int] = []
    for line in raw.splitlines():
        if f":{port}" not in line:
            continue
        # users:(("python",pid=123,fd=84))
        marker = "pid="
        idx = line.find(marker)
        if idx < 0:
            continue
        tail = line[idx + len(marker):]
        digits = ""
        for ch in tail:
            if ch.isdigit():
                digits += ch
            else:
                break
        if digits:
            pids.append(int(digits))
    return pids or _comfy_pids_by_proc()


def _comfy_pids_by_proc() -> list[int]:
    """兜底：扫 /proc 找 cmdline 含 comfyui 的 python 进程。"""
    out: list[int] = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return out
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as handle:
                cmd = handle.read().decode("utf-8", errors="replace")
        except OSError:
            continue
        if "comfy" in cmd.lower() and "python" in cmd.lower():
            out.append(int(entry))
    return out


def _comfy_port() -> int:
    """ComfyUI 的端口（默认 8188，可被环境变量覆盖）。"""
    raw = os.environ.get("H3STUDIO_COMFY_PORT", "8188")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 8188


# ------------------------------------------------------------------
# 潜空间定位
# ------------------------------------------------------------------
# 【单一真源】路径规则在 continuation.py（协议层）——
# 工作流拼装、落盘检查、错误提示都必须用同一套规则，
# 否则会出现「提示里说的路径」和「实际检查的路径」不一致。
def stage_relpath(run_id: str, stage_index: int) -> str:
    """Relay-Kit 段文件相对 ComfyUI output 的路径。

    形如 `relay_kit/<run_id>/stage_00000.safetensors`。
    与 Relay-Kit `_stage_path` 完全一致（它就是
    `os.path.join(output, "relay_kit", rid, "stage_%05d.safetensors")`）。
    """
    return cont.stage_relpath(run_id, stage_index)


def stage_path(run_id: str, stage_index: int) -> Path:
    """Relay-Kit 段文件的绝对路径。"""
    return cont.stage_file_path(comfy_output_dir(), run_id, stage_index)


def stage_exists(run_id: str, stage_index: int) -> bool:
    """段文件在不在。"""
    return cont.stage_file_exists(comfy_output_dir(), run_id, stage_index)


# ------------------------------------------------------------------
# 链状态（从任务记录反推）
# ------------------------------------------------------------------
def chain_info(params: dict) -> dict:
    """从任务参数里读出接续信息（没有就是空字典）。

    params["continuation"] 的形状（由服务端在提交时写定）：
      {
        "chain_id":      链 id,
        "stage_index":   本段段号,
        "source_job":    上一段任务 id,
        "source_segment": 上一段段号,
        "context_frames":钉住窗口,
        "anchor_frames": 外观锚帧数,
      }
    """
    info = (params or {}).get("continuation")
    if not isinstance(info, dict) or not info:
        return {}
    return info


def next_stage_index(params: dict) -> int:
    """本段（作为链的一环）的段号。

    没有接续信息 = 链的第 1 段（段号 0）。
    """
    info = chain_info(params)
    try:
        return max(0, int(info.get("stage_index", 0)))
    except (TypeError, ValueError):
        return 0


def latent_coords_of(params: dict) -> tuple[str, int]:
    """本段的潜空间坐标 `(run_id, 文件里的段号)`。

    唯一真源在 continuation.latent_coords —— 工作流拼装与服务层
    必须用同一个，否则会「写到 A 目录、从 B 目录读」。
    """
    return cont.latent_coords(chain_info(params))


# ------------------------------------------------------------------
# 可接续性（唯一判定入口）
# ------------------------------------------------------------------
def continuation_state(mode: str, params: dict, job_id: str = "") -> dict:
    """这个产物能不能被接续？以及接续时要用什么。

    返回：
      {
        "can_continue":  bool,
        "reason":        不能时的中文原因（给用户看）,
        "run_id":        落盘目录名,
        "stage_index":   本段段号,
        "chain_id":      链 id,
        "has_latent":    本段的潜空间文件在不在,
        "latent_exists": 同上（别名，给前端用）,
        "context_frames":建议的钉住窗口,
        "total_frames":  本段交付的总帧数（接续后累计）,
        "total_seconds": 本段交付的总时长,
      }

    【为什么 has_latent 要在这里判】潜空间文件可能被清理、被手工删、
    或者任务是在**本功能上线前**生成的（压根没落盘）——
    这三种都要**明确告诉用户**，不能等点下去才炸。
    """
    allowed, reason = cont.continuation_allowed(mode, params)
    info = chain_info(params)
    stage = next_stage_index(params)
    # 潜空间坐标（run_id + 文件里的段号）—— 唯一真源在 continuation.latent_coords。
    # 新任务：每段一个目录，段号恒 0；旧任务：一条链一个目录 + 链段号。
    run_id, latent_stage = cont.latent_coords(info)

    # 上传前的任务还没有 job_id -> 还没有落盘目录 -> 不能接续。
    # 上线前的历史任务同样没有 chain_id -> 也不可接续。
    has_chain = bool(run_id)
    latent_ok = bool(run_id) and stage_exists(run_id, latent_stage)

    state = {
        "can_continue": bool(allowed and has_chain and latent_ok),
        "reason": "",
        "run_id": run_id,
        "latent_stage": latent_stage,
        "stage_index": stage,
        "chain_id": str(info.get("chain_id") or ""),
        "has_latent": latent_ok,
        "latent_exists": latent_ok,
        "context_frames": cont.DEFAULT_CONTEXT_FRAMES,
        "total_frames": 0,
        "total_seconds": 0,
    }

    if not allowed:
        state["reason"] = reason
        return state
    if not has_chain:
        state["reason"] = ("这段视频没有可接续的潜空间（不是用「接续」生成的，"
                           "或潜空间已被清理）—— 请改用「复用参数」重新生成一段。")
        return state
    if not latent_ok:
        state["reason"] = cont.missing_reason(comfy_output_dir(), run_id,
                                               latent_stage)
        return state

    # 累计帧数：本段交付的帧数 + 之前各段交付的帧数。
    #
    # 【钉住区只对「接续段」成立】接续段会把钉住区裁掉，所以它的交付
    # 帧数 = 本段总帧数 - 钉住帧数；而**链首段**没有上游、根本没裁，
    # 交付的就是整段。
    #
    # ⚠ 旧实现对链首段也减了钉住窗口 -> 显示的累计长度每段少 22 帧。
    # 实测对照（真实产物）：2 段完整视频 ffprobe = 226 帧，
    # 而旧算法报 204（= 102 + 102）；3 段 ffprobe = 328，旧算法报 306。
    # 视频文件本身是对的（拼接用的是真实分段文件），错的只是这个**显示值**
    # —— 它同时是 chain_total_frames 的来源，所以会一路带偏后面的每一段。
    chain_frames = info.get("chain_total_frames")
    try:
        chain_frames = int(chain_frames)
    except (TypeError, ValueError):
        chain_frames = 0
    seg_frames = int(info.get("segment_frames") or 0)
    if not seg_frames:
        duration = float((params or {}).get("duration") or 0)
        seg_frames = cont.align_frame_count(int(round(duration * cont.FPS)))
    window = cont.snap_context_frames(int(info.get("context_frames")
                                          or cont.DEFAULT_CONTEXT_FRAMES))
    # 只有接续段才裁了钉住区。
    has_source = bool(str(info.get("source_job") or "").strip())
    if has_source:
        seg_frames = max(0, seg_frames - window)
    state["total_frames"] = chain_frames + seg_frames
    state["total_seconds"] = round(state["total_frames"] / cont.FPS, 3)
    return state


# ------------------------------------------------------------------
# 接续上下文（回填给前端的那一份）
# ------------------------------------------------------------------
def build_continuation_context(mode: str, params: dict, job_id: str) -> dict:
    """组装「点接续之后要回填到左侧配置区」的内容。

    返回：
      {
        "ok":          能不能接续,
        "error":       不能时的中文原因,
        "job_id":      来源任务 id,
        "mode":        来源模式,
        "params":      {要回填的字段},
        "summary":     [{label, value}]  只读展示,
        "continuation": {链信息} 提交时原样带回,
      }

    【提示词不在这份里】用户需求：提示词由用户自己写下一段。
    上下文里带一个空的 prompt 字段，前端据此知道「提示词要自己填」。
    """
    state = continuation_state(mode, params, job_id)
    if not state["can_continue"]:
        return {"ok": False, "error": state["reason"], "job_id": job_id,
                "mode": mode, "params": {}, "summary": [],
                "continuation": {}}

    info = chain_info(params)
    context = cont.build_context(params, mode)

    # 链信息：下一段要接的是**本段**。
    try:
        stage = int(state["stage_index"])
    except (TypeError, ValueError):
        stage = 0
    window = cont.snap_context_frames(int(info.get("context_frames")
                                          or cont.DEFAULT_CONTEXT_FRAMES))
    anchor = cont.snap_context_frames(int(info.get("anchor_frames")
                                          or cont.DEFAULT_CONTEXT_FRAMES))

    # 【关键】接力棒的**精确坐标** = 上游自己那一份潜空间的位置。
    #
    # 新任务：上游有自己的专属目录（latent_run_id）+ 段号 0。
    # 下一段必须去**那个目录**读 —— 不能自己按链段号推一个坐标，
    # 否则兄弟分支会读到彼此的文件（分叉覆盖的根因）。
    #
    # 旧任务（上线前）：没有 latent_run_id -> 沿用「链目录 + 链段号」。
    source_run_id, source_latent_stage = cont.latent_coords(info)

    # 接力棒文件的**绝对路径** —— 读回必须精确到这一个文件。
    # 服务端提交时会用同样的规则重算（不信任前端），但这里也带上，
    # 便于排查「到底读的是哪一份」。
    source_path = ""
    if source_run_id:
        source_path = str(cont.stage_file_path(comfy_output_dir(), source_run_id,
                                               source_latent_stage))

    continuation_info = {
        "chain_id": state["chain_id"],
        # 下一段 = 本段 + 1（链上的位置，用于拼完整视频）
        "stage_index": stage + 1,
        # 接力棒 = 本段
        "source_job": job_id,
        "source_segment": stage,
        # 接力棒的**落盘坐标**（权威：服务端从上游任务记录算出来的）
        "source_latent_run_id": source_run_id,
        "source_latent_stage": source_latent_stage,
        "source_latent_path": source_path,
        "context_frames": window,
        "anchor_frames": anchor,
        # 累计帧数（用于算拼接后的总长）
        "chain_total_frames": state["total_frames"],
        "segment_frames": int(info.get("segment_frames") or 0),
    }

    return {
        "ok": True,
        "error": "",
        "job_id": job_id,
        "mode": mode,
        "params": context["params"],
        "summary": context["summary"],
        "continuation": continuation_info,
        "context_frames": window,
        "total_frames": state["total_frames"],
        "total_seconds": state["total_seconds"],
    }
