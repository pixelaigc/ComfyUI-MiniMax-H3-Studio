# -*- coding: utf-8 -*-
"""ComfyUI **实际可用模型清单**的查询客户端。

【为什么要有这个模块（2026-11-11 用户报缺陷）】

文生视频面板原来对「这个底模现在能不能选」的判断有两条互不相干的路径：

  1. `h3studio/video_models.py` 的 `model_available()` —— 自己扫
     `extra_model_paths.yaml` 声明的目录 + 几个写死的默认目录，
     判断**文件在不在盘上**；
  2. 面板上那行红字 —— 是一句**写死的字符串**（`VIDEO_MODELS[...]["warn"]`），
     跟 (1) 的结论**没有任何关系**。

于是出现了用户截图里的矛盾：面板明明显示「本机已安装」、下拉框里那个
模型也能选（`available: true`），下面却挂着一句「盘未挂载时 ComfyUI 下拉里
根本没有这个名字，提交会被拒绝」—— 让人以为「获取不到模型」。

**结论性的错��**：那行字的前提（「盘未挂载」）在本机根本不成立
（外置盘 `/mnt/nvme1t_colorful` 已挂载，权重就在盘上），所以这句话
是在**没有查过 ComfyUI 模型列表 API** 的情况下写死的断言。

【这个模块的职责】

把「ComfyUI 现在真能用哪些模型」这件事，从「我们自己扫盘」改成
**问 ComfyUI 本人**：

  * `GET /models/diffusion_models` —— ComfyUI 0.37.0 的模型目录接口
    （实测本机 151 个条目，全部 H3 FL2VA 权重都在里面）；
  * `GET /models/loras` —— 同上（实测 686 个条目）；
  * `GET /object_info/<NodeName>` —— 某个节点输入槽的**可选值枚举**，
    与 ComfyUI 界面下拉框**完全同源**（folder_paths.get_filename_list）。

注意 ComfyUI **没有** `/models/unet` 这个路由（实测 404）——
`unet` 是 `diffusion_models` 的旧名（`folder_paths.map_legacy` 会映射），
所以对外只暴露 `diffusion_models` / `loras` 两个真实存在的目录。

【缓存】

ComfyUI 自己的 `folder_paths.get_filename_list` 也是缓存的（按目录
mtime 失效），所以这里用同样的短 TTL 缓存 + `force=True` 强制刷新。
默认 TTL 20 秒 —— 面板打开时拉一次，用户改完权重再回来能刷出来。

【降级】

ComfyUI 不可达时**不能**静默返回空列表（那会让面板误以为「一个模型都没有」，
这正是本次要修的病）。降级策略：
  * `fetch_model_list()` 抛 `ComfyModelListError`；
  * 调用方（`video_models.list_models()`）捕获后 **保留文件系统扫描的结论**，
    并在 payload 里带上 `model_list_source: "filesystem_fallback"` +
    `model_list_error: <原因>`，前端据此显示「未能连上 ComfyUI，
    以下清单来自本机磁盘扫描」—— 如实说明，而不是谎称「获取不到模型」。
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request
from typing import Iterable

#: ComfyUI 里「模型目录」的真实名字。
#: 实测 `/models/unet` 返回 404（unet 是 diffusion_models 的旧别名，
#: ComfyUI 的 folder_paths.map_legacy 会映射，但 HTTP 路由不做映射）。
COMFY_MODEL_FOLDERS = ("diffusion_models", "loras")

#: 缓存 TTL（秒）。ComfyUI 自己的目录列表也是缓存的，短 TTL 足够。
DEFAULT_TTL = 20.0

#: 单次 HTTP 超时（秒）。
#:
#: 【为什么是 60 而不是 10】实测 ComfyUI 0.37.0 的 `/models/diffusion_models`
#: **首次**调用要 43 秒 —— 它要递归扫 extra_model_paths.yaml 里声明的
#: **所有**模型目录（含外接盘 /mnt/nvme1t_colorful 与 /mnt/usb2t），
#: 目录多、文件多，扫描本身就很慢（第二次走 ComfyUI 自己的
#: folder_paths 缓存，0.007 秒）。
#:
#: 所以超时必须留足余量，否则「盘没挂载」这种**恰好会触发全量重扫**的
#: 场景会被误判成「ComfyUI 连不上」—— 那就正好复现了本次要修的病。
DEFAULT_TIMEOUT = 60.0


class ComfyModelListError(RuntimeError):
    """拿不到 ComfyUI 模型列表（连接失败 / 超时 / 返回体不对）。"""


# ------------------------------------------------------------------
# 缓存
# ------------------------------------------------------------------
_CACHE: dict[str, tuple[float, list[str]]] = {}


def _cache_get(folder: str, ttl: float, now: float) -> list[str] | None:
    hit = _CACHE.get(folder)
    if hit is None:
        return None
    ts, names = hit
    if ttl > 0 and (now - ts) < ttl:
        return list(names)
    return None


def _cache_set(folder: str, names: list[str], now: float) -> None:
    _CACHE[folder] = (now, list(names))


def clear_cache() -> None:
    """清空缓存（测试 / 「强制刷新」用）。"""
    _CACHE.clear()


# ------------------------------------------------------------------
# 底层 HTTP
# ------------------------------------------------------------------
def _http_get_json(url: str, timeout: float) -> object:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise ComfyModelListError(
            f"ComfyUI 返回 HTTP {exc.code}（{url}）") from exc
    except Exception as exc:  # noqa: BLE001 —— 连接失败/超时/DNS
        raise ComfyModelListError(
            f"连不上 ComfyUI（{url}）：{type(exc).__name__}: {exc}") from exc
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise ComfyModelListError(
            f"ComfyUI 返回的不是合法 JSON（{url}）") from exc


def fetch_model_list(base_url: str,
                     folder: str = "diffusion_models",
                     *,
                     ttl: float = DEFAULT_TTL,
                     timeout: float = DEFAULT_TIMEOUT,
                     force: bool = False,
                     now: float | None = None) -> list[str]:
    """拉 ComfyUI 某个模型目录下的**实际可用**文件清单。

    返回 ComfyUI 界面下拉框里**一模一样**的那份列表（folder_paths
    的扫描结果，含子目录前缀，如 `h3/xxx.safetensors`）。

    Raises:
        ComfyModelListError: ComfyUI 不可达 / 返回体不是字符串数组。
    """
    if folder not in COMFY_MODEL_FOLDERS:
        raise ComfyModelListError(
            f"未知的模型目录 {folder}（ComfyUI 实测只暴露 "
            f"{'、'.join(COMFY_MODEL_FOLDERS)}；`unet` 是旧别名，HTTP 层不认）")
    stamp = time.time() if now is None else now
    if not force:
        cached = _cache_get(folder, ttl, stamp)
        if cached is not None:
            return cached
    url = f"{base_url.rstrip('/')}/models/{folder}"
    data = _http_get_json(url, timeout)
    if not isinstance(data, list) or not all(isinstance(x, str) for x in data):
        raise ComfyModelListError(
            f"ComfyUI {url} 返回的不是字符串数组（类型 {type(data).__name__}）")
    names = list(data)
    _cache_set(folder, names, stamp)
    return names


def fetch_object_info_enum(base_url: str,
                           node_name: str,
                           input_name: str = "unet_name",
                           *,
                           timeout: float = DEFAULT_TIMEOUT) -> list[str]:
    """拉某个节点输入槽的**可选值枚举**（ComfyUI 界面下拉框同源）。

    例：`fetch_object_info_enum(base, "UNETLoader", "unet_name")`
    -> 151 个 diffusion model 名，与 `/models/diffusion_models` 一致。

    与 `fetch_model_list` 的区别：这是**节点视角**（工作流真正往里塞的
    那个槽），`/models/*` 是**目录视角**。两者都取，作为交叉验证。

    Raises:
        ComfyModelListError: 连不上 / 节点不存在 / 槽位不是枚举。
    """
    url = f"{base_url.rstrip('/')}/object_info/{node_name}"
    data = _http_get_json(url, timeout)
    if not isinstance(data, dict) or node_name not in data:
        raise ComfyModelListError(f"ComfyUI 里没有节点 {node_name}")
    node = data[node_name] or {}
    required = ((node.get("input") or {}).get("required") or {})
    slot = required.get(input_name)
    if not isinstance(slot, (list, tuple)) or not slot:
        raise ComfyModelListError(
            f"节点 {node_name} 没有 {input_name} 输入槽")
    options = slot[0]
    if not isinstance(options, list):
        raise ComfyModelListError(
            f"节点 {node_name}.{input_name} 不是枚举（类型 "
            f"{type(options).__name__}）")
    return [str(x) for x in options]


def names_present(names: Iterable[str], wanted: Iterable[str]) -> dict[str, bool]:
    """wanted 里每个名字是否出现在 names 里。

    返回 {name: bool}，用来回答「ComfyUI 的下拉框里到底有没有这个东西」。
    """
    pool = set(names)
    return {name: (name in pool) for name in wanted}


# ------------------------------------------------------------------
# 异步版（**服务端必须用这个**）
# ------------------------------------------------------------------
#
# 同步版用 urllib，在 aiohttp 事件循环里会阻塞整个服务。
# 而 ComfyUI 的 /models/diffusion_models 首次调用实测要 43 秒
# （它要递归扫所有 extra_model_paths 目录）—— 阻塞 43 秒不可接受。


async def _aget_json(session, url: str) -> object:
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                raise ComfyModelListError(
                    f"ComfyUI 返回 HTTP {resp.status}（{url}）")
            raw = await resp.read()
    except ComfyModelListError:
        raise
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ComfyModelListError(
            f"连不上 ComfyUI（{url}）：{type(exc).__name__}: {exc}") from exc
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise ComfyModelListError(
            f"ComfyUI 返回的不是合法 JSON（{url}）") from exc


async def fetch_model_list_async(session,
                                 base_url: str,
                                 folder: str = "diffusion_models",
                                 *,
                                 timeout: float = DEFAULT_TIMEOUT) -> list[str]:
    """异步版 `fetch_model_list`（用已有 aiohttp session）。

    注意：**不走本模块的本地缓存** —— 缓存由上层
    `video_models.refresh_comfy_snapshot_async` 统一管（快照级缓存），
    避免两套 TTL 语义打架。
    """
    if folder not in COMFY_MODEL_FOLDERS:
        raise ComfyModelListError(
            f"未知的模型目录 {folder}（ComfyUI 实测只暴露 "
            f"{'、'.join(COMFY_MODEL_FOLDERS)}；`unet` 是旧别名，HTTP 层不认）")
    url = f"{base_url.rstrip('/')}/models/{folder}"
    data = await _aget_json(session, url)
    if not isinstance(data, list) or not all(isinstance(x, str) for x in data):
        raise ComfyModelListError(
            f"ComfyUI {url} 返回的不是字符串数组（类型 {type(data).__name__}）")
    return list(data)


async def fetch_object_info_enum_async(session,
                                       base_url: str,
                                       node_name: str,
                                       input_name: str = "unet_name",
                                       *,
                                       timeout: float = DEFAULT_TIMEOUT) -> list[str]:
    """异步版 `fetch_object_info_enum`。"""
    url = f"{base_url.rstrip('/')}/object_info/{node_name}"
    data = await _aget_json(session, url)
    if not isinstance(data, dict) or node_name not in data:
        raise ComfyModelListError(f"ComfyUI 里没有节点 {node_name}")
    node = data[node_name] or {}
    required = ((node.get("input") or {}).get("required") or {})
    slot = required.get(input_name)
    if not isinstance(slot, (list, tuple)) or not slot:
        raise ComfyModelListError(
            f"节点 {node_name} 没有 {input_name} 输入槽")
    options = slot[0]
    if not isinstance(options, list):
        raise ComfyModelListError(
            f"节点 {node_name}.{input_name} 不是枚举（类型 "
            f"{type(options).__name__}）")
    return [str(x) for x in options]
