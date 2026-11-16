/* ============================================================
   H3 Studio —— 随机种子的「正整数」前端闸门（2026-11-16）
   ------------------------------------------------------------
   【为什么要有这个模块】

   用户实测：把非数字内容填进「种子」框 -> 任务一路跑到 worker 才炸，
   用户只看到一句没有任何信息量的：

       ✕ 生成失败（服务端未能给出具体原因）。
         请稍后重试；若反复失败，请查看服务端日志，或改用「复用参数」重新生成一段。

   本模块把这件事**提前到提交前**：非法种子在点「生成」的那一刻就被拦下，
   并给出一句能照着改的中文原因，不发起任何请求。

   【与后端的关系】

   前端是第一道闸（省一次请求 + 即时反馈），
   后端 h3studio/seed_guard.py 是**唯一强制点**（直接调 API / 内置 Agent
   都要过它）。两边口径必须一致 —— 所以这里的上限常量与后端同源
   （后端 seed_guard.SEED_MAX = 2**53-1，JSON number 能精确表示的上限）。

   【为什么「留空 = 随机」是合法的】

   面板上的种子框写着「留空随机」，placeholder 也是「随机」。
   所以空串、以及用户直接敲「随机」/「random」，都等价于「随机」。

   对外 API：
     window.SeedGuard.validate(value)  -> {ok: bool, seed: int|null, error: string}
     window.SeedGuard.readInput(el)    -> {ok: bool, seed: int|null, error: string}
     window.SeedGuard.guardInput(el, onError) -> bool
   ============================================================ */
(function () {
  "use strict";

  /** 与后端 h3studio/seed_guard.py 的 SEED_MAX 同源。 */
  const SEED_MAX = 2 ** 53 - 1;

  /** 「随机」的等价写法（与后端 _RANDOM_TOKENS 一致）。 */
  const RANDOM_TOKENS = ["", "随机", "random", "rand", "auto"];

  /**
   * 校验一个种子值。
   *
   * 返回 {ok, seed, error}：
   *   ok=true  -> seed 是正整数，或 null（表示「随机」）
   *   ok=false -> error 是**可直接展示给用户**的中文原因
   */
  function validate(value) {
    if (value === null || value === undefined) {
      return { ok: true, seed: null, error: "" };
    }
    if (typeof value === "boolean") {
      return {
        ok: false, seed: null,
        error: "种子只能是正整数：True/False 不是合法种子。请填一个大于 0 的整数，或留空随机。",
      };
    }
    if (typeof value === "number") {
      if (!Number.isFinite(value)) {
        return {
          ok: false, seed: null,
          error: "种子只能是正整数：请填一个大于 0 的整数，或留空随机。",
        };
      }
      if (!Number.isInteger(value)) {
        return {
          ok: false, seed: null,
          error: "种子只能是正整数：" + value + " 是小数。请填一个大于 0 的整数，或留空随机。",
        };
      }
      return checkRange(value, String(value));
    }
    const text = String(value).trim();
    if (RANDOM_TOKENS.indexOf(text.toLowerCase()) >= 0) {
      return { ok: true, seed: null, error: "" };
    }
    // 只接受纯数字字面量（可带前导 +）—— 不接受小数点 / 指数 / 字母 / 空格分隔
    const body = text.charAt(0) === "+" ? text.slice(1) : text;
    if (!body || !/^[0-9]+$/.test(body)) {
      return {
        ok: false, seed: null,
        error: "种子只能是正整数：" + JSON.stringify(text) + " 不是数字。请填一个大于 0 的整数，或留空随机。",
      };
    }
    const parsed = Number(text);
    if (!Number.isSafeInteger(parsed)) {
      return {
        ok: false, seed: null,
        error: "种子只能是正整数：" + text + " 超出可精确表示的范围。请填一个不超过 "
          + SEED_MAX + " 的正整数，或留空随机。",
      };
    }
    return checkRange(parsed, text);
  }

  /** 正整数范围核对（>0 且 <= SEED_MAX）。 */
  function checkRange(value, shown) {
    if (value <= 0) {
      return {
        ok: false, seed: null,
        error: "种子必须大于 0：" + shown + " 不是正整数。请填一个大于 0 的整数，或留空随机。",
      };
    }
    if (value > SEED_MAX) {
      return {
        ok: false, seed: null,
        error: "种子太大了：" + shown + " 超过上限 " + SEED_MAX
          + "。请填一个不超过该上限的正整数，或留空随机。",
      };
    }
    return { ok: true, seed: value, error: "" };
  }

  /** 读一个输入框并校验。 */
  function readInput(el) {
    if (!el) return { ok: true, seed: null, error: "" };
    return validate(el.value);
  }

  /**
   * 给一个输入框装上「提交前闸门」。
   *
   * onError(msg) 会被调用（默认 alert）。返回 true = 合法。
   *
   * 【为什么不是靠 input 事件即时提示】输入过程中用户会经过「1」「12」
   * 「12.」这些中间态 —— 边打边报红会变成噪音。真正需要拦的是**提交那一刻**。
   * 所以这里只在失焦与提交时校验。
   */
  function guardInput(el, onError) {
    if (!el) return true;
    const report = (msg) => {
      if (typeof onError === "function") onError(msg);
      else if (typeof window.alert === "function") window.alert(msg);
    };
    el.addEventListener("blur", () => {
      const result = validate(el.value);
      if (!result.ok) report(result.error);
    });
    return true;
  }

  window.SeedGuard = {
    validate,
    readInput,
    guardInput,
    SEED_MAX,
  };
})();
