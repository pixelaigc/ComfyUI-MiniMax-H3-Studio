# -*- coding: utf-8 -*-
"""素材库页切回生成标签时的「首图巨大闪现」回归测试。

真实缺陷（用户报）：在素材库页状态下点击「Z-IMAGE 文生图」或其它生成标签，
约 1 秒内右侧先显示第一张图的**原始尺寸巨图**，随后才渲染网格。

三个真实根因（本测试逐条锁死）：
  1. **列数算错**：素材库页把 #jobsArea 设成 display:none，容器宽度 = 0；
     gridColumnCount(0) 返回 1，把「1 列」写进 inline style。
     切回生成页时第一帧按 1 列铺满整行 —— 首张卡片宽 = 整个容器（实测 1040px）。
  2. **预览框高度由图片决定**：.job-preview 用 aspect-ratio + flex 居中 +
     img 的 max-width/max-height，flex 容器的 aspect-ratio 会被内容的固有
     尺寸压过 —— 1024×1024 的原图把 335px 宽的预览框撑到 1024px 高。
     两个根因叠加 = 首帧一张 1040px 宽、1024px 高的原图巨��。
  3. **没有占位骨架**：数据到达前网格是空的，图片解码完成的瞬间
     才把卡片撑开 —— 即使列数正确也会有「先塌后立」的布局抖动。

覆盖场景（任务书要求）：
  · 进入素材库状态
  · 任意生成标签互切
  · 快速连续切换
  · 后台实时刷新到达

判定口径（全部基于真实渲染几何，不是「看起来没问题」）：
  · 切换过程中任何一帧，预览框宽高都必须 ≤ 容器宽 / 16:9，
    且**绝不能**等于图片的自然像素尺寸（naturalWidth/Height）——
    出现「渲染尺寸 == 自然尺寸」就说明是原图直接铺开了；
  · 网格列数在切换过程中不得从 N 列掉到 1 列；
  · 骨架占位与真实卡片的盒模型必须一致（数据到达前后网格高度不变）。

运行：python3 -m pytest tests/test_grid_flash.py -v
需要：playwright（chromium）。缺失时整类 skip，不算失败。
"""
import json
import sqlite3
import sys
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except Exception:                                   # pragma: no cover
    sync_playwright = None
    HAS_PLAYWRIGHT = False

from test_frontend_defaults import StudioServer  # noqa: E402


# 预置任务：全部带产物，图片输出指向真实存在的大图（1024×1024）
SEED_COUNT = 12
BIG_IMAGE = 1024          # 预置图片的像素尺寸（正方形）
BIG_IMAGE_BYTES = 40      # 伪图的字节数（小图，够触发解码即可）


def _make_png(path, size):
    """写一张 size×size 的真 PNG（无第三方依赖，用 zlib 手写最小 PNG）。"""
    import struct
    import zlib

    raw = b""
    for _ in range(size):
        raw += b"\x00" + bytes([(size * 7) % 256, 90, 200] * size)

    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 6))
           + chunk(b"IEND", b""))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)


def seed_jobs(server):
    """预置带图片产物的已完成任务。"""
    conn = sqlite3.connect(server.data_dir / "jobs.db")
    now = time.time()
    for i in range(SEED_COUNT):
        params = {"title": f"闪现探针 {i}", "prompt": f"flash probe {i}",
                  "aspect": "1:1", "width": BIG_IMAGE, "height": BIG_IMAGE}
        outputs = json.dumps(
            [{"kind": "image", "file": f"images/flash{i:03d}.png",
              "name": f"flash{i:03d}.png"}], ensure_ascii=False)
        conn.execute(
            "INSERT OR REPLACE INTO jobs (id, created_at, mode, params, status,"
            " progress, progress_text, outputs) VALUES (?,?,?,?,?,?,?,?)",
            (f"flash{i:03d}", now - i * 30, "t2i",
             json.dumps(params, ensure_ascii=False),
             "done", 1.0, "", outputs))
    conn.commit()
    conn.close()
    # 生成真实的 1024×1024 图片
    for i in range(SEED_COUNT):
        _make_png(server.data_dir / "outputs" / "images" / f"flash{i:03d}.png", BIG_IMAGE)


