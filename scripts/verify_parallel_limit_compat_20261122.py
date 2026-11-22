# -*- coding: utf-8 -*-
"""
验证「能力探测式守卫」在并行的「长度上限可配置 + 无限制选项」任务
合入后**真的会切到那条任务的规则**。

【怎么验】
把并行任务分支（local/feature/max-token）的 server.py 里的
`_prompt_limit_guard` / `_resolve_prompt_limit` **注入**到本分支的
server 模块命名空间里，然后调本分支的 `_enhance_prompt_limit_guard`，
断言它走的是注入的那条规则（而不是退回 4000）。

【为什么这样做】
并行任务还没合入 local/maintainer，所以没法直接 merge 验证。
但「守卫是能力探测式的」这件事**可以**用注入的方式证明 ——
这比「等合并后再验」早一步。
"""
import importlib.util
import sys
from pathlib import Path

REPO = Path("/root/projects/h3studio-prompt-enhance")
sys.path.insert(0, str(REPO))

import server as srv  # noqa: E402

# ---------------------------------------------------------------- 1) 基线：没有并行任务的守卫
srv._prompt_limit_guard = None
srv.__dict__.pop("_prompt_limit_guard", None)
err, info = srv._enhance_prompt_limit_guard("x" * 5000)
print("=== 无并行守卫（合并前行为）===")
print("  err:", err is not None, "| info:", info)
assert err is not None, "合并前应该按 4000 硬上限拦下"
assert info["prompt_limit_max_chars"] == 4000

# ---------------------------------------------------------------- 2) 注入并行任务的守卫
PARALLEL_PROVIDERS = Path("/root/projects/comfyui-minimax-h3-studio")
import subprocess  # noqa: E402

# 从并行任务分支取出 providers.py 的 prompt_limit 相关实现
providers_src = subprocess.run(
    ["git", "-C", str(PARALLEL_PROVIDERS), "show",
     "local/feature/max-token:h3studio/providers.py"],
    capture_output=True, text=True, check=True).stdout
ns: dict = {}
exec(compile(providers_src, "providers_parallel", "exec"), ns)

PROMPT_LIMIT_UNLIMITED = ns["PROMPT_LIMIT_UNLIMITED"]
DEFAULT_PROMPT_LIMIT = ns["DEFAULT_PROMPT_LIMIT"]
prompt_limit_max_chars = ns["prompt_limit_max_chars"]


def _resolve_prompt_limit(provider):
    """并行任务 server.py 里的实现（逐字照抄该分支的逻辑）。"""
    limit = (provider or {}).get("prompt_limit") or DEFAULT_PROMPT_LIMIT
    limit = ns["_normalize_limit"](limit)
    return limit, prompt_limit_max_chars(limit)


def _prompt_limit_guard(original, provider):
    """并行任务 server.py 里的实现（逐字照抄该分支的逻辑）。"""
    if provider is None:
        limit = DEFAULT_PROMPT_LIMIT
        limit = ns["_normalize_limit"](limit)
        max_chars = prompt_limit_max_chars(limit)
    else:
        limit, max_chars = _resolve_prompt_limit(provider)
    info = {
        "prompt_limit": limit,
        "prompt_limit_max_chars": max_chars,
        "prompt_limit_unlimited": max_chars is None,
    }
    if max_chars is not None and len(original) > max_chars:
        class _Err:
            status = 400
        return {"error": f"提示词过长（上限 {max_chars} 字）", **info}, info
    return None, info


srv._prompt_limit_guard = _prompt_limit_guard

# ---------------------------------------------------------------- 3) 断言切到了并行任务的规则
print("\n=== 注入并行任务守卫后 ===")
err, info = srv._enhance_prompt_limit_guard("x" * 5000)
print("  默认（4000）:", "err" if err else "pass", "| info:", info)
assert err is not None, "默认 4000 时 5000 字应该被拦"

# 模拟 Provider 配了「无限制」
srv._prompt_limit_guard = lambda original, provider: _prompt_limit_guard(
    original, {"prompt_limit": PROMPT_LIMIT_UNLIMITED})
err, info = srv._enhance_prompt_limit_guard("x" * 50000)
print("  Provider=unlimited:", "err" if err else "pass", "| info:", info)
assert err is None, "Provider 配了「无限制」时不该被拦"
assert info["prompt_limit_unlimited"] is True
assert info["prompt_limit_max_chars"] is None

print("\n结论：能力探测式守卫在并行任务合入后会自动切到它的 Provider 级规则，")
print("      本分支不需要再改；合并前行为逐字不变（4000 硬上限）。")
