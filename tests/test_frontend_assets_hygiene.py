# -*- coding: utf-8 -*-
"""第 3 轮收口回归测试：A-6（HTML 注释进 DOM）+ D-2（静态资源版本号过期）。

【为什么要有这个测试】

A-6 —— HTML 注释不渲染，但**会进 DOM**。用户「查看源代码」「复制页面
内容」都能带走，里面是内部日期标记 + 需求编号（A2 / A3 补漏）这类纯
开发语境。第 1 轮只修了 ``web/app.js`` 的 jobCard 模板，漏了
``web/assets.js`` 的两处（素材卡模板 + 右键菜单模板）。所以断言必须
**覆盖 web/ 下全部 .js**，而不是某一个文件的一个模板。

D-2 —— ``/static/*`` 走 aiohttp 默认静态处理，**没有 Cache-Control**
（实测：只有 Etag + Last-Modified）。浏览器于是套用「启发式新鲜度」
（10% × 资源年龄），在这段时间里**根本不回源**。``index.html`` 自身
是 ``no-cache, must-revalidate``，所以它每次都拿新的 ``?v=`` 清单 ——
但如果资源内容在设值之后又改过而 ``?v=`` 没跟着升，老用户拿到的仍是
旧文件。判据必须是「该资源内容在上次设值之后是否又变过」，而不是
「merge commit 在不在后面」。

【本文件的两条断言都做过变异自证】
- ``test_no_html_comment_in_any_js_template``：往任意 web/*.js 注入
  一段 HTML 注释 -> 断言必须失败。
- ``test_bumped_static_assets_are_not_stale``：把清单里某个资源的
  ``?v=`` 改回旧值 -> 断言必须失败。

运行：
    python3 -m pytest tests/test_frontend_assets_hygiene.py -q --tb=short
"""

import os
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WEB = REPO / "web"


# ==========================================================================
# A-6：web/ 下全部 .js 的 innerHTML 模板不得含 HTML 注释
# ==========================================================================

class TestNoHtmlCommentInAnyJsTemplate(unittest.TestCase):
    """A-6 关卡：HTML 注释不得进入任何 JS 的 innerHTML 模板。"""

    def _js_files(self):
        return sorted(p for p in os.listdir(WEB) if p.endswith(".js"))

    def test_no_html_comment_in_any_js_template(self):
        """web/ 下**所有** .js 都不许含 ``<!--``。

        【为什么必须扫全目录】第 1 轮的断言只扫 ``app.js`` 的 jobCard
        一个模板，于是 ``assets.js`` 的两处（素材卡多选勾选框注释、
        右键菜单「添加到播放器」注释）漏网。HTML 注释不渲染但会进
        DOM 注释节点 —— 用户「查看源代码」能带走。
        """
        hits = []
        files = self._js_files()
        self.assertTrue(files, "web/ 下没找到任何 .js，测试本身失效")
        for fn in files:
            src = (WEB / fn).read_text(encoding="utf-8", errors="replace")
            for i, line in enumerate(src.splitlines(), 1):
                if "<!--" in line:
                    hits.append(f"web/{fn}:{i} :: {line.strip()[:120]}")
        self.assertEqual(
            [], hits,
            "以下 web/*.js 里仍有 HTML 注释（会随 innerHTML 进 DOM）：\n  "
            + "\n  ".join(hits))

    def test_html_comment_check_is_not_vacuous(self):
        """护栏：断言本身不能空跑 —— 确认 web/ 下确实有 .js 被扫到。"""
        files = self._js_files()
        self.assertGreaterEqual(len(files), 20,
                                f"web/ 下 .js 数量异常少（{len(files)}），"
                                "A-6 断言可能形同虚设")

    def test_assets_js_has_no_html_comment(self):
        """针对本轮实际修掉的两处，单独点名，防止以后又被塞回来。"""
        src = (WEB / "assets.js").read_text(encoding="utf-8")
        self.assertNotIn(
            "<!--", src,
            "web/assets.js 里还有 HTML 注释（素材卡模板 / 右键菜单模板）")

    def test_assets_js_user_visible_structure_preserved(self):
        """移动注释出去之后，模板里的 DOM 结构与文案必须一字不变。"""
        src = (WEB / "assets.js").read_text(encoding="utf-8")
        # 多选勾选框：结构必须还在
        self.assertIn('class="al-selbox-wrap"', src,
                      "assets.js 的多选勾选框结构被误删")
        self.assertIn('class="al-selbox"', src,
                      "assets.js 的多选勾选框 input 被误删")
        # 右键菜单：两个播放器按钮必须还在
        self.assertIn('data-act="add-player"', src,
                      "assets.js 右键菜单的「添加到播放器」按钮被误删")
        self.assertIn('data-act="remove-player"', src,
                      "assets.js 右键菜单的「从播放器移除」按钮被误删")

    def test_assets_js_comment_moved_outside_template(self):
        """注释必须以 JS 注释（``//``）留在模板**外面**，而不是被删掉。

        【为什么要这样约束】直接把注释删掉也算「没有 HTML 注释」，
        但会丢掉维护者需要的上下文。本轮的做法是把它们移到模板外面
        变成 ``//`` 注释 —— 既不进 DOM，又保留信息。
        """
        src = (WEB / "assets.js").read_text(encoding="utf-8")
        self.assertIn("多选勾选框", src,
                      "assets.js 里找不到「多选勾选框」注释 —— 说明是被删掉了，"
                      "而不是被移到模板外")
        self.assertIn("添加到播放器清单", src,
                      "assets.js 里找不到「添加到播放器清单」注释 —— 说明是被删掉了，"
                      "而不是被移到模板外")


