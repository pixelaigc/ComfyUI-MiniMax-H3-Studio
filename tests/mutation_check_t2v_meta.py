# -*- coding: utf-8 -*-
"""变异验证：证明 test_t2v_meta_consistency.py 的断言**真的会红**。

【为什么需要这个】
一套全绿的测试可能**什么都没测**。本文件对被测逻辑做定点变异，
每次只改一处，然后跑对应测试 —— 必须变红才算有效。

【变异纪律】
  * 每轮先 **restore**（git checkout -- <file>），改前先确认工作区干净；
    否则变异会污染下一轮。
  * 断言必须由「命名该行为的那条断言」抓住 —— 所以这里检查的是
    具体那条 testcase 变红，而不是「随便红一个」。
  * 测量点要挑**常量巧合抓不到**的位置：
    这里刻意用「strength 0.65」（不是 0.5）——
    0.5 在某些格式化路径下可能被四舍五入成同一个串。

运行：python3 tests/mutation_check_t2v_meta.py
"""
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = "/home/ubuntu/comfyui/venv/bin/python"
#: 两个测试文件都要跑 —— 前者测服务端摘要，后者测前端**真实执行**的行为。
#: 只跑前者会漏掉前端逻辑退化（第一版就是这么漏的：变异跑绿了）。
TESTS = ["tests/test_t2v_meta_consistency.py", "tests/test_t2v_card_dom.py"]

#: (变异名, 目标文件, 原串, 变异串, 必须变红的 testcase 名)
MUTATIONS = [
    (
        "摘要读错模型字段（退回遗留 model）",
        "h3studio/job_meta.py",
        '    model_key = str(params.get("model_key") or "").strip()\n'
        '    if not model_key:\n'
        '        return out\n',
        '    model_key = str(params.get("model") or "").strip()\n'
        '    if not model_key:\n'
        '        return out\n',
        "test_hybrid_model_and_dmad_lora",
    ),
    (
        "摘要吞掉用户选的加速 LoRA（恒为空）",
        "h3studio/job_meta.py",
        '        out["lora_label"] = _accel_lora_display(key)\n',
        '        out["lora_label"] = ""\n',
        "test_hybrid_model_and_dmad_lora",
    ),
    (
        "摘要用遗留 lora 字段代替用户勾选的加速 LoRA",
        "h3studio/job_meta.py",
        '    for item in params.get("accel_loras") or []:\n',
        '    _legacy_items = ([{"key": params.get("lora"), "strength": 1.0}]\n'
        '                     if params.get("lora") else [])\n'
        '    for item in _legacy_items:\n',
        "test_summary_never_reports_legacy_lora_key",
    ),
    (
        "摘要丢掉 LoRA 强度（恒为 None）",
        "h3studio/job_meta.py",
        '        out["lora_strength"] = strength\n',
        '        out["lora_strength"] = None\n',
        "test_strength_is_preserved",
    ),
    (
        "摘要无视 enabled=False（把停用的 LoRA 也算上）",
        "h3studio/job_meta.py",
        '        if item.get("enabled") is False:\n'
        '            continue\n'
        '        key = str(item.get("key") or "").strip()\n',
        '        key = str(item.get("key") or "").strip()\n',
        "test_disabled_accel_lora_is_ignored",
    ),
    (
        "前端卡片不再读 model_info（退回读遗留字段）",
        "web/app.js",
        "function jobModelInfo(job) {\n"
        '  const p = (job && job.params) || {};\n'
        '  const info = (job && job.model_info) || null;\n'
        '  if (info && (info.model_label || info.lora_label || info.model_key)) return info;\n'
        "  return localModelInfo(job.mode || \"\", p);\n"
        "}\n",
        "function jobModelInfo(job) {\n"
        '  const p = (job && job.params) || {};\n'
        "  return localModelInfo(job.mode || \"\", p);\n"
        "}\n",
        "test_model_row_shows_selected_model_not_legacy_lora",
    ),
    (
        "前端删掉 LoRA 行",
        "web/app.js",
        '<span>LoRA</span>',
        '<span>加速权重</span>',
        "test_card_has_lora_row",
    ),
    (
        "前端把「未选 LoRA」的显示改成编一个名字",
        "web/app.js",
        '  } else {\n'
        '    // 用户没额外选加速 LoRA —— 如实显示「空」（不是留白，也不是编一个名字）\n'
        '    parts.push("空");\n'
        "  }\n",
        "  }\n",
        "test_no_accel_lora_shows_empty_marker",
    ),
    (
        "前端提交层又把遗留 lora 写回所有模式",
        "web/app.js",
        '  if (mode === "flf" || mode === "r2v") {\n'
        '    params.lora = state.model === "lora" ? state.lora : "none";\n'
        "    params.lora_strength = state.loraStrength;\n"
        "  }\n",
        '  params.lora = state.model === "lora" ? state.lora : "none";\n'
        "  params.lora_strength = state.loraStrength;\n",
        "test_legacy_lora_only_in_flf_r2v_branch",
    ),
    (
        "前端本地兜底退回读遗留字段（复现原始 bug 的形状）",
        "web/app.js",
        '    out.model_key = p.model_key;\n'
        '    out.model_label = entry ? (entry.label || p.model_key) : p.model_key;\n',
        '    out.model_key = p.model || "";\n'
        '    out.model_label = p.model ? `H3 + ${p.lora || "ckpt850"}` : (p.model_key || "");\n',
        "test_falls_back_to_local_summary_when_server_silent",
    ),
]


