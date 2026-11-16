# -*- coding: utf-8 -*-
"""随机种子的**正整数**校验（唯一实现）。

【为什么要有这个模块 —— 2026-11-16 用户实测】

用户把非数字内容填进「种子」框后，任务一路跑到 worker 才炸：

    server.py  workflows.py:339
        seed = params.get("seed") or random.randint(0, 2**48)
    server.py  workflows.py:380
        p = {..., "_seed": int(seed), ...}      # <- ValueError 在这里

worker 捕获后只留一句兜底文案，于是用户看到的是：

    ✕ 生成失败（服务端未能给出具体原因）。
      请稍后重试；若反复失败，请查看服务端日志，或改用「复用参数」重新生成一段。

这条信息**对解决问题毫无帮助** —— 用户不知道自己填错了什么。

【修法】把「种子必须是正整数」这件事从「worker 崩了才知道」提到
「提交前就被拦下」，且**拦下时给出一句能照着改的中文原因**。

【为什么是「正整数」而不是「整数」】
  * 0 与负数在 ComfyUI 的 RandomNoise / KSampler 里没有意义
    （noise_seed 会取模，负数会被折成另一个值 —— 界面显示的种子与实际用的
    种子对不上，是另一种「界面说谎」）；
  * 小数会被 int() 静默截断（1.9 -> 1），用户以为固定了随机性；
  * 空 = 随机是面板上写着的语义（「留空随机」），必须保留。

【为什么上限是 2**53-1】
前端用 JSON number 传种子，JavaScript 的 Number 只能精确表示到
2**53-1。再大的值在 JSON 里会变成 1e+21 之类的科学计数法，
服务端 int() 直接抛错 —— 与「不校验」是同一种失败，只是更隐蔽。
"""

from __future__ import annotations

#: 种子允许的最大值（JSON number 能精确表示的上限）。
#: 前端与服务端共用这一个常量 —— 两边口径不同就会出现「前端过了、
#: 服务端炸」或反之。
SEED_MAX = 2 ** 53 - 1

#: 种子文案里的「随机」写法（留空也等价）。用户直接敲「随机」时
#: 不该被判成非法值 —— 面板上的 placeholder 就是这么写的。
_RANDOM_TOKENS = {"", "随机", "random", "rand", "auto"}


class SeedValidationError(ValueError):
    """种子非法。message 是**可直接展示给用户**的中文原因。"""

    def __init__(self, message: str, code: str = "invalid_seed"):
        super().__init__(message)
        self.message = message
        self.code = code


def _looks_like_int_literal(text: str) -> bool:
    """纯数字字面量（可带前导 +），不含小数点 / 指数 / 字母。

    【为什么不直接用 int()】int("1_0") == 10（Python 允许下划线分隔）、
    int(" 12 ") == 12 —— 这些都不是「用户输入的正整数」的直觉形态，
    更关键的是 int("1.0") 会**抛错**而不是给出「这是小数」这种可执行提示。
    """
    body = text[1:] if text[:1] in "+-" else text
    return bool(body) and body.isdigit()


def parse_seed(raw) -> tuple[int | None, str]:
    """校验一个种子值。

    返回 ``(seed, error)``：
      * ``seed`` 为 ``None`` 表示「随机」（合法的空值）；
      * ``error`` 非空表示非法，此时 ``seed`` 一定是 ``None``。

    【为什么不抛异常】调用方分布在三处（面板提交 / 内置 Agent /
    任务重试），有的要返回 400、有的要抛 AgentError —— 统一成
    「返回 (值, 原因)」让调用方自己决定怎么呈现。
    """
    if raw is None:
        return None, ""
    if isinstance(raw, bool):
        # bool 是 int 的子类 —— 但 True/False 当种子毫无意义
        return None, ("种子只能是正整数：True/False 不是合法种子。"
                      "请填一个大于 0 的整数，或留空随机。")
    if isinstance(raw, int):
        return _check_int(raw, str(raw))
    if isinstance(raw, float):
        # 小数：明确说「是小数」，别让它被 int() 悄悄截断
        if raw != int(raw):
            return None, (f"种子只能是正整数：{raw} 是小数。"
                          "请填一个大于 0 的整数，或留空随机。")
        return _check_int(int(raw), str(raw))
    text = str(raw).strip()
    if text.lower() in _RANDOM_TOKENS:
        return None, ""
    if not _looks_like_int_literal(text):
        return None, (f"种子只能是正整数：{text!r} 不是数字。"
                      "请填一个大于 0 的整数，或留空随机。")
    return _check_int(int(text), text)


def _check_int(value: int, shown: str) -> tuple[int | None, str]:
    if value <= 0:
        return None, (f"种子必须大于 0：{shown} 不是正整数。"
                      "请填一个大于 0 的整数，或留空随机。")
    if value > SEED_MAX:
        return None, (f"种子太大了：{shown} 超过上限 {SEED_MAX}。"
                      "请填一个不超过该上限的正整数，或留空随机。")
    return value, ""


def normalize_seed_params(params: dict) -> None:
    """就地校验并归一 ``params["seed"]``。非法时抛 SeedValidationError。

    【为什么是「就地归一」而不是只校验】前端可能传来字符串 "4242"（表单
    控件的值本来就是字符串）。校验通过后顺手转成 int，落库的种子类型就
    统一了 —— 历史库里躺着字符串种子（那正是提示词误填进种子框的遗留），
    新任务不再制造新的。

    【为什么留空 = None 而不是删键】worker / workflow 都按
    ``params.get("seed") or random`` 取值，None 与缺键等价。
    """
    if not isinstance(params, dict):
        return
    if "seed" not in params:
        return
    raw = params.get("seed")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        # 空 = 随机（面板上写着「留空随机」）
        params["seed"] = None
        return
    seed, error = parse_seed(raw)
    if error:
        raise SeedValidationError(error)
    params["seed"] = seed
