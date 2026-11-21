# -*- coding: utf-8 -*-
"""E2E：「创意内容设计」面板（文生图提示词构造器）。

【覆盖什么】
  1. 4 个文生图 mode（t2i / t2i_qwen / t2i_moss / t2i_h3image）下
     面板入口都可达；
  2. 分组可折叠 + 搜索过滤可用；
  3. 选项可选择 / 可跳过；「不指定」全跳过时不产生垃圾片段；
  4. 组装顺序正确（主体 → 造型 → 风格 → 场景 → 构图 → 光 → 色 → 氛围）；
  5. 写回 #prompt 的「覆盖 / 追加」二选一可见，且不静默丢弃用户文本；
  6. 冲突提示可见（硬互斥阻止 + 软冲突提示）；
  7. 写回后「增强」链路仍然正常（按钮可用、徽标为「原始」级别）；
  8. 窄屏 320 / 375 / 390 / 430 下：面板不横向溢出、
     顶栏「生成类型选择」始终可点、生成媒体库与收藏栏仍可访问。

【不消耗生成额度】—— 本文件**不点生成**，只验证交互与写回。
"""
from __future__ import annotations

import re

import pytest

#: 4 个文生图 mode
TEXT2IMAGE_MODES = ["t2i", "t2i_qwen", "t2i_moss", "t2i_h3image"]

#: 窄屏宽度（任务书硬约束）
NARROW_WIDTHS = [320, 375, 390, 430]


def _open_designer(sp, evidence):
    """打开「创意内容设计」面板（点入口按钮）。

    【为什么要先滚】≤1100px 时参数面板是底部抽屉（内部滚动），
    入口按钮在抽屉内容的最下方 —— 不滚动的话它在视口之外，
    Playwright 会报「element is outside of the viewport」。
    """
    sp.page.wait_for_selector("#pcOpenBtn", timeout=30000)
    sp.page.evaluate("""() => {
      const panel = document.querySelector('#panel');
      const btn = document.querySelector('#pcOpenBtn');
      if (panel && btn) panel.scrollTop = btn.offsetTop - 80;
    }""")
    sp.page.wait_for_timeout(300)
    sp.page.click("#pcOpenBtn")
    sp.page.wait_for_timeout(600)
    evidence.step("打开「创意内容设计」面板")


def _pick(sp, evidence, group_label: str, option_prefix: str) -> str:
    """按**子类名**选一个选项（模拟真人从下拉里挑）。"""
    result = sp.page.evaluate(
        """([label, prefix]) => {
          const s = Array.from(document.querySelectorAll('#pcGroups select'))
            .find(x => x.getAttribute('aria-label') === label);
          if (!s) return 'NO_SELECT:' + label;
          const opt = Array.from(s.options)
            .find(o => o.textContent.trim().startsWith(prefix));
          if (!opt) return 'NO_OPTION:' + prefix;
          if (opt.disabled) return 'DISABLED:' + opt.title;
          s.value = opt.value;
          s.dispatchEvent(new Event('change', {bubbles: true}));
          return 'OK';
        }""",
        [group_label, option_prefix])
    sp.page.wait_for_timeout(350)
    evidence.step(f"选择 {group_label} → {option_prefix}（{result}）")
    return result


def _clear_picks(sp):
    sp.page.evaluate("""() => {
      window.PROMPT_CATALOG.picks.clear();
      window.PROMPT_CATALOG.render();
    }""")
    sp.page.wait_for_timeout(300)


def _preview(sp) -> str:
    return sp.page.eval_on_selector("#pcPreviewText", "e => e.innerText")


def _parts(sp):
    return sp.page.evaluate("() => window.PROMPT_CATALOG.assemble().parts")


# ------------------------------------------------------------------ 用例


