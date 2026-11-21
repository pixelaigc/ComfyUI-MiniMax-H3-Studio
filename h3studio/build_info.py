# -*- coding: utf-8 -*-
"""构建版本标识 —— 让用户能自己确认「手机在跑哪一版」。

【为什么需要】
2026-11-21 用户在移动端报「选完生成类型回不去 / 看不到生成媒体库」，
而我在 8190 上怎么测都复现不了。当时双方都无法确认一件事：
**用户手机上的页面到底是不是当前这版代码**。

在没有版本标识的情况下，排查只能靠猜：
「是不是缓存了？」「是不是访问了别的端口？」「是不是我记错了？」
⇒ 只能给出一堆**无法证伪**的替代解释。

有了这个标识，用户看一眼侧栏底部就知道自己跑的是哪一版，
下一次反馈就能直接对照，不必再猜。

【给出什么】
  · commit   —— 部署到线上的那一个 commit 短哈希（最权威）
  · assets  —— index.html 里静态资源的 ?v= 版本号（前端实际拿到的）
  · built_at —— 服务进程启动时间（能看出服务是不是刚重启过）
  · served  —— 服务实际所在目录（能看出是不是跑在 live worktree）

【为什么 assets 版本要单独给】
commit 变了但某个静态资源的 ?v= 没变 ⇒ 浏览器仍可能用旧文件。
「commit 是新的但 assets 是旧的」本身就是一条**可观测的异常**，
能直接指向「忘了升版本号」这类问题。
"""
from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent

#: 服务进程启动时刻（模块 import 时取一次）
_STARTED_AT = time.time()

#: 缓存：commit 只查一次（git 调用约 10~30ms，不该每请求都跑）
_commit_cache: str | None = None


def _git(*args: str) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(BASE), *args],
            encoding="utf-8", timeout=5, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return ""


def commit_short() -> str:
    """当前部署目录所在 commit 的短哈希（查不到就返回空串）。"""
    global _commit_cache
    if _commit_cache is None:
        _commit_cache = _git("rev-parse", "--short", "HEAD") or "unknown"
    return _commit_cache


def asset_versions() -> dict:
    """index.html 里各静态资源的 ?v= 版本号。

    【为什么现场解析而不是维护一张表】
    版本表在 tests/test_frontend_assets_hygiene.py 里已经有一份，
    但那是**测试用**的（列的是「历史上被改过的版本」）。
    这里要的是**此刻实际下发的**版本 —— 从 index.html 现场读，
    才是浏览器真正会请求的 URL。
    """
    out: dict = {}
    idx = BASE / "web" / "index.html"
    try:
        html = idx.read_text(encoding="utf-8")
    except Exception:
        return out
    for m in re.finditer(r'(?:href|src)="/static/([^"?]+)(?:\?v=([^"]*))?"', html):
        name, ver = m.group(1), m.group(2)
        if name:
            out[name] = ver or ""
    return out


def build_info() -> dict:
    """给前端的版本信息。"""
    assets = asset_versions()
    return {
        "commit": commit_short(),
        "assets": assets,
        # 静态资源版本号的一个「指纹」——比逐个列更短，适合显示在 UI 上
        "assets_id": assets_fingerprint(assets),
        "built_at": int(_STARTED_AT),
        "served_from": str(BASE),
        "server_time": int(time.time()),
    }


def assets_fingerprint(assets: dict) -> str:
    """静态资源版本的短指纹。

    【为什么不能取「最大的版本号」】
    版本号是人工命名的（``mobnav20261121a`` / ``vparams7`` / ``refctx1``），
    字典序大小跟「新旧」**毫无关系** —— 实测取 max 得到的
    ``vparams7`` 是个 2026-07 的老版本号，毫无指示意义。

    【改成哈希】
    对「文件名=版本号」的清单（排序后）取 SHA-256 前 8 位。
    性质：
      · 任何静态资源的 ?v= 变了 ⇒ 指纹必变 ⇒ 一眼能看出「资源清单变了」
      · 同一个 commit ⇒ 同一个指纹 ⇒ 可用于交叉核对
      · 长度固定 ⇒ 适合显示在一行小字里
    """
    if not assets:
        return "—"
    import hashlib
    payload = "\n".join(f"{k}={v}" for k, v in sorted(assets.items()))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:8]
