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

import hashlib
import os
import shutil
import time
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
#: comfy_output_dir 的**结果缓存**。
#:
#: 【为什么必须缓存 —— 2026-11-14 性能缺陷实测】
#: comfy_output_dir() -> _comfy_candidates() -> _comfy_proc_cwds() ->
#: _comfy_pids()，而 _comfy_pids() **fork 一个 `ss -ltnp` 子进程**来解析
#: 端口 8188 的监听进程 —— 实测单次 **48ms**（fork+exec+等管道）。
#:
#: 调用方是 continuation_state()，而 continuation_state() 在
#: **每一条任务记录**里各调一次（`/api/jobs` 一次返回 184 条）
#: -> 184 × 48ms ≈ 9s，且发生在 **aiohttp 事件循环里**（同步阻塞）
#: -> 整个 8190 服务被卡住 -> 用户看到「改了效果 LoRA 强度，
#: 20 秒后才落盘」。
#:
#: 缓存策略：**TTL 60 秒**。ComfyUI 的安装目录 / 监听端口在运行期
#: 不会变（重启 ComfyUI 也不换目录），60 秒内的陈旧无实际影响。
#: 装 ComfyUI / 换端口后最坏 60 秒内解析不到 —— 那时兜底路径
#: （/home/ubuntu/comfyui/output）仍然可用，不会报错。
_COMFY_OUTPUT_CACHE: tuple[float, Path] | None = None