# 采样：把「渲染尺寸 vs 自然尺寸」「列数」一次性取回来
# 安装到页面上的探针函数（供 rAF 采样器反复调用）
PROBE_BODY = """
() => {
  const grid = document.querySelector('#jobsGrid');
  const area = document.querySelector('#jobsArea');
  const cards = Array.from(document.querySelectorAll('#jobsGrid .job-card'));
  const previews = Array.from(document.querySelectorAll('#jobsGrid .job-preview'));
  const imgs = Array.from(document.querySelectorAll('#jobsGrid .job-preview img'));
  const gw = grid ? grid.getBoundingClientRect().width : 0;
  return {
    t: Math.round(performance.now()),
    cols: grid ? Number(grid.dataset.cols || 0) : 0,
    gridW: Math.round(gw),
    areaVisible: area ? getComputedStyle(area).display !== 'none' : false,
    cardCount: cards.length,
    skeletonCount: document.querySelectorAll('#jobsGrid .job-skeleton').length,
    // 预览框尺寸（真实渲染几何）
    previews: previews.slice(0, 4).map(p => {
      const r = p.getBoundingClientRect();
      return {w: Math.round(r.width), h: Math.round(r.height)};
    }),
    // 图片：渲染尺寸 vs 自然像素尺寸
    imgs: imgs.slice(0, 4).map(i => {
      const r = i.getBoundingClientRect();
      return {w: Math.round(r.width), h: Math.round(r.height),
              nw: i.naturalWidth, nh: i.naturalHeight,
              complete: i.complete};
    }),
    // 网格总高度（骨架 → 真实卡片应保持一致 = 无布局抖动）
    gridH: Math.round(grid ? grid.getBoundingClientRect().height : 0),
  };
}
"""
#: 注入页面的安装脚本：把探针挂到 window.__flashProbe
PROBE_INSTALL = "window.__flashProbe = " + PROBE_BODY


