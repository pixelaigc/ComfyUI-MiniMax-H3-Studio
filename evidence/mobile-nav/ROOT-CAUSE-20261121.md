# 移动端导航死路 —— 实测根因报告

日期：2026-11-21
基线：`fa397b9`（live 8190，local/maintainer）
环境：真实 Chromium（Playwright 1.5x，`is_mobile=True, has_touch=True`），视口 375×812 / 390×844 / 768×1024 / 1280×900

---

## 用户反馈

> 「移动端的人机交互流程有问题：选择某个生成类型后，就无法回到生成类型选择，也无法看到生成媒体库。」

---

## 复现方式

```bash
python3 scripts/probe_mobile_nav_matrix.py --base http://127.0.0.1:8190 \
    --out evidence/mobile-nav/matrix-before --width 375 --height 812
python3 scripts/probe_mobile_nav_decisive.py --base http://127.0.0.1:8190 \
    --out evidence/mobile-nav/decisive-before --width 375 --height 812
python3 scripts/probe_mobile_deep.py --base http://127.0.0.1:8190 \
    --out evidence/mobile-nav/deep-before --width 375 --height 812
```

---

## 根因（全部实测，非推断）

### 根因 1 —— 参数抽屉的遮罩层盖住顶栏，汉堡按钮**真的点不动**

`web/responsive.css:128`：

```css
.panel-mask {
  position: fixed; inset: 0; background: rgba(18, 22, 30, .42);
  z-index: 949; display: none;
}
```

`inset: 0` = 从视口顶（y=0）铺满。顶栏 `.topbar` 只有 44px 高、
**没有 z-index**（`web/style.css:20`），所以遮罩（z-index 949）压在它上面。

**决定性证据**（`decisive-before/decisive.json`）：

```json
{
  "panel_open": true,
  "real_click_hamburger": "FAIL: Page.click: Timeout 4000ms exceeded.",
  "sidebar_open_after_click": false,
  "real_click_mask": "FAIL: Page.click: Timeout 4000ms exceeded.",
  "real_click_card": "FAIL: ElementHandle.click: Timeout 4000ms exceeded."
}
```

Playwright 的 `page.click()` 走真实命中测试（`elementFromPoint`），
报 Timeout 就是**用户手指点到那儿命中的是遮罩而不是按钮**。

命中测试佐证（`matrix-before/matrix.json` → S6）：

```
G1_hamburger  ok=False  why=covered-by  hit=panelMask
G2_media_card ok=False  why=covered-by  hit=imgSteps
```

⇒ 参数抽屉一开，**返回类型选择的唯一入口（汉堡）被物理遮死**；
点遮罩也点不动（遮罩在抽屉下方区域被抽屉本体盖住）——
这是一个**真正的死路**。

### 根因 2 —— 视频类型项是动态渲染的，收抽屉的监听绑不上

`web/app.js:7711`（`initResponsive()` 内）：

```js
$$(".nav-item").forEach(item => item.addEventListener("click", () => {
  if (isPhoneViewport()) setSidebarOpen(false);
}));
```

`$$(".nav-item")` 在**启动时**求值。视频模型项由 `web/video-nav.js:186`
在 `/api/video-categories` 返回后**动态创建**：

```js
item.className = "nav-item nav-model-item";
```

⇒ 这 4 个项（t2v / flf / r2v / longfilm）**绑不到上面那个监听**。

**实测**（`deep-before/deep.json`）：

```json
{
  "binding_audit": {"total": 18, "static": 14, "dynamicVideoModel": 4},
  "static_item_closes_drawer": true,
  "dynamic_video_item_closes_drawer": false,
  "drawer_open_after_dynamic": true,
  "can_reopen_after_dynamic": false
}
```

⇒ 点「文生视频」后**抽屉不收**，而且因为抽屉一直开着，
再点汉堡是「关抽屉」而不是「开抽屉」——用户想回类型选择却越点越乱。

对照矩阵（`matrix-before/matrix.json`）：

```
S2-after-static-t2i    drawer closed   G2card hit-self      ← 静态项：正常
S3-after-dynamic-t2v   drawer OPEN     G2card covered-by    ← 动态项：媒体库被盖死
S5-after-longfilm      drawer OPEN     G2card covered-by    ← 同上
```

### 根因 3 —— 抽屉内没有任何「关闭」控件

`matrix-before/matrix.json` 全状态扫描 `backish` 字段：

```json
"backish": []      // 每个状态都是空数组
```

