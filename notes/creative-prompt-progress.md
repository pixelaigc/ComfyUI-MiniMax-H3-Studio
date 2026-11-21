# 第 2 轮进度（2026-11-21）—— 全部完成

## (a) 窄屏 320/375/390/430 —— ✅

真因不是遮挡，是测试路径错：窄屏第 3 步开左侧导航抽屉后，`app.js` 的
`updateResponsiveChrome()` 规定「两个抽屉任何一个开着，FAB 都让位」
⇒ `.panel-fab` 是 hidden，测试点它必然 30s 超时。
修法（照真人路径）：`Esc` 关抽屉 → 点「参数」浮钮回参数抽屉 →
按 `aria-expanded` 判断设计面板是否已展开。

**证据**：`4 passed in 47.92s`（320/375/390/430）。
判定全是真实证据：面板无横向溢出、顶栏生成类型入口命中 navToggle、
抽屉内关闭按钮 close-OK、收藏栏 favorites-OK、
词库下拉与自行输入框 elementFromPoint 命中自身，
并真实走完 选词库项 → 自行输入 → 改回「不指定」（组装 2 段 → 1 段）。

## (b) 全量测试对拍 —— ✅（对拍数字）

| | 失败 | 通过 | 错误 | 用时 |
|---|---|---|---|---|
| **本分支** | **55** | 4009 | 63 | 57m19s |
| **mainline 基线**（97e7294） | **51** | 3957 | 85 | 54m41s |

命令：`pytest tests/ -q -p no:randomly -m "not e2e"`（同口径、同机器、同时段）。
逐用例差集（`.pytest_cache/v/cache/lastfailed`）：
- 只在基线失败 = 19 条；共同失败 = 117 条（主线上就有的环境类失败）
- **只在本分支失败 = 1 条**：`test_asset_picker_onpick.py::test_asset_button_fills_slot_without_error`
  → 单独复跑证伪：本分支 `3 passed`、基线 `3 passed`，**两边都过**（并发/端口抖动）。
  同批复核：`test_page_size.py` 两边都是 `21 passed / 1 failed`（失败的是不同用例，均为实时刷新时序抖动）；
  `test_continuation_frontend.py + test_panel_fallback.py` 本分支 `48 passed`。
- **结论：无新增回归。**
- 本任务相关套件：`test_prompt_catalog.py`(31) + `test_frontend_assets_hygiene.py`(14) → **45 passed**；
  `tests/e2e/test_prompt_catalog_flow.py`(21) → **21 passed**（真实浏览器）。

## (c) 新增浏览器 E2E + 三组组装样本 + 截图 —— ✅

### 三组真实组装结果（真实浏览器跑出来，非手写期望）
产物：`evidence/prompt-catalog/samples/assembly-samples.{json,md}`

**① 纯中文选项（10 段）**
```
人物（单人）, 长直发, 衬衫, 赛璐璐动画, 森林, 三分法, 中景, 黄金时刻, 互补色, 宁静
```
英文辅助版本（不参与默认组装）：
`1girl, long_hair, shirt, cel animation, forest, rule of thirds, medium shot, golden hour, complementary colors, serene`

**② 只填部分维度（其余 43 组全部「不指定」跳过）**
```
人物（单人）, 黄金时刻
```

**③ 含自行输入自定义项**
```
人物（单人）, 赛璐璐动画, 复古漫画网点, 远景是雾中的高压电塔
```
（自定义按它自己子类的骨架槽位插入：subject → style（词库）→ style（自定义）→ scene）

### 截图（真实浏览器，绝对路径）
- 桌面 1440px：`/root/projects/h3studio-creative-prompt/evidence/prompt-catalog/desktop-1440.png`
- 桌面冲突态：`/root/projects/h3studio-creative-prompt/evidence/prompt-catalog/desktop-conflict.png`
- 窄屏 375px：`/root/projects/h3studio-creative-prompt/evidence/prompt-catalog/narrow-375.png`
- 窄屏 375px 预览：`/root/projects/h3studio-creative-prompt/evidence/prompt-catalog/narrow-375-preview.png`
- 另有 320 / 390 / 430 各 2 张
- 线上：`/root/projects/h3studio-creative-prompt/evidence/prompt-catalog/live/live-desktop-1440.png`
  `.../live/live-conflict.png`、`.../live/live-narrow-375.png`