def _read_bytes(path: Path) -> bytes:
    return path.read_bytes()


def _write_bytes(path: Path, data: bytes) -> None:
    """**按字节**写回。

    【为什么不能用 write_text】write_text 走的是文本模式 + universal
    newlines：读进来时 \r\n 已经被折成 \n，写回去时只写 \n ——
    于是每跑一次变异验证，被测的 CRLF 文件（web/app.js）就被悄悄改成 LF。

    实测后果：本脚本跑完后，tests/test_enhance_architecture_fixes.py::
    TestLineEndings::test_worktree_bytes_are_crlf 与
    tests/test_yue2_flow_review_fixes.py::TestCrlfFilesStayCrlf 双双变红
    （4883 个裸 LF），而这与被测代码毫无关系 —— 是**验证脚本自己**弄坏的。

    仓库有两条专门的 CRLF 守卫测试，说明这是硬约束。
    """
    path.write_bytes(data)


def run_tests(repo: Path) -> tuple[int, str]:
    proc = subprocess.run(
        [PY, "-m", "pytest", *TESTS, "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=str(repo), capture_output=True, text=True, timeout=600)
    return proc.returncode, proc.stdout + proc.stderr


def main() -> int:
    # 改前先确认工作区干净（变异纪律：每轮都要能干净地 restore）
    status = subprocess.run(["git", "status", "--porcelain"], cwd=str(REPO),
                            capture_output=True, text=True).stdout.strip()
    if status:
        print("!! 工作区不干净，先提交或 stash 再跑变异验证：")
        print(status)
        return 2

    failures = []
    for name, rel, old, new, expect in MUTATIONS:
        target = REPO / rel
        # 【按字节读】保持 CRLF 文件的行尾不被 universal newlines 折掉
        original_bytes = _read_bytes(target)
        original = original_bytes.decode("utf-8")
        # 变异串按**当前文件实际的行尾**构造：目标串里写的是 \n，
        # 而文件里是 \r\n -> 这里统一换算
        old_b = old.replace("\n", "\r\n").encode("utf-8") if b"\r\n" in original_bytes else old.encode("utf-8")
        new_b = new.replace("\n", "\r\n").encode("utf-8") if b"\r\n" in original_bytes else new.encode("utf-8")
        if old_b not in original_bytes:
            print(f"[SKIP] {name}: 目标串在 {rel} 里找不到（可能代码已变）")
            failures.append((name, "target-not-found"))
            continue
        try:
            _write_bytes(target, original_bytes.replace(old_b, new_b, 1))
            code, out = run_tests(REPO)
            # 变红 = 有效变异；并且必须是**指定的那条** testcase 变红
            named_red = expect in out
            if code != 0 and named_red:
                print(f"[OK ] {name} -> 变红，且是 '{expect}' 抓住的")
            else:
                print(f"[BAD] {name} -> 期望 '{expect}' 变红，实际 code={code} "
                      f"named_red={named_red}")
                failures.append((name, f"code={code} named_red={named_red}"))
        finally:
            # restore：还原到变异前的状态（**按字节**，保住行尾）
            _write_bytes(target, original_bytes)

    # 还原后必须全绿
    code, out = run_tests(REPO)
    if code != 0:
        print("[BAD] 还原后测试仍不绿 —— restore 出了问题")
        failures.append(("restore", "still-failing"))
    else:
        print("[OK ] 还原后全绿")

    # 行尾守卫：还原后被测文件的行尾必须与变异前**逐字节一致**
    # （这正是 write_text 会悄悄弄坏 CRLF 文件的地方）
    for name, rel, _old, _new, _expect in MUTATIONS:
        target = REPO / rel
        data = target.read_bytes()
        bare_lf = data.count(b"\n") - data.count(b"\r\n")
        if bare_lf:
            print(f"[BAD] {name}: 还原后 {rel} 有 {bare_lf} 个裸 LF —— 行尾被弄坏了")
            failures.append((name, f"bare-lf={bare_lf}"))
            break
    else:
        print("[OK ] 还原后行尾与变异前一致（按字节写回）")

    if failures:
        print(f"\n变异验证失败 {len(failures)} 项：")
        for name, why in failures:
            print(f"  - {name}: {why}")
        return 1
    print("\n全部变异均被指定断言抓住（变异验证通过）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
