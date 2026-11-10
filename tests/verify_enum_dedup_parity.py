#!/usr/bin/env python3
"""P1-1 去重的**行为等价性**验证 —— 不只看结构，看实际输出。

【要证明什么】
  1. music_style.build_style_prompt(位置参数) 与
     yue2_schema.build_style_prompt(dict) 产出的字符串**完全一致**
     （覆盖纯器乐 / custom 语言 / custom 曲风 / 括号省略 等边界）
  2. music_style 的 re-export 与权威模块**是同一个对象**（不是副本）
  3. length_hint 映射：前端拿到的与权威 LENGTH_HINTS 一致
  4. 权威枚举只有一份：改权威源会同步影响所有引用方

运行：python3 tests/verify_enum_dedup_parity.py
"""
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from h3studio import music_style, yue2_schema   # noqa: E402

FIELDS = ["language", "genre", "mood", "vocal", "vocal_color", "instruments",
          "tempo_bpm", "energy", "length_hint", "extra",
          "custom_language", "custom_genre"]

#: 边界用例：纯器乐 / custom 语言 / custom 曲风 / 括号省略 / 空值 / BPM 非法
CASES = [
    ("普通", dict(language="Japanese", genre="piano pop", mood="bittersweet",
                  vocal="female vocal", vocal_color="breathy",
                  instruments="acoustic piano, sustained strings",
                  tempo_bpm=76, energy="builds from quiet to loud",
                  length_hint="full song (~3.5-5 min)", extra="雨夜告别",
                  custom_language="", custom_genre="")),
    ("纯器乐", dict(language="English", genre="ambient", mood="calm",
                    vocal="(instrumental / no lead vocal)", vocal_color="(none)",
                    instruments="felt piano", tempo_bpm=60, energy="low energy",
                    length_hint="model decides", extra="",
                    custom_language="", custom_genre="")),
    ("括号省略", dict(language="(custom / none)", genre="(custom / none)",
                      mood="(none)", vocal="(none)", vocal_color="(none)",
                      instruments="", tempo_bpm=0, energy="(none)",
                      length_hint="", extra="",
                      custom_language="", custom_genre="")),
    ("custom 语言与曲风", dict(language="(custom / none)", genre="(custom / none)",
                              mood="warm", vocal="male vocal", vocal_color="husky",
                              instruments="acoustic guitar", tempo_bpm=92,
                              energy="mid energy", length_hint="single edit (~2-3 min)",
                              extra="", custom_language="粤语", custom_genre="city pop")),
    ("BPM 非数字", dict(language="English", genre="jazz", mood="smooth",
                        vocal="female vocal", vocal_color="warm",
                        instruments="upright bass", tempo_bpm="abc",
                        energy="mid energy", length_hint="short clip (~45-75s)",
                        extra="", custom_language="", custom_genre="")),
    ("全空", dict(language="", genre="", mood="", vocal="", vocal_color="",
                  instruments="", tempo_bpm=0, energy="", length_hint="",
                  extra="", custom_language="", custom_genre="")),
]


def main():
    failures = []
    checks = []

    def check(ok, label, detail=""):
        checks.append((ok, label, detail))
        if not ok:
            failures.append(f"{label} :: {detail}")

    # ---- 1) 行为等价：位置参数封装 vs dict 权威实现 ----
    for name, style in CASES:
        positional = music_style.build_style_prompt(*[style[f] for f in FIELDS])
        authoritative = yue2_schema.build_style_prompt(dict(style))
        check(positional == authoritative,
              f"[等价] {name} 两条路径输出必须一致",
              f"wrapper={positional!r} authority={authoritative!r}")

    # ---- 2) re-export 必须是**同一个对象**（不是副本）----
    check(music_style.STATIC_ENUMS is yue2_schema.STATIC_ENUMS,
          "[同一对象] STATIC_ENUMS 是 re-export 而非副本",
          "identity 不同 -> 有一份拷贝")
    check(music_style.LENGTH_HINTS is yue2_schema.LENGTH_HINTS,
          "[同一对象] LENGTH_HINTS 是 re-export 而非副本")
    check(music_style.LYRIC_TEMPLATES is yue2_schema.LYRIC_TEMPLATES,
          "[同一对象] LYRIC_TEMPLATES 是 re-export 而非副本")
    check(music_style.STYLE_FIELDS is yue2_schema.STYLE_FIELDS,
          "[同一对象] STYLE_FIELDS 是 re-export 而非副本")
    check(music_style._omit is yue2_schema._omit,
          "[同一对象] _omit 是 re-export 而非副本")
    check(music_style.get_enums is yue2_schema.get_enums,
          "[同一对象] get_enums 是 re-export 而非副本")

    # ---- 3) length_hint 映射：面板下发的 == 权威的 ----
    opts = music_style.style_options()
    check(opts["length_hint_text"] == dict(yue2_schema.LENGTH_HINTS),
          "[下发一致] style_options().length_hint_text == 权威 LENGTH_HINTS",
          f"got={opts['length_hint_text']} want={dict(yue2_schema.LENGTH_HINTS)}")
    # 旧名也必须指向同一份
    check(music_style.LENGTH_HINT_TEXT is yue2_schema.LENGTH_HINTS,
          "[同一对象] 旧名 LENGTH_HINT_TEXT 仍指向权威映射")

    # ---- 4) 权威唯一：全仓只能有一份字面量（源码级）----
    py_files = list((REPO / "h3studio").glob("*.py"))
    dup_defs = {}
    for p in py_files:
        src = p.read_text(encoding="utf-8", errors="replace")
        for token in ("STATIC_ENUMS = {", "LENGTH_HINTS = {",
                      "LYRIC_TEMPLATES = {", "def _omit("):
            if token in src:
                dup_defs.setdefault(token, []).append(p.name)
    for token, files in dup_defs.items():
        check(len(files) == 1,
              f"[唯一权威] `{token}` 只应在一处定义",
              f"发现于 {files}")

    # ---- 5) 前端不得硬编码 length_hint 英文串 ----
    web_js = list((REPO / "web").glob("*.js"))
    offenders = []
    for p in web_js:
        src = p.read_text(encoding="utf-8", errors="replace")
        for lit in yue2_schema.LENGTH_HINTS.values():
            if lit and lit in src:
                offenders.append(f"{p.name}: {lit!r}")
    check(not offenders, "[前端不硬编码] 前端 JS 里不得出现 length_hint 英文描述字面量",
          "; ".join(offenders))

    # ---- 6) 前端必须从 /api/music-options 取 length_hint_text ----
    music_js = (REPO / "web" / "music.js").read_text(encoding="utf-8")
    check("length_hint_text" in music_js and "music-options" in music_js,
          "[前端取服务端] music.js 必须从 /api/music-options 读 length_hint_text")

    out = REPO / "evidence" / "enum-dedup-parity.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "cases": {name: {
            "wrapper": music_style.build_style_prompt(*[s[f] for f in FIELDS]),
            "authority": yue2_schema.build_style_prompt(dict(s)),
        } for name, s in CASES},
        "checks": [{"ok": ok, "label": lab, "detail": det} for ok, lab, det in checks],
        "passed": len(checks) - len(failures),
        "failed": len(failures),
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"=== 断言 {len(checks)-len(failures)} 通过 / {len(failures)} 失败 ===")
    for ok, lab, det in checks:
        print(("  PASS " if ok else "  FAIL ") + lab + ("" if ok else " :: " + det))
    print(f"证据: {out}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
