# -*- coding: utf-8 -*-
"""ComfyUI(8188) 的**单一权威客户端** —— 会话复用 + 缓存 + 退避。

【为什么要有这个模块 —— 2026-11-22 「8190 对 8188 请求风暴」取证结论】

现场实测（2026-11-22，tcpdump 40s + 71s 采样）：

  * 8190 -> 8188 的请求速率 **2.3~3.7 req/s**，其中
    `/queue` 占 **2.6 req/s**、`/history/{id}` 占 0.66 req/s、
    `/system_stats` 占 0.38 req/s；
  * 60 秒窗口内 8190 上挂了 **9 条 SSE 连接**（`/api/status/stream`），
    每条 SSE 每秒独立打一次 ComfyUI `/queue`
    （server.py `api_status_stream` -> `status_snapshot` -> `fetch_comfy_queue`）
    -> **9 条 SSE = 9 req/s 打到 ComfyUI 的 /queue**；
  * 前端 `/api/system` 每 3 秒轮询一次，**每一次**都新建
    `ClientSession` 并独立打一次 `/system_stats` + 一次 `/queue`
    （server.py `api_system`）—— 每次都是**新连接**（pcap 里每个
    请求一个独立源端口，FIN-WAIT 堆积）；
  * `yue2_schema._fetch_enums()` 拉的是**全量** `/object_info`
    （实测 37.5 MB / 3~10 秒 / 7129 个节点），而它的缓存**只在
    成功时更新**（`h3studio/yue2_schema.py:185` 的 except 分支
    **不写 `_cache["at"]`**）—— 于是**每次失败都会立刻重试**，
    形成 37 MB 的重复拉取风暴（journal 里 16 次
    「拉 http://127.0.0.1:8188/object_info 失败，用静态兜底枚举」）。

【这个模块解决什么】

1. **会话复用**：全进程**一个** `ClientSession`（连接池复用），
   不再每个请求新建一条 TCP 连接。
2. **昂贵接口缓存 + TTL**：`/object_info`（全量）、`/system_stats`、
   `/models/*` 都有 TTL 缓存，缓存未命中时**并发请求合并**（single-flight），
   N 个并发调用只打 1 次 ComfyUI。
3. **失败退避**：失败后**不立即重试**，按指数退避（带抖动）静默 N 秒。
   这直接消灭了「失败即重试」的放大环。

【红线】

* **不改**任何对外接口的语义与返回结构。
* **不**把「ComfyUI 不可达」伪装成「队列为空」—— 两者的区别由
  `None` vs 空集合表达，与既有 `live_comfy_queue_ids()` 一致。
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from typing import Any

import aiohttp


#: 单次请求的默认超时（秒）。
DEFAULT_TIMEOUT = 5.0

#: 全量 `/object_info` 的缓存 TTL（秒）。
#:
#: 【为什么是 900】ComfyUI 的节点清单**只在启动/装节点时变化**。
#: 实测拉一次 37.5 MB / 3~10 秒 —— 为了 8 个枚举字段付这个代价毫无道理。
#: 15 分钟的 TTL 让「点一次音乐面板」最多付一次全量拉取的代价。
OBJECT_INFO_TTL = 900.0

#: `/system_stats` 的缓存 TTL（秒）。
#:
#: 【为什么是 3】前端每 3 秒刷一次状态栏。3 秒缓存 = ComfyUI 侧
#: 的 `/system_stats` 频率被压到 1/3，同时对用户**不可感知**。
SYSTEM_STATS_TTL = 3.0

#: `/queue` 的缓存 TTL（秒）。
#:
#: 【为什么是 2】worker 每 1.5 秒查一次 history，队列状态面板每 1 秒
#: 刷一次。2 秒 TTL 足以让「任务是不是还在跑」的判定不失真
#: （人眼分辨不出 2 秒），但把 9 条 SSE 的 9 req/s 压到 0.5 req/s。
QUEUE_TTL = 2.0

#: 失败后的**最小**退避时间（秒）。
#:
#: 【为什么需要】没有退避时，ComfyUI 一旦变慢（实测 /object_info
#: 满载 8.9 秒），所有轮询源会**同时**失败、**同时**立刻重试 ——
#: 这就是请求风暴的放大环。退避把重试摊开到时间轴上。
BACKOFF_BASE = 2.0

#: 失败后的**最大**退避时间（秒）。
BACKOFF_MAX = 60.0

#: 单飞（single-flight）合并：同一 key 的并发请求只打一次上游。
#: key -> (future, 失效时刻)
_INFLIGHT: dict[str, tuple[asyncio.Future, float]] = {}

#: 缓存：key -> (值, 写入时刻)
_CACHE: dict[str, tuple[Any, float]] = {}

#: 失败退避状态：key -> (下次允许重试的时刻, 连续失败次数)
_BACKOFF: dict[str, tuple[float, int]] = {}

#: 进程级共享会话（惰性创建）。
_SESSION: aiohttp.ClientSession | None = None

#: 会话的连接池上限。
#:
#: 【为什么是 16】8190 到 8188 的真实并发需求：
#: worker 1 条 + SSE 若干 + 状态轮询 1 条 + 列表页 1 条 ≈ 8~12。
#: 给 16 留余量，又不至于把 ComfyUI 的 HTTP 线程池压爆。
POOL_LIMIT = 16


def session() -> aiohttp.ClientSession:
    """取**进程级共享**的 aiohttp 会话（连接池复用）。

    【为什么必须共享】原来每个 handler 都 `async with ClientSession(...)`
    —— 每次请求 = 一条新 TCP 连接 + 一次 TLS/握手开销。pcap 实测
    40 秒内 8188 上出现 100+ 条新建/关闭连接，FIN-WAIT 堆积。
    """
    global _SESSION
    if _SESSION is None or _SESSION.closed:
        _SESSION = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT),
            connector=aiohttp.TCPConnector(limit=POOL_LIMIT, ttl_dns_cache=300),
        )
    return _SESSION


async def close_session() -> None:
    """关闭共享会话（进程退出时调用，避免 aiohttp 的 Unclosed 警告）。"""
    global _SESSION
    if _SESSION is not None and not _SESSION.closed:
        await _SESSION.close()
    _SESSION = None


def _backoff_state(key: str) -> tuple[float, int]:
    """取某 key 的退避状态（下次允许重试的时刻, 连续失败次数）。"""
    return _BACKOFF.get(key, (0.0, 0))


def _record_failure(key: str) -> None:
    """记一次失败 -> 指数退避（带抖动）。"""
    _, failures = _backoff_state(key)
    failures += 1
    delay = min(BACKOFF_BASE * (2 ** (failures - 1)), BACKOFF_MAX)
    # ±25% 抖动：避免多个调用方在同一毫秒一起重试（惊群）。
    delay *= 0.75 + random.random() * 0.5
    _BACKOFF[key] = (time.monotonic() + delay, failures)


def _record_success(key: str) -> None:
    """成功 -> 清掉退避状态。"""
    _BACKOFF.pop(key, None)


def backoff_remaining(key: str) -> float:
    """某 key 当前还剩多少秒退避（0 = 允许立刻重试）。"""
    until, _ = _backoff_state(key)
    return max(0.0, until - time.monotonic())


def cache_get(key: str, ttl: float) -> tuple[bool, Any]:
    """取缓存。返回 (命中?, 值)。"""
    hit = _CACHE.get(key)
    if hit is None:
        return False, None
    value, at = hit
    if (time.monotonic() - at) >= ttl:
        return False, None
    return True, value


def cache_put(key: str, value: Any) -> None:
    """写缓存。"""
    _CACHE[key] = (value, time.monotonic())


def cache_invalidate(prefix: str | None = None) -> None:
    """清缓存（prefix=None 清全部；否则清所有以 prefix 开头的 key）。"""
    if prefix is None:
        _CACHE.clear()
        return
    for key in [k for k in _CACHE if k.startswith(prefix)]:
        _CACHE.pop(key, None)


async def _single_flight(key: str, factory):
    """同一 key 的并发调用只执行一次 factory（其余等待同一结果）。

    【为什么需要】9 条 SSE + 列表页 + 状态轮询**同时**要 /queue 时，
    没有合并就是 11 次上游请求；合并后是 1 次。
    """
    existing = _INFLIGHT.get(key)
    if existing is not None:
        fut, _ = existing
        return await asyncio.shield(fut)

    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    _INFLIGHT[key] = (fut, time.monotonic())
    try:
        result = await factory()
    except BaseException as exc:  # noqa: BLE001
        if not fut.done():
            fut.set_exception(exc)
        # 让其它等待者立刻看到失败（而不是等 future 被 GC）
        await asyncio.sleep(0)
        raise
    else:
        if not fut.done():
            fut.set_result(result)
        return result
    finally:
        _INFLIGHT.pop(key, None)


async def get_json(path: str,
                   *,
                   timeout: float = DEFAULT_TIMEOUT,
                   cache_ttl: float = 0.0,
                   cache_key: str | None = None,
                   backoff_key: str | None = None,
                   force: bool = False) -> Any:
    """GET 一个 JSON 端点，带缓存 + 单飞 + 失败退避。

    返回：
      * 正常 -> 解析后的 JSON（list / dict）；
      * 缓存命中 -> 缓存值；
      * 退避中 -> ``None``（**不是**空列表 —— 空列表会被误读成「队列为空」）；
      * 上游失败 -> ``None``。

    【为什么失败返回 None 而不是抛】调用方的既有语义就是
    「读不到 ComfyUI」= None / False，与「读到但为空」= [] / 0 区分开。
    抛异常会破坏这个语义（且让 handler 变成 500）。
    """
    key = cache_key or path
    bkey = backoff_key if backoff_key is not None else key

    if not force:
        hit, value = cache_get(key, cache_ttl)
        if hit:
            return value
        # 退避中 -> 不打上游，直接返回 None（**不立即重试**）
        if backoff_remaining(bkey) > 0:
            return None

    async def _do():
        sess = session()
        try:
            async with sess.get(f"{_comfy_base()}{path}",
                                timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                if resp.status != 200:
                    _record_failure(bkey)
                    return None
                data = await resp.json(content_type=None)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            _record_failure(bkey)
            return None
        _record_success(bkey)
        return data

    result = await _single_flight(f"get:{key}", _do)
    if result is not None and cache_ttl > 0:
        cache_put(key, result)
    return result


async def get_bytes(path: str,
                    *,
                    timeout: float = 60.0,
                    cache_ttl: float = 0.0,
                    cache_key: str | None = None,
                    backoff_key: str | None = None,
                    force: bool = False) -> bytes | None:
    """GET 一个二进制端点（带同样的缓存 / 单飞 / 退避）。"""
    key = cache_key or path
    bkey = backoff_key if backoff_key is not None else key

    if not force:
        hit, value = cache_get(key, cache_ttl)
        if hit:
            return value
        if backoff_remaining(bkey) > 0:
            return None

    async def _do():
        sess = session()
        try:
            async with sess.get(f"{_comfy_base()}{path}",
                                timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                if resp.status != 200:
                    _record_failure(bkey)
                    return None
                data = await resp.read()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            _record_failure(bkey)
            return None
        _record_success(bkey)
        return data

    result = await _single_flight(f"getb:{key}", _do)
    if result is not None and cache_ttl > 0:
        cache_put(key, result)
    return result


async def get_text(path: str,
                   *,
                   timeout: float = 60.0,
                   cache_ttl: float = 0.0,
                   cache_key: str | None = None,
                   backoff_key: str | None = None,
                   force: bool = False) -> str | None:
    """GET 一个文本端点（带同样的缓存 / 单飞 / 退避）。"""
    key = cache_key or path
    bkey = backoff_key if backoff_key is not None else key

    if not force:
        hit, value = cache_get(key, cache_ttl)
        if hit:
            return value
        if backoff_remaining(bkey) > 0:
            return None

    async def _do():
        sess = session()
        try:
            async with sess.get(f"{_comfy_base()}{path}",
                                timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                if resp.status != 200:
                    _record_failure(bkey)
                    return None
                data = await resp.text()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            _record_failure(bkey)
            return None
        _record_success(bkey)
        return data

    result = await _single_flight(f"gett:{key}", _do)
    if result is not None and cache_ttl > 0:
        cache_put(key, result)
    return result


_COMFY_BASE = {"url": "http://127.0.0.1:8188"}


def configure(base_url: str) -> None:
    """设置 ComfyUI 基址（server.py 启动时调用一次）。"""
    _COMFY_BASE["url"] = base_url.rstrip("/")


def _comfy_base() -> str:
    return _COMFY_BASE["url"]


async def post_json(path: str, payload: dict, *, timeout: float = 60.0) -> Any:
    """POST 一个 JSON（**不走缓存** —— 写操作必须真实到达）。"""
    sess = session()
    try:
        async with sess.post(f"{_comfy_base()}{path}", json=payload,
                             timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            if resp.status != 200:
                return None
            return await resp.json(content_type=None)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        return None


def stats_snapshot() -> dict:
    """本模块的运行时统计（给 /api/system 或诊断用）。"""
    return {
        "cache_keys": sorted(_CACHE.keys()),
        "backoff_keys": {k: round(max(0.0, v[0] - time.monotonic()), 1)
                         for k, v in _BACKOFF.items()},
        "inflight": sorted(_INFLIGHT.keys()),
        "session_shared": _SESSION is not None and not _SESSION.closed,
    }
