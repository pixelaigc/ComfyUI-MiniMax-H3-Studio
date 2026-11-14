# -*- coding: utf-8 -*-
"""
H3 Studio 资产素材库 —— 入库服务层。

职责：
  * 上传落盘（流式、原子、不整文件进内存）
  * 内容哈希去重
  * 元数据提取（只读探测；损坏才转码并标注原因）
  * 缩略图 / 派生文件生成
  * 从「媒体库」（生成结果）添加素材 —— 携带任务/批次引用与生成参数
  * 素材被任务引用时的引用登记

与其它模块的边界：
  * 本模块只管素材库自己的库（asset_library.db）与 /mnt/usb2t/asset_library 目录。
  * 任务表在 data/jobs.db，本模块**只读**任务信息（拿提示词/种子/模型等生成参数），
    不修改 jobs 表结构 —— 并发任务正在改这块。
"""
import hashlib
import shutil
import uuid
from pathlib import Path

from . import asset_repo
from . import assets as A
from . import job_meta
from . import media_probe
from .assets import LibraryError


# ------------------------------------------------------------------ 通用入库


def _write_stream_to_temp(source_path: Path, root: Path) -> tuple[Path, str, int]:
    """
    把 source_path 流式复制到临时区，同时算 sha256。

    - 不整文件读进内存（64 KB 一块）
    - 落在 tmp 区，后续用原子 rename 移到正式位置
    """
    tmp_dir = root / A.DIR_TMP
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = tmp_dir / f".upload-{uuid.uuid4().hex}.tmp"
    digest = hashlib.sha256()
    total = 0
    try:
        with source_path.open("rb") as src, tmp.open("wb") as dst:
            while True:
                chunk = src.read(256 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                dst.write(chunk)
                total += len(chunk)
                if total > A.MAX_UPLOAD_BYTES:
                    raise LibraryError(
                        f"文件超过上限 {A.MAX_UPLOAD_BYTES // (1024 ** 3)} GB", status=413)
        return tmp, digest.hexdigest(), total
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _build_preview(asset_id: str, version: dict, source: Path, root: Path,
                   category: str) -> str | None:
    """生成缩略图；返回相对路径（失败返回 None，不影响入库）。"""
    if category == "image":
        rel = A.preview_relpath(asset_id, ".jpg")
        dest = A.resolve_inside(root, rel)
        if dest and media_probe.make_image_preview(source, dest):
            return rel
        return None
    if category == "video":
        rel = A.preview_relpath(asset_id, ".jpg")
        dest = A.resolve_inside(root, rel)
        if dest is None:
            return None
        frames_dir = root / A.DIR_DERIVED / asset_id[:2] / f"{asset_id}_frames"
        frames = media_probe.extract_frames(source, frames_dir, prefix="thumb",
                                            interval=1.0, max_frames=1, width=640)
        if frames:
            # 目标目录可能还没建（预览区按 id 前两位分目录）
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(frames[0]["path"], dest)
            return rel
        return None
    if category == "audio":
        rel = A.preview_relpath(asset_id, ".png")
        dest = A.resolve_inside(root, rel)
        if dest and media_probe.make_waveform(source, dest):
            return rel
        return None
    return None


def _probe_and_repair(asset_id: str, version_row: dict, source: Path,
                      category: str, root: Path) -> dict:
    """
    探测元数据；**只有原格式严重损坏/无法解码时**才转码兜底。

    转码产物放派生区，原始文件保持原样；转码原因写进 transcode_reason。
    """
    ok, reason = media_probe.is_decodable(source, category)
    if ok:
        try:
            return media_probe.probe(source, category)
        except media_probe.ProbeError as exc:
            reason = str(exc)

    # 到这里 = 原文件无法解码
    suffix = {"image": ".png", "video": ".mp4", "audio": ".mp3"}.get(category, ".bin")
    repaired_rel = A.derived_relpath(asset_id, "repaired", suffix)
    repaired = A.resolve_inside(root, repaired_rel)
    transcode_reason = f"原文件无法解码（{reason or '未知原因'}），已转码为可读版本；原始文件仍原样保留"
    if repaired and media_probe.transcode_for_repair(source, repaired, category, reason):
        try:
            info = media_probe.probe(repaired, category)
            info["transcode_reason"] = transcode_reason
            info["repaired_path"] = repaired_rel
            return info
        except media_probe.ProbeError:
            pass
    info = media_probe.probe(source, category) if ok else {}
    info["transcode_reason"] = f"{transcode_reason}；转码也失败，媒体信息可能不完整"
    return info


def register_asset(conn, *, source_path: Path, filename: str, mime: str | None = None,
                   name: str | None = None, category: str | None = None,
                   subtype: str | None = None, tags=None, note: str | None = None,
                   source: str = "upload", source_ref: str | None = None,
                   source_meta: dict | None = None, creator: str | None = None,
                   origin_note: str | None = None) -> dict:
    """
    核心入库：落盘 + 去重 + 建素材记录 + 提元数据 + 出缩略图。

    去重规则：sha256 命中已有版本 -> 不新建文件，直接返回既有素材（标记 duplicated）。
    """
    root = A.library_root()
    if not (root / A.DIR_ORIGINALS).is_dir():
        raise LibraryError("素材库未初始化或磁盘不可用", status=503, code="library_unavailable")

    source_path = Path(source_path)
    if not source_path.is_file():
        raise LibraryError("待入库文件不存在", status=400)

    size = source_path.stat().st_size
    if size <= 0:
        raise LibraryError("文件为空，无法入库", status=400)
    if size > A.MAX_UPLOAD_BYTES:
        raise LibraryError(f"文件超过上限 {A.MAX_UPLOAD_BYTES // (1024 ** 3)} GB", status=413)

    mime = mime or A.guess_mime(filename)
    if not category:
        category, guessed_subtype = A.classify(mime, filename)
    else:
        guessed_subtype = subtype or A.classify(mime, filename)[1]
    subtype = subtype or guessed_subtype

    # --- 流式落临时区 + 算哈希 -----------------------------------------
    tmp, fingerprint, total = _write_stream_to_temp(source_path, root)

    # --- 去重 ----------------------------------------------------------
    dup = conn.execute(
        "SELECT v.asset_id, v.version FROM asset_versions v WHERE v.sha256=? "
        "ORDER BY v.version DESC LIMIT 1", (fingerprint,)).fetchone()
    if dup:
        existing = asset_repo.get_asset(conn, dup["asset_id"], include_deleted=True)
        if existing and not existing["deleted_at"]:
            # 已有**活着的**同内容素材 -> 直接复用，临时文件可以删了
            tmp.unlink(missing_ok=True)
            return {"asset": existing, "duplicated": True,
                    "sha256": fingerprint, "message": "素材已存在，已复用原记录"}
        # 【修复 —— 2026-11-11 实测踩到的 bug】
        #
        # 命中的这条 asset_versions 属于一个**已软删除**（进回收站）的素材：
        # soft_delete 把物理文件移到了 trash/，但**版本行还在**（可恢复）。
        #
        # 原实现在这里**无条件** tmp.unlink()，然后往下走到
        # atomic_move(tmp, target) —— 而 tmp 已经被自己删了 ->
        #   FileNotFoundError: tmp -> target
        #
        # 实测触发路径：上传同一张图 -> 测试清理把它删了（进回收站）
        # -> 再上传同一张图 -> 500。
        #
        # 修法：只有**确实要复用**时才删临时文件；
        # 否则保留它，继续走下面的正常入库流程（会建一条新的素材记录）。
        # （物理文件已经不在 originals/ 里了，所以 target 不存在，
        #   atomic_move 会把临时文件搬过去 —— 这正是我们要的。）

    # --- 原子移动到正式位置 --------------------------------------------
    rel = A.original_relpath(fingerprint, filename)
    target = A.resolve_inside(root, rel)
    if target is None:
        tmp.unlink(missing_ok=True)
        raise LibraryError("素材库路径校验失败（越界）", status=500)
    if target.exists():
        tmp.unlink(missing_ok=True)
    else:
        A.atomic_move(tmp, target)

    # --- 元数据 --------------------------------------------------------
    asset_id = A.new_asset_id()
    info = _probe_and_repair(asset_id, {}, target, category, root)

    preview_rel = _build_preview(asset_id, info, target, root, category)

    # --- 写库 ----------------------------------------------------------
    stamp = A.now()
    display_name = (name or Path(filename or "未命名").name).strip()[:200] or "未命名"
    conn.execute(
        "INSERT INTO assets (id, name, category, subtype, mime, tags, note, source, "
        "source_ref, source_meta, creator, latest_version, version_count, total_bytes, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,1,1,?,?,?)",
        (asset_id, display_name, category, subtype, mime, "[]", note, source,
         source_ref, A.jdump(source_meta or {}), creator, total, stamp, stamp))

    conn.execute(
        "INSERT INTO asset_versions (asset_id, version, storage_path, sha256, bytes, mime, "
        "width, height, duration_sec, fps, codec, audio_codec, sample_rate, channels, "
        "container, transcode_reason, extracted_json, preview_path, origin, origin_note, "
        "created_at, created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (asset_id, 1, rel, fingerprint, total, mime,
         info.get("width"), info.get("height"), info.get("duration_sec"), info.get("fps"),
         info.get("codec"), info.get("audio_codec"), info.get("sample_rate"),
         info.get("channels"), info.get("container"), info.get("transcode_reason"),
         A.jdump(info.get("extracted_json") or {}), preview_rel, source, origin_note,
         stamp, creator))

    if tags:
        asset_repo.add_tags(conn, asset_id, tags)

    asset = asset_repo.get_asset(conn, asset_id)
    version = asset_repo.current_version(conn, asset_id)
    return {"asset": asset, "version": version, "duplicated": False,
            "sha256": fingerprint, "info": info}


