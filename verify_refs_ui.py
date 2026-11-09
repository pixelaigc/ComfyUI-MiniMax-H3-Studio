"""浏览器真机验证 —— Qwen-Image 2.1 图片编辑的「素材库来源参考图」。

跑真实 Chromium 打真实服务（默认 http://127.0.0.1:8193），
截图存到 evidence/ 目录。

用法：
  /usr/local/lib/hermes-agent/venv/bin/python verify_refs_ui.py [base_url]
"""
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8193"
OUT = Path(__file__).resolve().parent / "evidence"
OUT.mkdir(exist_ok=True)

errors = []
shots = []


def shot(page, name, scroll_to=None):
    """截图。scroll_to 给一个选择器时，先把该元素滚进视口，
    否则参数面板在首屏之下，截到的是「生成库」而不是面板。"""
    if scroll_to:
        page.eval_on_selector(
            scroll_to,
            "el => el.scrollIntoView({block: 'center'})")
        page.wait_for_timeout(400)
    path = OUT / f"{name}.png"
    page.screenshot(path=str(path))
    shots.append(str(path))
    print(f"  [shot] {name}.png")
    return str(path)


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1500, "height": 1000})
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))

        print("== 打开页面 ==")
        page.goto(BASE, wait_until="networkidle")
        page.wait_for_timeout(1500)

        # ---- 切到「图片编辑」分类 ----------------------------------
        print("== 切到 Qwen 图片编辑 ==")
        page.evaluate(
            "() => document.querySelector('.nav-item[data-mode=\"i2i_qwen\"]').click()")
        page.wait_for_timeout(1500)

        state = page.evaluate("""() => ({
          mode: state.mode,
          refVisible: getComputedStyle(document.querySelector('#imgRefField')).display !== 'none',
          addBtnText: document.querySelector('#imgRefAdd').textContent.trim(),
          hasFileInput: !!document.querySelector('#imgRefInput'),
          sortBtn: !!document.querySelector('#imgRefSort'),
          note: document.querySelector('#imgRefNote').textContent,
        })""")
        print("  state:", json.dumps(state, ensure_ascii=False))
        assert state["mode"] == "i2i_qwen"
        assert state["refVisible"], "图片编辑应显示参考图区"
        assert "素材库" in state["addBtnText"], "添加按钮应指向素材库"
        assert not state["hasFileInput"], "不应有本地参考图上传通道"
        shot(page, "50-i2i-panel-asset-library", scroll_to="#imgRefField")

        # ---- 用内部 API 模拟「从素材库选了 3 张」 --------------------
        # 直接走真实的 /api/refs/resolve（后端真接口），
        # 再灌进 QwenRefs —— 等价于用户点选后的状态。
        print("== 从素材库选 3 张（走真实 /api/refs/resolve）==")
        picked = page.evaluate("""async () => {
          const list = await (await fetch('/api/refs/assets?category=image&page_size=10')).json();
          const withPreview = list.assets.filter(a => a.preview_url).slice(0, 3);
          const res = await (await fetch('/api/refs/resolve', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({refs: withPreview.map(a => ({asset_id: a.id}))}),
          })).json();
          const descs = [
            '黄昏海岸边的白色灯塔，暖色落日映在海面上',
            '雪山��峰在日出时泛起金色高山辉光，倒映在平静的湖面上',
            '蓝色 MPU-6050 六轴惯性传感器模块',
          ];
          res.refs.forEach((r, i) => {
            if (descs[i]) { r.description = descs[i]; r.description_source = 'analysis'; }
          });
          window.QwenRefs.setRefs(res.refs);
          return res.refs.map(r => ({
            asset_id: r.asset_id, desc: r.description, missing: r.missing,
          }));
        }""")
        print("  picked:", json.dumps(picked, ensure_ascii=False))
        assert len(picked) == 3
        page.wait_for_timeout(800)

        info = page.evaluate("""() => ({
          count: window.QwenRefs.getRefs().length,
          hint: document.querySelector('#imgRefHint').textContent,
          items: [...document.querySelectorAll('.img-ref-item')].map(el => ({
            idx: el.querySelector('.img-ref-idx').textContent,
            title: el.getAttribute('title'),
          })),
          descRows: [...document.querySelectorAll('#imgRefDescList textarea')].map(t => t.value),
          markerNote: document.querySelector('#imgRefMarkerNote').textContent,
          sortDisabled: document.querySelector('#imgRefSort').disabled,
        })""")
        print("  info:", json.dumps(info, ensure_ascii=False, indent=1))
        assert info["count"] == 3
        assert len(info["descRows"]) == 3, "应有 3 行描述编辑框"
        shot(page, "51-i2i-3-refs-from-library", scroll_to="#imgRefField")

        # ---- 智能排序 ------------------------------------------------
        # 提示词刻意选「传感器 + 灯塔 + 湖面倒影」——
        # 这三张参考图的描述与提示词的匹配度不同，排序应该真的会重排。
        print("== 智能排序 ==")
        page.fill("#prompt", "把 MPU-6050 传感器模块放在白色灯塔前的沙滩上，湖面要有雪山倒影")
        page.wait_for_timeout(300)
        before = page.evaluate("() => window.QwenRefs.getRefs().map(r => r.asset_id)")
        page.click("#imgRefSort")
        page.wait_for_timeout(2500)
        after = page.evaluate("() => window.QwenRefs.getRefs().map(r => r.asset_id)")
        print("  before:", before)
        print("  after :", after)
        assert before != after, "智能排序应当真的重排顺序（提示词匹配度不同）"
        shot(page, "52-i2i-smart-sort", scroll_to="#imgRefField")

        # ---- 移除中间一张 -> 编号重排 --------------------------------
        print("== 移除中间一张 ==")
        page.evaluate("() => window.QwenRefs.removeAt(1)")
        page.wait_for_timeout(800)
        after_remove = page.evaluate("""() => ({
          count: window.QwenRefs.getRefs().length,
          ids: [...document.querySelectorAll('.img-ref-item .img-ref-idx')].map(e => e.textContent),
          descRows: [...document.querySelectorAll('#imgRefDescList textarea')].map(t => t.value),
          hint: document.querySelector('#imgRefHint').textContent,
        })""")
        print("  after_remove:", json.dumps(after_remove, ensure_ascii=False))
        assert after_remove["count"] == 2
        assert after_remove["ids"] == ["1", "2"], "移除后编号必须重排为连续 1,2"
        shot(page, "53-i2i-after-remove-renumbered", scroll_to="#imgRefField")

        # ---- 校验：占位符与图片数不匹配时必须被拦 --------------------
        print("== 一致性校验（占位符与图片数不匹配）==")
        page.fill("#prompt", "只有 <image1> 一张")
        page.wait_for_timeout(300)
        problems = page.evaluate("() => window.QwenRefs.validate(document.querySelector('#prompt').value)")
        print("  validate:", json.dumps(problems, ensure_ascii=False))
        assert problems and any("image2" in p for p in problems), "必须报出缺失的 <image2>"
        shot(page, "54-i2i-validate-mismatch", scroll_to="#imgRefField")

        # ---- 超限拦截 ------------------------------------------------
        print("== 超限拦截（11 张）==")
        overflow = page.evaluate("""() => {
          const many = [];
          for (let i = 0; i < 11; i++) {
            many.push({asset_id: 'asset_x' + i, description: 'd' + i,
                       description_source: 'analysis', tags: [], created_at: i,
                       missing: false, name: 'n' + i});
          }
          window.QwenRefs.setRefs(many);
          return {count: window.QwenRefs.getRefs().length,
                  hint: document.querySelector('#imgRefHint').textContent,
                  addDisabled: document.querySelector('#imgRefAdd').disabled,
                  problems: window.QwenRefs.validate('x')};
        }""")
        print("  overflow:", json.dumps(overflow, ensure_ascii=False))
        assert overflow["count"] == 10, "超出上限应被截断到 10"
        assert overflow["addDisabled"], "达上限时添加按钮应禁用"
        assert overflow["problems"], "超限必须被校验拦下"
        shot(page, "55-i2i-overflow-cap", scroll_to="#imgRefField")

        # ---- 恢复 3 张，用于提交出图 ----------------------------------
        print("== 恢复 3 张参考图 ==")
        page.evaluate("""async () => {
          const list = await (await fetch('/api/refs/assets?category=image&page_size=10')).json();
          const withPreview = list.assets.filter(a => a.preview_url).slice(0, 3);
          const res = await (await fetch('/api/refs/resolve', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({refs: withPreview.map(a => ({asset_id: a.id}))}),
          })).json();
          const descs = [
            '黄昏海岸边的白色灯塔，暖色落日映在海面上',
            '雪山���峰在日出时泛起金色高山辉光，倒映在平静的湖面上',
            '蓝色 MPU-6050 六轴惯性传感器模块',
          ];
          res.refs.forEach((r, i) => {
            if (descs[i]) { r.description = descs[i]; r.description_source = 'analysis'; }
          });
          window.QwenRefs.setRefs(res.refs);
        }""")
        page.wait_for_timeout(800)
        shot(page, "56-i2i-final-3-refs", scroll_to="#imgRefField")

        print("\n== 控制台错误 ==")
        print("  ", [e for e in errors if e.strip()])
        result = {
            "base": BASE,
            "shots": shots,
            "console_errors": [e for e in errors if e.strip()],
            "state": state,
            "after_remove": after_remove,
            "overflow": overflow,
            "validate_mismatch": problems,
        }
        (OUT / "ref-ui-verification.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=1))
        print("\n结果已写入", OUT / "ref-ui-verification.json")
        browser.close()


if __name__ == "__main__":
    main()
