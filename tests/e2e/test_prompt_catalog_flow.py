# -*- coding: utf-8 -*-
"""E2E：「创意内容设计」面板（文生图提示词构造器）。

【覆盖什么】
  1. 4 个文生图 mode（t2i / t2i_qwen / t2i_moss / t2i_h3image）下
     面板入口都可达；
  2. 分组可折叠 + 搜索过滤可用；
  3. 选项可选择 / 可跳过；「不指定」全跳过时不产生垃圾片段；
  4. 组装顺序正确（主体 → 造型 → 风格 → 场景 → 构图 → 光 → 色 → 氛围），
     且**默认是中文**（用户 2026-11-21 要求中文优先）；英文片段只作为
     辅助版本存在，不参与默认组装与写回；
  5. **每个分组**都有「不指定」默认项 + 自由文本入口（省略 / 自行输入）；
     只填部分维度也能正常组装；自定义内容不被 UI 或校验拦掉；
     用户直接编辑整段提示词时词库不干预，仍走既有增强链路；
  6. 写回 #prompt 的「覆盖 / 追加」二选一可见，且不静默丢弃用户文本；
  7. 冲突提示可见（硬互斥阻止 + 软冲突提示）；
  8. 写回后「增强」链路仍然正常（按钮可用、徽标为「原始」级别）；
  9. 窄屏 320 / 375 / 390 / 430 下：面板不横向溢出、
     顶栏「生成类型选择」始终可点、生成媒体库与收藏栏仍可访问、
     词库下拉与自行输入框 elementFromPoint 命中自己且真的能改值。

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
      window.PROMPT_CATALOG.customs.clear();
      window.PROMPT_CATALOG.render();
    }""")
    sp.page.wait_for_timeout(300)


def _type_custom(sp, evidence, group_label: str, text: str):
    """在某个分组的「自行输入」里键入内容（模拟真人打字）。"""
    result = sp.page.evaluate(
        """([label, text]) => {
          const inp = Array.from(document.querySelectorAll('#pcGroups [data-custom]'))
            .find(x => x.getAttribute('aria-label') === label + ' 自行输入');
          if (!inp) return 'NO_INPUT:' + label;
          inp.value = text;
          inp.dispatchEvent(new Event('input', {bubbles: true}));
          return 'OK';
        }""",
        [group_label, text])
    sp.page.wait_for_timeout(350)
    evidence.step(f"自行输入 {group_label} → {text!r}（{result}）")
    return result


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
    # 【中文优先】默认组装的是中文片段（用户 2026-11-21 明确要求）。
    expected = ["人物（单人）", "长直发", "赛璐璐动画", "森林",
                "三分法", "黄金时刻", "宁静"]
    assert parts == expected, f"组装顺序不对：{parts} != {expected}"

    # 不含权重语法（(x:1.5) 之类）
    text = _preview(sp)
    assert not re.search(r"\(\s*\w+\s*:\s*[\d.]+\s*\)", text), \
        f"拼装结果含权重语法：{text}"
    # 预览文本必须是中文为主（不是纯英文）
    zh_chars = len(re.findall(r"[\u4e00-\u9fff]", text))
    en_chars = len(re.findall(r"[A-Za-z]", text))
    evidence.step(f"预览文本：{text!r} 中文字符={zh_chars} 拉丁字符={en_chars}")
    assert zh_chars > en_chars, f"组装结果不是中文为主：{text!r}"
    evidence.screenshot(sp.page, "assembly-order")


def test_english_fragments_are_available_as_auxiliary_only(studio_page, evidence):
    """英文片段仍保留在数据里，但只作为辅助版本（不参与默认组装）。"""
    sp = studio_page
    sp.open()
    sp.goto_mode("t2i")
    _open_designer(sp, evidence)
    _clear_picks(sp)

    _pick(sp, evidence, "3.0 主体类别（顶层，决定后面哪些子维度可用）", "人物（单人）")
    _pick(sp, evidence, "1.1 动漫 / 二次元", "赛璐璐动画")

    zh_parts = _parts(sp)
    en_parts = sp.page.evaluate(
        "() => window.PROMPT_CATALOG.assemble({english: true}).parts")
    evidence.step(f"默认（中文）={zh_parts}  英文辅助版本={en_parts}")
    assert zh_parts == ["人物（单人）", "赛璐璐动画"], f"默认组装不是中文：{zh_parts}"
    assert en_parts == ["1girl", "cel animation"], f"英文辅助版本不对：{en_parts}"

    # 写回 #prompt 的必须是中文版本（英文只走「复制含英文片段版本」）
    sp.page.click("#pcWriteBtn")
    sp.page.wait_for_timeout(600)
    written = sp.value("#prompt")
    evidence.step(f"写回内容：{written}")
    assert "人物（单人）" in written and "1girl" not in written, \
        f"写回的不是中文版本：{written}"

    # 「复制含英文片段版本」按钮存在且可用
    disabled = sp.page.eval_on_selector("#pcCopyEn", "e => e.disabled")
    assert disabled is False, "「复制含英文片段版本」按钮不可用"
    evidence.screenshot(sp.page, "zh-vs-en")


