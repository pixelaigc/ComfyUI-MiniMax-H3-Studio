# -*- coding: utf-8 -*-
"""
H3 Studio 资产素材库 —— 存储层 + 数据模型。

与 jobs.db 分库存放：
  * 素材库自己的库：/mnt/usb2t/asset_library/asset_library.db
  * 任务表仍在 data/jobs.db，素材库**不修改** jobs 表结构，
    只用 asset_job_refs 表记录「哪个任务引用了哪个素材」。

为什么分库：
  1. 并发任务正在同一仓库改 jobs 相关逻辑 —— 素材库不碰 jobs.db 的表结构，
     避免 PRAGMA foreign_keys 打开后旧数据里悬空引用导致建表失败。
  2. 素材库主目录在 /mnt/usb2t（外接盘），库跟着数据走，
     搬盘/换机不需要迁库。

引用关系用「双写 + 引用计数」实现（见 refs.py 的说明）：
  - 素材被任务引用 -> asset_refs 记一行
  - 删除素材前服务端强制检查 asset_refs + asset_job_refs，有引用 -> 409 + 引用清单
  - 删任务 -> 素材不受影响（引用关系随任务删除而清理，素材本体保留）
"""
import hashlib
import json
import os
import shutil
import sqlite3
import time
import uuid
from pathlib import Path

# ------------------------------------------------------------------ 常量

#: 素材库主目录（外接盘）
DEFAULT_LIBRARY_ROOT = "/mnt/usb2t/asset_library"

#: 物理分区
DIR_ORIGINALS = "originals"    # 原始文件区（按 sha256 分层）
DIR_PREVIEWS = "previews"      # 缩略图 / 预览图区
DIR_DERIVED = "derived"        # 导出或派生文件区（分析用抽帧、波形图、转码件）
DIR_TMP = "tmp"                # 临时区（上传中转、原子重命名的 staging）
DIR_TRASH = "trash"            # 回收站

SUBDIRS = (DIR_ORIGINALS, DIR_PREVIEWS, DIR_DERIVED, DIR_TMP, DIR_TRASH)

#: 素材大类
CATEGORIES = ("image", "video", "audio", "doc", "workflow", "other")

#: 细分类（每个大类下的 subtype）
SUBTYPES = {
    "image": ("photo", "character", "storyboard"),
    "video": ("clip", "reference_motion"),
    "audio": ("music", "voice", "sfx"),
    "doc": ("prompt", "storyboard_script"),
    "workflow": ("comfy_json",),
    "other": ("subtitle", "binary"),
}

#: 来源
SOURCES = ("upload", "job_output", "import", "comfy_workflow", "plan_shot")

#: 单文件大小上限（默认 8 GiB）
MAX_UPLOAD_BYTES = 8 * 1024 * 1024 * 1024

#: 回收站保留期（秒）
TRASH_RETENTION_SEC = 30 * 24 * 3600


