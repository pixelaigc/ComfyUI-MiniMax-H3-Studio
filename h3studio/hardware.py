# -*- coding: utf-8 -*-
"""硬件信息采集 —— CPU 型号识别 + NVIDIA 显卡功耗上限。

设计原则：
1. 纯函数 + 无 aiohttp 依赖，方便单元测试（所有外部读数都可注入）。
2. CPU 名称必须可辨识：多级来源逐级降级，最后兜底到「架构 + 核心数」，
   绝不返回「未知 CPU」。
3. 功耗上限安全约束：**默认不允许提高**功耗上限；下调随时允许；
   提高需要调用方显式传 allow_raise=True（前端必须二次确认）。
4. 任何不支持的分支都必须带明确 reason，不允许静默失败。
"""
import os
import platform
import subprocess
from pathlib import Path

# Windows 专有的 creationflags，POSIX 上不存在
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

CPUINFO_PATH = "/proc/cpuinfo"

# /proc/cpuinfo 里可能承载型号的字段（x86 "model name"，ARM "Processor"/"Hardware"）
_CPU_MODEL_KEYS = (
    "model name",
    "hardware",
    "cpu model",
    "processor",
    "processor name",
    "cpu",
    "cpu part",
    "cpu variant",
    "machine",
)

# lscpu 中可能是型号的 key（英文 + 常见本地化）
_LSCPU_MODEL_KEYS = ("model name", "型号名称", "型号名", "Modelo del CPU")

# 兜底猜测用的厂商特征串
_VENDOR_TOKENS = (
    "intel", "amd", "ryzen", "epyc", "athlon", "core(tm)", "core(tm)",
    "apple", "arm", "cortex", "qualcomm", "snapdragon", "rockchip",
    "allwinner", "amlogic", "marvell", "broadcom", "nvidia", "xeon",
    "pentium", "celeron", "i5", "i7", "i9", "m1", "m2", "m3", "m4",
)


# ------------------------------------------------------------------ 底层命令
def _run_cmd(cmd, timeout=6, env_extra=None):
    """执行外部命令，返回 (returncode, stdout, stderr)。不抛异常。"""
    env = os.environ.copy()
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    if env_extra:
        env.update(env_extra)
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            env=env, creationflags=CREATE_NO_WINDOW)
        return proc.returncode, (proc.stdout or ""), (proc.stderr or "")
    except FileNotFoundError:
        return 127, "", f"命令不存在: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", f"命令超时: {' '.join(cmd)}"
    except Exception as exc:  # pragma: no cover - 防御性
        return 1, "", str(exc)


# ------------------------------------------------------------------ CPU 解析
def parse_cpuinfo_models(text):
    """解析 /proc/cpuinfo 内容 → [(型号名, 该型号的逻辑核数), ...]（按出现顺序）。

    - x86：每个 processor 块一条 "model name"，合并同型号计数。
    - ARM：没有 "model name"，用 "Processor"/"Hardware"/"Model" 字段。
    - "processor : 0" 这种序号字段会被跳过（纯数字）。
    """
    models = []
    index = {}
    for block in (text or "").split("\n\n"):
        block = block.strip()
        if not block:
            continue
        name = None
        for line in block.splitlines():
            key, val = _split_kv(line)
            if key is None:
                continue
            key = key.lower()
            if not val or key not in _CPU_MODEL_KEYS:
                continue
            if key in ("processor", "cpu") and val.isdigit():
                continue          # x86 的 processor / cpu 是序号或数量，不是型号
            name = val
            break
        if not name:
            continue
        if name in index:
            models[index[name]][1] += 1
        else:
            index[name] = len(models)
            models.append([name, 1])
    return [(n, c) for n, c in models]


def parse_cpuinfo_vendor(text):
    """从 /proc/cpuinfo 取 vendor_id（x86 有，ARM 一般没有）。"""
    for line in (text or "").splitlines():
        key, val = _split_kv(line)
        if key is not None and key.lower() == "vendor_id" and val:
            return val
    return None


