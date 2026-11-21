# 第 2 轮进度（2026-11-21）

## (a) 窄屏 320/375/390/430 —— ✅ 完成

**失败真因**（不是遮挡，是测试路径错）：窄屏第 3 步开左侧导航抽屉后，
`app.js` 的 `updateResponsiveChrome()` 规则是「两个抽屉任何一个开着，
FAB 都让位」⇒ 此时 `.panel-fab` 是 `hidden`，测试去点它必然 30s 超时。

**修法**（照真人路径）：`Esc` 关掉抽屉 → 点「参数」浮钮回参数抽屉 →
按 `aria-expanded` 判断设计面板是否已展开（已展开就不能再点
`#pcOpenBtn`，那会把它收起）。

**证据**：
```
pytest tests/e2e/test_prompt_catalog_flow.py::test_narrow_screen_constraints
→ 4 passed in 47.92s   (320 / 375 / 390 / 430)
```
判定口径（全部是真实证据，不是「看起来没坏」）：
- 每个宽度：面板无横向溢出（docOverflow=0、pcOverflow=[]）
- 顶栏「生成类型选择」入口 elementFromPoint 命中 navToggle
- 抽屉内：关闭按钮命中 close-OK / 收藏栏命中 favorites-OK
- **词库下拉 + 自行输入框**：elementFromPoint 命中自身（hit=OK）
- 真实操作三连：选词库项 → 自行输入 → 改回「不指定」，
  组装结果从 2 段 → 1 段（省略真的生效，不留空片段）

## (d) 数据对账 —— ✅ 完成

```
dimensions        : 9   (调研 9)
groups            : 48  (调研 48)
research_items    : 871 (调研原文 871 项**逐条保留，无删减**)
items (可选总数)  : 891 (= 871 + 本次补的显式「不指定」项 20)
by_level          : {'通': 625, '模': 70, '标': 176}   (调研同)
tag_items         : 166   tag_verified(post_count>0): 166  (调研 166)
hard/soft/cap     : 22 / 17 / 3
version           : f6dcce209bfa
```
- 标签体系词 166 条非跳过项**逐条**带 Danbooru 官方 `post_count` 且全部 > 0
  （脚本实测：bad = []）。
- 独立交叉核：直接遍历调研 `_data_d1..d4.py` 原文条目数 = **871**，
  与目录 `research_items` 一致（不是只信目录自报）。
- 无删减 ⇒ 不需要「逐条列出删减项并给理由」。

## (b) 全量测试对拍 —— 进行中
## (c) 新增 E2E + 三组组装样本 + 截图 —— 待做
## (e) 合并部署 8190 —— 待做
