# -*- coding: utf-8 -*-
"""
H3 Studio 本地模型 Provider 存储层。

所有局域网客户端共用同一份配置 —— 落在服务端 SQLite（data/jobs.db），
不依赖浏览器 localStorage。

表结构:
  model_providers        —— Provider 主表（一个 Provider = 一条记录）
  model_provider_prefs  —— 全局偏好（默认 Provider / 默认多模态 Provider）

Provider 类型:
  openai        OpenAI 兼容 API（/v1/chat/completions）
  ollama        Ollama（/api/chat，或 /v1/chat/completions）
  llamacpp      llama.cpp server（OpenAI 兼容）
  comfyui       ComfyUI 本地节点（走 ComfyUI 的 LLM 节点工作流）
"""
import json
import sqlite3
import time
import uuid

# Provider 类型白名单
PROVIDER_TYPES = ("openai", "ollama", "llamacpp", "comfyui")

PROVIDER_TYPE_LABELS = {
    "openai": "OpenAI 兼容 API",
    "ollama": "Ollama",
    "llamacpp": "llama.cpp server",
    "comfyui": "ComfyUI 本地节点",
}

# 字段长度上限
MAX_NAME = 60
MAX_URL = 300
MAX_MODEL = 120
MAX_KEY = 200

# ------------------------------------------------------------------ 校验


def _clean(value, limit):
    """去空白 + 截断，任何类型都安全。"""
    return str(value if value is not None else "").strip()[:limit]


def normalize_provider(payload: dict) -> dict:
    """
    把前端提交的 payload 规整成一条合法的 Provider 记录。
    必填项缺失在这里抛 ValueError（带中文原因）。
    """
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是 JSON 对象")

    name = _clean(payload.get("name"), MAX_NAME)
    if not name:
        raise ValueError("显示名不能为空")

    ptype = _clean(payload.get("type"), 20).lower()
    if not ptype:
        raise ValueError("类型不能为空")
    if ptype not in PROVIDER_TYPES:
        raise ValueError(f"不支持的类型 {ptype}，可选：{'、'.join(PROVIDER_TYPES)}")

    base_url = _clean(payload.get("base_url"), MAX_URL)
    if not base_url:
        raise ValueError("Base URL 不能为空")
    if not base_url.startswith(("http://", "https://")):
        raise ValueError("Base URL 必须以 http:// 或 https:// 开头")
    base_url = base_url.rstrip("/")

    model = _clean(payload.get("model"), MAX_MODEL)
    if not model:
        raise ValueError("模型名不能为空")

    api_key = _clean(payload.get("api_key"), MAX_KEY)

    # ComfyUI 类型专用：节点内部去连的 LLM 服务地址（可空 = 与 base_url 相同）
    llm_base_url = _clean(payload.get("llm_base_url"), MAX_URL)
    if llm_base_url:
        if not llm_base_url.startswith(("http://", "https://")):
            raise ValueError("LLM 服务地址必须以 http:// 或 https:// 开头")
        llm_base_url = llm_base_url.rstrip("/")

    # ComfyUI 类型专用：是否走 H3 Studio 的 OpenAI 兼容代理
    # （thinking 模型必须走代理 —— 节点自己关不掉 thinking）
    use_proxy = 1 if payload.get("use_proxy") in (True, 1, "1", "true", "yes") else 0
    proxy_base_url = _clean(payload.get("proxy_base_url"), MAX_URL)
    if proxy_base_url:
        if not proxy_base_url.startswith(("http://", "https://")):
            raise ValueError("代理地址必须以 http:// 或 https:// 开头")
        proxy_base_url = proxy_base_url.rstrip("/")

    multimodal = payload.get("multimodal")
    multimodal = 1 if multimodal in (True, 1, "1", "true", "yes") else 0

    enabled = payload.get("enabled")
    enabled = 0 if enabled in (False, 0, "0", "false", "no") else 1

    return {
        "id": _clean(payload.get("id"), 40) or uuid.uuid4().hex[:12],
        "name": name,
        "type": ptype,
        "base_url": base_url,
        "llm_base_url": llm_base_url,
        "use_proxy": use_proxy,
        "proxy_base_url": proxy_base_url,
        "model": model,
        "api_key": api_key,
        "multimodal": multimodal,
        "enabled": enabled,
        "created_at": time.time(),
    }


# ------------------------------------------------------------------ 读写


def _row_to_provider(row) -> dict:
    """DB 行 -> 对外 dict。API Key 只回传掩码，不回传明文。"""
    key = row["api_key"] or ""
    masked = ""
    if key:
        masked = (key[:4] + "…" + key[-4:]) if len(key) > 10 else ("*" * len(key))
    return {
        "id": row["id"],
        "name": row["name"],
        "type": row["type"],
        "type_label": PROVIDER_TYPE_LABELS.get(row["type"], row["type"]),
        "base_url": row["base_url"],
        "llm_base_url": row["llm_base_url"] or "",
        "use_proxy": bool(row["use_proxy"]),
        "proxy_base_url": row["proxy_base_url"] or "",
        "model": row["model"],
        "has_api_key": bool(key),
        "api_key_masked": masked,
        "multimodal": bool(row["multimodal"]),
        "enabled": bool(row["enabled"]),
        "created_at": row["created_at"],
    }