# ==========================================================================
# D-2：静态资源 ?v= 版本号不得过期
# ==========================================================================

# 本轮判定为「过期」的 14 个资源：内容在设值之后又变过。
# （判据 = 「该资源文件内容相对其 ?v= 引入点是否发生过变化」，
#   用 git 走历史 + 内容 diff 实测得到，见 docs/audit-evidence/）
STALE_BUMPED = {
    "assets.css":              ("uxbatch1",     "cr20261115b"),
    "responsive.css":          ("agentpower1",  "cr20261115b"),
    # 【2026-11-19 第 3 轮 A 项】qwen-refs.js 的旧条目 ("fix3preview1", …)
    #   同样被下面第 6 轮的 ("cr20261115b", "cr20261118a") 覆盖（隐藏条目）。
    "video-panel-fallback.js": ("vpfb1",        "cr20261115b"),
    # 【2026-11-19 第 3 轮 A 项】continuation.js 的旧条目 ("continuation1", …)
    #   同样被下面第 6 轮的 ("cr20261115b", "cr20261118a") 覆盖（隐藏条目）。
    "job-meta.js":             ("jobmeta3",     "cr20261115b"),
    # 【2026-11-19 第 3 轮 A 项】providers.js 的旧条目 ("h3image1", …)
    #   曾被后面第 6 轮的 ("cr20261115b", "cr20261118a") **覆盖** ——
    #   Python dict 字面量后写覆盖先写 ⇒ 前一条是**隐藏条目**。
    #   已把信息并入下面的生效条目，此处只留注释（见下方 providers.js 条目）。
    "assets.js":               ("uxbatch1",     "cr20261115b"),
    "asset-player.js":         ("uxbatch1",     "cr20261115b"),
    "enhance-menu-shared.js":  ("shared2",      "cr20261115b"),
    # 【2026-11-16 S5】enhance-menu.js 内容又变了（director 补显式
    # write 契约声明）⇒ ?v= 必须再升一次（cr20261115b -> cr20261116b）。
    # 【2026-11-19 第 3 轮 A 项】表过期 —— commit 59707f5
    #   「fix(enhance): 提示词增强必须把已引用素材传给模型」**合法地**改了
    #   enhance-menu.js 的内容（三条增强路径都提交当前面板已引用的素材）
    #   并把 ?v= 从 cr20261116b 升到 enhassets1，**但没同步这张表**
    #   ⇒ 本条一直在报假红。
    #   判定：**表过期，不是真漂移** —— enhassets1 是正确的当前值，
    #   且 enhance-menu.js 的内容确实变了（必须保留这个新版本号）。
    #   ⇒ 同步表：旧值取**直接前值** cr20261116b，新值取当前 enhassets1。
    "enhance-menu.js":         ("cr20261116b", "enhassets1"),
    "music-enhance.js":        ("uxbatch1",     "cr20261115b"),
    # 【2026-11-16 内置 Agent 审查】agent.js 内容又变了（SSE 失败路径
    #   保留「重试」入口的修复）⇒ ?v= 必须再升一次
    #   （cr20261115b -> cr20261116b）。漏升 = 老缓存用户拿不到修复。
    # 口径与 enhance-menu.js 一致：记「最初的旧值」与「当前最新值」。
    # 【2026-11-19 第 7 轮审查】agent.js 再次改内容（并行任务 76eac36
    #   的生成状态修复）⇒ ?v= 升到 genfix20261119a（见下方生效条目）。
    # 【2026-11-19 生成状态缺陷修复】agent_sessions.js 的内容也被改过
    #   （76eac36：「done + 无产物」显示明确失败占位，而不是空黑框）
    #   ⇒ ?v= 必须升到 genfix20261119a。
    #   【为什么这条必须有】/static/* 没有 Cache-Control，浏览器按启发式
    #   新鲜度缓存。内容变了而 ?v= 不变 ⇒ 老用户拿到的仍是旧文件，
    #   修复等于没上线。
    "agent_sessions.js":       ("agtsess2",     "genfix20261119a"),
    # app.js 承载第 3 轮的 Critical B-2 修复（提交 c6676e6 改了它 +35 行），
    # 但那个提交没升它的 ?v= ⇒ 浏览器拿到的仍是旧 app.js，修复不生效。
    # 【2026-11-16 第 4 轮】app.js 再次改内容（B-3/B-4/B-6/B-7/B-9/B-10/
    #   B-11/B-12）⇒ ?v= 必须再升一次（cr20261115c -> cr20261115d）。
    # 【2026-11-16 第 5 轮】app.js 再次改内容（A 段骨架卡文案泄漏修复）
    #   ⇒ ?v= 必须再升一次（cr20261115d -> cr20261116a）。
    # 【2026-11-17 素材库选择回调修复】app.js 再次改内容（点空槽位补传
    #   onPick 回调 + onPick 必填守卫）⇒ ?v= 必须再升一次
    #   （cr20261116b -> cr20261116c）。
    #   【为什么这一步是必须的】/static/* 没有 Cache-Control，浏览器按启发式
    #   新鲜度缓存。app.js 内容变了而 ?v= 不变 ⇒ 老用户浏览器拿到的仍是
    #   **旧的坏文件**（漏传 onPick 那版），修复等于没上线。
    # 【2026-11-18 第 6 轮审查】app.js 再次改内容（flf/r2v 上限 2.0MP 的
    #   前端镜像 + 负面提示词提示的 markdown 残留清理）⇒ ?v= 升到 cr20261118a。
    # 【2026-11-19 接续链收口】app.js 再次改内容（接续上下文提交提到三模式
    #   公共作用域 —— flf/r2v 面板发起接续时必须带 params.continuation，
    #   否则本段静默退化成普通生成）⇒ ?v= 必须再升一次
    #   （cr20261118a -> cont20261119a）。
    #   【为什么这一步是必须的】/static/* 没有 Cache-Control，浏览器按启发式
    #   新鲜度缓存。app.js 内容变了而 ?v= 不变 ⇒ 老用户浏览器拿到的仍是
    #   **旧 app.js**（不带 continuation 提交的那个版本）⇒ 修复等于没上线。
    # 【2026-11-19 第 7 轮审查】并行任务（76eac36「视频不再 30 分钟判超时
    #   + OOM 不再误报成功」）改了 style.css / generation-queue.js /
    #   app.js / job-detail.js / agent.js 的内容，并把它们与 favicon.svg
    #   的 ?v= 统一升到 genfix20261119a。
    #   ⇒ 表要跟着走，否则这张断言自己变成假红（它已经报过 2 轮假红）。
    #   旧值取**直接前值**（各文件自己的上一个版本号）。
    "app.js":                  ("cont20261119a", "genfix20261119b"),
    "generation-queue.js":     ("a5truth1",      "genfix20261119a"),
    "job-detail.js":           ("a5truth1",      "genfix20261119a"),
    "agent.js":                ("cr20261116b",   "genfix20261119a"),
    # 【2026-11-19 第 7 轮审查】并行任务 76eac36 改了 style.css 的内容
    #   （生成状态修复的样式）⇒ ?v= 升到 genfix20261119a。
    "style.css":               ("cr20261115a",   "genfix20261119b"),
    # video-panel.js 改内容（B-5：submitParams 不再裸引用 app.js 全局 state）
    # ⇒ ?v= 必须升（ac1steps1 -> ac1steps2）。
    # 【2026-11-17】video-panel.js 此前 ?v= 已是 vparams3（比表里的
    #   ac1steps2 新）⇒ 表里的期望值同步到当前线上值，并再升一档到
    #   vparams4，保持「内容变了必须升版号」的口径闭合。
    # 【2026-11-17 晚】并行任务（二采 NFE 预算护栏）又改了 video-panel.js
    #   并把它升到 vparams5 ⇒ 期望值同步到 vparams5。
    #   （这张表是「内容变了必须升版号」的关卡；版本号被别人推进时，
    #     表要跟着走，否则关卡自己变成假红 —— 那正是它之前一直在报红的原因。）
    # 【2026-11-18 第 6 轮审查】video-panel.js 又改了内容（加速 LoRA
    #   提示里的 markdown 残留清理 + flf/r2v 上限 2.0MP 的文案同步）
    #   ⇒ ?v= 升到 vparams6。
    # 【2026-11-19 R2 可见文案】video-panel.js 又改了内容（模型「位置」
    #   只显示目录名，不再把本机绝对路径 /mnt/nvme1t_colorful/... 显示
    #   给用户）⇒ ?v= 必须再升一次（vparams6 -> vparams7）。
    #   【为什么这一步是必须的】/static/* 没有 Cache-Control，浏览器按
    #   启发式新鲜度缓存。内容变了而 ?v= 不变 ⇒ 老用户拿到的仍是旧文件。
    "video-panel.js":          ("vparams5",       "vparams7"),
    # 【2026-11-18 第 6 轮审查】以下资源的内容在本轮被改过
    #   （接续面板 markdown 残留 / 参考图提示 / 引擎自检说明 / 清理确认
    #    / token 预算说明）⇒ ?v= 必须升，否则老用户拿到的仍是旧文件。
    "continuation.js":         ("cr20261118a", "contsw20261119a"),
    "qwen-refs.js":            ("cr20261115b", "cr20261118a"),
    # 【2026-11-19 第 3 轮 A 项】providers.js 同属 commit 59707f5 的
    #   「表过期」—— 该 commit 改了 providers.js 的内容（增强结果 meta 行
    #   显示素材上下文）并把 ?v= 从 cr20261118a 升到 enhassets1，
    #   但没同步这张表 ⇒ 一直报假红。
    #   判定：**表过期，不是真漂移** —— enhassets1 是正确的当前值。
    #   旧值取**直接前值** cr20261115b（cr20261118a 是它上一版的中间态，
    #   已被 enhassets1 取代）。
    "providers.js":            ("cr20261115b", "enhassets1"),
    "media-manage.js":         ("uxbatch1",     "cr20261118a"),
    "agent-manage.js":         ("a4gen1",       "cr20261118a"),
}