# ------------------------------------------------------------------ 从媒体库添加


def add_from_media_output(conn, *, job: dict, output: dict, batch: dict | None = None,
                          creator: str | None = None, tags=None,
                          outputs_dir: Path | None = None) -> dict:
    """
    从「媒体库」（生成结果）右键添加到素材库。

    覆盖两种来源：
      * **有批次**：output 属于某个 job_batch（batch 不为 None）
      * **无批次**：output 直接挂在 job 上（batch 为 None）

    携带来源引用与生成参数：
      * source_ref  = job_id（批次来源为 job_id/batch_id）
      * source_meta = {prompt, seed, model, workflow, mode, batch_id, batch_index, ...}

    outputs_dir 由调用方（server.py）传入，避免本模块反向 import server 造成循环依赖。
    """
    raw = output.get("file") or output.get("name")
    if not raw:
        raise LibraryError("生成结果缺少文件路径", status=400)

    params = job.get("params") or {}
    kind = output.get("kind") or (
        "image" if str(raw).lower().endswith((".png", ".jpg", ".jpeg", ".webp"))
        else "audio" if str(raw).lower().endswith((".flac", ".mp3", ".opus", ".wav",
                                                     ".m4a", ".ogg", ".aac"))
        else "video")

    if outputs_dir is None:
        raise LibraryError("缺少生成结果目录", status=500)
    outputs_dir = Path(outputs_dir)

    candidates = []
    relative = str(raw).replace("\\", "/")
    if "/" in relative:
        candidates.append(outputs_dir / relative)
    candidates.append(outputs_dir / job["id"] / relative)
    candidates.append(outputs_dir / relative.split("/")[-1])
    source_path = next((p for p in candidates if p.is_file()), None)
    if source_path is None:
        raise LibraryError(f"找不到生成结果文件：{relative}", status=404)

    # ------------------------------------------------------------------
    # LoRA 元数据（2026-11-13 修 —— 白名单语义）
    # ------------------------------------------------------------------
    #
    # 【为什么不能直接抄 params】
    # 用户 2026-11-13 实测报出：素材库条目的「LoRA」显示的根本不是该条目
    # 用过的 LoRA —— 甚至是一段提示词
    # （"integrated_multimodal_description: [Shot 1] ..."）。
    #
    # 根因有两处：
    #   1. 落库端：agent_schema 的 LoRA 校验对图像/音乐模式是**空转**的
    #      （mode_loras() 返回空列表 -> `if allowed and ...` 整个分支跳过），
    #      所以 Agent 误填的提示词能进 params["lora"]；
    #   2. 入库端：这里原本 `params.get("lora")` **原文**照抄进 source_meta，
    #      前端素材抽屉把 source_meta 的每个键都原样印出来 ——
    #      于是提示词被印在「lora」那一行。
    #
    # 修法：**按字段语义白名单取值**。
    #   * 「这个模式的工作流真的会读 params.lora」吗？
    #     - 视频（t2v / flf / r2v / longfilm）：读，且值必须是真实 LoRA key；
    #     - 图像 / 音乐：工作流根本不读这个字段（图像读 params["loras"]，
    #       音乐没有 LoRA 概念）-> 一律视为「没有 LoRA」。
    #   * 值本身还要过 job_meta.is_real_lora_key() 白名单，
    #     不在真实清单里的（提示词、乱填）一律当「没有」。
    #
    # 【与落库端共用同一份判定】
    # 落库端（server.prepare_job_params）与显示端（这里）必须用**同一个**
    # job_meta 白名单函数 —— 各写一套必然漂移，这正是本缺陷的成因。
    lora_value = job_meta.resolve_job_lora(job.get("mode") or "", params)
    meta = {
        "job_id": job.get("id"),
        "job_mode": job.get("mode"),
        "prompt": params.get("prompt", ""),
        "seed": params.get("seed"),
        "model": params.get("model"),
        "resolution": params.get("resolution"),
        "aspect": params.get("aspect"),
        "duration": params.get("duration"),
        "steps": params.get("steps"),
        # 【2026-11-13】LoRA 三个字段统一由 job_meta.resolve_job_lora 判定，
        # 不再原文照抄 params —— 否则提示词会被当成 LoRA 印出来。
        # （以 media-card-detail 为准：它比 h3-model-meta 多带了 lora_strength；
        #   两个旧字段 lora / loras 与 h3-model-meta 完全一致，保留即可。）
        "lora": lora_value["lora"],
        "loras": lora_value["loras"],
        "lora_strength": lora_value["strength"],
        "negative_prompt": params.get("negative_prompt"),
        "sampler": params.get("sampler"),
        "scheduler": params.get("scheduler"),
        "cfg": params.get("cfg"),
        "shift": params.get("shift"),
        # 【2026-11-12】注意力机制 —— 素材库条目要能显示「这条视频是
        # 用哪种注意力机制生成的」（用户要求：媒体库/生成库标注注意力机制）。
        # 存 **key**（不是显示名）—— 显示名由 job_meta 从清单里现算，
        # 这样清单改了显示名，历史素材的标注也会跟着更新。
        "attention": params.get("attention"),
        "attention_label": job_meta.attention_label(params.get("attention") or ""),
        "batch_id": (batch or {}).get("id"),
        "batch_index": (batch or {}).get("idx"),
        "batch_seed": (batch or {}).get("seed"),
        "original_file": relative,
        "job_created_at": job.get("created_at"),
    }

    result = register_asset(
        conn,
        source_path=source_path,
        filename=Path(source_path).name,
        mime=A.guess_mime(source_path.name),
        name=Path(source_path).name,
        # 【2026-11-09 修】音乐产物 kind="audio" 之前被归成 "video" ——
        # 素材库的音频筛选（category=audio）查不到，右键入库看着像没生效。
        category=("image" if kind == "image"
                  else "audio" if kind == "audio"
                  else "video"),
        tags=tags,
        source="job_output",
        source_ref=job.get("id"),
        source_meta=meta,
        creator=creator,
    )

    # 引用登记：这条素材来自哪个任务/批次
    asset_id = result["asset"]["id"]
    if batch:
        asset_repo.add_ref(conn, asset_id, "job_batch", batch["id"], slot="output",
                           label=f"任务 {job.get('id')} 批次 #{batch.get('idx', 0) + 1}")
        asset_repo.add_ref(conn, asset_id, "job", job["id"], slot="output",
                           label=f"任务 {job.get('id')} 的产物")
    else:
        asset_repo.add_ref(conn, asset_id, "job", job["id"], slot="output",
                           label=f"任务 {job.get('id')} 的产物")
    return result


# ------------------------------------------------------------------ 引用登记


def asset_reference_summary(conn, asset_id: str) -> dict:
    """引用统计 + 明细（详情抽屉用）。"""
    refs = asset_repo.refs_of(conn, asset_id)
    by_kind: dict[str, int] = {}
    for ref in refs:
        by_kind[ref["kind"]] = by_kind.get(ref["kind"], 0) + 1
    return {"count": len(refs), "by_kind": by_kind, "refs": refs}
