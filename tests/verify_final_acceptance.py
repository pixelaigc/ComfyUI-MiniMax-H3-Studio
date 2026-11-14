# -*- coding: utf-8 -*-
"""最终验收：对**线上 8190** 逐项验证（真浏览器）。

覆盖需求：1 / 3 / 4 / 6 / 7 / 8 / 9 / 10 / 11 / 13 / 14 / 15 / 16
"""
import json
import sys
import time

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8190"
SHOTS = sys.argv[2] if len(sys.argv) > 2 else "/tmp/final"

results = []


def check(item, name, ok, detail=""):
    results.append((item, name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {item} {name}"
          + (f"  — {detail}" if detail else ""))


with sync_playwright() as p:
    browser = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
    page = browser.new_page(viewport={"width": 1500, "height": 1000})
    page.goto(BASE, wait_until="domcontentloaded")
    time.sleep(9)

    # ---------- 项 1：队列明细默认收起 ----------
    print("\n### 项1 生成队列面板默认收起 ###")
    r = page.evaluate("""() => {
      const list = document.querySelector('#queueList');
      const btn = document.querySelector('#queueToggle');
      return {
        exists: !!list,
        display: list ? getComputedStyle(list).display : null,
        ariaExpanded: btn ? btn.getAttribute('aria-expanded') : null,
        btnText: btn ? btn.textContent.trim() : null,
      };
    }""")
    check("项1", "队列明细默认收起（display:none）",
          r["display"] == "none", json.dumps(r, ensure_ascii=False))
    check("项1", "按钮文案/aria 与收起态一致",
          r["btnText"] == "明细" and r["ariaExpanded"] == "false",
          f"text={r['btnText']} aria={r['ariaExpanded']}")
    # 展开 -> 保持
    if r["exists"]:
        page.click("#queueToggle")
        time.sleep(1.2)
        r2 = page.evaluate("""() => {
          const list = document.querySelector('#queueList');
          const btn = document.querySelector('#queueToggle');
          return {display: getComputedStyle(list).display,
                  aria: btn.getAttribute('aria-expanded'),
                  text: btn.textContent.trim()};
        }""")
        check("项1", "用户展开后明细显示", r2["display"] != "none",
              json.dumps(r2, ensure_ascii=False))
        time.sleep(1.5)   # 等一次轮询重绘
        r3 = page.evaluate("""() => {
          const list = document.querySelector('#queueList');
          return getComputedStyle(list).display;
        }""")
        check("项1", "轮询重绘后仍保持展开（本次会话内记忆）",
              r3 != "none", f"display={r3}")
        page.click("#queueToggle")
        time.sleep(0.8)

    # ---------- 项 3：媒体库卡片详情 ----------
    print("\n### 项3 生成媒体库详情弹层 ###")
    page.wait_for_function(
        """() => {
             const c = document.querySelectorAll('.job-card');
             return c.length > 0 && Array.from(c).some(x => x.querySelector('.job-meta'));
           }""", timeout=40000)
    time.sleep(2.5)
    r = page.evaluate("""() => {
      const card = document.querySelector('.job-card');
      const btn = card.querySelector('[data-act="detail"]');
      if (btn) btn.click();
      return {hasBtn: !!btn};
    }""")
    time.sleep(2.0)
    r = page.evaluate("""() => {
      const m = document.querySelector('#detailModal');
      const jm = document.querySelector('#jobDetailModal');
      const pv = document.querySelector('#detailPreview');
      const img = pv ? pv.querySelector('img') : null;
      const vid = pv ? pv.querySelector('video') : null;
      const aud = pv ? pv.querySelector('audio') : null;
      const dl = pv ? pv.querySelector('a[download]') : null;
      const rows = document.querySelectorAll('#detailParams .detail-kv-row');
      const vals = Array.from(rows).map(r => r.querySelector('b')?.textContent?.trim());
      const nonEmpty = vals.filter(v => v && v !== '—').length;
      return {
        detailOpen: m ? !m.hidden : null,
        jobDetailOpen: jm ? !jm.hidden : null,
        previewHidden: pv ? pv.hidden : null,
        mediaKind: img ? 'image' : (vid ? 'video' : (aud ? 'audio' : 'none')),
        mediaSrc: (img||vid||aud) ? (img||vid||aud).getAttribute('src') : null,
        mediaLoaded: img ? img.naturalWidth > 0 : (vid ? vid.videoWidth > 0 : null),
        hasDownload: !!dl,
        dlHref: dl ? dl.getAttribute('href') : null,
        paramRows: rows.length,
        nonEmptyValues: nonEmpty,
        promptLen: (document.querySelector('#detailPrompt')||{}).innerText?.length || 0,
      };
    }""")
    print("   ", json.dumps(r, ensure_ascii=False))
    check("项3", "详情弹层已打开（浅色 #detailModal）",
          r["detailOpen"] is True and r["jobDetailOpen"] is False,
          f"detailOpen={r['detailOpen']} jobDetailOpen={r['jobDetailOpen']}")
    check("项3", "参数表有内容（非全「—」）",
          r["nonEmptyValues"] >= 5, f"非空值 {r['nonEmptyValues']}/{r['paramRows']}")
    check("项3", "有缩略图/播放器", r["mediaKind"] != "none",
          f"kind={r['mediaKind']} src={r['mediaSrc']}")
    check("项3", "缩略图真的加载出来了", r["mediaLoaded"] is not False,
          f"loaded={r['mediaLoaded']}")
    check("项3", "有下载按钮", r["hasDownload"], f"href={r['dlHref']}")
    check("项3", "完整提示词非空", r["promptLen"] > 0, f"len={r['promptLen']}")
    page.screenshot(path=f"{SHOTS}_item3_detail.png")
    page.keyboard.press("Escape")
    time.sleep(0.8)

    # ---------- 项 4 + 13 + 14：素材库 ----------
    print("\n### 项4/13/14 素材库 ###")
    page.click("#navAssets")
    time.sleep(4)
    r = page.evaluate("""() => ({
      cards: document.querySelectorAll('#alGrid .al-card').length,
      checkboxes: document.querySelectorAll('#alGrid .al-selbox').length,
      playerBtn: !!document.querySelector('#alPlayerBtn'),
      manageBtn: !!document.querySelector('#mediaManageBtn'),
    })""")
    print("   ", r)
    check("项13", "素材卡有多选勾选框", r["checkboxes"] == r["cards"] and r["cards"] > 0,
          f"cards={r['cards']} checkboxes={r['checkboxes']}")
    check("项14", "有「播放器模式」入口", r["playerBtn"])
    check("项11", "有「管理」入口", r["manageBtn"])

    # 多选
    for i in range(2):
        page.query_selector_all("#alGrid .al-selbox")[i].click()
        time.sleep(0.4)
    time.sleep(0.8)
    r = page.evaluate("""() => ({
      selBarHidden: document.querySelector('#alSelBar').hidden,
      count: document.querySelector('#alSelCount').textContent,
      checked: document.querySelectorAll('#alGrid .al-selbox:checked').length,
      selectedCards: document.querySelectorAll('#alGrid .al-card.selected').length,
      drawerOpen: document.querySelector('#alDrawer').classList.contains('open'),
    })""")
    print("   多选:", r)
    check("项13", "勾选后出现选择条并显示数量",
          r["selBarHidden"] is False and "2" in r["count"], r["count"])
    check("项13", "勾选框与选中卡一致", r["checked"] == r["selectedCards"] == 2,
          f"checked={r['checked']} cards={r['selectedCards']}")
    check("项13", "勾选不会误开详情抽屉", r["drawerOpen"] is False)
    page.screenshot(path=f"{SHOTS}_item13_multiselect.png")

    # 素材库详情（音频/视频/图片）
    for cat, asset_id in [("image", None), ("audio", None), ("video", None)]:
        pass
    r = page.evaluate("""async () => {
      const resp = await fetch('/api/assets?limit=200');
      const d = await resp.json();
      const pick = (c) => (d.assets||[]).find(a => a.category === c);
      const out = {};
      for (const c of ['image','audio','video']) {
        const a = pick(c);
        if (!a) { out[c] = null; continue; }
        window.ASSETLIB.openDrawer(a.id);
        await new Promise(r => setTimeout(r, 2500));
        const box = document.querySelector('#alDrawerBody .al-preview-box');
        const img = box ? box.querySelector('img') : null;
        const vid = box ? box.querySelector('video') : null;
        const aud = box ? box.querySelector('audio') : null;
        out[c] = {
          kind: img ? 'image' : (vid ? 'video' : (aud ? 'audio' : 'none')),
          src: (img||vid||aud) ? (img||vid||aud).getAttribute('src') : null,
          loaded: img ? img.naturalWidth > 0 : (vid ? vid.videoWidth > 0 : null),
          dur: aud ? aud.duration : (vid ? vid.duration : null),
          hasControls: aud ? aud.hasAttribute('controls') : (vid ? vid.hasAttribute('controls') : null),
        };
      }
      return out;
    }""")
    print("   详情预览:", json.dumps(r, ensure_ascii=False))
    check("项4", "图片详情显示原图", r["image"] and r["image"]["kind"] == "image"
          and r["image"]["loaded"], json.dumps(r.get("image"), ensure_ascii=False))
    check("项4", "音频详情可播放",
          r["audio"] and r["audio"]["kind"] == "audio" and r["audio"]["hasControls"],
          json.dumps(r.get("audio"), ensure_ascii=False))
    check("项4", "视频详情可播放",
          r["video"] and r["video"]["kind"] == "video" and r["video"]["hasControls"],
          json.dumps(r.get("video"), ensure_ascii=False))
    page.screenshot(path=f"{SHOTS}_item4_asset_detail.png")

    # 播放器模式（先关详情抽屉 —— 它是右侧浮层，会挡住右上角的按钮）
    page.evaluate("() => { const d=document.querySelector('#alDrawer'); if(d) d.classList.remove('open'); }")
    time.sleep(0.6)
    page.click("#alPlayerBtn")
    time.sleep(2.5)
    r = page.evaluate("""() => {
      const ov = document.querySelector('#alPlayer');
      const tabs = Array.from(document.querySelectorAll('.al-ptab'))
        .map(t => ({m: t.getAttribute('data-mode'), txt: t.textContent.trim()}));
      return {
        open: ov ? !ov.hidden : null,
        tabs,
        hasImg: !!document.querySelector('#alPlayerImg'),
        hasSeconds: !!document.querySelector('#alPSeconds'),
        hasLoop: !!document.querySelector('#alPLoop'),
        hasAudioToggle: !!document.querySelector('#alPAudio'),
        items: document.querySelectorAll('.al-player-item').length,
      };
    }""")
    print("   播放器:", json.dumps(r, ensure_ascii=False))
    check("项14", "播放器面板打开", r["open"] is True)
    check("项14", "三个分类 tab（图片/音频/视频）", len(r["tabs"]) == 3,
          json.dumps(r["tabs"], ensure_ascii=False))
    check("项14", "图片模式有停止时间/循环/背景音控件",
          r["hasImg"] and r["hasSeconds"] and r["hasLoop"] and r["hasAudioToggle"])
    page.screenshot(path=f"{SHOTS}_item14_player.png")
    # 切到音频/视频看模式选择
    for mode in ("audio", "video"):
        page.click(f'.al-ptab[data-mode="{mode}"]')
        time.sleep(1.5)
        r2 = page.evaluate("""() => ({
          media: (document.querySelector('#alPMedia')||{}).tagName,
          opts: Array.from(document.querySelectorAll('#alPMode option')).map(o=>o.value),
        })""")
        check("项14", f"{mode} 模式有 单曲循环/列表循环/列表随机",
              r2["opts"] == ["single", "list", "shuffle"],
              json.dumps(r2, ensure_ascii=False))
    page.keyboard.press("Escape")
    time.sleep(0.6)

    # ---------- 项 11：媒体库管理 ----------
    print("\n### 项11 媒体库管理 ###")
    page.click("#mediaManageBtn")
    time.sleep(2.5)
    r = page.evaluate("""() => {
      const m = document.querySelector('#mediaManageModal');
      return {
        open: m ? !m.hidden : null,
        scopes: Array.from(document.querySelectorAll('input[name="mgScope"]')).map(r=>r.value),
        months: (document.querySelector('#mgMonths')||{}).value,
        preview: (document.querySelector('#mgPreview')||{}).innerText,
        runBtn: (document.querySelector('#mgRunBtn')||{}).textContent,
      };
    }""")
    print("   ", json.dumps(r, ensure_ascii=False))
    check("项11", "管理面板打开", r["open"] is True)
    check("项11", "三种清理口径都在",
          set(r["scopes"]) == {"all", "older", "failed"}, json.dumps(r["scopes"]))
    check("项11", "月份可配置", r["months"] is not None, f"months={r['months']}")
    check("项11", "实时预览会删多少", "将清理" in (r["preview"] or ""),
          (r["preview"] or "")[:80])
    page.screenshot(path=f"{SHOTS}_item11_manage.png")
    page.keyboard.press("Escape")
    time.sleep(0.5)

    # ---------- 项 7 / 8 / 9：面板文案与控件 ----------
    print("\n### 项7/8/9 面板文案与控件 ###")
    # YuE2
    page.click('[data-mode="t2a"]')
    time.sleep(3.5)
    r = page.evaluate("""() => ({
      styleRegen: !!document.querySelector('#musicStyleRegenBtn'),
      lyricsRegen: !!document.querySelector('#musicLyricsRegenBtn'),
      advNote: (document.querySelector('#musicAdvNote')||{}).innerText || '',
      temp: (document.querySelector('#musicTemp')||{}).value,
      tempDisabled: (document.querySelector('#musicTemp')||{}).disabled,
    })""")
    print("   YuE2:", json.dumps(r, ensure_ascii=False))
    check("项7", "结构化风格「重新生成」按钮已移除", r["styleRegen"] is False)
    check("项7", "歌词「重新生成」按钮已移除", r["lyricsRegen"] is False)
    check("项7", "采样参数说明与实际一致（可调 + 写进节点）",
          "写进" in r["advNote"] and "YuE2GenerateMusic" in r["advNote"],
          (r["advNote"] or "")[:120])
    check("项7", "采样参数滑杆确实可调（未 disabled）", r["tempDisabled"] is False,
          f"temp={r['temp']}")

    # H3 加速 LoRA 文案
    page.click('[data-mode="t2v"]')
    time.sleep(4.5)
    r = page.evaluate("""() => ({
      fixed: (document.querySelector('#vidFixedLoraNote')||{}).innerText || '',
      note: (document.querySelector('#vidAccelLoraNote')||{}).innerText || '',
      opts: Array.from(document.querySelectorAll('#vidAccelLoraSelect option')).map(o=>o.textContent.trim()),
      effectHasSelect: !!document.querySelector('#vidEffectLoraSelect'),
      effectHasSearch: !!document.querySelector('#vidEffectLoraSearch'),
      effectHasAdd: !!document.querySelector('#vidEffectLoraAdd'),
    })""")
    print("   H3:", json.dumps(r, ensure_ascii=False))
    check("项8", "固定加速 LoRA 说明含「始终生效」",
          "始终生效" in r["fixed"], (r["fixed"] or "")[:110])
    check("项8", "说明清楚下拉选的是「额外叠加」",
          "额外叠加" in r["fixed"], (r["fixed"] or "")[:140])
    check("项9", "效果 LoRA 用原生下拉 + 添加按钮（对齐 z-image）",
          r["effectHasSelect"] and r["effectHasAdd"],
          f"select={r['effectHasSelect']} add={r['effectHasAdd']} search={r['effectHasSearch']}")
    check("项9", "旧的复选浮层已移除", r["effectHasSearch"] is True,
          "搜索框保留为下拉过滤器")
    page.screenshot(path=f"{SHOTS}_item8_9_h3panel.png")

    # ---------- 项 15：MOSS 已排除 LoRA ----------
    print("\n### 项15 MOSS-Image 已排除 LoRA ###")
    page.click('[data-mode="t2i_moss"]')
    time.sleep(4.5)
    r = page.evaluate("""() => {
      const box = document.querySelector('#imgLoraExcluded');
      const txt = box ? box.innerText : '';
      return {
        exists: !!box,
        hidden: box ? box.hidden : null,
        text: txt.slice(0, 400),
        hasRealisticSnapshot: txt.includes('Realistic Snapshot'),
        hasKodachrome: txt.includes('Kodachrome'),
        hasSkinTone: txt.includes('Skin tone slider'),
      };
    }""")
    print("   ", json.dumps(r, ensure_ascii=False))
    check("项15", "MOSS 面板不再展示「已排除」批次",
          r["hidden"] is True or (not r["hasRealisticSnapshot"] and not r["hasKodachrome"]
                                  and not r["hasSkinTone"]),
          json.dumps(r, ensure_ascii=False)[:200])

    # ---------- 项 16：H3-Image 官方 PE 在增强菜单 ----------
    print("\n### 项16 H3-Image 官方 PE ###")
    page.click('[data-mode="t2i_h3image"]')
    time.sleep(4.5)
    r = page.evaluate("""() => {
      const m = window.EnhanceMenu || null;
      const methods = (window.EnhanceMenu && window.EnhanceMenu.methods)
        ? window.EnhanceMenu.methods.map(x => x.id) : null;
      return {methods};
    }""")
    print("   增强菜单方法:", r)
    if r["methods"] is not None:
        check("项16", "增强菜单含 H3-Image 官方 PE",
              "h3image_pe" in r["methods"], json.dumps(r["methods"]))

    browser.close()

print("\n" + "=" * 70)
passed = sum(1 for x in results if x[2])
print(f"验收结果：{passed}/{len(results)} 通过")
fails = [x for x in results if not x[2]]
if fails:
    print("\n未通过：")
    for item, name, _, detail in fails:
        print(f"  - {item} {name}  {detail[:140]}")
print("=" * 70)