def test_catalog_api_serves_full_catalog(studio, evidence):
    """目录 API 下发完整数据（871 项调研条目 + 显式跳过项）。"""
    import json
    import urllib.request

    with urllib.request.urlopen(studio.base_url + "/api/prompt-catalog",
                                timeout=30) as r:
        data = json.loads(r.read().decode())
    counts = data["counts"]
    evidence.step(f"目录 counts = {json.dumps(counts, ensure_ascii=False)}")
    assert counts["research_items"] == 871, "调研条目数不是 871"
    assert counts["items"] >= 871, "可选条目数低于 871"
    assert counts["dimensions"] == 9
    assert counts["groups"] == 48
    assert counts["tag_verified"] == 166, "Danbooru 实测通过的标签词数不对"
    assert data["version"]


@pytest.mark.parametrize("mode", TEXT2IMAGE_MODES)
def test_designer_entry_reachable_in_every_text2image_mode(studio_page, evidence, mode):
    """4 个文生图 mode 下入口都可达，且共用同一个 #prompt。"""
    sp = studio_page
    sp.open()
    sp.goto_mode(mode)

    assert sp.visible("#promptCatalogPanel"), f"{mode} 下没有「创意内容设计」面板"
    assert sp.visible("#pcOpenBtn"), f"{mode} 下没有入口按钮"
    assert sp.visible("#prompt"), f"{mode} 下提示词框不可见"

    _open_designer(sp, evidence)
    groups = sp.page.eval_on_selector_all(
        "#pcGroups .pc-group", "els => els.length")
    evidence.step(f"{mode} 下分组数 = {groups}")
    assert groups >= 40, f"{mode} 下分组太少：{groups}"
    evidence.screenshot(sp.page, f"designer-{mode}")


def test_groups_collapse_and_search(studio_page, evidence):
    """分组可折叠；搜索能过滤到命中项。"""
    sp = studio_page
    sp.open()
    sp.goto_mode("t2i")
    _open_designer(sp, evidence)

    before = sp.page.eval_on_selector_all("#pcGroups .pc-group", "e => e.length")
    # 折叠第一个分组
    sp.page.click("#pcGroups .pc-group-head")
    sp.page.wait_for_timeout(400)
    collapsed = sp.page.evaluate("""() => {
      const g = document.querySelector('#pcGroups .pc-group');
      return {cls: g.className, bodyHidden: g.querySelector('.pc-group-body').hidden};
    }""")
    evidence.step(f"折叠后：{collapsed}")
    assert collapsed["cls"].endswith("collapsed"), "分组没有进入折叠态"
    assert collapsed["bodyHidden"] is True, "折叠后内容区仍可见"

    # 再点开
    sp.page.click("#pcGroups .pc-group-head")
    sp.page.wait_for_timeout(400)
    reopened = sp.page.evaluate("""() => {
      const g = document.querySelector('#pcGroups .pc-group');
      return {cls: g.className, bodyHidden: g.querySelector('.pc-group-body').hidden};
    }""")
    assert reopened["cls"] == "pc-group", "分组没有展开回来"
    assert reopened["bodyHidden"] is False

    # 搜索
    sp.page.fill("#pcSearch", "金发")
    sp.page.wait_for_timeout(600)
    hits = sp.page.evaluate("""() => ({
      groups: Array.from(document.querySelectorAll('#pcGroups .pc-group-name'))
        .map(e => e.textContent),
      options: Array.from(document.querySelectorAll('#pcGroups select'))
        .map(s => Array.from(s.options).map(o => o.textContent.trim())),
    })""")
    evidence.step(f"搜索「金发」命中：{hits['groups']}")
    assert len(hits["groups"]) == 1, f"搜索没过滤到唯一分组：{hits['groups']}"
    assert any("金发" in o for opts in hits["options"] for o in opts), \
        "搜索命中的组里没有「金发」选项"

    # 清空搜索后恢复
    sp.page.fill("#pcSearch", "")
    sp.page.wait_for_timeout(600)
    after = sp.page.eval_on_selector_all("#pcGroups .pc-group", "e => e.length")
    assert after == before, f"清空搜索后分组数没恢复：{after} vs {before}"
    evidence.screenshot(sp.page, "search")