def test_every_group_has_skip_and_free_input(studio_page, evidence):
    """每个可选分组都必须有「不指定」默认项 + 自由文本入口。

    【为什么必须逐组核】用户 2026-11-21 的新约束：
      「每个维度都必须允许省略，也必须允许自行输入。」
    只测一两个分组证明不了「每个」—— 必须遍历全部渲染出来的分组。
    """
    sp = studio_page
    sp.open()
    sp.goto_mode("t2i")
    _open_designer(sp, evidence)

    state = sp.page.evaluate("""() => {
      const out = [];
      document.querySelectorAll('#pcGroups .pc-group').forEach(g => {
        const gid = g.dataset.group;
        const name = (g.querySelector('.pc-group-name') || {}).textContent || gid;
        const sel = g.querySelector('select[data-select]');
        const inp = g.querySelector('[data-custom]');
        out.push({
          gid, name,
          hasSelect: !!sel,
          skipOption: sel ? Array.from(sel.options).some(o => o.textContent.trim() === '不指定') : null,
          defaultIsSkip: sel ? (sel.options[sel.selectedIndex] || {}).textContent.trim() === '不指定' : null,
          hasFreeInput: !!inp,
          placeholder: inp ? inp.getAttribute('placeholder') : null,
        });
      });
      return out;
    }""")
    evidence.step(f"全部 {len(state)} 个分组的省略/自定义入口")

    assert len(state) >= 45, f"渲染出的分组太少：{len(state)}"
    noInput = [g for g in state if not g["hasFreeInput"]]
    assert not noInput, f"这些分组没有自由输入入口：{noInput[:5]}"
    noSkip = [g for g in state if g["hasSelect"] and not g["skipOption"]]
    assert not noSkip, f"这些分组没有「不指定」选项：{noSkip[:5]}"
    badDefault = [g for g in state if g["hasSelect"] and not g["defaultIsSkip"]]
    assert not badDefault, f"这些分组的默认项不是「不指定」：{badDefault[:5]}"
    noPlaceholder = [g for g in state if not (g["placeholder"] or "").strip()]
    assert not noPlaceholder, f"这些分组的自由输入没有中文示例提示：{noPlaceholder[:5]}"

    # 「写法对照清单」那一组（8.4）没有下拉，但必须有自由输入
    guide = [g for g in state if not g["hasSelect"]]
    evidence.step(f"无下拉的分组（对照清单）：{[g['gid'] for g in guide]}")
    assert all(g["hasFreeInput"] for g in guide), "对照清单分组没有自由输入入口"
    evidence.screenshot(sp.page, "skip-and-custom")


def test_partial_fill_only_still_assembles(studio_page, evidence):
    """只填其中几项（其余全部省略）也能正常组装出可用的原始提示词。"""
    sp = studio_page
    sp.open()
    sp.goto_mode("t2i")
    _open_designer(sp, evidence)
    _clear_picks(sp)

    # 只填 2 项：主体 + 光线，其余 43 个分组全停在「不指定」
    r1 = _pick(sp, evidence, "3.0 主体类别（顶层，决定后面哪些子维度可用）", "人物（单人）")
    r2 = _pick(sp, evidence, "7.1 自然光时段与天气光", "黄金时刻")
    assert r1 == "OK" and r2 == "OK"

    parts = _parts(sp)
    preview = _preview(sp)
    evidence.step(f"只填 2 项的组装结果：{parts} / {preview!r}")
    assert parts == ["人物（单人）", "黄金时刻"], f"部分填写的组装结果不对：{parts}"
    assert preview == "人物（单人）, 黄金时刻", f"预览不对：{preview!r}"

    # 省略的分组不得产生任何片段（不写「无某某」这类否定式）
    assert "无" not in preview.replace("人物（单人）", ""), "省略项产生了否定式片段"

    # 面板没有卡死：写回按钮仍可用，且能真的写回
    disabled = sp.page.eval_on_selector("#pcWriteBtn", "e => e.disabled")
    assert disabled is False, "只填部分维度时写回按钮被禁用了"
    sp.page.click("#pcWriteBtn")
    sp.page.wait_for_timeout(600)
    assert sp.value("#prompt") == "人物（单人）, 黄金时刻", "部分填写写回结果不对"

    # 再省略一级（把光线改回「不指定」）也不破坏后续
    r3 = _pick(sp, evidence, "7.1 自然光时段与天气光", "不指定")
    assert r3 == "OK", f"把某一级改回不指定失败：{r3}"
    parts2 = _parts(sp)
    evidence.step(f"撤销一级后的组装结果：{parts2}")
    assert parts2 == ["人物（单人）"], f"撤销一级后结果不对：{parts2}"
    evidence.screenshot(sp.page, "partial-fill")