def ensure_tables(conn: sqlite3.Connection):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS model_providers (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            type TEXT NOT NULL,
            base_url TEXT NOT NULL,
            llm_base_url TEXT DEFAULT '',
            use_proxy INTEGER DEFAULT 0,
            proxy_base_url TEXT DEFAULT '',
            model TEXT NOT NULL,
            api_key TEXT DEFAULT '',
            multimodal INTEGER DEFAULT 0,
            enabled INTEGER DEFAULT 1,
            created_at REAL
        );
        CREATE TABLE IF NOT EXISTS model_provider_prefs (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """
    )
    # 迁移：老版本建过表时补上新增列（SQLite 的 ADD COLUMN 是幂等友好的）
    columns = {row["name"] for row in
               conn.execute("PRAGMA table_info(model_providers)").fetchall()}
    if "llm_base_url" not in columns:
        conn.execute("ALTER TABLE model_providers "
                     "ADD COLUMN llm_base_url TEXT DEFAULT ''")
    if "use_proxy" not in columns:
        conn.execute("ALTER TABLE model_providers "
                     "ADD COLUMN use_proxy INTEGER DEFAULT 0")
    if "proxy_base_url" not in columns:
        conn.execute("ALTER TABLE model_providers "
                     "ADD COLUMN proxy_base_url TEXT DEFAULT ''")


def list_providers(conn: sqlite3.Connection) -> list:
    rows = conn.execute(
        "SELECT * FROM model_providers ORDER BY created_at ASC").fetchall()
    return [_row_to_provider(r) for r in rows]


def get_provider(conn: sqlite3.Connection, provider_id: str):
    row = conn.execute("SELECT * FROM model_providers WHERE id=?",
                       (provider_id,)).fetchone()
    return row


def get_provider_full(conn: sqlite3.Connection, provider_id: str):
    """含 API Key 明文，仅供服务端内部调用。"""
    row = get_provider(conn, provider_id)
    if not row:
        return None
    return {
        "id": row["id"],
        "name": row["name"],
        "type": row["type"],
        "base_url": row["base_url"],
        "llm_base_url": row["llm_base_url"] or "",
        "use_proxy": bool(row["use_proxy"]),
        "proxy_base_url": row["proxy_base_url"] or "",
        "model": row["model"],
        "api_key": row["api_key"] or "",
        "multimodal": bool(row["multimodal"]),
        "enabled": bool(row["enabled"]),
    }


def insert_provider(conn: sqlite3.Connection, data: dict) -> dict:
    conn.execute(
        "INSERT INTO model_providers "
        "(id,name,type,base_url,llm_base_url,use_proxy,proxy_base_url,"
        "model,api_key,multimodal,enabled,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (data["id"], data["name"], data["type"], data["base_url"],
         data.get("llm_base_url", ""), data.get("use_proxy", 0),
         data.get("proxy_base_url", ""), data["model"],
         data["api_key"], data["multimodal"], data["enabled"], data["created_at"]))
    return _row_to_provider(get_provider(conn, data["id"]))


def update_provider(conn: sqlite3.Connection, provider_id: str, data: dict) -> dict | None:
    """更新。api_key 传 None 表示保持原值不变（前端不回显明文）。"""
    current = get_provider(conn, provider_id)
    if not current:
        return None
    api_key = current["api_key"] if data["api_key"] is None else data["api_key"]
    conn.execute(
        "UPDATE model_providers SET name=?, type=?, base_url=?, llm_base_url=?, "
        "use_proxy=?, proxy_base_url=?, model=?, api_key=?, multimodal=?, enabled=? "
        "WHERE id=?",
        (data["name"], data["type"], data["base_url"],
         data.get("llm_base_url", "") or "", data.get("use_proxy", 0),
         data.get("proxy_base_url", "") or "", data["model"], api_key,
         data["multimodal"], data["enabled"], provider_id))
    return _row_to_provider(get_provider(conn, provider_id))


def delete_provider(conn: sqlite3.Connection, provider_id: str) -> bool:
    row = get_provider(conn, provider_id)
    if not row:
        return False
    conn.execute("DELETE FROM model_providers WHERE id=?", (provider_id,))
    # 清掉指向它的默认项
    conn.execute("DELETE FROM model_provider_prefs WHERE key=? AND value=?",
                 ("default", provider_id))
    conn.execute("DELETE FROM model_provider_prefs WHERE key=? AND value=?",
                 ("default_multimodal", provider_id))
    return True


# ------------------------------------------------------------------ 默认项


def get_pref(conn: sqlite3.Connection, key: str):
    row = conn.execute("SELECT value FROM model_provider_prefs WHERE key=?",
                       (key,)).fetchone()
    return row["value"] if row else None


def set_pref(conn: sqlite3.Connection, key: str, value):
    conn.execute("INSERT INTO model_provider_prefs (key,value) VALUES (?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, value))
    return value


def resolve_default(conn: sqlite3.Connection, key: str, require_multimodal=False):
    """
    取默认 Provider。规则：
      1. 优先读 model_provider_prefs 里显式设置的 id
      2. 该 id 必须存在、且已启用
      3. require_multimodal 时还必须是多模态 Provider
      4. 落空则回退：启用中的多模态 Provider -> 第一个启用的 Provider
    """
    candidates = list_providers(conn)
    enabled = [p for p in candidates if p["enabled"]]

    preferred = get_pref(conn, key)
    if preferred:
        for p in candidates:
            if p["id"] == preferred:
                if not p["enabled"]:
                    break
                if require_multimodal and not p["multimodal"]:
                    break
                return p
    if require_multimodal:
        for p in enabled:
            if p["multimodal"]:
                return p
    if enabled:
        return enabled[0]
    return None


def prefs_snapshot(conn: sqlite3.Connection) -> dict:
    return {
        "default_provider_id": get_pref(conn, "default"),
        "default_multimodal_provider_id": get_pref(conn, "default_multimodal"),
    }