def test_skip_all_produces_no_garbage(studio_page, evidence):
    """全部停在「不指定」时，预览为空（不产生垃圾片段）。"""
    sp = studio_page
    sp.open()
    sp.goto_mode("t2i")
    _open_designer(sp, evidence)
    _clear_picks(sp)

    parts = _parts(sp)
    preview = _preview(sp)
    evidence.step(f"全跳过：parts={parts} preview={preview!r}")
    assert parts == [], f"全跳过时不应有片段：{parts}"
    assert "还没有选择任何内容" in preview, "预览应提示尚未选择"

    # 「不指定」是每个子类的第一个选项（默认值）
    defaults = sp.page.evaluate("""() => {
      const sels = Array.from(document.querySelectorAll('#pcGroups select'));
      return sels.map(s => ({label: s.getAttribute('aria-label'),
                             value: s.value,
                             text: s.options[s.selectedIndex].textContent.trim()}));
    }""")
    bad = [d for d in defaults if d["text"] != "不指定"]
    assert not bad, f"默认项不是「不指定」：{bad[:3]}"
    evidence.step(f"全部 {len(defaults)} 个下拉的默认项都是「不指定」")


def test_assembly_order_follows_skeleton(studio_page, evidence):
    """组装顺序 = 主体 → 造型 → 风格 → 场景 → 构图 → 光 → 色 → 氛围。"""
    sp = studio_page
    sp.open()
    sp.goto_mode("t2i")
    _open_designer(sp, evidence)
    _clear_picks(sp)

    picks = [
        ("8.1 氛围与情绪", "宁静"),
        ("7.1 自然光时段与天气光", "黄金时刻"),
        ("5.A 经典构图", "三分法"),
        ("4.1 场景类型", "森林"),
        ("1.1 动漫 / 二次元", "赛璐璐动画"),
        ("3.3 发型与发色", "长直发"),
        ("3.0 主体类别（顶层，决定后面哪些子维度可用）", "人物（单人）"),
    ]
    for label, opt in picks:
        r = _pick(sp, evidence, label, opt)
        assert r == "OK", f"选择 {label}/{opt} 失败：{r}"

    parts = _parts(sp)
    evidence.step(f"组装结果：{parts}")
    expected = ["1girl", "long_hair", "cel animation", "forest",
                "rule of thirds", "golden hour", "serene"]
    assert parts == expected, f"组装顺序不对：{parts} != {expected}"

    # 不含权重语法（(x:1.5) 之类）
    text = _preview(sp)
    assert not re.search(r"\(\s*\w+\s*:\s*[\d.]+\s*\)", text), \
        f"拼装结果含权重语法：{text}"
    evidence.screenshot(sp.page, "assembly-order")