def test_free_input_joins_the_assembly_and_is_not_blocked(studio_page, evidence):
    """自行输入的自定义项与词库选项一起参与组装，且不被 UI/校验拦掉。"""
    sp = studio_page
    sp.open()
    sp.goto_mode("t2i")
    _open_designer(sp, evidence)
    _clear_picks(sp)

    # 1) 纯自定义（一个词库选项都不选）
    r = _type_custom(sp, evidence, "3.0 主体类别（顶层，决定后面哪些子维度可用）",
                    "一只戴护目镜的柯基犬")
    assert r == "OK", f"自由输入入口不可用：{r}"
    r = _type_custom(sp, evidence, "4.4 远景 / 背景", "远景是雾中的高压电塔")
    assert r == "OK"
    parts = _parts(sp)
    evidence.step(f"纯自定义的组装结果：{parts}")
    assert parts == ["一只戴护目镜的柯基犬", "远景是雾中的高压电塔"], \
        f"纯自定义没有进组装：{parts}"

    # 2) 词库选项 + 自定义混合：自定义按**它自己子类的槽位**插入，
    #    排在同槽位词库选项之后 —— 不是简单追加到末尾。
    #    （主体的自定义在 subject 槽 = 第 1 位；风格的自定义在 style 槽、
    #      排在风格词库选项之后；远景的自定义在 scene 槽。）
    _pick(sp, evidence, "1.1 动漫 / 二次元", "赛璐璐动画")
    _type_custom(sp, evidence, "1.1 动漫 / 二次元", "复古漫画网点")
    parts = _parts(sp)
    evidence.step(f"词库 + 自定义混合的组装结果：{parts}")
    assert parts == ["一只戴护目镜的柯基犬",      # subject（自定义）
                     "赛璐璐动画",                  # style（词库）
                     "复古漫画网点",                # style（自定义，同槽位在词库之后）
                     "远景是雾中的高压电塔",        # scene
                     ], f"混合组装不对：{parts}"

    # 3) 自定义内容不被校验拦掉：带数字/英文/标点的自由文本照样进组装
    _type_custom(sp, evidence, "7.1 自然光时段与天气光", "逆光, 85mm f/1.4 的浅景深光斑")
    parts = _parts(sp)
    evidence.step(f"含数字/英文的自定义：{parts}")
    assert "逆光, 85mm f/1.4 的浅景深光斑" in parts, f"自定义被拦掉了：{parts}"

    # 4) 写回：自定义内容照样进 #prompt，仍可走增强
    sp.page.click("#pcWriteBtn")
    sp.page.wait_for_timeout(600)
    written = sp.value("#prompt")
    evidence.step(f"写回内容：{written}")
    assert "一只戴护目镜的柯基犬" in written and "复古漫画网点" in written
    disabled = sp.page.eval_on_selector("#enhanceBtn", "e => e.disabled")
    assert disabled is False, "含自定义内容后增强按钮被禁用"

    # 5) 清空 = 真的不输出（不留空片段）
    sp.page.evaluate("""() => {
      document.querySelectorAll('#pcGroups [data-custom]').forEach(i => {
        i.value = '';
        i.dispatchEvent(new Event('input', {bubbles: true}));
      });
    }""")
    sp.page.wait_for_timeout(500)
    parts = _parts(sp)
    evidence.step(f"清空自定义后的组装结果：{parts}")
    assert "柯基" not in " ".join(parts), "清空自定义后仍残留片段"
    assert not any(p.strip() == "" for p in parts), f"产生了空片段：{parts}"
    evidence.screenshot(sp.page, "free-input")


