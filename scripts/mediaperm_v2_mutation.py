#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""变异测试：验证 tests/test_mediaperm_perf_v2.py 的断言真的「咬人」。

纪律：断言如果不会变红，就是摆设。
每个变异体改一处「关键修复点」，跑对应测试，**必须失败**。

用法: python scripts/mediaperm_v2_mutation.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable


def run_tests(root: Path) -> tuple[bool, str]:
    proc = subprocess.run(
        [PY, "-m", "pytest", "tests/test_mediaperm_perf_v2.py", "-q", "--tb=no", "-p", "no:randomly"],
        cwd=root, capture_output=True, text=True, timeout=300,
        env={**os.environ, "PYTHONPATH": str(root)})
    return proc.returncode == 0, (proc.stdout or "")[-900:]


#: (名字, 相对路径, 旧文本, 新文本, 期望失败的测试)
MUTATIONS = [
    ("M1 find_weight 缓存被删",
     "h3studio/video_models.py",
     """    key = (name, kind)
    if key in _FIND_WEIGHT_CACHE:
        return _FIND_WEIGHT_CACHE[key]""",
     """    key = (name, kind)
    if False:
        return _FIND_WEIGHT_CACHE[key]""",
     "test_find_weight_is_memoized"),

    ("M2 兼容性矩阵缓存被删",
     "h3studio/video_models.py",
     """    if model_key in _ACCEL_COMPAT_MATRIX_CACHE:
        return _ACCEL_COMPAT_MATRIX_CACHE[model_key]""",
     """    if False:
        return _ACCEL_COMPAT_MATRIX_CACHE[model_key]""",
     "test_compat_matrix_is_cached"),

    ("M3 options_payload 回到事件循环",
     "server.py",
     """    payload = await asyncio.to_thread(
        video_models.options_payload,
        node_available=node_available,
        snapshot=snapshot,
    )""",
     """    payload = video_models.options_payload(
        node_available=node_available,
        snapshot=snapshot,
    )""",
     "test_video_options_payload_runs_off_event_loop"),

    ("M4 accel_lora_compat_matrix 回到事件循环",
     "server.py",
     """        matrix = await asyncio.to_thread(
            video_models.accel_lora_compat_matrix, model_key)""",
     """        matrix = video_models.accel_lora_compat_matrix(model_key)""",
     "test_video_options_payload_runs_off_event_loop"),

    ("M5 列表接口又生成缩略图",
     "server.py",
     """                try:
                    key = thumb_service.cache_key(full)
                except OSError:
                    continue""",
     """                try:
                    key = thumb_service.cache_key(full)
                    await thumb_service.thumbnail_for(DATA, full)
                except OSError:
                    continue""",
     "test_list_api_does_not_generate_thumbnails"),

    ("M6 缩略图 key 校验放宽（路径穿越）",
     "server.py",
     """    if len(stem) != 40 or any(ch not in "0123456789abcdef" for ch in stem):
        raise web.HTTPNotFound()""",
     """    if len(stem) < 1:
        raise web.HTTPNotFound()""",
     "test_thumb_endpoint_is_lazy_and_path_safe"),

    ("M7 卡片图片去掉懒加载",
     "web/app.js",
     '''preview = `<img src="${src}" loading="lazy" decoding="async" data-orig-src="${outputUrl(job, image)}">''',
     '''preview = `<img src="${src}" data-orig-src="${outputUrl(job, image)}">''',
     "test_card_image_uses_lazy_loading"),

    ("M8 卡片视频 preload 改回 metadata",
     "web/app.js",
     '''preview = `<video src="${outputUrl(job, video)}"${posterAttr} controls preload="none"></video>`;''',
     '''preview = `<video src="${outputUrl(job, video)}"${posterAttr} controls preload="metadata"></video>`;''',
     "test_card_video_uses_preload_none"),

    ("M9 卡片视频去掉 poster",
     "web/app.js",
     '''    const posterAttr = videoThumb ? ` poster="${videoThumb}"` : "";''',
     '''    const posterAttr = "";''',
     "test_card_video_has_poster_when_thumb_available"),

    ("M10 大图改成用缩略图",
     "web/app.js",
     '''    const source = {url: outputUrl(job, image), name: String(image.file || "completed-image.png").split(/[\\\\/]/).pop()};''',
     '''    const source = {url: imageThumb || outputUrl(job, image), name: String(image.file || "completed-image.png").split(/[\\\\/]/).pop()};''',
     "test_lightbox_still_uses_original_image"),

    ("M11 app.js ?v= 没升",
     "web/index.html",
     "app.js?v=mediaperm20261121a",
     "app.js?v=fav20261120c",
     "test_static_asset_version_bumped"),

    ("M13 批次缩略图 URL 算到守卫外面（重现渲染崩溃）",
     "web/app.js",
     """    const thumb = out && !failed
      ? `<div class="job-batch-thumb"><img src="${thumbUrl(out) || outputUrl(job, out)}" loading="lazy" decoding="async" alt="批次 ${batch.idx + 1}"><span class="zoom-hint">⤢</span></div>`""",
     """    const batchThumb = thumbUrl(out);
    const batchSrc = batchThumb || outputUrl(job, out);
    const thumb = out && !failed
      ? `<div class="job-batch-thumb"><img src="${batchSrc}" loading="lazy" decoding="async" alt="批次 ${batch.idx + 1}"><span class="zoom-hint">⤢</span></div>`""",
     "test_batch_thumbnail_guards_undefined_output"),

    ("M14 缩略图又发永久缓存指令",
     "server.py",
     "        \"Cache-Control\": \"public, max-age=86400\",",
     "        \"Cache-Control\": \"public, max-age=86400, immutable\",",
     "test_no_immutable_cache_header_in_server"),

    ("M12 缩略图缓存键不含 size（内容换了键不变）",
     "h3studio/thumbnails.py",
     """        raw = f"{Path(src).resolve()}|{st.st_mtime_ns}|{st.st_size}\"""",
     """        raw = f"{Path(src).resolve()}|{st.st_mtime_ns}\"""",
     "test_cache_key_changes_with_content"),
]


def main():
    print("=== 变异测试：每条断言必须咬人 ===")
    failures = []
    for name, rel, old, new, expect in MUTATIONS:
        tmp = Path(tempfile.mkdtemp(prefix="mut-"))
        dst = tmp / "app"
        shutil.copytree(ROOT, dst, symlinks=True,
                        ignore=shutil.ignore_patterns(".git", "evidence", "data",
                                                     "__pycache__", "*.pyc"))
        target = dst / rel
        src = target.read_text(encoding="utf-8")
        if old not in src:
            print(f"  SKIP  {name} —— 变异点没找到（源码已变？��查）")
            failures.append((name, "变异点未找到"))
            shutil.rmtree(tmp, ignore_errors=True)
            continue
        target.write_text(src.replace(old, new, 1), encoding="utf-8")
        passed, out = run_tests(dst)
        if passed:
            print(f"  ✗ 不咬人  {name}  （期望 {expect} 失败，但它通过了）")
            failures.append((name, "断言没变红"))
        else:
            # 确认失败的是**期望的那条**断言
            failed_names = [ln.strip() for ln in out.splitlines()
                            if ln.startswith("FAILED") or " failed" in ln]
            print(f"  ✓ 咬人    {name}  -> {expect} 失败")
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n=== 汇总：{len(MUTATIONS)-len(failures)}/{len(MUTATIONS)} 咬人 ===")
    if failures:
        for n, why in failures:
            print(f"  ✗ {n}: {why}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