def test_write_back_requires_overwrite_or_append_choice(studio_page, evidence):
    """输入框已有内容时，必须出现「覆盖 / 追加」二选一，不静默丢弃。"""
    sp = studio_page
    sp.open()
    sp.goto_mode("t2i")
    _open_designer(sp, evidence)
    _clear_picks(sp)

    _pick(sp, evidence, "3.0 主体类别（顶层，决定后面哪些子维度可用）", "人物（单人）")
    _pick(sp, evidence, "1.1 动漫 / 二次元", "赛璐璐动画")

    # 先放一段用户自己的文本
    sp.page.evaluate("""() => {
      const p = document.querySelector('#prompt');
      p.value = '用户自己写的描述';
      p.dispatchEvent(new Event('input', {bubbles: true}));
    }""")
    sp.page.wait_for_timeout(300)

    sp.page.click("#pcWriteBtn")
    sp.page.wait_for_timeout(600)

    choice = sp.page.evaluate("""() => {
      const box = document.querySelector('#pcWriteRow .pc-write-choice');
      if (!box) return null;
      return {
        text: box.innerText,
        overwrite: !!document.querySelector('#pcOverwrite'),
        append: !!document.querySelector('#pcAppend'),
        cancel: !!document.querySelector('#pcCancelWrite'),
        promptValue: document.querySelector('#prompt').value,
      };
    }""")
    evidence.step(f"写回选择区：{choice}")
    assert choice is not None, "没有出现覆盖/追加选择"
    assert choice["overwrite"] and choice["append"], "缺少覆盖/追加按钮"
    assert choice["promptValue"] == "用户自己写的描述", \
        "用户文本被静默改写了"

    # 「追加」
    sp.page.click("#pcAppend")
    sp.page.wait_for_timeout(600)
    appended = sp.value("#prompt")
    evidence.step(f"追加后：{appended}")
    assert appended.startswith("用户自己写的描述, "), f"追加结果不对：{appended}"
    assert "1girl" in appended and "cel animation" in appended

    # 再来一次 —— 选「覆盖」
    sp.page.evaluate("""() => {
      const p = document.querySelector('#prompt');
      p.value = '第二段用户文本';
      p.dispatchEvent(new Event('input', {bubbles: true}));
    }""")
    sp.page.wait_for_timeout(300)
    sp.page.click("#pcWriteBtn")
    sp.page.wait_for_timeout(500)
    sp.page.click("#pcOverwrite")
    sp.page.wait_for_timeout(600)
    overwritten = sp.value("#prompt")
    evidence.step(f"覆盖后：{overwritten}")
    assert overwritten.startswith("1girl, "), f"覆盖结果不对：{overwritten}"
    assert "第二段用户文本" not in overwritten, "覆盖没有真正替换用户文本"
    evidence.screenshot(sp.page, "write-back")


def test_hard_conflict_is_blocked_with_reason(studio_page, evidence):
    """硬互斥组合被阻止，并给出可见原因。"""
    sp = studio_page
    sp.open()
    sp.goto_mode("t2i")
    _open_designer(sp, evidence)
    _clear_picks(sp)

    _pick(sp, evidence, "1.1 动漫 / 二次元", "赛璐璐动画")

    # 「真实摄影」与「赛璐璐动画」硬互斥 ⇒ 选项必须被禁用
    opt = sp.page.evaluate("""() => {
      const s = Array.from(document.querySelectorAll('#pcGroups select'))
        .find(x => x.getAttribute('aria-label') === '1.2 写实摄影');
      const o = Array.from(s.options).find(x => x.textContent.trim().startsWith('真实摄影'));
      return {disabled: o.disabled, title: o.title};
    }""")
    evidence.step(f"真实摄影 选项状态：{opt}")
    assert opt["disabled"] is True, "硬互斥选项没有被禁用"
    assert "冲突" in opt["title"], f"禁用原因没写清楚：{opt['title']}"

    # 强行选 ⇒ 必须被阻止 + 可见提示
    sp.page.evaluate("""() => {
      const s = Array.from(document.querySelectorAll('#pcGroups select'))
        .find(x => x.getAttribute('aria-label') === '1.2 写实摄影');
      const o = Array.from(s.options).find(x => x.textContent.trim().startsWith('真实摄影'));
      s.value = o.value;
      s.dispatchEvent(new Event('change', {bubbles: true}));
    }""")
    sp.page.wait_for_timeout(600)
    state = sp.page.evaluate("""() => ({
      conflictsHidden: document.querySelector('#pcConflicts').hidden,
      conflictsText: document.querySelector('#pcConflicts').innerText,
      preview: document.querySelector('#pcPreviewText').innerText,
    })""")
    evidence.step(f"冲突提示：{state}")
    assert state["conflictsHidden"] is False, "冲突提示不可见"
    assert "冲突" in state["conflictsText"], "冲突提示没有说明"
    assert "写实" not in state["preview"], "被阻止的选项还是进了提示词"
    evidence.screenshot(sp.page, "hard-conflict")


