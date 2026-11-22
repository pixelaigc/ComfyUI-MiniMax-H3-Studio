// ---------- 轮询调度器（抖动 + 退避 + 页面不可见暂停 + 永不重叠） ----------
//
// 【2026-11-22 请求风暴修 —— 8190 -> ComfyUI(8188) 请求风暴】
//
// 【为什么要单独一个文件】它必须**早于**所有用它的模块加载
// （generation-queue.js 在 app.js 之前加载，而队列轮询在
// app.js 的 boot() 里启动）。所以不能放在 app.js 里。
//
// 【老实现的三个问题】
//   1. **裸 setInterval**：N 个标签页的定时器**相位相同** -> 同一毫秒
//      一起打服务端 -> 服务端瞬间收到 N 倍请求；
//   2. **慢响应时请求重叠**：setInterval 不等上一轮结束。服务端一旦
//      变慢（实测 /api/jobs 曾劣化到 1279ms），并发请求会堆起来；
//   3. **失败也照打 + 页面不可见也照打**：网络抖动时每一轮都打；
//      后台标签页（用户根本没看）也在轮询。
//
// 【现在的语义】
//   * **递归 setTimeout**（不是 setInterval）—— 上一轮**完成**后才排
//     下一轮，因此**永不重叠**；
//   * 每轮加 **±20% 抖动** -> 多标签页相位打散；
//   * 失败后**指数退避**（interval -> 2x -> 4x ... 上限 30s），
//     成功即复位；
//   * `document.hidden` 时**跳过**本轮；页面重新可见时**立刻补跑**一次；
//   * **同 key 去重**：同一个 name 重复注册时**替换**旧调度器，
//     不会叠出两个轮询（这正是「多标签页/多面板各自 setInterval」
//     造成重复轮询的根因之一）。
//
// 【用法】
//   window.H3Poll("jobs", () => fetchJobs(), 2000);
//   window.H3Poll.stop("jobs");
(function (global) {
  "use strict";

  const registry = new Map();
  const DEFAULT_MAX_BACKOFF = 30000;
  const DEFAULT_JITTER = 0.2;

  function schedule(name, fn, intervalMs, opts) {
    const o = opts || {};
    const maxBackoff = o.maxBackoffMs || DEFAULT_MAX_BACKOFF;
    const jitter = o.jitter === undefined ? DEFAULT_JITTER : o.jitter;

    // 同名替换 —— 杜绝「同一个东西被注册两次」的重复轮询。
    const prev = registry.get(name);
    if (prev && prev.stop) prev.stop();

    let timer = null;
    let failures = 0;
    let stopped = false;

    async function tick() {
      if (stopped) return;
      // 页面不可见 -> 跳过本轮（后台标签页不轮询）。
      if (typeof document !== "undefined" && document.hidden) {
        timer = setTimeout(tick, intervalMs);
        return;
      }
      let ok = true;
      try {
        await fn();
      } catch (e) {
        ok = false;
      }
      if (stopped) return;
      if (ok) {
        failures = 0;
      } else {
        failures += 1;
      }
      // 退避：连续失败越多等得越久（上限 maxBackoff）。
      const base = failures > 0
        ? Math.min(intervalMs * Math.pow(2, failures), maxBackoff)
        : intervalMs;
      // 抖动 —— 打散多标签页的相位。
      const delta = base * jitter * (Math.random() * 2 - 1);
      timer = setTimeout(tick, Math.max(500, Math.round(base + delta)));
    }

    // 页面重新可见 -> 立刻补跑一次（不用等下一个周期）。
    const onVisible = () => {
      if (stopped) return;
      if (typeof document === "undefined" || document.hidden) return;
      if (timer) { clearTimeout(timer); timer = null; }
      tick();
    };
    if (typeof document !== "undefined" && o.resumeOnVisible !== false) {
      document.addEventListener("visibilitychange", onVisible);
    }

    const handle = {
      stop() {
        stopped = true;
        if (timer) { clearTimeout(timer); timer = null; }
        if (typeof document !== "undefined") {
          document.removeEventListener("visibilitychange", onVisible);
        }
        if (registry.get(name) === handle) registry.delete(name);
      },
      get failures() { return failures; },
      get stopped() { return stopped; },
    };

    registry.set(name, handle);
    timer = setTimeout(
      tick,
      Math.round(intervalMs * (1 + jitter * (Math.random() * 2 - 1))));
    return handle;
  }

  function stop(name) {
    const h = registry.get(name);
    if (h) h.stop();
  }

  function stats() {
    const out = {};
    registry.forEach((h, name) => { out[name] = h.failures; });
    return out;
  }

  global.H3Poll = schedule;
  global.H3Poll.stop = stop;
  global.H3Poll.stats = stats;
})(window);
