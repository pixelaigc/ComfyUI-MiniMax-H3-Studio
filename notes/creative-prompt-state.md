# 现场恢复结论 —— 「创意内容设计」面板（2026-11-21 第 2 轮开局）

分支：`local/feature/creative-prompt-design`
worktree：`/root/projects/h3studio-creative-prompt`
仓库：`/root/projects/comfyui-minimax-h3-studio`

## 额度闸门（第 0 步，只读）

- 快照文件：`<workspace>/quota/minimax_quota_latest.json`
- `sampled_at`：**2026-11-19 07:40:32**（Asia/Shanghai）
- `remaining_percent_5h`：**77**（另 `five_hour_window_remaining_percent` = 62）
- 判定：≥ 20% ⇒ 闸门开。
- 【如实说明】该快照**已滞后约 2 天**（现在是 2026-11-21 晚）。按任务书要求
  **没有**为此新建采样任务、也没有调用远端额度 API。本轮**不生成任何
  图片/视频作业**，不消耗生成额度。

## 已确认在分支上的东西（不用重做）

| 项 | 位置 | 状态 |
|---|---|---|
| 871 项数据源 + 后端 API | `h3studio/prompt_catalog.py`（806 行）+ `h3studio/prompt_catalog_data/{_data_d1..d4.py, catalog.json, danbooru_postcounts.json}` | ✅ 已在 `8a2096b` |
| `GET /api/prompt-catalog` | `server.py:12158`（处理器在 `server.py:8467`） | ✅ |
| 前端模块 | `web/prompt-catalog.js`（779 行）+ `web/prompt-catalog.css`（451 行） | ✅ |
| 静态资源引用 + 版本号 | `web/index.html:16` / `:1675`，`?v=cpd20261121b` | ✅ |
| 中文优先 / 每维度可跳过 / 每维度可自填 | `e501a75` | ✅ 已提交 |
| 单元测试（13 条新增） | `tests/test_prompt_catalog.py`（共 31 条） | ✅ |
| E2E 测试（21 条） | `tests/e2e/test_prompt_catalog_flow.py` | ✅ |
| 主线并入 | `a430683`（merge local/maintainer = 97e7294） | ✅ |

## 本轮（第 2 轮）新增提交

- `8c8f5d8` —— 修正「自定义混合组装」测试期望（上轮实测：组装逻辑**正确**，
  主体→风格→场景 的槽位顺序本就对，是**测试期望**把自定义当成了「追加到
  末尾」）；窄屏 elementFromPoint 判定改为逐层滚动定位；
  截图脚本补上「自行输入」示例。

## 上轮未完成的唯一事项

**窄屏 320 / 375 / 390 / 430 的失败项**。已定位到两个独立原因：

1. `test_free_input_joins_the_assembly_and_is_not_blocked`
   → 期望写错，已在 `8c8f5d8` 修好（上轮已实跑验证：组装结果
   `['一只戴护目镜的柯基犬', '赛璐璐动画', '复古漫画网点', '远景是雾中的高压电塔']`）。

2. `test_narrow_screen_constraints[320/375/390/430]`
   → 窄屏第 4 步的 elementFromPoint 判定拿不到有效证据。原因**不是**遮挡，
   而是**定位方法在双滚动容器下失效**：
   - 参数面板在 ≤1100px 是 `position:fixed` 的底部抽屉（`.panel overflow-y:auto`）；
   - 面板内部还有一层 `.pc-groups`（`max-height:46vh; overflow-y:auto`）；
   - `scrollIntoView` 只滚外层，元素 rect 停在 `top=1012`（视口只有 760）
     ⇒ 断言拿到「在视口外」，而不是「被谁遮挡」——**等于没测**。
   - 另一个真实原因：窄屏第 3 步点顶栏汉堡会**收起**底部参数抽屉
     （app.js 的抽屉互斥逻辑），第 4 步必须先点回 FAB（真人路径）。

   ⇒ 已在 `8c8f5d8` 改为「逐层滚最近的可滚容器 + 迭代收敛」，
   并在第 4 步先点回 FAB。**尚需实跑验证。**

## 并行任务冲突点（与「生成媒体库 / 收藏栏」任务）

- 本分支已把 `local/maintainer`（含并行任务的 `8d3e4de` build-id、
  `97e7294` 移动端导航第三轮）merge 进来（`a430683`），
  `git merge-tree` 验证**无冲突**。
- 本轮对共用文件的改动面：
  - `web/index.html`：只加 2 行（1 个 css + 1 个 js），且**挪到与并行任务
    不重叠的位置**（`7fb2559`：css 放在 responsive.css 之后、js 放在
    moss-layout.js 之后）。并行任务动的是 `favorites.css` / `favorites.js`
    / `build-id.js` 那几行 —— 不同 hunk。
  - `web/app.js`：只加 2 行（导出 `pushPromptVersion` / `clearPromptEnhanced`）。
  - `tests/test_frontend_assets_hygiene.py`：STALE_BUMPED 表追加 2 行
    （prompt-catalog.css / .js），并把 `app.js` 行的版本号与并行任务对齐
    （`dc0c697` / `d77c853`）。
- 本分支**不碰** `web/favorites.js` / `web/favorites.css` / `web/build-id.js`
  / `web/style.css` / `web/responsive.css`。
- 部署纪律：合并用 `git commit-tree` + `update-ref`（不切分支），
  只精确 checkout 本次改动的文件，只重启 `h3-studio.service`；
  部署前重新核实 live 仍在 `97e7294`，否则中止。

## 现场 HEAD

- `local/maintainer` = `97e7294`
- `local/deploy/qwen-ref-assets-live` = `97e7294`
- 本分支 HEAD = `8c8f5d8`（父链含 `a430683` merge）
- 线上 8190：`WorkingDirectory=/root/projects/h3studio-live`，
  分支 `local/deploy/qwen-ref-assets-live`，工作区无 tracked 改动。
