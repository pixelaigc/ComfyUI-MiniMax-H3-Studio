#!/usr/bin/env python3
"""变异测试：逐个把修复改坏，验证对应断言**真的变红**。

纪律：
  * 每个变异 = 改一处 -> 跑相关断言 -> 必须红 -> 还原。
  * 变异测试自身**不改**仓库文件：全部在**临时副本**上做。
  * 变异体必须是「常量巧合抓不到」的改动。
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path("/root/projects/h3s-perf-fix")
PY = "/home/ubuntu/comfyui/venv/bin/python"

# (变异名, 目标文件, 原字符串, 替换字符串, 期望失败的断言子串)
MUTATIONS = [
    ("M1 显示函数改回直接调扫盘",
     "h3studio/job_meta.py",
     "        mapping = display_map if display_map is not None else _effect_lora_display_map()\n"
     "        hit = mapping.get(name)\n"
     "        if hit:\n"
     "            return hit",
     "        from . import h3_effect_loras as effect_registry\n"
     "        for entry in effect_registry.discover_effect_loras():\n"
     "            if entry['name'] == name:\n"
     "                return entry.get('display') or name",
     "test_effect_lora_display_does_not_call_discover_directly"),

    ("M2 白名单改回直接调扫盘",
     "h3studio/job_meta.py",
     "        if text in _effect_lora_display_map():\n"
     "            return True",
     "        from . import h3_effect_loras as effect_registry\n"
     "        for entry in effect_registry.discover_effect_loras():\n"
     "            if entry['name'] == text:\n"
     "                return True",
     "test_in_lora_registry_does_not_call_discover_directly"),

    ("M3 映射不记忆化（每次重建）",
     "h3studio/job_meta.py",
     "    if (cached is not None and (now - at) < _EFFECT_LORA_DISPLAY_TTL\n"
     "            and _EFFECT_LORA_DISPLAY_CACHE.get(\"gen\") == gen):\n"
     "        return cached",
     "    if False:\n"
     "        return cached",
     "test_display_map_is_memoized"),

    ("M4 预热改成直接同步调用（回到事件循环）",
     "server.py",
     "    await asyncio.to_thread(job_meta.warm_effect_lora_display_map)",
     "    job_meta.warm_effect_lora_display_map()",
     "test_api_list_warms_map_via_to_thread"),

    ("M5 预热挪到 row_to_job 之后（顺序错）",
     "server.py",
     "    await asyncio.to_thread(job_meta.warm_effect_lora_display_map)\n"
     "    # 【2026-11-21 性能修】开启血缘的**请求级**缓存 ——\n"
     "    # 同一次请求里同一个 job_id 的血缘只回溯一次（见 _LINEAGE_CACHE）。\n"
     "    _LINEAGE.begin()\n"
     "    try:\n"
     "        jobs = []\n"
     "        for r in rows:\n"
     "            job = row_to_job(r)\n"
     "            jobs.append(job)\n"
     "    finally:\n"
     "        _LINEAGE.end()",
     "    # 【2026-11-21 性能修】开启血缘的**请求级**缓存 ——\n"
     "    # 同一次请求里同一个 job_id 的血缘只回溯一次（见 _LINEAGE_CACHE）。\n"
     "    _LINEAGE.begin()\n"
     "    try:\n"
     "        jobs = []\n"
     "        for r in rows:\n"
     "            job = row_to_job(r)\n"
     "            jobs.append(job)\n"
     "    finally:\n"
     "        _LINEAGE.end()\n"
     "    await asyncio.to_thread(job_meta.warm_effect_lora_display_map)",
     "test_api_list_warms_map_via_to_thread"),

    ("M6 /history 不缓存（查过也重查）",
     "server.py",
     "    cached = _HISTORY_PRESENT_CACHE.get(prompt_id)\n"
     "    if cached is not None:\n"
     "        at, present = cached\n"
     "        if present:\n"
     "            # 出现过就**永久**缓存 —— ComfyUI 的 history 不删条目。\n"
     "            return True\n"
     "        if (time.time() - at) < _HISTORY_MISS_TTL:\n"
     "            return False",
     "    cached = None",
     "test_history_present_is_cached"),

    ("M7 /history miss 也永久缓存（任务永远显示运行中）",
     "server.py",
     "        if (time.time() - at) < _HISTORY_MISS_TTL:\n"
     "            return False",
     "        return False",
     "test_history_miss_is_short_cached"),

    ("M8 /history 异常被缓存成 False",
     "server.py",
     "    except Exception:\n"
     "        # 读不到（ComfyUI 超时 / 不可达）-> 不写缓存（下次重试）。\n"
     "        return False\n"
     "    present = bool(hist)",
     "    except Exception:\n"
     "        _HISTORY_PRESENT_CACHE[prompt_id] = (time.time(), False)\n"
     "        return False\n"
     "    present = bool(hist)",
     "test_history_exception_is_not_cached"),

    ("M9 批次改回每条查一次（N+1）",
     "server.py",
     "    _bm = batches_map([j[\"id\"] for j in jobs])\n"
     "    for job in jobs:\n"
     "        job[\"batches\"] = _bm.get(job[\"id\"], [])",
     "    for job in jobs:\n"
     "        job[\"batches\"] = batches_of(job[\"id\"])",
     "test_batches_sql_count_is_one"),

    ("M10 血缘回溯重新查本段 params（多一次查询）",
     "server.py",
     "            if current == job_id and own_params is not None:\n"
     "                params = own_params\n"
     "            else:\n"
     "                row = conn.execute(\"SELECT params FROM jobs WHERE id=?\",\n"
     "                                   (current,)).fetchone()\n"
     "                if not row:\n"
     "                    break\n"
     "                try:\n"
     "                    params = json.loads(row[\"params\"] or \"{}\")\n"
     "                except (TypeError, ValueError, json.JSONDecodeError):\n"
     "                    break",
     "            row = conn.execute(\"SELECT params FROM jobs WHERE id=?\",\n"
     "                               (current,)).fetchone()\n"
     "            if not row:\n"
     "                break\n"
     "            try:\n"
     "                params = json.loads(row[\"params\"] or \"{}\")\n"
     "            except (TypeError, ValueError, json.JSONDecodeError):\n"
     "                break",
     "test_lineage_reuses_caller_params"),
]


def run_tests(workdir: Path, test_filter: str = "test_mediaperm_perf.py"):
    return subprocess.run(
        [PY, "-m", "pytest", f"tests/{test_filter}", "-q", "--tb=line",
         "-p", "no:randomly"],
        cwd=workdir, capture_output=True, text=True, timeout=600)


def main():
    results = []
    for name, rel, old, new, expect_fail in MUTATIONS:
        tmp = Path(tempfile.mkdtemp(prefix="h3mut-"))
        work = tmp / "repo"
        shutil.copytree(ROOT, work, symlinks=True,
                        ignore=shutil.ignore_patterns(".git", "__pycache__",
                                                      "data", "evidence"))
        target = work / rel
        src = target.read_text(encoding="utf-8")
        if old not in src:
            results.append({"mutation": name, "result": "SKIP(原串未找到)",
                            "expect_fail": expect_fail})
            shutil.rmtree(tmp, ignore_errors=True)
            continue
        target.write_text(src.replace(old, new, 1), encoding="utf-8")
        proc = run_tests(work)
        out = proc.stdout + proc.stderr
        failed = [ln for ln in out.splitlines()
                  if ln.startswith("FAILED") or " failed" in ln]
        n_failed = out.count("FAILED ")
        passed_line = [ln for ln in out.splitlines() if " passed" in ln]
        if expect_fail is None:
            verdict = ("OK(无预期失败断言)" if n_failed == 0
                       else "NOTE(有失败:%d)" % n_failed)
            ok = None
        else:
            ok = expect_fail in out
            verdict = "PASS(断言变红)" if ok else "FAIL(断言没红!)"
        results.append({
            "mutation": name,
            "result": verdict,
            "expect_fail": expect_fail,
            "n_failed": n_failed,
            "tail": failed[:3] or passed_line[:1],
        })
        print("### %-42s %s" % (name, verdict))
        for ln in (failed[:3] or passed_line[:1]):
            print("      ", ln[:150])
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n=== 变异测试汇总 ===")
    for r in results:
        print("  %-42s %s" % (r["mutation"], r["result"]))
    bad = [r for r in results
           if r["expect_fail"] and r["result"].startswith("FAIL")]
    print("\n咬人失败（断言没变红）的变异：%d 条" % len(bad))
    for r in bad:
        print("   ", r["mutation"])
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