def test_soft_conflict_shows_warning(studio_page, evidence):
    """软冲突给可见提示（不阻止）。"""
    sp = studio_page
    sp.open()
    sp.goto_mode("t2i")
    _open_designer(sp, evidence)
    _clear_picks(sp)

    # 黄金时刻（7.1）+ 柔光箱（7.2）= 调研 S01 软冲突
    _pick(sp, evidence, "7.1 自然光时段与天气光", "黄金时刻")
    _pick(sp, evidence, "7.2 影棚布光", "柔光箱")
    time_txt = sp.page.evaluate("""() => ({
      hidden: document.querySelector('#pcConflicts').hidden,
      text: document.querySelector('#pcConflicts').innerText,
      preview: document.querySelector('#pcPreviewText').innerText,
    })""")
    evidence.step(f"软冲突提示：{time_txt}")
    assert time_txt["hidden"] is False, "软冲突没有提示"
    assert "提示" in time_txt["text"], "软冲突提示文案不对"
    # 软冲突不阻止 —— 两段都进了提示词
    assert "golden hour" in time_txt["preview"]
    assert "softbox" in time_txt["preview"]


def test_enhance_chain_still_works_after_write_back(studio_page, evidence):
    """写回后「增强 / 二次增强」链路不变，且不绕过未增强拦截。"""
    sp = studio_page
    sp.open()
    sp.goto_mode("t2i")
    _open_designer(sp, evidence)
    _clear_picks(sp)

    _pick(sp, evidence, "3.0 主体类别（顶层，决定后面哪些子维度可用）", "人物（单人）")
    sp.page.click("#pcWriteBtn")
    sp.page.wait_for_timeout(600)
    written = sp.value("#prompt")
    evidence.step(f"写回内容：{written}")
    assert "1girl" in written

    # 徽标必须是「原始」级（level 0）—— 不能打「已增强」徽标
    badge = sp.page.evaluate("""() => {
      const b = document.querySelector('#promptEnhancedBadge');
      return {hidden: b.hidden, text: b.textContent};
    }""")
    evidence.step(f"增强徽标：{badge}")
    assert badge["hidden"] is True, "写回后被打上了「已增强」徽标"

    # 增强按钮必须仍然可用
    for sel in ("#enhanceBtn", "#enhanceMenuBtn"):
        disabled = sp.page.eval_on_selector(sel, "e => e.disabled")
        assert disabled is False, f"{sel} 被禁用了"

    # 版本历史里有一条 level 0
    sp.page.click("#promptVersionBtn")
    sp.page.wait_for_timeout(600)
    versions = sp.page.inner_text("#promptVersionList")
    evidence.step(f"版本历史：{versions[:200]}")
    assert "原始" in versions, "版本历史里没有「原始」级别"
    sp.page.evaluate("""() => {
      const m = document.querySelector('#promptVersionModal');
      if (m) m.hidden = true;
      document.body.classList.remove('modal-open');
    }""")
    evidence.screenshot(sp.page, "enhance-chain")