#: 缓存有效期（秒）。
_COMFY_OUTPUT_CACHE_TTL = 60.0


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

    【性能】解析要 fork `ss` 子进程（48ms），且被「每条任务记录一次」
    地调用 -> 结果按 TTL 缓存（见 _COMFY_OUTPUT_CACHE 的说明）。
    """
    override = os.environ.get("H3STUDIO_COMFY_OUTPUT")
    if override:
        return Path(override)

    # 命中缓存 -> 直接返回（省掉 fork `ss` 的 48ms）
    global _COMFY_OUTPUT_CACHE
    cached = _COMFY_OUTPUT_CACHE
    if cached is not None and (time.time() - cached[0]) < _COMFY_OUTPUT_CACHE_TTL:
        return cached[1]

    resolved = Path("/home/ubuntu/comfyui/output")
    for candidate in _comfy_candidates():
        out = candidate / "output"
        if out.is_dir():
            resolved = out
            break
    _COMFY_OUTPUT_CACHE = (time.time(), resolved)
    return resolved


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
# 内容指纹（破 ComfyUI 的节点缓存）
# ------------------------------------------------------------------
# 【为什么需要这一层 —— D1 调研报告第 4 项】
#
# ComfyUI 的节点缓存 key = 节点**输入**的哈希，**不看输入指向的文件
# 内容有没有变**（见 comfy_execution/caching.py 的
# `get_immediate_node_signature`：对每个 input 取「链路 or 字面值」，
# 文件路径只是一个普通字符串）。
#
# 后果（nonebot 咏唱 h3 踩过并记录在案）：
#   上一段潜空间被**重跑**替换后，`H3RelayLatentLoad` 的
#   run_id / stage_index / explicit_path 三项**一字未变**
#   -> 命中缓存 -> 节点**压根没被调用**（日志里连「已读 stage N」
#   那一行都没有）-> 返回**老 latent** -> 新一段实际接的是**上一版**的
#   画面。**不报错，界面全程显示成功。**
#
# 【解法 —— 借鉴 nonebot 的内容指纹机制】
# 把「内容的指纹」编进**读回用的文件路径**：
#   内容变 -> 指纹变 -> 路径变 -> `explicit_path` 这个输入必然变
#   -> 缓存 key 必然变 -> 节点必然重跑。
#
# 本项目与 nonebot 的差异：nonebot 是「把上一段 latent 拷成
# `stage_<N>_<指纹>.safetensors` 的副本」；本项目的落盘坐标是
# **每段一个专属目录**（`h3studio_s<job_id>`），同一坐标**天然不会**
# 被别的段覆盖 —— 所以只需给「读回路径」加指纹后缀：
#   `stage_00000_<指纹>.safetensors`
# 落盘仍写固定名 `stage_00000.safetensors`（Relay-Kit 节点自己算路径，
# 改不了），读回时另存一份带指纹的副本并把那个路径交给节点。
# 副本与原文件在同一目录、大小相同（硬链接优先，跨设备时退回复制）。
def latent_fingerprint(path: str | os.PathLike) -> str:
    """算潜空间文件的**内容指纹**（8 位 md5）。

    与 nonebot 咏唱 h3 的 `_latent_fingerprint` 同一口径：
      * 文件**大小**进指纹（追加 1 字节必变）；
      * 大文件只读头 8MB + 尾 8MB（不必整份读完 —— 一份约 36MB，
        每次执行都全读会明显拖慢）。

    ⚠️ 指纹**不是**安全哈希，只是「内容变了大概率变」的缓存键。
    碰撞概率对本用途（破节点缓存）完全够用。
    """
    p = Path(path)
    try:
        size = p.stat().st_size
    except OSError:
        return "missing"
    h = hashlib.md5()
    h.update(str(size).encode())
    try:
        with open(p, "rb") as f:
            h.update(f.read(8 * 1024 * 1024))
            if size > 16 * 1024 * 1024:
                f.seek(-8 * 1024 * 1024, os.SEEK_END)
                h.update(f.read(8 * 1024 * 1024))
            else:
                f.seek(size // 2)
                h.update(f.read(8 * 1024 * 1024))
    except OSError:
        return "unreadable"
    return h.hexdigest()[:8]


def fingerprinted_stage_path(run_id: str, stage_index: int) -> Path:
    """读回用的**带指纹副本**路径（不存在时返回原路径）。

    原路径： `relay_kit/<run_id>/stage_00000.safetensors`
    副本：   `relay_kit/<run_id>/stage_00000_<指纹>.safetensors`

    原文件不存在 -> 直接返回原路径（让调用方按老路径报错，
    错误信息里仍然是那个真实路径）。
    """
    src = stage_path(run_id, stage_index)
    if not src.is_file():
        return src
    fp = latent_fingerprint(src)
    if fp in ("missing", "unreadable"):
        return src
    return src.with_name(f"{src.stem}_{fp}{src.suffix}")


def ensure_fingerprinted_copy(run_id: str, stage_index: int) -> Path:
    """把段文件复制/硬链接成**带指纹的副本**，返回副本路径。

    副本已存在且指纹一致时直接复用（幂等）。
    写副本失败时返回**原路径**（不阻断接续 —— 退化成「可能命中缓存」
    的旧行为，好过直接失败）。
    """
    src = stage_path(run_id, stage_index)
    if not src.is_file():
        return src
    fp = latent_fingerprint(src)
    if fp in ("missing", "unreadable"):
        return src
    dst = src.with_name(f"{src.stem}_{fp}{src.suffix}")
    try:
        if dst.is_file() and dst.stat().st_size == src.stat().st_size:
            return dst
    except OSError:
        pass
    try:
        # 硬链接优先（同文件系统，零拷贝）；跨设备/失败则退回复制。
        try:
            if dst.exists():
                dst.unlink()
            os.link(src, dst)
        except OSError:
            shutil.copy2(src, dst)
    except OSError:
        return src
    return dst


def cleanup_fingerprint_copies(run_id: str, stage_index: int) -> int:
    """清掉同坐标下**非当前指纹**的旧副本（防止无限堆积）。

    返回清掉的个数。副本命名规则与 ensure_fingerprinted_copy 一致。
    """
    src = stage_path(run_id, stage_index)
    if not src.parent.is_dir():
        return 0
    keep = ""
    try:
        fp = latent_fingerprint(src)
        if fp not in ("missing", "unreadable"):
            keep = f"{src.stem}_{fp}{src.suffix}"
    except OSError:
        keep = ""
    removed = 0
    for candidate in src.parent.glob(f"{src.stem}_*.safetensors"):
        if candidate.name == keep:
            continue
        try:
            candidate.unlink()
            removed += 1
        except OSError:
            pass
    return removed


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
    #
    # 【带内容指纹】与 _prepare_continuation_params 同口径：内容变则
    # 路径变 -> ComfyUI 节点缓存必然失效（否则会拿回上一版的 latent）。
    source_path = ""
    if source_run_id:
        source_path = str(ensure_fingerprinted_copy(source_run_id,
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
