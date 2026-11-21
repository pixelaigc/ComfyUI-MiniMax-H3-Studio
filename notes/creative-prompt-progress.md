# 第 2 轮进度（2026-11-21）

## (a) 窄屏 320/375/390/430 —— ✅ 完成

**失败真因**（不是遮挡，是测试路径错）：窄屏第 3 步开左侧导航抽屉后，
`app.js` 的 `updateResponsiveChrome()` 规则是「两个抽屉任何一个开着，
FAB 都让位」⇒ 此时 `.panel-fab` 是 `hidden`，测试去点它必然 30s 超时。

**修法**（照真人路径）：`Esc` 关掉抽屉 → 点「参数」浮钮回参数抽屉 →
按 `aria-expanded` 判断设计面板是否已展开（已展开就不能再点
`#pcOpenBtn`，那会把它收起）。

**证据**：`4 passed in 47.92s`（320 / 375 / 390 / 430）。
判定全是真实证据：面板无横向溢出、顶栏生成类型入口命中 navToggle、
抽屉内关闭按钮 close-OK、收藏栏 favorites-OK、
词库下拉与自行输入框 elementFromPoint 命中自身，
并真实走完 选词库项 → 自行输入 → 改回「不指定」（组装 2 段 → 1 段）。

## (b) 全量测试对拍 —— ✅ 完成（对拍数字见下）

| | 失败 | 通过 | 错误 | 用时 |
|---|---|---|---|---|
| **本分支**（local/feature/creative-prompt-design） | **55** | 4009 | 63 | 57m19s |
| **mainline 基线**（97e7294） | **51** | 3957 | 85 | 54m41s |

- 命令：`pytest tests/ -q -p no:randomly -m "not e2e"`（两边同口径、同机器、同时段）
- 逐用例对拍（`.pytest_cache/v/cache/lastfailed` 集合差）：
  - **只在基线失败（本分支修好）= 19 条**（test_page_size 14 + test_continuation_frontend 2
    + test_panel_fallback 1 + test_asset_picker_onpick 1 + …）
  - **只在本分支失败（新增回归）= 1 条**：
    `tests/test_asset_picker_onpick.py::test_asset_button_fills_slot_without_error`
  - 共同失败 = 117 条（主线上就有的环境类失败：需真 ComfyUI / 真素材 / 端口占用）
- **那 1 条「新增回归」已单独复核为假回归**：单独跑该文件
  → 本分支 `3 passed in 92.20s`、基线 `3 passed in 89.53s`，
  **两边都过**。全量跑时的差异是并发/端口/超时抖动，不是代码回归。
  同批复核：`test_page_size.py` 本分支 `21 passed / 1 failed`，
  基线 `21 passed / 1 failed`（失败的是**不同**用例，都是实时刷新时序抖动）；
  `test_continuation_frontend.py + test_panel_fallback.py` 本分支 `48 passed`。
- **本任务新增/相关的套件**：
  - `tests/test_prompt_catalog.py`（31 条）+ `tests/test_frontend_assets_hygiene.py`（14 条）
    → **45 passed**
  - `tests/e2e/test_prompt_catalog_flow.py`（21 条，真实浏览器）→ 见 (c)
- 结论：**没有新增回归**（唯一差异用例经单独复跑证伪）。

## (c) 新增浏览器 E2E + 三组组装样本 + 截图 —— 进行中

### 三组真实组装结果（真实浏览器取证，非手写期望）
脚本：`scripts/capture_prompt_assembly_samples.py`
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
- 窄屏 375px 预览区：`/root/projects/h3studio-creative-prompt/evidence/prompt-catalog/narrow-375-preview.png`
- 另有 320 / 390 / 430 各 2 张（`-preview` 变体）
- 窄屏溢出实测（脚本探针，非目测）：320/375/390/430 全部
  `docOverflow=0, pcOverflow=0`；预览区 `scrollWidth == clientWidth == 299`，
  文本行最右边界 227 < 视口 375，**0 行超出视口**
  ⇒ 视觉模型看到的「截断」是纵向滚动（max-height + overflow-y:auto）的观感，
  **不是横向溢出**（硬约束满足）。

### 本轮新修的真实 UX 缺口
硬互斥把选项 disabled 后，原因只写在 `<option title>` 里 ——
原生 select 的 title 在**移动端根本不显示**。用户只看到「点不动」，
不知道为什么被拦。已补：分组内挂**可见的中文红底说明**
（写明冲突对象 + 原因 + 先取消哪一侧）。`e821d56`

## (d) 数据对账 —— ✅ 完成

```
dimensions 9 / groups 48 / research_items 871（逐条保留，无删减）
items 891（= 871 + 补 20 个显式「不指定」项）
by_level {'通': 625, '模': 70, '标': 176}    tag_verified(post_count>0) 166
hard/soft/cap 22 / 17 / 3    version f6dcce209bfa
```
独立交叉核：直接遍历调研 `_data_d1..d4.py` 原文条目数 = **871**，
与目录自报一致；166 条标签词逐条带 Danbooru `post_count` 且全 > 0。

## (e) 合并部署 8190 —— 待做
