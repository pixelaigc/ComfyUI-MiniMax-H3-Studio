#!/usr/bin/env python3
"""变异测试 —— 证明 tests/test_reqstorm_fix.py 的断言**真的会红**。

【为什么必须做这一步】
一个「全绿」的测试套件，如果它的断言是**空转**的（抓不住任何东西），
那它证明不了任何事 —— 哪怕 27/27 全绿。

【方法】
逐个把被测代码**改坏**（变异），跑测试，断言**必须变红**，
然后**还原**。如果某个变异后测试仍全绿 -> 那条断言是空的。

【红线】
* 每轮**先还原**再改（`git checkout --` 会丢未提交改动 ——
  所以这里用 `git stash` 的反面：直接用**原始内容**覆盖回去，
  改完再写回原始内容，全程不碰 git 索引）；
* 每个变异独立一轮，跑完立刻还原。

跑法：
    /home/ubuntu/comfyui/venv/bin/python tests/mutation_check_reqstorm.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEST = "tests/test_reqstorm_fix.py"
PY = "/home/ubuntu/comfyui/venv/bin/python"

#: (编号, 文件, 原始片段, 变异片段, 说明)
MUTATIONS = [
    ("M01-cache-off",
     "h3studio/comfy_client.py",
     "        hit, value = cache_get(key, cache_ttl)\n        if hit:\n            return value",
     "        hit, value = cache_get(key, cache_ttl)\n        if False:\n            return value",
     "缓存永不命中 -> 每次都打上游"),

    ("M02-singleflight-off",
     "h3studio/comfy_client.py",
     "    existing = _INFLIGHT.get(key)\n    if existing is not None:",
     "    existing = None\n    if existing is not None:",
     "单飞合并失效 -> N 个并发打 N 次上游"),

    ("M03-backoff-off",
     "h3studio/comfy_client.py",
     "        if backoff_remaining(bkey) > 0:\n            return None",
     "        if False:\n            return None",
     "失败后立刻重试（放大环）"),

    ("M04-failure-returns-empty-list",
     "h3studio/comfy_client.py",
     "        except Exception:  # noqa: BLE001\n            _record_failure(bkey)\n            return None\n        _record_success(bkey)\n        return data",
     "        except Exception:  # noqa: BLE001\n            _record_failure(bkey)\n            return {}\n        _record_success(bkey)\n        return data",
     "失败返回空字典 -> 会被误读成「队列为空」"),

    ("M05-hysteresis-off",
     "server.py",
     "    if streak >= COMFY_HEALTH_FAIL_THRESHOLD:\n        _COMFY_HEALTH[\"ok\"] = False",
     "    if streak >= 1:\n        _COMFY_HEALTH[\"ok\"] = False",
     "一次失败即判未连接（顶栏闪烁）"),

    ("M06-threshold-lowered",
     "server.py",
     "COMFY_HEALTH_FAIL_THRESHOLD = 3",
     "COMFY_HEALTH_FAIL_THRESHOLD = 1",
     "迟滞阈值降到 1（等于没有迟滞）"),

    ("M07-sse-per-connection",
     "server.py",
     "                snap = await _status_stream_snapshot()",
     "                snap = await status_snapshot()",
     "SSE 回到「每连接各自取数」"),

    ("M08-queue-no-cache",
     "server.py",
     "        cache_ttl=comfy_client.QUEUE_TTL,\n        cache_key=\"queue\",\n        backoff_key=\"queue\")\n    if not isinstance(data, dict):\n        return None\n    return {\n        \"running\": len(data.get(\"queue_running\") or []),",
     "        cache_ttl=0,\n        cache_key=\"queue\",\n        backoff_key=\"queue\")\n    if not isinstance(data, dict):\n        return None\n    return {\n        \"running\": len(data.get(\"queue_running\") or []),",
     "/queue 缓存失效"),

    ("M09-yue2-fail-no-cache-write",
     "h3studio/yue2_schema.py",
     "            # 【关键】失败**也**标记缓存为新鲜（短 TTL）—— 杜绝立即重试。\n            _cache[\"at\"] = time.monotonic()\n            _cache[\"ttl\"] = OBJECT_INFO_FAIL_TTL",
     "            pass",
     "失败不写缓存 -> 每次失败都重拉 37.5MB"),

    ("M10-yue2-full-object-info-only",
     "h3studio/yue2_schema.py",
     "    try:\n        out = {}\n        style = _fetch_node_input(STYLE_NODE, timeout=timeout)",
     "    try:\n        raise RuntimeError('force full path')\n        out = {}\n        style = _fetch_node_input(STYLE_NODE, timeout=timeout)",
     "不再按节点名问 -> 回到 37.5MB 全量"),

    ("M11-frontend-forces-comfyui-ok",
     "web/app.js",
     "      if (typeof data.comfyui_ok === \"undefined\") {\n        const prev = statusState.applied || {};\n        data.comfyui_ok = prev.comfyui_ok;\n      }",
     "      data.comfyui_ok = true;",
     "前端又写死 comfyui_ok: true（抖动回归）"),

    ("M12-frontend-two-writers",
     "web/app.js",
     "    applyStatusSnapshot(r, \"poll\");",
     "    renderStatus(r);",
     "轮询绕过单一入口 -> 两个写回源"),

    ("M13-frontend-poll-scheduler-removed",
     "web/poll-scheduler.js",
     "      const delta = base * jitter * (Math.random() * 2 - 1);",
     "      const delta = 0;",
     "抖动被抹掉 -> 多标签页相位相同"),

    ("M14-frontend-no-backoff",
     "web/poll-scheduler.js",
     "        ? Math.min(intervalMs * Math.pow(2, failures), maxBackoff)",
     "        ? intervalMs",
     "退避被抹掉 -> 失败即立刻重试"),

    ("M15-frontend-no-visibility-pause",
     "web/poll-scheduler.js",
     "      if (typeof document !== \"undefined\" && document.hidden) {\n        timer = setTimeout(tick, intervalMs);\n        return;\n      }",
     "      if (false) {\n        timer = setTimeout(tick, intervalMs);\n        return;\n      }",
     "页面不可见时照轮询"),

    ("M16-api-system-new-session",
     "server.py",
     "    sysinfo = await comfy_client.get_json(\n        \"/system_stats\", timeout=3.0,",
     "    import aiohttp as _a\n    _s = _a.ClientSession()\n    async with _s.get(f\"{COMFY}/system_stats\") as _r:\n        sysinfo = await _r.json() if _r.status == 200 else None\n    _unused = comfy_client.get_json(\n        \"/system_stats\", timeout=3.0,",
     "/api/system 回到「每次新建 session」"),
]


def run_tests() -> tuple[int, str]:
    proc = subprocess.run(
        [PY, "-m", "pytest", TEST, "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=300,
        env={**__import__("os").environ, "H3STUDIO_DATA_DIR": "/tmp/h3storm-testdata"},
    )
    out = proc.stdout + proc.stderr
    # 解析 pytest 结尾的 "N failed, M passed"
    failed = passed = 0
    for line in reversed(out.splitlines()):
        if "passed" in line or "failed" in line or "error" in line:
            for part in line.replace(",", " ").split():
                if part == "failed":
                    failed = max(failed, 1)
                if part == "error":
                    failed = max(failed, 1)
            break
    if " failed" in out:
        import re
        m = re.search(r"(\d+) failed", out)
        if m:
            failed = int(m.group(1))
    if " passed" in out:
        import re
        m = re.search(r"(\d+) passed", out)
        if m:
            passed = int(m.group(1))
    return failed, out


def main() -> int:
    print("=" * 72)
    print("变异测试 —— 证明断言会红")
    print("=" * 72)

    baseline_failed, baseline_out = run_tests()
    if baseline_failed:
        print("!! 基线就红了，先修测试：")
        print(baseline_out[-3000:])
        return 2
    print(f"基线：全绿\n")

    originals: dict[str, str] = {}
    for _, rel, _, _, _ in MUTATIONS:
        p = ROOT / rel
        if rel not in originals:
            originals[rel] = p.read_text(encoding="utf-8")

    survivors = []
    try:
        for mid, rel, old, new, desc in MUTATIONS:
            p = ROOT / rel
            src = originals[rel]
            if old not in src:
                print(f"  {mid}  跳过（片段未找到，可能代码已变）: {rel}")
                survivors.append((mid, desc, "片段未找到"))
                continue
            p.write_text(src.replace(old, new, 1), encoding="utf-8")
            failed, out = run_tests()
            if failed:
                print(f"  {mid}  ✅ 变红（{failed} 个失败）  {desc}")
            else:
                print(f"  {mid}  ❌ 仍全绿 —— 断言是空的！  {desc}")
                survivors.append((mid, desc, "仍全绿"))
            # 还原
            p.write_text(src, encoding="utf-8")
    finally:
        for rel, src in originals.items():
            (ROOT / rel).write_text(src, encoding="utf-8")

    print()
    print("=" * 72)
    if survivors:
        print(f"❌ {len(survivors)} 个变异存活（断言无效）：")
        for mid, desc, why in survivors:
            print(f"   {mid}: {desc}  ({why})")
        return 1
    print(f"✅ 全部 {len(MUTATIONS)} 个变异都被抓住 —— 断言有效")
    return 0


if __name__ == "__main__":
    sys.exit(main())