# 相对「内容最后改动时刻」已经同步、不需要再升的资源（本轮实测为 ok）
#
# 【app.js 的口径修正 —— 第 3 轮收口时踩到的自指坑】
# app.js 曾在 NOT_STALE_OK 里（?v=ac1steps1），理由是「它的内容在
# cd7f175 升版号之后没再变过」。但第 3 轮收口的 B-2 修复**本身**
# 就改在 web/app.js 里（+35 行，提交 c6676e6）—— 于是 app.js 在
# 同一个提交里「内容变了、版本号没变」，正是 D-2 要消灭的那类缺陷：
# 浏览器拿到的仍是旧 app.js（Critical 的跨模式槽位串台修复不生效），
# 而 /static/* 没有 Cache-Control，浏览器会按启发式新鲜度缓存。
# ⇒ app.js 已改到 STALE_BUMPED；NOT_STALE_OK 里只留「本轮及之后
#    内容确实没再变过」的资源。判定仍然用 git 历史 + 内容 diff 实测。
NOT_STALE_OK = {
    # 【2026-11-19 第 7 轮审查】style.css / favicon.svg 原先在这里
    #   （?v=cr20261115a），但并行任务 76eac36 改了 style.css 的内容
    #   （生成状态修复的样式）并把 ?v= 升到 genfix20261119a
    #   ⇒ 移入 STALE_BUMPED（下面）。
    # 【2026-11-18 第 6 轮审查】media-manage.js 原先在这里
    #   （?v=uxbatch1），但本轮它的内容被改了（清理确认弹窗里的
    #   markdown 残留清理）⇒ 已升到 cr20261118a 并移入 STALE_BUMPED。
    #   同一份文件不能同时出现在两张表里（否则这张断言必红）。
}


