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

## (b) 全量测试对拍 —— 待做
## (c) 新增 E2E + 三组组装样本 + 截图 —— 待做
## (d) 数据对账 —— 待做
## (e) 合并部署 8190 —— 待做
