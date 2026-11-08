"""Z-Image 可用 LoRA 清单与服务端持久化配置。

只收录**本机实际存在**的 Z-Image 系列 LoRA —— 清单由 ComfyUI 自己的
folder_paths 扫描得出（跟 /object_info/LoraLoaderModelOnly 同一个来源），
不写死任何可能不存在的文件名。

每个 LoRA 的说明字段来自：
  * safetensors 头部 __metadata__（modelspec / ss_* 字段）
  * 上游仓库 README（ModelScope / Civitai），触发词即上游 tags
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

# ------------------------------------------------------------------ 已知说明
# 说明来源全部标注在 source 字段里，界面上可展开查看。
# trigger 为上游 README tags 中明确标出的触发词；上游未标注的一律留空，
# 不用「猜测」填满。
LORA_CATALOG: dict[str, dict] = {
    "Kook_Zimage_如梦似幻.safetensors": {
        "display": "如梦似幻（Kook Z-Image 滤镜）",
        "base": "Tongyi-MAI/Z-Image（Z_IMAGE_TURBO）",
        "trigger": "如梦似幻",
        "recommended_strength": 1.0,
        "scene": "梦幻朦胧/柔光滤镜风格，官方 tags 标注为「滤镜lora」。",
        "source": "ModelScope KookYan/Kook_Zimage_Rmsh（README tags）",
    },
    "Kook_Zimage_瑶光.safetensors": {
        "display": "瑶光（Kook Z-Image 真实幻想）",
        "base": "Tongyi-MAI/Z-Image（Z_IMAGE_TURBO）",
        "trigger": "瑶光 / 真实幻想",
        "recommended_strength": 1.0,
        "scene": "写实向「真实幻想」质感，官方 tags 标注为「瑶光 / 真实幻想」。",
        "source": "ModelScope KookYan/Kook_Zimage_Yg（README tags）",
    },
    "Z-Image-Fun-Lora-Distill-8-Steps-2603-ComfyUI.safetensors": {
        "display": "Z-Image-Fun 蒸馏加速 8 步（2603）",
        "base": "Z-Image Base（ZImageBase）",
        "trigger": "",
        "recommended_strength": 1.0,
        "scene": "步数+CFG 双蒸馏加速：8 步出图，比 Base 的 25 步快 3 倍；"
                 "2603 版新增随机时间步策略，sigma<0.5 区间不再糊。",
        "source": "Civitai 2362961 / HF alibaba-pai/Z-Image-Fun-Lora-Distill",
    },
    "z-image/Z-Image-Fun-Distill-Lora.safetensors": {
        "display": "Z-Image 蒸馏 LoRA（本地抽取版 rank64）",
        "base": "Z-Image（zimage）",
        "trigger": "",
        "recommended_strength": 1.0,
        "scene": "从 Z-Image 权重抽出的 rank64 蒸馏 LoRA（ModelDiffToLoRA 抽取），"
                 "用于低步数加速，本地无上游训练参数说明。",
        "source": "本地 safetensors __metadata__（extracted_by: comfyui-zimage-realtime-lora）",
    },
}

# 只认这些关键词算「Z-Image 相关」，避免把 285 个 LoRA 全塞进面板
Z_IMAGE_MARKERS = ("zimage", "z-image", "z_image", "kook_zimage")


# ------------------------------------------------------------------ 扫描本机
def _comfyui_root() -> Path | None:
    """定位本机 ComfyUI 根目录（不写死 /root/ComfyUI）。"""
    cfg = Path(os.environ.get("H3STUDIO_CONFIG", "")) if os.environ.get("H3STUDIO_CONFIG") else None
    candidates: list[Path] = []
    if cfg and cfg.is_file():
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
            models_dir = data.get("models_dir")
            if models_dir:
                candidates.append(Path(models_dir))
        except Exception:
            pass
    candidates += [
        Path("/home/ubuntu/comfyui/models"),
        Path("/root/ComfyUI/models"),
    ]
    for base in candidates:
        if (base / "loras").is_dir():
            return base
    return None


def _parse_extra_lora_dirs(raw: str) -> list[Path]:
    """解析 extra_model_paths.yaml，取出所有 section 的 loras 目录。

    文件形态（注意 base_path 缩进在 section 名下面）：
        local_fast:
            base_path: /home/ubuntu/comfyui/models
            loras: loras
        nvme1t_models:
            base_path: /mnt/nvme1t_colorful/comfyui-models/
            loras: loras
    """
    out: list[Path] = []
    base_path: str | None = None
    for raw_line in raw.splitlines():
        line = raw_line.split("#")[0].rstrip()
        if not line.strip():
            continue
        indented = line.startswith((" ", "\t"))
        key, sep, val = line.strip().partition(":")
        if not sep:
            continue
        key = key.strip()
        val = val.strip()
        if not indented:
            # 顶层行：section 名 -> 重置 base_path
            base_path = None
            continue
        if key == "base_path":
            base_path = val
        elif key == "loras" and base_path:
            for item in val.split("|"):
                item = item.strip()
                if item:
                    out.append(Path(base_path) / item)
    return out


def comfyui_lora_dirs() -> list[Path]:
    """列出 ComfyUI 实际会扫描的 loras 目录（本地 + extra_model_paths.yaml）。"""
    dirs: list[Path] = []
    root = _comfyui_root()
    if root:
        dirs.append(root / "loras")
    # extra_model_paths.yaml 里声明的额外 loras 目录
    for extra in (Path("/home/ubuntu/comfyui/extra_model_paths.yaml"),
                   Path("/root/ComfyUI/extra_model_paths.yaml")):
        if not extra.is_file():
            continue
        try:
            dirs.extend(_parse_extra_lora_dirs(extra.read_text(encoding="utf-8")))
        except Exception:
            pass
    # 去重 + 只保留存在的
    seen: set[str] = set()
    out: list[Path] = []
    for d in dirs:
        key = str(d)
        if key in seen:
            continue
        seen.add(key)
        if d.is_dir():
            out.append(d)
    return out


def _rel_to_lora_dir(name: str, dirs: list[Path]) -> tuple[str, str]:
    """返回 (ComfyUI 里的 lora_name, 绝对路径)。找不到返回 ("", "")。"""
    for d in dirs:
        candidate = d / name
        if candidate.is_file():
            return name, str(candidate)
    # 支持带子目录的写法（如 z-image/xxx.safetensors）
    for d in dirs:
        for path in d.rglob(name):
            if path.is_file():
                rel = path.relative_to(d).as_posix()
                return rel, str(path)
    return "", ""


def list_z_image_loras() -> list[dict]:
    """扫描本机，返回真实存在的 Z-Image LoRA 清单。"""
    dirs = comfyui_lora_dirs()
    found: list[dict] = []
    seen: set[str] = set()
    for d in dirs:
        try:
            candidates = sorted(d.rglob("*.safetensors"))
        except Exception:
            continue
        for path in candidates:
            rel = path.relative_to(d).as_posix()
            lowered = rel.lower()
            if not any(marker in lowered for marker in Z_IMAGE_MARKERS):
                continue
            if rel in seen:
                continue
            seen.add(rel)
            meta = _read_safetensors_metadata(path)
            info = LORA_CATALOG.get(rel) or LORA_CATALOG.get(path.name) or {}
            found.append({
                "name": rel,
                "path": str(path),
                "size_mb": round(path.stat().st_size / (1024 * 1024), 1),
                "display": info.get("display") or path.stem,
                "base": info.get("base") or (meta.get("modelspec.architecture") or meta.get("ss_base_model_version") or "Z-Image"),
                "trigger": info.get("trigger", ""),
                "recommended_strength": info.get("recommended_strength", 1.0),
                "scene": info.get("scene", ""),
                "source": info.get("source", ""),
                "metadata": meta,
            })
    return found


def _read_safetensors_metadata(path: Path) -> dict:
    """只读 safetensors 头部元数据（不加载权重）。"""
    try:
        import struct
        with open(path, "rb") as handle:
            size = struct.unpack("<Q", handle.read(8))[0]
            header = json.loads(handle.read(size))
        return header.get("__metadata__") or {}
    except Exception:
        return {}


def resolve_lora(name: str) -> tuple[str, Path | None]:
    """把界面上的 lora_name 解析成 (ComfyUI lora_name, 绝对路径)。

    找不到时返回 ("", None) —— 调用方必须据此报错，不能静默忽略。
    """
    dirs = comfyui_lora_dirs()
    rel, absolute = _rel_to_lora_dir(name, dirs)
    return rel, (Path(absolute) if absolute else None)


# ------------------------------------------------------------------ 持久化
DEFAULT_PREF_KEY = "z_image_loras"


def load_config(conn: sqlite3.Connection, key: str = DEFAULT_PREF_KEY) -> list[dict]:
    """读取服务端保存的 LoRA 配置（局域网所有客户端共用这一份）。"""
    try:
        row = conn.execute("SELECT value FROM model_provider_prefs WHERE key=?", (key,)).fetchone()
    except sqlite3.Error:
        return []
    if not row or not row["value"]:
        return []
    try:
        data = json.loads(row["value"])
    except Exception:
        return []
    return data if isinstance(data, list) else []


def save_config(conn: sqlite3.Connection, items: list[dict], key: str = DEFAULT_PREF_KEY) -> list[dict]:
    payload = json.dumps(items, ensure_ascii=False)
    conn.execute(
        "INSERT INTO model_provider_prefs (key,value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, payload),
    )
    return items