def _split_kv(line):
    """拆 "key: value" / "key：value"（lscpu 本地化后用全角冒号）。"""
    for sep in (":", "\uff1a"):
        key, found, val = line.partition(sep)
        if found:
            return key.strip(), val.strip()
    return None, None


def parse_lscpu_model(text):
    """从 lscpu 文本里取型号名（兼容英文与常见本地化 key）。"""
    for line in (text or "").splitlines():
        key, val = _split_kv(line)
        if key is None:
            continue
        k = key.lower()
        if not val:
            continue
        if k in _LSCPU_MODEL_KEYS or k.endswith("model name"):
            return val
    # 兜底：任何一行看着像 CPU 型号（包含厂商特征串）
    for line in (text or "").splitlines():
        _key, val = _split_kv(line)
        if val is None:
            continue
        low = val.lower()
        if len(val) >= 5 and any(tok in low for tok in _VENDOR_TOKENS):
            return val
    return None


def _read_text(path):
    try:
        return Path(path).read_text(errors="replace")
    except OSError:
        return None


def _cpu_name_windows():
    """Windows：读注册表 ProcessorNameString。"""
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                             r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
        name, _ = winreg.QueryValueEx(key, "ProcessorNameString")
        return (name or "").strip() or None
    except Exception:
        return None


def _cpu_name_macos():
    rc, out, _ = _run_cmd(["sysctl", "-n", "machdep.cpu.brand_string"])
    if rc == 0:
        val = out.strip()
        return val or None
    return None


def _cpu_name_sysfs():
    """sysfs / device-tree：ARM 机器常用。"""
    for path in ("/sys/devices/virtual/cpu/model",
                 "/proc/device-tree/model",
                 "/sys/firmware/devicetree/base/model"):
        text = _read_text(path)
        if not text:
            continue
        val = text.replace("\x00", "").strip()
        if val:
            return val
    return None


def detect_cpu(cpuinfo_text=None, lscpu_text=None, platform_processor=None,
                machine=None, physical_cores=None, logical_cores=None):
    """返回结构化 CPU 信息。

    参数均为可注入的测试钩子；传 None 表示去读真实系统。
    返回 dict：
        name        显示用名称（异构时形如 "A ×12 + B ×12"）
        model       主型号名
        cores       物理核心数（取不到则为逻辑核数）
        threads     逻辑核数
        architecture 架构（x86_64 / aarch64 ...）
        vendor      厂商（可能为 None）
        source      型号来源（/proc/cpuinfo / lscpu / sysfs / ...）
        heterogeneous 是否大小核 / 多型号异构
        models      [{name, logical}] 各型号的逻辑核数
    """
    if machine is None:
        machine = platform.machine()
    if logical_cores is None:
        try:
            import psutil
            logical_cores = psutil.cpu_count(logical=True)
        except Exception:
            logical_cores = None
    if physical_cores is None:
        try:
            import psutil
            physical_cores = psutil.cpu_count(logical=False)
        except Exception:
            physical_cores = None
    if logical_cores is None:
        logical_cores = os.cpu_count() or 1
    if physical_cores is None:
        physical_cores = logical_cores

    if cpuinfo_text is None:
        cpuinfo_text = _read_text(CPUINFO_PATH)
    models = parse_cpuinfo_models(cpuinfo_text or "")
    vendor = parse_cpuinfo_vendor(cpuinfo_text or "")

    source = None
    model = None
    if models:
        model = models[0][0]
        source = CPUINFO_PATH
    else:
        if lscpu_text is None:
            rc, out, _ = _run_cmd(["lscpu"])
            lscpu_text = out if rc == 0 else ""
        model = parse_lscpu_model(lscpu_text)
        if model:
            source = "lscpu"
        else:
            model = _cpu_name_windows() or _cpu_name_macos() or _cpu_name_sysfs()
            if model:
                source = "sysfs/registry"

    if not model:
        if platform_processor is None:
            platform_processor = platform.processor()
        if platform_processor and platform_processor.strip():
            model = platform_processor.strip()
            source = "platform.processor()"

    heterogeneous = len(models) > 1
    if models:
        detail = " + ".join(f"{n} ×{c}" for n, c in models)
        name = detail
    elif model:
        name = model
    else:
        # 最终兜底：架构 + 核心数，绝不显示「未知」
        name = f"{machine or '未知架构'} 架构 · {logical_cores} 核"

    return {
        "name": name,
        "model": model or name,
        "cores": physical_cores,
        "threads": logical_cores,
        "architecture": machine,
        "vendor": vendor,
        "source": source or "fallback(arch+cores)",
        "heterogeneous": heterogeneous,
        "models": [{"name": n, "logical": c} for n, c in models],
    }