class LibraryError(RuntimeError):
    """素材库错误。message 面向用户，直接可读。"""

    def __init__(self, message, status=400, code="asset_error", extra=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.extra = extra or {}


# ------------------------------------------------------------------ 存储布局


def library_root() -> Path:
    """素材库主目录。可用 ASSET_LIBRARY_ROOT 环境变量覆盖（测试用）。"""
    return Path(os.environ.get("ASSET_LIBRARY_ROOT") or DEFAULT_LIBRARY_ROOT)


def db_path() -> Path:
    return library_root() / "asset_library.db"


def ensure_layout(root: Path | None = None) -> dict:
    """
    建立（或校验）素材库目录结构，并做磁盘可用性自检。

    自检内容（启动时调用）：
      1. 根目录所在磁盘是否挂载 —— 用 os.statvfs 判断可用空间，
         并与 /proc/mounts 里该挂载点的实际设备对比，
         防止「目录还在但盘没挂」时把数据写到系统盘根上（数据会被系统盘写满）。
      2. 各分区目录是否可写（写临时文件 -> 删掉）。
      3. 素材库数据库是否可打开。

    返回自检报告 dict。任何一项不通过都**不破坏既有数据**，
    只返回 ok=False + 明确原因，由上层决定拒绝写入。
    """
    root = root or library_root()
    report = {"root": str(root), "ok": True, "errors": [], "warnings": [],
              "free_bytes": 0, "total_bytes": 0, "mounted": False}

    # --- 1. 挂载点检查 -------------------------------------------------
    # 目录存在 != 盘还挂着。挂载点被卸载后，目录会退化成根盘上的普通空目录，
    # 这时继续写入 = 把用户的素材库塞进系统盘（已用 88%）。
    mounted, mount_detail = check_disk_mounted(root)
    report["mount_detail"] = mount_detail
    report["mounted"] = mounted
    if not mounted:
        report["errors"].append(
            f"素材库主目录 {root} 所在磁盘未挂载（{mount_detail}），"
            "拒绝写入以免占满系统盘。请确认外接盘已挂载后重启 Studio。")
        report["ok"] = False
        return report

    # --- 2. 目录结构 ----------------------------------------------------
    try:
        root.mkdir(parents=True, exist_ok=True)
        for name in SUBDIRS:
            (root / name).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        report["errors"].append(f"无法创建素材库目录结构：{exc}")
        report["ok"] = False
        return report

    # --- 3. 空间与可写性 -----------------------------------------------
    try:
        stat = os.statvfs(root)
        report["free_bytes"] = stat.f_bavail * stat.f_frsize
        report["total_bytes"] = stat.f_blocks * stat.f_frsize
        if report["free_bytes"] < 64 * 1024 * 1024:
            report["errors"].append(
                f"素材库所在磁盘可用空间仅 {report['free_bytes'] // 1048576} MB，"
                "低于 64 MB 安全线，已拒绝写入。")
            report["ok"] = False
    except OSError as exc:
        report["errors"].append(f"无法读取素材库磁盘空间：{exc}")
        report["ok"] = False

    for name in SUBDIRS:
        probe = root / name / ".write-probe"
        try:
            probe.write_bytes(b"probe")
            probe.unlink(missing_ok=True)
        except OSError as exc:
            report["errors"].append(f"分区 {name} 不可写：{exc}")
            report["ok"] = False

    return report


def _mount_device(path: Path) -> str | None:
    """返回 path 所在挂载点的设备标识（stat.st_dev 字符串）。"""
    try:
        return str(path.stat().st_dev)
    except OSError:
        return None


def _nearest_existing(path: Path) -> Path:
    """向上找第一个真实存在的祖先目录。"""
    current = path
    for _ in range(8):
        if current.exists():
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    return current


def _mount_table() -> list[tuple[str, str]]:
    """读 /proc/mounts，返回 [(挂载点, 设备)]。"""
    table = []
    try:
        with open("/proc/mounts", "r", encoding="utf-8") as handle:
            for line in handle:
                parts = line.split()
                if len(parts) < 2:
                    continue
                table.append((parts[1].replace("\\040", " "), parts[0]))
    except OSError:
        pass
    return table


def check_disk_mounted(path: Path) -> tuple[bool, str]:
    """
    判断 path 所在的磁盘是否真的挂载着。

    为什么不能只看「目录存不存在」：
    磁盘被卸载后，挂载点目录**仍然存在**（变成根文件系统上的一个空目录）。
    此时继续写入 = 把用户的素材库塞进系统盘（本机根盘已用 88%）。

    判定方法（Linux 标准做法）：
      1. 从 /proc/mounts 找 path 的最长挂载点前缀 M
      2. 比较 stat(M).st_dev 与 stat(M 的父目录).st_dev
         * 不同 -> M 确实是独立挂载点 -> 磁盘在
         * 相同 -> M 只是父文件系统里的普通目录 -> 磁盘**没挂**
    """
    probe = _nearest_existing(path)
    table = _mount_table()
    if not table:
        # 拿不到挂载表时退化为「目录可写即可用」，不阻断功能
        return True, "无法读取 /proc/mounts，跳过挂载检查"

    best = None
    best_len = -1
    for mountpoint, device in table:
        mountpoint_path = Path(mountpoint)
        if probe == mountpoint_path or mountpoint_path in probe.parents:
            length = len(str(mountpoint_path))
            if length > best_len:
                best = (mountpoint_path, device)
                best_len = length

    if best is None:
        return True, f"{probe} 不在任何挂载点内（按普通目录处理）"

    mountpoint, device = best
    try:
        dev_self = mountpoint.stat().st_dev
        dev_parent = mountpoint.parent.stat().st_dev
    except OSError as exc:
        return False, f"无法读取挂载点 {mountpoint}：{exc}"

    if dev_self == dev_parent:
        return False, (f"{mountpoint} 不是独立挂载点（设备 {device}），"
                       "磁盘很可能未挂载")
    return True, f"{mountpoint} 已挂载（设备 {device}）"


# ------------------------------------------------------------------ 落盘


def sha256_file(path: Path, chunk: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def original_relpath(sha256: str, filename: str) -> str:
    """
    原始文件区的相对路径：按 sha256 前 2 层分目录，避免单目录堆十万文件。
    例：originals/ab/cd/abcdef0123456789__原名.png
    """
    ext = Path(filename or "").suffix.lower()
    if not re_is_safe_ext(ext):
        ext = ""
    stem = (sha256 or "")[:2]
    sub = (sha256 or "")[2:4]
    base = f"{sha256[:16]}__{safe_stem(filename, keep_ext=False)}"
    return f"{DIR_ORIGINALS}/{stem}/{sub}/{base}{ext}"


def safe_stem(filename: str, keep_ext: bool = True) -> str:
    """去掉路径分隔符与控制字符，保留可读部分。"""
    name = Path(str(filename or "file")).name
    if not keep_ext:
        name = Path(name).stem
    cleaned = "".join(ch for ch in name if ch.isprintable() and ch not in '\\/:*?"<>|')
    cleaned = cleaned.strip() or "file"
    return cleaned[:120]


def re_is_safe_ext(ext: str) -> bool:
    return bool(ext) and len(ext) <= 12 and ext[1:].isalnum()


def preview_relpath(asset_id: str, suffix: str = ".jpg") -> str:
    return f"{DIR_PREVIEWS}/{asset_id[:2]}/{asset_id}{suffix}"


def derived_relpath(asset_id: str, tag: str, suffix: str = "") -> str:
    return f"{DIR_DERIVED}/{asset_id[:2]}/{asset_id}__{safe_stem(tag)}{suffix}"


def resolve_inside(root: Path, relative: str) -> Path | None:
    """把相对路径解析成 root 内的绝对路径；越界返回 None。"""
    try:
        target = (root / relative).resolve()
        target.relative_to(root.resolve())
        return target
    except (ValueError, OSError):
        return None


def atomic_write_bytes(target: Path, data: bytes) -> None:
    """原子落盘：先写同目录临时文件，再 rename（同文件系统内 rename 是原子的）。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
    try:
        with tmp.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(target)
    finally:
        tmp.unlink(missing_ok=True)


def atomic_move(source: Path, target: Path) -> None:
    """原子移动（同盘 rename）。跨盘时退化为 copy + replace。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(source, target)
    except OSError:
        shutil.copy2(source, target)
        source.unlink(missing_ok=True)


# ------------------------------------------------------------------ 数据库

SCHEMA = """
PRAGMA journal_mode=WAL;

-- 素材主体：一个素材 = 一个逻辑身份，可有多个版本
CREATE TABLE IF NOT EXISTS assets (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    category      TEXT NOT NULL,
    subtype       TEXT NOT NULL DEFAULT 'binary',
    mime          TEXT,
    tags          TEXT NOT NULL DEFAULT '[]',
    note          TEXT,
    source        TEXT NOT NULL DEFAULT 'upload',
    source_ref    TEXT,
    source_meta   TEXT,                        -- JSON：来源任务的生成参数等
    creator       TEXT,
    latest_version INTEGER NOT NULL DEFAULT 1,
    version_count INTEGER NOT NULL DEFAULT 1,
    total_bytes   INTEGER NOT NULL DEFAULT 0,
    deleted_at    REAL,                        -- 软删除：非空 = 在回收站
    purge_at      REAL,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);

-- 版本：每个版本一个物理文件
CREATE TABLE IF NOT EXISTS asset_versions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id      TEXT NOT NULL,
    version       INTEGER NOT NULL,
    storage_path  TEXT NOT NULL,
    sha256        TEXT NOT NULL,
    bytes         INTEGER NOT NULL DEFAULT 0,
    mime          TEXT,
    width         INTEGER,
    height        INTEGER,
    duration_sec  REAL,
    fps           REAL,
    codec         TEXT,                        -- 图像/视频编码器
    audio_codec   TEXT,                       -- 音频编码
    sample_rate   INTEGER,                    -- 采样率
    channels      INTEGER,                    -- 声道
    container     TEXT,                       -- 容器/格式
    transcode_reason TEXT,                    -- 非空 = 已转码，记录原因
    extracted_json TEXT,                      -- 工作流 JSON 解析摘要
    preview_path  TEXT,
    trash_path    TEXT,                        -- 进回收站时的物理位置（还原用）
    origin        TEXT NOT NULL DEFAULT 'upload',
    origin_note   TEXT,
    created_at    REAL NOT NULL,
    created_by    TEXT,
    FOREIGN KEY (asset_id) REFERENCES assets(id) ON DELETE CASCADE
);

-- 用户标签（全局共享，多对多）
CREATE TABLE IF NOT EXISTS asset_tags (
    tag           TEXT NOT NULL,
    asset_id      TEXT NOT NULL,
    created_at    REAL NOT NULL,
    PRIMARY KEY (tag, asset_id),
    FOREIGN KEY (asset_id) REFERENCES assets(id) ON DELETE CASCADE
);

-- 引用关系：谁引用了这个素材
CREATE TABLE IF NOT EXISTS asset_refs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id      TEXT NOT NULL,
    ref_kind      TEXT NOT NULL,              -- job|job_batch|plan|plan_shot|project|workflow_slot
    ref_id        TEXT NOT NULL,
    slot          TEXT NOT NULL DEFAULT 'ref',
    ordinal       INTEGER NOT NULL DEFAULT 0,
    label         TEXT,
    created_at    REAL NOT NULL,
    FOREIGN KEY (asset_id) REFERENCES assets(id) ON DELETE CASCADE
);

-- 模型分析结果（每次分析/纠错一版，保留修订历史）
CREATE TABLE IF NOT EXISTS asset_analyses (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id      TEXT NOT NULL,
    version       INTEGER NOT NULL DEFAULT 1,
    revision      INTEGER NOT NULL DEFAULT 1,
    kind          TEXT NOT NULL,              -- image|video|audio
    text          TEXT NOT NULL,              -- 本次输出的中文分析文本
    prompt        TEXT,                       -- 本次发给模型的指令
    provider_id   TEXT,
    provider_name TEXT,
    model         TEXT,
    frames        TEXT,                       -- JSON：视频抽帧的时间点
    extra         TEXT,                       -- JSON：分析器附加数据
    error         TEXT,
    created_at    REAL NOT NULL,
    FOREIGN KEY (asset_id) REFERENCES assets(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_assets_cat_created ON assets(category, subtype, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_assets_deleted ON assets(deleted_at);
CREATE INDEX IF NOT EXISTS idx_assets_source ON assets(source, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_versions_sha ON asset_versions(sha256);
CREATE INDEX IF NOT EXISTS idx_versions_asset ON asset_versions(asset_id, version DESC);
CREATE INDEX IF NOT EXISTS idx_refs_asset ON asset_refs(asset_id);
CREATE INDEX IF NOT EXISTS idx_refs_ref ON asset_refs(ref_kind, ref_id);
CREATE INDEX IF NOT EXISTS idx_tags_tag ON asset_tags(tag);
CREATE INDEX IF NOT EXISTS idx_analyses_asset ON asset_analyses(asset_id, revision DESC);
"""


def connect() -> sqlite3.Connection:
    """打开素材库数据库（不存在则建库建表）。"""
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def init_db() -> dict:
    """建表 + 索引。返回自检报告。"""
    report = ensure_layout()
    if not report["ok"]:
        return report
    with connect() as conn:
        conn.executescript(SCHEMA)
        _migrate_columns(conn)
    report["db"] = str(db_path())
    return report


#: 新增列的迁移表：列名 -> DDL 片段
_COLUMN_MIGRATIONS = (
    ("asset_versions", "trash_path", "TEXT"),
    ("asset_versions", "extracted_json", "TEXT"),
    ("asset_versions", "transcode_reason", "TEXT"),
    ("asset_versions", "container", "TEXT"),
    ("asset_versions", "audio_codec", "TEXT"),
    ("asset_versions", "sample_rate", "INTEGER"),
    ("asset_versions", "channels", "INTEGER"),
)


def _migrate_columns(conn: sqlite3.Connection) -> None:
    """
    补齐新增列。

    SQLite 的 CREATE TABLE IF NOT EXISTS 不会给已存在的表加列，
    所以老库升级后必须显式 ALTER TABLE ADD COLUMN。
    """
    for table, column, decl in _COLUMN_MIGRATIONS:
        try:
            rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        except sqlite3.Error:
            continue
        if not rows:
            continue
        names = {row[1] for row in rows}
        if column not in names:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


# ------------------------------------------------------------------ 工具


def new_asset_id() -> str:
    return "asset_" + uuid.uuid4().hex[:12]


def now() -> float:
    return time.time()


def jdump(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def jload(value, default=None):
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def classify(mime: str, filename: str) -> tuple[str, str]:
    """
    根据 mime / 文件名猜 category + subtype。
    猜不准时返回 ('other', 'binary')。
    """
    mime = (mime or "").lower()
    name = (filename or "").lower()
    if mime.startswith("image/"):
        subtype = "character" if any(k in name for k in ("character", "角色", "三视图", "ref")) else "photo"
        return "image", subtype
    if mime.startswith("video/"):
        subtype = "reference_motion" if any(k in name for k in ("motion", "动作", "参考动作")) else "clip"
        return "video", subtype
    if mime.startswith("audio/"):
        if any(k in name for k in ("voice", "配音", "tts", "speech")):
            subtype = "voice"
        elif any(k in name for k in ("sfx", "音效", "foley")):
            subtype = "sfx"
        else:
            subtype = "music"
        return "audio", subtype
    if mime in ("application/json", "text/json"):
        return "workflow", "comfy_json" if name.endswith(".json") else "prompt"
    if mime.startswith("text/"):
        return "doc", "storyboard_script" if any(k in name for k in ("分镜", "storyboard")) else "prompt"
    if name.endswith(".srt") or name.endswith(".vtt"):
        return "other", "subtitle"
    if name.endswith(".json"):
        return "workflow", "comfy_json"
    return "other", "binary"


def guess_mime(filename: str, fallback: str = "application/octet-stream") -> str:
    import mimetypes
    guessed, _ = mimetypes.guess_type(filename or "")
    return guessed or fallback