@unittest.skipUnless(HAS_PLAYWRIGHT, "未安装 playwright")
class TestGridFlash(unittest.TestCase):
    """真实浏览器 + 真实后端：锁死「首图巨大闪现」的三条根因。"""

    @classmethod
    def setUpClass(cls):
        cls.server = StudioServer(seed_jobs=0)
        assert cls.server.start(), "Studio 实例启动失败"
        seed_jobs(cls.server)
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(args=["--no-sandbox"])
        cls.ctx = cls.browser.new_context(viewport={"width": 1600, "height": 900})
        cls.page = cls.ctx.new_page()
        cls.errors = []
        cls.page.on("pageerror", lambda e: cls.errors.append(str(e)))
        cls.page.goto("http://127.0.0.1:%d/" % cls.server.port,
                      wait_until="domcontentloaded")
        cls.page.wait_for_function(
            "() => document.querySelectorAll('#jobsGrid .job-card').length > 0",
            timeout=20000)
        cls.page.wait_for_timeout(1500)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.stop()

    # ------------------------------------------------------------ 工具
    def start_sampler(self, duration_ms=3200):
        """在页面里挂一个 rAF 采样器，返回采样函数名。"""
        self.page.evaluate("""
          (dur) => {
            window.__flashSamples = [];
            const t0 = performance.now();
            const tick = () => {
              window.__flashSamples.push(window.__flashProbe());
              if (performance.now() - t0 < dur) requestAnimationFrame(tick);
            };
            requestAnimationFrame(tick);
          }
        """, duration_ms)

    def samples(self):
        return self.page.evaluate("() => window.__flashSamples || []")

    def goto_assets(self):
        """切到素材库页（把 #jobsArea 藏起来）。"""
        self.page.click("#navAssets")
        self.page.wait_for_function(
            "() => getComputedStyle(document.querySelector('#jobsArea')).display === 'none'",
            timeout=10000)
        self.page.wait_for_timeout(1200)

    # ---------------------------------------------- 1. 素材库状态下切标签
    def test_switch_from_assets_never_shows_natural_size_image(self):
        """核心：从素材库切回生成标签，任何一帧都不能出现「原图按自然尺寸铺开」。

        判定：图片的**渲染尺寸**必须小于它的**自然像素尺寸**。
        1024×1024 的图渲染成 1024×1024 = 原图直接铺开了（就是那个巨图）。
        """
        self.goto_assets()
        # 采样器读的是同一套字段
        self.page.evaluate(PROBE_INSTALL)
        self.start_sampler(3200)
        self.page.click(".nav-item[data-mode='t2i']")
        self.page.wait_for_timeout(3400)
        samples = self.samples()
        self.assertTrue(samples, "采样器没有采到任何帧")

        bad = []
        for s in samples:
            cols = max(1, s["cols"])
            cell_w = s["gridW"] / cols if s["gridW"] else 0
            for box in s["previews"]:
                # 形态 A：卡片被拉成整行宽（预览框超过一个网格单元）
                if cell_w and box["w"] > cell_w + 2:
                    bad.append((s["t"], "stretched", box, cell_w))
            for img in s["imgs"]:
                # 形态 B：图片按自然像素尺寸直接铺开
                if img["nw"] and img["w"] >= img["nw"] and img["h"] >= img["nh"]:
                    bad.append((s["t"], "natural", img))
        self.assertEqual(
            bad, [],
            "有 %d 帧出现巨图（卡片被拉成整行宽 / 原图按自然尺寸铺开）：%r"
            % (len(bad), bad[:3]))

    def test_switch_from_assets_never_collapses_to_one_column(self):
        """根因 1：素材库状态下容器宽度为 0，不能据此算成 1 列。

        切回生成页后列数必须 ≥ 2（1600px 视口 + 每页 12 → 3 列）。
        """
        self.goto_assets()
        self.page.evaluate(PROBE_INSTALL)
        self.start_sampler(3200)
        self.page.click(".nav-item[data-mode='t2i']")
        self.page.wait_for_timeout(3400)
        samples = self.samples()

        # 只看生成页可见之后的帧
        visible = [s for s in samples if s["areaVisible"]]
        self.assertTrue(visible, "没有任何一帧里生成页是可见的")
        one_col = [s for s in visible if s["cols"] <= 1 and s["cardCount"] > 0]
        self.assertEqual(
            one_col, [],
            "有 %d 帧网格塌成 1 列（cols<=1）：%r"
            % (len(one_col), [(s["t"], s["cols"], s["cardCount"]) for s in one_col[:3]]))

    def test_preview_box_height_never_follows_image_pixels(self):
        """根因 2：预览框高度必须由 CSS 决定（16:9），不能被原图撑开。

        判定：预览框高度 ≤ 宽度 × 0.6（16:9 = 0.5625，留一点余量）。
        """
        self.page.evaluate(PROBE_INSTALL)
        self.page.wait_for_timeout(600)
        boxes = self.page.evaluate("""
          () => Array.from(document.querySelectorAll('#jobsGrid .job-preview'))
            .slice(0, 8).map(p => {
              const r = p.getBoundingClientRect();
              return {w: Math.round(r.width), h: Math.round(r.height)};
            })
        """)
        self.assertTrue(boxes, "没有渲染出任何预览框")
        for b in boxes:
            self.assertLessEqual(
                b["h"], b["w"] * 0.6 + 2,
                "预览框 %dx%d 不是 16:9 —— 高度被内容撑开了" % (b["w"], b["h"]))

    def test_image_never_exceeds_preview_box(self):
        """图片渲染尺寸必须不超过预览框（object-fit: contain）。"""
        self.page.evaluate(PROBE_INSTALL)
        self.page.wait_for_timeout(800)
        pairs = self.page.evaluate("""
          () => Array.from(document.querySelectorAll('#jobsGrid .job-preview'))
            .slice(0, 8).map(p => {
              const pr = p.getBoundingClientRect();
              const img = p.querySelector('img');
              if (!img) return null;
              const ir = img.getBoundingClientRect();
              return {box: [Math.round(pr.width), Math.round(pr.height)],
                      img: [Math.round(ir.width), Math.round(ir.height)]};
            }).filter(Boolean)
        """)
        self.assertTrue(pairs, "没有可比的图片/预览框")
        for p in pairs:
            self.assertLessEqual(p["img"][0], p["box"][0] + 1,
                                 "图片宽 %d 超过预览框宽 %d" % (p["img"][0], p["box"][0]))
            self.assertLessEqual(p["img"][1], p["box"][1] + 1,
                                 "图片高 %d 超过预览框高 %d" % (p["img"][1], p["box"][1]))

    # ---------------------------------------------- 2. 骨架占位 + 无抖动
    def test_skeleton_shown_while_switching(self):
        """切换筛选时先铺骨架占位（而不是留空网格）。

        采样点选在「请求刚发出」这一刻 —— 那正是骨架应该在的时刻：
        骨架先铺好，列宽/行高已经稳定，数据回来后原地替换。
        """
        self.page.click(".nav-item[data-mode='t2i']")
        self.page.wait_for_timeout(1200)
        # 先把状态标签拨到「全部」，再切到「已完成」——
        # 必须是一次**真实的状态切换**，否则筛选签名没变、不会铺骨架。
        # （测试之间共享页面状态，不显式归位会互相干扰。）
        self.page.click(".jobs-tabs button[data-st='all']")
        self.page.wait_for_timeout(1500)
        # 装一个「记录型」fetch 钩子：每次请求都记下当时的网格状态。
        # 不能用 Promise 等下一次 fetch —— 后台 2 秒轮询会先触发，
        # 拿到的是「轮询时刻」而不是「筛选切换时刻」的网格状态。
        self.page.evaluate("""
          () => {
            window.__skLog = [];
            if (!window.__skOrigFetch) window.__skOrigFetch = window.fetch;
            const orig = window.__skOrigFetch;
            window.fetch = function (...args) {
              window.__skLog.push({
                url: String(args[0]),
                skeletons: document.querySelectorAll('#jobsGrid .job-skeleton').length,
                cards: document.querySelectorAll('#jobsGrid .job-card[data-jid]').length,
              });
              return orig.apply(this, args);
            };
          }
        """)
        # 切到「已完成」状态标签 —— 这会改变筛选态，触发骨架
        self.page.click(".jobs-tabs button[data-st='done']")
        self.page.wait_for_timeout(1200)
        log = self.page.evaluate("() => window.__skLog")
        # 只看带筛选参数的那次请求（状态切换发的是 status=done）
        switched = [e for e in log if "status=done" in e["url"]]
        self.assertTrue(switched, "没有捕获到状态切换的请求：%r" % log)
        self.assertGreater(
            switched[0]["skeletons"], 0,
            "发起请求时网格里应有骨架占位，实际 0 个 —— 切换期间网格是空的")
        self.assertEqual(
            switched[0]["cards"], 0,
            "骨架阶段不该同时存在旧卡片（否则布局会在数据到达时跳动）")

    def test_grid_height_stable_across_switch(self):
        """骨架 → 真实卡片：网格高度不得变化（无布局抖动）。

        骨架卡与真实卡片共用 .job-card 盒模型，
        所以一帧内从骨架变成真实卡片时总高度应保持一致。
        """
        self.page.click(".nav-item[data-mode='t2i']")
        self.page.wait_for_timeout(1200)
        # 触发一次**真实**的筛选变化（骨架 → 真实卡片的完整过程）：
        # 先归位到「全部」，再切到「已完成」，保证筛选签名确实变了。
        self.page.click(".jobs-tabs button[data-st='all']")
        self.page.wait_for_timeout(1500)
        self.page.evaluate("""
          () => {
            const btn = document.querySelector(".jobs-tabs button[data-st='done']");
            if (btn) btn.click();
          }
        """)
        heights = self.page.evaluate("""
          () => new Promise(resolve => {
            const out = [];
            const t0 = performance.now();
            const tick = () => {
              const grid = document.querySelector('#jobsGrid');
              const cards = grid.querySelectorAll('.job-card');
              if (cards.length) {
                out.push({t: Math.round(performance.now() - t0),
                          h: Math.round(grid.getBoundingClientRect().height),
                          sk: grid.querySelectorAll('.job-skeleton').length});
              }
              if (performance.now() - t0 < 2600) requestAnimationFrame(tick);
              else resolve(out);
            };
            requestAnimationFrame(tick);
          })
        """)
        self.assertTrue(heights, "没采到任何帧")
        # 骨架帧与真实卡片帧都要有；高度差 ≤ 2px
        sk_frames = [h for h in heights if h["sk"] > 0]
        real_frames = [h for h in heights if h["sk"] == 0]
        self.assertTrue(sk_frames, "整个切换过程都没出现骨架占位")
        self.assertTrue(real_frames, "整个切换过程都没出现真实卡片")
        sk_h = sk_frames[0]["h"]
        real_h = real_frames[-1]["h"]
        self.assertLessEqual(
            abs(sk_h - real_h), 2,
            "网格高度从骨架 %dpx 变成真实卡片 %dpx —— 布局抖动了" % (sk_h, real_h))

    # ---------------------------------------------- 3. 任意生成标签互切
    def test_switch_between_generation_types_is_clean(self):
        """任意两个生成标签互切都不出现巨图。"""
        modes = ["t2i", "t2i_qwen", "i2i_qwen", "t2v", "flf", "r2v"]
        self.goto_assets()
        self.page.evaluate(PROBE_INSTALL)
        self.start_sampler(6000)
        for mode in modes:
            self.page.click(".nav-item[data-mode='%s']" % mode)
            self.page.wait_for_timeout(700)
        self.page.wait_for_timeout(6200)
        samples = self.samples()
        bad = []
        for s in samples:
            cols = max(1, s["cols"])
            cell_w = s["gridW"] / cols if s["gridW"] else 0
            for box in s["previews"]:
                # 形态 A：卡片被拉成整行宽（预览框超过一个网格单元）
                if cell_w and box["w"] > cell_w + 2:
                    bad.append((s["t"], "stretched", box, cell_w))
            for img in s["imgs"]:
                # 形态 B：图片按自然像素尺寸直接铺开
                if img["nw"] and img["w"] >= img["nw"] and img["h"] >= img["nh"]:
                    bad.append((s["t"], "natural", img))
        self.assertEqual(bad, [],
                         "标签互切过程中有 %d 帧把原图按自然尺寸铺开：%r"
                         % (len(bad), bad[:3]))

    def test_rapid_consecutive_switching_is_clean(self):
        """快速连续切换（不等数据返回）也不能出现巨图。"""
        self.goto_assets()
        self.page.evaluate(PROBE_INSTALL)
        self.start_sampler(3000)
        for mode in ["t2i", "t2v", "t2i", "i2i_qwen", "t2i"]:
            self.page.click(".nav-item[data-mode='%s']" % mode)
            self.page.wait_for_timeout(120)      # 只等 120ms，远小于取数耗时
        self.page.wait_for_timeout(3200)
        samples = self.samples()
        bad = []
        for s in samples:
            cols = max(1, s["cols"])
            cell_w = s["gridW"] / cols if s["gridW"] else 0
            for box in s["previews"]:
                # 形态 A：卡片被拉成整行宽（预览框超过一个网格单元）
                if cell_w and box["w"] > cell_w + 2:
                    bad.append((s["t"], "stretched", box, cell_w))
            for img in s["imgs"]:
                # 形态 B：图片按自然像素尺寸直接铺开
                if img["nw"] and img["w"] >= img["nw"] and img["h"] >= img["nh"]:
                    bad.append((s["t"], "natural", img))
        self.assertEqual(bad, [],
                         "快速连续切换中有 %d 帧把原图按自然尺寸铺开：%r"
                         % (len(bad), bad[:3]))

    # ---------------------------------------------- 4. 后台实时刷新
    def test_background_refresh_does_not_introduce_flash(self):
        """后台 2 秒实时刷新到达时也不出现巨图。"""
        self.page.click(".nav-item[data-mode='t2i']")
        self.page.wait_for_timeout(1500)
        self.page.evaluate(PROBE_INSTALL)
        self.start_sampler(5200)          # 覆盖至少两轮 2 秒轮询
        self.page.wait_for_timeout(5400)
        samples = self.samples()
        bad = []
        for s in samples:
            cols = max(1, s["cols"])
            cell_w = s["gridW"] / cols if s["gridW"] else 0
            for box in s["previews"]:
                # 形态 A：卡片被拉成整行宽（预览框超过一个网格单元）
                if cell_w and box["w"] > cell_w + 2:
                    bad.append((s["t"], "stretched", box, cell_w))
            for img in s["imgs"]:
                # 形态 B：图片按自然像素尺寸直接铺开
                if img["nw"] and img["w"] >= img["nw"] and img["h"] >= img["nh"]:
                    bad.append((s["t"], "natural", img))
        self.assertEqual(bad, [],
                         "实时刷新期间有 %d 帧把原图按自然尺寸铺开：%r"
                         % (len(bad), bad[:3]))
        # 刷新不能把列数刷掉
        cols = [s["cols"] for s in samples if s["cardCount"] > 0]
        self.assertTrue(cols, "没采到任何有卡片的帧")
        self.assertGreaterEqual(min(cols), 2,
                                "实时刷新期间网格塌成了 1 列：%r" % cols)

    # ---------------------------------------------- 5. 不回归
    def test_no_console_errors(self):
        """没有 JS 报错。"""
        self.assertEqual([e for e in self.errors if e.strip()], [])


if __name__ == "__main__":
    unittest.main()