DOM 里**不存在**任何语义为「返回/关闭」的按钮。
抽屉只能靠点遮罩或点汉堡关闭 —— 而这两个在某些状态下都不可靠
（根因 1：遮罩被抽屉盖住；根因 2：汉堡被当成了「关」）。

### 根因 4 —— 「收藏」入口在抽屉里落在视口外且点不到

`deep-before/deep.json`：

```json
"favorites_reachable_after_scroll": {
  "inViewport": true, "hitIsSelf": false, "rect": {"top": 477, "left": -290}
}
```

`left: -290` —— 抽屉虽然 `translateX(0)` 打开了，但 `.nav-scroll`
内部的滚动让「收藏」项仍横向在视口外侧。命中测试 `hitIsSelf: false`。
（横向 -290 说明是 `.sidebar` 的 `left:0/width:min(300px,84vw)` 与
`.nav-scroll` 内容宽度的组合问题，具体机制见
`decisive-before` 的 `favorites_in_view_after_scroll`。）

⇒ 本任务新增的「收藏」栏在移动端抽屉里**点不到**。

### 根因 5 —— 移动端右键菜单**根本唤不出来**（长按无效）

`longpress-before/longpress.json`（CDP `Input.dispatchTouchEvent`，真·触摸，按住 850ms）：

```json
{
  "target_point": {"x": 97.25, "y": 428.2, "inViewport": true},
  "contextmenu_fired": 0,
  "ctx_menu_open": false,
  "ctx_menu_items": [],
  "contextmenu_fired_on_tap": 0
}
```

⇒ 卡片只绑了 `contextmenu` 事件（`app.js:5660/5666/5672/5684`），
移动端浏览器长按**不派发** `contextmenu` ⇒
「添加到收藏」在手机上完全不可达。

### 根因 6 —— FAB 压住卡片缩略图

`fab-before/fab-overlap.json` → scroll 50%：

```
fab={'top': 752, 'left': 275, 'w': 86, 'h': 44}
压住文字元素 0 个
压住缩略图 1 个
```

文字没被压住（B4 的「正文」口径满足），但缩略图被压住一块。

---

## 桌面端对照（1280×900）

`repro-before/repro-report.json` → `desktop-1280`：

```
navToggle_visible: false      ← 桌面端没有汉堡（常驻侧栏）
jobsArea_visible: true
cardCount: 12
```

⇒ 桌面端无此问题，**修复必须不影响桌面端**。

---

## 结论

| # | 根因 | 性质 | 修复方向 |
|---|---|---|---|
| 1 | 参数抽屉遮罩 `inset:0` 盖住顶栏 → 汉堡点不动 | 硬死路 | 遮罩让开顶栏（`inset: 44px 0 0 0`）+ 顶栏提 z-index |
| 2 | 动态视频项绑不到「收抽屉」监听 | 抽屉常开盖住媒体库 | 事件委托到 `#sidebar` |
| 3 | 抽屉内无关闭控件 | 无退路 | 抽屉顶部加「类型选择」标题行 + 关闭按钮 |
| 4 | 「收藏」在抽屉里点不到 | 入口不可达 | 抽屉内加「收藏」快捷入口 / 修滚动定位 |
| 5 | 移动端长按不派发 contextmenu | 右键菜单在手机上是死的 | 加触摸长按 → 打开同一份右键菜单 |
| 6 | FAB 压住缩略图 | 轻微遮挡 | FAB 与卡片网格留出安全区 |

---

## 证据文件

| 文件 | 内容 |
|---|---|
| `evidence/mobile-nav/repro-before/` | 四档视口全流程复现 + 截图 |
| `evidence/mobile-nav/probe-before/` | 逐项根因探针 + 截图 |
| `evidence/mobile-nav/matrix-before/matrix.json` | 状态×目标可达性矩阵（9 状态 × 6 目标） |
| `evidence/mobile-nav/decisive-before/decisive.json` | **决定性证据**：Playwright 命中测试 FAIL |
| `evidence/mobile-nav/deep-before/deep.json` | 绑定审计 + 动态项不收抽屉 |
| `evidence/mobile-nav/longpress-before/longpress.json` | 长按不触发 contextmenu |
| `evidence/mobile-nav/fab-before/fab-overlap.json` | FAB 与卡片重叠量化 |
| `evidence/mobile-nav/extra-before/extra.json` | 收藏入口真实点击（能点到，但抽屉常开时进不去） |