窄屏溢出实测（脚本探针，非目测）：320/375/390/430 全部
`docOverflow=0, pcOverflow=0`；预览区 `scrollWidth == clientWidth == 299`，
文本行最右边界 227 < 视口 375，**0 行超出视口**
⇒ 视觉模型看到的「截断」是纵向滚动（max-height + overflow-y:auto）的观感，**非横向溢出**。

### 本轮新修的真实 UX 缺口
硬互斥把选项 disabled 后，原因只写在 `<option title>` 里 —— 原生 select 的
title 在**移动端根本不显示**。用户只看到「点不动」，不知道为什么被拦。
已补：分组内挂**可见的中文红底说明**（冲突对象 + 原因 + 先取消哪一侧）。

## (d) 数据对账 —— ✅

```
dimensions 9 / groups 48 / research_items 871（逐条保留，无删减）
items 891（= 871 + 补 20 个显式「不指定」项）
by_level {'通': 625, '模': 70, '标': 176}    tag_verified(post_count>0) 166
hard/soft/cap 22 / 17 / 3    version f6dcce209bfa
```
独立交叉核：直接遍历调研 `_data_d1..d4.py` 原文条目数 = **871**，与目录自报一致；
166 条标签词逐条带 Danbooru `post_count` 且全 > 0。

## (e) 合并 local/maintainer 并部署 8190 —— ✅

- 合并提交：`46632454`（面板主体）→ `1e62ec26`（脚本修正 + 线上证据）
- `local/maintainer` = `local/deploy/qwen-ref-assets-live` = live HEAD = `1e62ec26`
- 本分支已并入主线（`git merge-base --is-ancestor` = YES）
- live 工作区 == HEAD（无 tracked 差异）
- 静态资源版本号：`prompt-catalog.css|js ?v=cpd20261121c`
  （`app.js` 因并行任务用了 `cpd20261121a`，本轮未再改 app.js）
- 回滚点：`backup/live-before-cpd-20261121-221249` /
  `backup/maintainer-before-cpd-20261121-221249`（= 97e7294）

### 线上探活（部署后真实 Chromium 打线上）
```
GET / -> 200；GET /api/prompt-catalog -> 200
counts: 891 项 / 48 组 / 9 维度 / tag_verified=166 / version=f6dcce209bfa
index.html 已引用 prompt-catalog.css|js ?v=cpd20261121c
线上分组数 45
线上组装结果 ['人物（单人）', '赛璐璐动画', '远景是雾中的高压电塔']
硬互斥可见说明存在且可见（disabled=True）
375px: docOverflow=0 / pcOverflow=0 / 45 组 / 45 个自行输入框
页面 JS 错误: []
```

### 部署脚本两处真错（实测踩到并已修）
1. 自检判据写反：`git diff --quiet` 返回 0 = 无差异 = 一致，
   原脚本写成「不一致时报 OK」⇒ 判据完全颠倒。
2. 用 `git show > 文件` 写 live 文件**没刷新 live 的 git index**
   ⇒ `git status` 把每个文件报成 D，自检误判部署失败。已补 `git reset --mixed HEAD`。

### 回滚命令
```bash
cd /root/projects/comfyui-minimax-h3-studio
git update-ref refs/heads/local/maintainer 97e729486b0b925bfb85d4cc5efb038ad1895c60
git update-ref refs/heads/local/deploy/qwen-ref-assets-live 97e729486b0b925bfb85d4cc5efb038ad1895c60
cd /root/projects/h3studio-live && git reset --mixed HEAD && git checkout -- .
systemctl restart h3-studio.service
```
（等价于 `git branch backup/*-before-cpd-20261121-221249` 指向的 97e7294）