# ------------------------------------------------------------------ GPU 功耗上限
def _to_float(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.startswith("[") or text.lower() in ("n/a", "na", "none", "-"):
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def parse_power_limit_csv(text):
    """解析 nvidia-smi --query-gpu 的 CSV 行 → 结构化功耗信息列表。"""
    rows = []
    for line in (text or "").strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 6:
            continue
        try:
            index = int(parts[0])
        except (TypeError, ValueError):
            continue
        current = _to_float(parts[2])
        min_limit = _to_float(parts[3])
        max_limit = _to_float(parts[4])
        default_limit = _to_float(parts[5])
        supported = min_limit is not None and max_limit is not None and max_limit > 0
        reason = None
        if not supported:
            reason = "驱动未暴露可调功耗上限范围（Min/Max Power Limit 不可读，该卡或当前驱动不支持修改）"
        rows.append({
            "index": index,
            "name": parts[1],
            "current": current,
            "min": min_limit,
            "max": max_limit,
            "default": default_limit,
            "supported": supported,
            "reason": reason,
        })
    return rows


def query_power_limits():
    """读取全部 GPU 的功耗上限能力。

    返回 {"available": bool, "reason": str|None, "gpus": [...]}。
    available=False 时 reason 说明原因（nvidia-smi 缺失 / 无卡 / 驱动不支持）。
    """
    rc, out, err = _run_cmd([
        "nvidia-smi",
        "--query-gpu=index,name,power.limit,power.min_limit,power.max_limit,power.default_limit",
        "--format=csv,noheader,nounits"])
    if rc != 0:
        reason = (err or out or "").strip().splitlines()
        reason = reason[0] if reason else f"nvidia-smi 返回码 {rc}"
        if rc == 127:
            reason = "未安装 nvidia-smi，无法查询功耗上限"
        return {"available": False, "reason": reason, "gpus": []}
    gpus = parse_power_limit_csv(out)
    if not gpus:
        return {"available": False, "reason": "nvidia-smi 未返回任何 GPU", "gpus": []}
    return {"available": True, "reason": None, "gpus": gpus}


def query_gpus():
    """读取 GPU 实时状态（显存/利用率/温度/功耗/频率/驱动）+ 功耗上限能力。"""
    try:
        out = subprocess.check_output(
            ["nvidia-smi",
             "--query-gpu=index,name,memory.total,memory.used,memory.free,utilization.gpu,"
             "temperature.gpu,power.draw,power.limit,driver_version,clocks.current.graphics",
             "--format=csv,noheader,nounits"],
            encoding="utf-8", timeout=4, creationflags=CREATE_NO_WINDOW)
    except Exception:
        return []

    limits = {row["index"]: row for row in query_power_limits()["gpus"]}
    gpus = []
    for line in out.strip().splitlines():
        p = [x.strip() for x in line.split(",")]
        if len(p) < 9:
            continue
        try:
            index = int(p[0])
        except (TypeError, ValueError):
            index = len(gpus)
        info = limits.get(index, {})
        gpus.append({
            "index": index,
            "name": p[1],
            "vram_total": float(p[2]) * 1024 * 1024,
            "vram_used": float(p[3]) * 1024 * 1024,
            "vram_free": float(p[4]) * 1024 * 1024,
            "util": float(p[5]),
            "temp": float(p[6]),
            "power": float(p[7]),
            "power_limit": float(p[8]),
            "driver": p[9] if len(p) > 9 else "",
            "clock": p[10] if len(p) > 10 else "",
            "power_limit_info": {
                "current": info.get("current"),
                "min": info.get("min"),
                "max": info.get("max"),
                "default": info.get("default"),
                "supported": bool(info.get("supported")),
                "reason": info.get("reason"),
            },
        })
    return gpus


def set_power_limit(index, watts, allow_raise=False):
    """设置某张卡的功耗上限并回读校验。

    安全约束：
      - 目标值必须落在驱动给出的 [min, max] 区间内；
      - 目标值高于当前值（提高功耗）默认拒绝，除非 allow_raise=True；
      - 写入后必须用 nvidia-smi 读回并比对，不一致则视为失败。

    返回 dict：ok / index / requested / previous / current / verified / message。
    """
    try:
        index = int(index)
    except (TypeError, ValueError):
        return {"ok": False, "index": index, "verified": False,
                "message": "显卡序号无效"}
    try:
        watts = float(watts)
    except (TypeError, ValueError):
        return {"ok": False, "index": index, "verified": False,
                "message": "功耗上限必须是数值"}

    caps = query_power_limits()
    if not caps["available"]:
        return {"ok": False, "index": index, "verified": False,
                "message": f"无法设置功耗上限：{caps['reason']}"}

    target = None
    for row in caps["gpus"]:
        if row["index"] == index:
            target = row
            break
    if target is None:
        return {"ok": False, "index": index, "verified": False,
                "message": f"未找到序号为 {index} 的显卡"}
    if not target["supported"]:
        return {"ok": False, "index": index, "verified": False,
                "message": f"该显卡不支持修改功耗上限：{target['reason']}"}

    lo, hi = target["min"], target["max"]
    current = target["current"]
    if watts < lo or watts > hi:
        return {"ok": False, "index": index, "verified": False,
                "current": current,
                "message": f"目标 {watts:g} W 超出该卡可调范围 {lo:g}–{hi:g} W"}

    raising = current is not None and watts > current
    if raising and not allow_raise:
        return {"ok": False, "index": index, "verified": False, "current": current,
                "message": (f"拒绝提高功耗上限：当前 {current:g} W → 目标 {watts:g} W。"
                            f"默认只允许下调；如确需提高，请显式确认（allow_raise）。")}

    rc, out, err = _run_cmd(["nvidia-smi", "-i", str(index), "-pl", f"{watts:g}"], timeout=10)
    if rc != 0:
        msg = (err or out or "").strip().splitlines()
        msg = msg[0] if msg else f"nvidia-smi 返回码 {rc}"
        return {"ok": False, "index": index, "verified": False, "current": current,
                "message": f"设置失败：{msg}"}

    # 回读校验：写入成功不等于生效
    rc2, out2, _ = _run_cmd([
        "nvidia-smi", "--query-gpu=power.limit", "--format=csv,noheader,nounits",
        "-i", str(index)])
    actual = None
    if rc2 == 0:
        text = out2.strip().splitlines()
        if text:
            actual = _to_float(text[0])
    if actual is None:
        return {"ok": False, "index": index, "verified": False, "current": current,
                "message": "已下发设置但无法读回功耗上限，无法确认是否生效"}
    if abs(actual - watts) > 0.5:
        return {"ok": False, "index": index, "verified": False, "current": actual,
                "message": f"回读不一致：期望 {watts:g} W，实际 {actual:g} W"}
    return {"ok": True, "index": index, "requested": watts, "previous": current,
            "current": actual, "verified": True, "raised": bool(raising),
            "message": f"已设为 {actual:g} W（范围 {lo:g}–{hi:g} W）"}
