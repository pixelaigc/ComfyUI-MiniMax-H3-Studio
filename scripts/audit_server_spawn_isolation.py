#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""找出所有会拉起 server.py 的测试用例（AST 精确扫描）。

【为什么需要】2026-11-15 线上验证时，生产任务队列被卡住：
任务停在 queued 永远不被 worker 取走。

定位到根因：pytest 跑期间有**额外的 server.py 进程**被拉起来，
而它们的 cwd 落在仓库根 —— 那里有 config.json，
data_dir 指向**生产的数据目录**。
server.py 启动即 `WORKER.start()`，那个 worker 去生产的 jobs.db
捞 status='queued' 的任务执行 —— 于是「测试实例的 worker」
把生产任务抢走跑（甚至跑失败）。

【怎么用】
  python3 scripts/audit_server_spawn_isolation.py --repo /root/projects/h3studio-r2vflf

【输出】audits/server_spawn_isolation.json
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path


def _literal(node):
    try:
        return ast.literal_eval(node)
    except Exception:
        return None


def _unparse(node):
    try:
        return ast.unparse(node)
    except Exception:
        return "<?>"


def _collect(tree: ast.AST):
    """返回所有 spawn 调用的位置信息。"""
    rows = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = getattr(fn, "attr", None) or getattr(fn, "id", None)
        if name not in ("Popen", "run", "call", "check_call", "check_output"):
            continue
        # 收集参数里出现的所有字符串常量
        strs = []
        for sub in ast.walk(node):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                strs.append(sub.value)
        if not any("server.py" in s for s in strs):
            continue
        kw = {k.arg: k.value for k in node.keywords if k.arg}
        # env 里显式出现的环境变量名
        env_src = ""
        if "env" in kw:
            env_src = _unparse(kw["env"])
        # cwd
        cwd_src = _unparse(kw["cwd"]) if "cwd" in kw else ""
        rows.append({
            "line": node.lineno,
            "func": name,
            "cwd": cwd_src,
            "env_src": env_src[:300],
            "env_isolated": "H3STUDIO_DATA_DIR" in env_src,
            "cwd_is_repo": ("REPO" in cwd_src or "parent" in cwd_src
                            or cwd_src in ("'.'", '"."')),
            "cmd_strs": [s for s in strs if "server.py" in s][:2],
        })
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo",
                    default=str(Path(__file__).resolve().parent.parent))
    args = ap.parse_args()
    repo = Path(args.repo).resolve()

    findings = []
    for path in sorted(repo.rglob("*.py")):
        if any(part in ("__pycache__", ".git", "node_modules")
               for part in path.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for row in _collect(tree):
            row["file"] = str(path.relative_to(repo))
            # 判定：会不会抢生产队列
            #   危险 = 没隔离 data_dir  且  cwd 落在仓库内（含仓库根）
            risky = (not row["env_isolated"]) and (
                row["cwd_is_repo"] or row["cwd"] == "")
            if risky:
                row["risk"] = "HIGH"
            elif not row["env_isolated"]:
                row["risk"] = "MED"
            else:
                row["risk"] = "OK"
            findings.append(row)

    out = repo / "audits" / "server_spawn_isolation.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(findings, ensure_ascii=False, indent=2),
                   encoding="utf-8")

    high = [f for f in findings if f["risk"] == "HIGH"]
    med = [f for f in findings if f["risk"] == "MED"]
    print(f"扫描到 {len(findings)} 处 server.py 启动点")
    print(f"  HIGH（未隔离 data_dir 且 cwd 在仓库内）: {len(high)}")
    print(f"  MED  （未隔离 data_dir，cwd 在别处）  : {len(med)}")
    print(f"证据落盘：{out}")
    for f in high:
        print(f"  [HIGH] {f['file']}:{f['line']}  cwd={f['cwd']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