class TestStaticAssetVersionBump(unittest.TestCase):
    """D-2 关卡：静态资源 ?v= 版本号不得过期。"""

    def _index_html(self):
        return (WEB / "index.html").read_text(encoding="utf-8")

    def _current_version(self, fname):
        html = self._index_html()
        m = re.search(r'/static/' + re.escape(fname) + r'\?v=([^"\'\s>]+)', html)
        self.assertIsNotNone(m, f"index.html 里找不到 {fname} 的 ?v= 引用")
        return m.group(1)

    def test_bumped_static_assets_are_not_stale(self):
        """清单里的资源，``?v=`` 不得等于旧值（= 过期）。

        【表驱动而不是动态探测】动态探测要依赖 git 历史，在别的
        worktree / CI 上未必可复现。表驱动 + 「不得等于旧值」是
        离线可复现的，且对「忘了升版号」这个回归有牙齿。
        """
        bad = []
        for fname, (old_v, new_v) in sorted(STALE_BUMPED.items()):
            cur = self._current_version(fname)
            if cur == old_v:
                bad.append(f"{fname}: ?v= 仍是旧值 {old_v}（内容已变，缓存破不了）")
            elif cur != new_v:
                bad.append(f"{fname}: ?v= 是 {cur}，期望 {new_v}")
        self.assertEqual([], bad, "静态资源版本号问题：\n  " + "\n  ".join(bad))

    def test_stale_bumped_table_has_no_duplicate_keys(self):
        """STALE_BUMPED 表里**不得有重复 key**。

        【为什么要有这条】—— 2026-11-19 第 3 轮 A 项查出的真实根因
        Python dict 字面量是**后写覆盖先写**。表里曾同时存在
            "providers.js":    ("h3image1",     "cr20261115b"),
            "providers.js":    ("cr20261115b", "cr20261118a"),
        前一条被**静默覆盖** —— 它既不生效、也不报错，
        只在「读源码的人」眼里还在。这是**隐藏条目**：
        表看起来有 21 条，实际生效只有 18 条。
        而且它会让「旧值」不再是真的最初旧值（口径被破坏）。

        判据：从**源码文本**里正则抓出所有 `"文件名": (` 出现，
        任何出现 >1 次的文件名都算违规。
        （不能只查 dict —— dict 里已经看不到重复了。）
        """
        src = (REPO / "tests" / "test_frontend_assets_hygiene.py").read_text(
            encoding="utf-8")
        blk = src[src.index("STALE_BUMPED = {"):]
        blk = blk[:blk.index("\n}") + 2]
        names = re.findall(r'"([^"]+)"\s*:\s*\(', blk)
        counts = {}
        for n in names:
            counts[n] = counts.get(n, 0) + 1
        dups = {k: v for k, v in counts.items() if v > 1}
        self.assertEqual({}, dups,
                         f"STALE_BUMPED 表里有重复 key（后写覆盖先写 = 隐藏条目）: {dups}")

    def test_stale_bumped_table_matches_index_html(self):
        """表的**生效条目**必须与 index.html 里的实际 ?v= 一致。

        【为什么要有这条】—— 2026-11-19 第 3 轮 A 项的真实故障模式
        commit 59707f5 合法地改了 enhance-menu.js / providers.js 的内容
        并把 ?v= 升到 enhassets1，**但没同步这张表** ⇒
        `test_bumped_static_assets_are_not_stale` 报假红，
        而这条测试（表 vs 实际）能**直接指出不一致在哪个文件**。

        本测试不依赖 git 历史（别的 worktree / CI 上可复现），
        只读 index.html 与表 —— 与既有断言同一口径。
        """
        html = self._index_html()
        actual = {}
        for m in re.finditer(
                r'(?:src|href)="/static/([^"?]+)(?:\?v=([^"\'\s>]*))?', html):
            actual[m.group(1)] = m.group(2)
        mismatch = []
        for fname, (_old_v, new_v) in sorted(STALE_BUMPED.items()):
            cur = actual.get(fname)
            if cur != new_v:
                mismatch.append(f"{fname}: 表期望 {new_v}，index.html 实际 {cur}")
        self.assertEqual([], mismatch,
                         "STALE_BUMPED 表与 index.html 的 ?v= 不一致：\n  "
                         + "\n  ".join(mismatch))

    def test_bumped_assets_still_exist(self):
        """清单里的资源文件必须真实存在（防止清单本身写错）。"""
        missing = [f for f in STALE_BUMPED if not (WEB / f).exists()]
        self.assertEqual([], missing, f"清单里的静态资源不存在：{missing}")

    def test_new_version_is_not_shared_with_old(self):
        """新版本号必须与旧值不同（否则等于没升）。"""
        for fname, (old_v, new_v) in STALE_BUMPED.items():
            self.assertNotEqual(old_v, new_v,
                                f"{fname}: 新旧版本号相同，等于没升")

    def test_untouched_assets_keep_their_version(self):
        """本轮判定为「不过期」的资源，不得被顺手改掉（防止过度升版）。"""
        for fname, ver in sorted(NOT_STALE_OK.items()):
            cur = self._current_version(fname)
            self.assertEqual(ver, cur,
                             f"{fname}: ?v= 应保持 {ver}，实际 {cur}")

    def test_every_static_ref_has_version_param(self):
        """每个 /static/ 引用都必须带 ?v=（漏一个就漏一个缓存破除点）。"""
        html = self._index_html()
        refs = re.findall(r'(?:src|href)="/static/([^"?]+)(?:\?v=([^"\'\s>]*))?"', html)
        missing = [f for f, v in refs if not v]
        self.assertEqual([], missing,
                         f"这些静态资源没有 ?v= 参数：{missing}")

    def test_static_ref_points_to_existing_file(self):
        """引用了但文件不存在 -> 404 -> 整个面板脚本挂掉。"""
        html = self._index_html()
        refs = re.findall(r'(?:src|href)="/static/([^"?]+)', html)
        missing = [f for f in refs if not (WEB / f).exists()]
        self.assertEqual([], missing, f"index.html 引用了不存在的静态文件：{missing}")

    def test_server_static_has_no_cache_control_is_known_risk(self):
        """记录一个**已知的**服务端事实：``/static/*`` 没有 Cache-Control。

        【为什么把它写成断言】如果哪天有人给 ``/static/*`` 加上了
        ``no-cache``，那 ``?v=`` 机制就变成冗余，但本测试的「版本号
        必须升」仍然有效（无害）。反过来，如果服务端开始发
        ``immutable``，浏览器会**永久**缓存旧 URL 的内容 —— 那
        ``?v=`` 就是唯一防线，必须升。这条断言锁住「服务端不会
        偷偷给静态资源发 immutable」。
        """
        server_py = (REPO / "server.py")
        if not server_py.exists():
            self.skipTest("找不到 server.py")
        src = server_py.read_text(encoding="utf-8", errors="replace")
        # 若将来有人加了 immutable，必须同步复核 ?v= 升版策略
        self.assertNotIn(
            "immutable", src,
            "server.py 出现了 'immutable' 缓存指令 —— ?v= 是唯一破缓存手段，"
            "请复核静态资源升版号策略")


if __name__ == "__main__":
    unittest.main()