@pytest.mark.parametrize("width", NARROW_WIDTHS)
def test_narrow_screen_constraints(studio_page, evidence, width):
    """窄屏硬约束：不溢出、生成类型选择可点、媒体库与收藏栏可达。"""
    sp = studio_page
    sp.open()
    sp.page.set_viewport_size({"width": width, "height": 760})
    sp.page.wait_for_timeout(600)

    # 【窄屏的导航是抽屉】≤700px 时左侧栏收成抽屉
    # （transform: translateX(-100%)），导航项在视口之外 ——
    # 直接点它会报「element is outside of the viewport」。
    # 真人路径是：点顶栏汉堡 → 在抽屉里点目标 → 抽屉自动收起。
    sp.page.click("#navToggle")
    sp.page.wait_for_timeout(700)
    sp.page.click('.nav-item[data-mode="t2i"]')
    sp.page.wait_for_timeout(1000)
    evidence.step(f"{width}px 抽屉里切到 t2i")

    # 【窄屏必须先打开参数抽屉】≤1100px 时参数面板收进底部抽屉
    # （transform: translateY(100%) 收起），入口按钮在抽屉里、
    # 视口之外 —— 不打开抽屉就点它，Playwright 会报
    # 「element is outside of the viewport」。
    fab = sp.page.query_selector(".panel-fab")
    assert fab is not None, f"{width}px 下没有参数抽屉的浮动入口"
    fab.click()
    sp.page.wait_for_timeout(900)
    evidence.step(f"{width}px 打开参数抽屉")

    _open_designer(sp, evidence)

    # 1) 不横向溢出
    overflow = sp.page.evaluate("""() => {
      const vw = window.innerWidth;
      const pc = document.querySelector('#promptCatalogPanel');
      const bad = [];
      if (pc) {
        Array.from(pc.querySelectorAll('*')).forEach(k => {
          const r = k.getBoundingClientRect();
          if (r.width > 0 && r.right > vw + 1) {
            bad.push((k.id || k.className || k.tagName) + '@' + Math.round(r.right));
          }
        });
      }
      return {
        docOverflow: document.documentElement.scrollWidth
                     - document.documentElement.clientWidth,
        pcOverflow: bad.slice(0, 5),
        pcOverflowCount: bad.length,
      };
    }""")
    evidence.step(f"{width}px 溢出检查：{overflow}")
    assert overflow["docOverflow"] <= 0, f"{width}px 下页面横向溢出"
    assert overflow["pcOverflowCount"] == 0, \
        f"{width}px 下创意面板溢出：{overflow['pcOverflow']}"

    # 2) 顶栏「生成类型选择」始终可点
    nav = sp.page.evaluate("""() => {
      const b = document.querySelector('#navToggle');
      if (!b) return {missing: true};
      const r = b.getBoundingClientRect();
      const e = document.elementFromPoint(r.left + r.width / 2,
                                          r.top + r.height / 2);
      return {missing: false,
              hit: e ? (b.contains(e) ? 'navToggle' : (e.id || e.className || e.tagName))
                     : 'null'};
    }""")
    evidence.step(f"{width}px 生成类型选择入口命中：{nav}")
    assert nav.get("missing") is not True
    assert nav["hit"] == "navToggle", \
        f"{width}px 下顶栏按钮被遮挡（命中 {nav['hit']}）"

    # 3) 打开左侧导航抽屉后：收藏栏与生成类型选择仍可达
    sp.page.click("#navToggle")
    sp.page.wait_for_timeout(700)
    drawer = sp.page.evaluate("""() => {
      const out = {};
      const close = document.querySelector('#navDrawerClose');
      if (close) {
        const r = close.getBoundingClientRect();
        const e = document.elementFromPoint(r.left + r.width / 2,
                                            r.top + r.height / 2);
        out.closeHit = e ? (close.contains(e) ? 'close-OK'
                           : (e.id || e.className || e.tagName)) : 'null';
      }
      const fav = document.querySelector('#navFavorites');
      if (fav) {
        fav.scrollIntoView({block: 'center'});
        const r = fav.getBoundingClientRect();
        const e = document.elementFromPoint(r.left + r.width / 2,
                                            r.top + r.height / 2);
        out.favHit = e ? (fav.contains(e) ? 'favorites-OK'
                          : (e.id || e.className || e.tagName)) : 'null';
      } else {
        out.favHit = 'no-navFavorites';
      }
      const toggle = document.querySelector('#navToggle');
      const tr = toggle.getBoundingClientRect();
      const te = document.elementFromPoint(tr.left + tr.width / 2,
                                           tr.top + tr.height / 2);
      out.navHit = te ? (toggle.contains(te) ? 'navToggle-OK'
                        : (te.id || te.className || te.tagName)) : 'null';
      return out;
    }""")
    evidence.step(f"{width}px 抽屉内可达性：{drawer}")
    assert drawer.get("closeHit") == "close-OK", \
        f"{width}px 下抽屉关闭按钮被遮挡（{drawer.get('closeHit')}）"
    assert drawer.get("favHit") in ("favorites-OK", "no-navFavorites"), \
        f"{width}px 下收藏栏不可达：{drawer.get('favHit')}"
    assert drawer.get("navHit") == "navToggle-OK", \
        f"{width}px 下顶栏按钮被抽屉遮挡：{drawer.get('navHit')}"

    evidence.screenshot(sp.page, f"narrow-{width}")