def test_direct_prompt_edit_bypasses_catalog(studio_page, evidence):
    """用户直接编辑整段提示词时，绕过词库直接走原有增强链路。"""
    sp = studio_page
    sp.open()
    sp.goto_mode("t2i")
    _open_designer(sp, evidence)
    _clear_picks(sp)

    sp.page.evaluate("""() => {
      const p = document.querySelector('#prompt');
      p.value = '港风。90年代香港港口，亚洲年轻女性休闲散步';
      p.dispatchEvent(new Event('input', {bubbles: true}));
    }""")
    sp.page.wait_for_timeout(400)

    # 词库全停在「不指定」→ 预览为空，但用户的文本不受影响、增强按钮可用
    assert _parts(sp) == [], "词库没选任何项却产生了片段"
    assert "港风" in sp.value("#prompt"), "用户直接输入的文本被改写了"
    for sel in ("#enhanceBtn", "#enhanceMenuBtn"):
        assert sp.page.eval_on_selector(sel, "e => e.disabled") is False, \
            f"{sel} 被禁用，无法走既有增强链路"
    evidence.step("直接编辑整段提示词 → 词库不干预，增强链路照常可用")


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
    assert "人物（单人）" in appended and "赛璐璐动画" in appended

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
    assert overwritten.startswith("人物（单人）, "), f"覆盖结果不对：{overwritten}"
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
    assert "黄金时刻" in time_txt["preview"]
    assert "柔光箱" in time_txt["preview"]


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
    assert "人物（单人）" in written

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

    # 4) 【新约束】窄断点下「省略」与「自行输入」都可用
    #    取真实点击证据：一个词库下拉、一个自行输入框都必须
    #    elementFromPoint 命中自己（没被遮挡），并且真的能改值。
    #
    # 【为什么要先 Esc 再点 FAB】上面第 3 步开过左侧导航抽屉。
    # app.js 的规则是「两个抽屉任何一个开着，FAB 都让位」（updateResponsiveChrome）
    # ⇒ 此时 FAB 是 hidden，点它必然超时。
    # 真人路径：Esc 关掉抽屉 → 点「参数」浮钮回参数抽屉。照做一遍。
    sp.page.keyboard.press("Escape")
    sp.page.wait_for_timeout(600)
    fab2 = sp.page.query_selector(".panel-fab")
    assert fab2 is not None, f"{width}px 下没有参数抽屉的浮动入口"
    fab2.click()
    sp.page.wait_for_timeout(900)
    # 滚到入口按钮；设计面板若已展开就不该再点（那会把它收起）
    sp.page.evaluate("""() => {
      const panel = document.querySelector('#panel');
      const btn = document.querySelector('#pcOpenBtn');
      if (panel && btn) panel.scrollTop = btn.offsetTop - 80;
    }""")
    sp.page.wait_for_timeout(400)
    expanded = sp.page.eval_on_selector("#pcOpenBtn", "e => e.getAttribute('aria-expanded')")
    evidence.step(f"{width}px 参数抽屉已回位，设计面板 aria-expanded={expanded}")
    if expanded != "true":
        sp.page.click("#pcOpenBtn")
        sp.page.wait_for_timeout(700)

    usability = sp.page.evaluate("""() => {
      const out = {groups: 0, selectHit: null, customHit: null, missingSkip: []};
      const vw = window.innerWidth;
      const groups = Array.from(document.querySelectorAll('#pcGroups .pc-group'));
      out.groups = groups.length;
      /**
       * 把元素滚到可见位置 —— 逐层滚它自己的滚动容器。
       *
       * 【为什么不能只滚 .panel 或用 scrollIntoView】实测（320/375/390/430）：
       *   参数面板是 position:fixed 的底部抽屉（.panel overflow-y:auto），
       *   面板内部还有一层 .pc-groups（max-height:46vh; overflow-y:auto）。
       *   scrollIntoView 只滚了外层、元素 rect 停在 top=1076（视口 760）；
       *   只滚 .panel 也不够 —— 元素在 .pc-groups 这一层里还是看不见。
       *   于是断言拿到的是「元素在视口外」而不是「被谁遮挡」，等于没测。
       *   真人路径是：抽屉滚 + 分组列表滚。这里照做一遍。
       */
      const scrollIntoViewHere = (el) => {
        // 每次只滚**最近的一层**滚动容器，然后重新测量，迭代收敛。
        // （一次遍历全链会算错：滚完内层后外层的 rect 已经变了。）
        for (let step = 0; step < 8; step++) {
          const er = el.getBoundingClientRect();
          const vh = window.innerHeight, vw = window.innerWidth;
          if (er.top >= 4 && er.bottom <= vh - 4 && er.left >= 0 && er.right <= vw) return;
          let node = el.parentElement;
          let scrolled = false;
          while (node) {
            const cs = getComputedStyle(node);
            if (/(auto|scroll)/.test(cs.overflowY) && node.scrollHeight > node.clientHeight) {
              const nr = node.getBoundingClientRect();
              const mid = nr.top + Math.min(nr.height, vh) / 2;
              node.scrollTop += (er.top - mid);
              scrolled = true;
              break;
            }
            node = node.parentElement;
          }
          if (!scrolled) return;   // 没有可滚的容器了（真不可见）
        }
      };
      const hit = (el) => {
        scrollIntoViewHere(el);
        const r = el.getBoundingClientRect();
        const vw = window.innerWidth, vh = window.innerHeight;
        if (r.width <= 0 || r.height <= 0) return 'zero-size';
        if (r.right > vw + 1 || r.left < -1) return 'offscreen@' + Math.round(r.right);
        if (r.top < -1 || r.bottom > vh + 1) {
          return 'not-in-viewport@' + Math.round(r.top) + '..' + Math.round(r.bottom);
        }
        // 取元素自身范围内的点（夹到视口内），elementFromPoint 才有效
        const x = Math.max(1, Math.min(vw - 1, r.left + Math.min(6, r.width / 2)));
        const y = Math.max(1, Math.min(vh - 1, r.top + r.height / 2));
        const e = document.elementFromPoint(x, y);
        if (!e) return 'null-point@' + x + ',' + y;
        return (el.contains(e) || e.contains(el)) ? 'OK' : (e.id || e.className || e.tagName);
      };
      for (const g of groups) {
        const sel = g.querySelector('select[data-select]');
        if (sel && out.selectHit === null) out.selectHit = {gid: g.dataset.group, hit: hit(sel)};
        const inp = g.querySelector('[data-custom]');
        if (inp && out.customHit === null) out.customHit = {gid: g.dataset.group, hit: hit(inp)};
        if (out.selectHit && out.customHit) break;
      }
      document.querySelectorAll('#pcGroups select[data-select]').forEach(s => {
        const skip = Array.from(s.options).find(o => o.textContent.trim() === '不指定');
        if (!skip) out.missingSkip.push(s.dataset.select);
      });
      return out;
    }""")
    evidence.step(f"{width}px 省略/自定义可用性：{usability}")
    assert usability["groups"] >= 45, f"{width}px 下分组渲染不全：{usability['groups']}"
    assert usability["selectHit"] and usability["selectHit"]["hit"] == "OK", \
        f"{width}px 下词库下拉被遮挡：{usability['selectHit']}"
    assert usability["customHit"] and usability["customHit"]["hit"] == "OK", \
        f"{width}px 下自行输入框被遮挡：{usability['customHit']}"
    assert not usability["missingSkip"], \
        f"{width}px 下这些分组没有「不指定」：{usability['missingSkip'][:5]}"

    # 真实操作：选词库项 → 自行输入 → 改回「不指定」，三步都要真的生效
    _clear_picks(sp)
    pick_ok = _pick(sp, evidence, "3.0 主体类别（顶层，决定后面哪些子维度可用）", "人物（单人）")
    assert pick_ok == "OK", f"{width}px 下选择词库项失败：{pick_ok}"
    custom_ok = _type_custom(sp, evidence, "4.4 远景 / 背景", "远景是雾中的高压电塔")
    assert custom_ok == "OK", f"{width}px 下自行输入失败：{custom_ok}"
    narrow_parts = _parts(sp)
    evidence.step(f"{width}px 组装结果：{narrow_parts}")
    assert narrow_parts == ["人物（单人）", "远景是雾中的高压电塔"], \
        f"{width}px 下组装不对：{narrow_parts}"
    skip_ok = _pick(sp, evidence, "3.0 主体类别（顶层，决定后面哪些子维度可用）", "不指定")
    assert skip_ok == "OK", f"{width}px 下改回「不指定」失败：{skip_ok}"
    assert _parts(sp) == ["远景是雾中的高压电塔"], \
        f"{width}px 下省略没有生效：{_parts(sp)}"

    evidence.screenshot(sp.page, f"narrow-{width}")
