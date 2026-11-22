# -*- coding: utf-8 -*-
"""
端到端实测：三条能力（图像 / 视频 / 音频）各跑一次真实 hermes 增强。

【实测口径】
  * 调真实的 hermes CLI（/usr/local/bin/hermes），真实 profile（media）
  * 记录：实际命令、hermes 原始返回、提取出的增强提示词、耗时
  * 产物：本脚本 + JSON 证据文件

【不做】
  * 不跑全量 pytest
  * 不碰线上 8190 / 线上 jobs.db（本脚本直接调模块，不起服务）
  * 不往用户媒体库写卡片
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

REPO = Path("/root/projects/h3studio-prompt-enhance")
sys.path.insert(0, str(REPO))

from h3studio import hermes_enhance as he  # noqa: E402

OUT = Path("/root/.hermes-web-ui/.ekko/workspace/default/"
           "person-main-505e35f9-2826-4d4c-af53-d3dc5de6f957/"
           "evidence-hermes-enhance-20261122")
OUT.mkdir(parents=True, exist_ok=True)

SAVED = {
    "hermes": {
        "enabled": True,
        "binary": "/usr/local/bin/hermes",
        "profile": "media",
        "timeout": 900,
    }
}

CASES = [
    ("image", "一只橘猫坐在窗台上，午后阳光斜照进来"),
    ("video", "一个少女在雨中的霓虹街道上奔跑，镜头从背后跟拍到正面特写，6 秒"),
    ("audio", "一段适合深夜驾驶的电子音乐，节奏舒缓，情绪略带忧郁"),
]


async def main():
    evidence = {"cases": [], "config": he.resolve_config(SAVED)}
    for media, prompt in CASES:
        print(f"\n=== [{media}] 开始 ===", flush=True)
        spec = he.get_media_spec(media, SAVED)
        query = he.build_query(spec, prompt, duration_seconds=6 if media == "video" else None)
        qfile = OUT / f"query-{media}.txt"
        qfile.write_text(query, encoding="utf-8")

        t0 = time.time()
        err = None
        result = None
        try:
            result = await he.enhance(prompt, media, saved=SAVED,
                                       duration_seconds=6 if media == "video" else None,
                                       validate=False)
        except he.HermesEnhanceError as exc:
            err = {"reason": exc.reason, "detail": exc.detail, "message": str(exc)}
        elapsed = round(time.time() - t0, 1)

        rec = {
            "media": media,
            "original_prompt": prompt,
            "profile": spec.get("profile"),
            "skills": spec.get("skills"),
            "system_prompt": spec.get("system_prompt"),
            "user_template": spec.get("user_template"),
            "query_file": str(qfile),
            "query_bytes": qfile.stat().st_size,
            "elapsed_sec": elapsed,
            "error": err,
        }
        if result is not None:
            rec.update({
                "ok": result.ok,
                "attempts": result.attempts,
                "command": result.command,
                "enhanced": result.enhanced,
                "enhanced_len": len(result.enhanced or ""),
                "raw": result.raw,
            })
            (OUT / f"enhanced-{media}.txt").write_text(result.enhanced, encoding="utf-8")
            print(f"  ok  {len(result.enhanced or '')} 字 / {elapsed}s / "
                  f"attempts={result.attempts}", flush=True)
            print(f"  head: {result.enhanced[:160]}", flush=True)
        else:
            print(f"  FAIL {err}", flush=True)
        evidence["cases"].append(rec)

    (OUT / "evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n证据已写：{OUT / 'evidence.json'}")
    return evidence


if __name__ == "__main__":
    asyncio.run(main())
