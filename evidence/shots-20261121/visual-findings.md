# H3 Studio (OmniStudio) 真实渲染视觉 / 响应式问题清单

- **采集时间**：2026-11-21
- **目标**：`http://127.0.0.1:8190/`（systemd `h3-studio.service`，active；代码 `/root/projects/h3studio-live` HEAD = `e393e8a`）
- **工具**：Playwright + Chromium（`/home/ubuntu/comfyui/venv/bin/python`，chromium 已在 `/root/.cache/ms-playwright/`）
- **采集方式**：只读。未修改 `/root/projects/h3studio-live` 或任何仓库文件，未 git 操作，未重启 8190，未新建额度探针任务，未跑 GPU 生成作业。仅 GET 页面 + 点击导航/标签。
- **截图目录**：`/root/.hermes-web-ui/.ekko/workspace/default/person-main-505e35f9-2826-4d4c-af53-d3dc5de6f957/evidence/shots-20261121/shots/`（**96 个 PNG**）
- **测量原始数据**：`../measurements.json`（58 个页面的 JS 测量）、`../summary2.txt`（去重汇总）
- **复现脚本**：`../capture.py`（主采集）、`../narrow_probe.py` / `../narrow_probe2.py` / `../narrow_touch.py`（375px 专项）、`../fab_scan.py`（FAB 覆盖扫描）、`../verify_contrast.py`（对比度复核）

## 覆盖范围

| 类别 | 截图 | 说明 |
|---|---|---|
| 主界面 | `01-main-default.png` / `01b-main-panel-detail.png` / `01c-main-topbar-sidebar.png` | 1440×900 默认面板（Z-IMAGE 文生图 active） |
| 模式面板 | `02-mode-{t2v,flf,r2v,longfilm,t2i,t2i_qwen,t2i_moss,t2i_h3image,i2i_qwen,i2i_h3image,t2a,a2a}.png` + 各 `-panel` 局部 | 左侧导航实际存在的 **12 个** 模式全部截到 |
| 生成媒体库 | `03-media-tab-{all,active,done,longfilm,failed,canceled,favorites}.png` + `-grid` 局部 | 7 个标签全部截到 |
| 素材库 / 收藏 / Agent / 系统 | `04-assets-page.png`、`06-favorites-page.png`、`05-agent-page.png`、`16-agent-manage-modal.png`、`07-system-page.png`、`08-providers-page.png`、`09-flowdocs-page.png` | |
- **375px 窄断点** | `10-narrow375-main.png`、`11-narrow375-media-*.png`、`12-narrow375-drawer.png`、`13-narrow375-assets.png`、`14-narrow375-agent.png` + `10c/10f/13d` 全页 + `15-narrow375-media-bottom.png` | |

**关键前置事实**：本应用**只有浅色主题**（`web/style.css:2-6` 定义 `--bg:#f5f6f8; --panel:#fff; --text:#1a1d21; --muted:#8a919c; --border:#e6e8ec; --accent:#4f6ef7`；全仓 grep 无 `prefers-color-scheme` / `data-theme` / 主题切换 JS）。所以「暗色可读性」不适用，本清单的对比度问题全部是**浅色主题内部**的对比度不足。

---

## 一、问题清单

| # | 严重度 | 现象 | 复现步骤 | 证据（截图 + 选择器 + 数值） | 建议改法 |
|---|---|---|---|---|---|
| **H1** | **高** | 375px 下右下角蓝色「参数」悬浮按钮（FAB）**压在媒体库卡片正文上**，把「分辨率」的数值、「LoRA」「调度器」「种子」「Turbo 4 步 · ckpt850 ×1.00」等字段整行盖住，读不到。 | 1) 浏览器宽 375×812 打开 `http://127.0.0.1:8190/`；2) 滚动 `#jobsArea` 到任意位置（scrollTop≈98/196/392/588/686 均复现）。 | `shots/15c-narrow375-fab-worst-overlap.png`、`shots/10i-narrow375-fab-tiny.png`（3× 裁切）。DOM 测量：FAB `#panelFab` rect=[275,752,86,44]（`position:fixed`, `z-index:960`）；在 scrollTop=98 时 100% 被覆盖的元素：`div.job-meta`（ox=86 oy=44）、`b`「Turbo 4 步 · ckpt850 ×1.00」（ox=67 oy=14）、`span`「调度器」（ox=67 oy=11）、`div.row-group.gen-params`（87%）；scrollTop=392 时 100% 覆盖 `span.job-top-right`「11/15 13:25」。CSS 根因：`web/responsive.css:135-150` FAB `z-index:960` 高于 `#panel` 的 950，但**没有给 `.jobs-grid` 预留右侧避让区**。 | 给 `.jobs-grid` / `.job-card` 在 ≤700px 加右内边距或 `padding-right` 避让 FAB（如 `.jobs-area{padding-right:104px}` 或给最后一行卡片加 margin），或在 FAB 上方加 `pointer-events` 无关的渐隐遮罩提示；也可把 FAB 改成悬浮在卡片网格**下方**的独立条。 |
| **H2** | **高** | 375px 下展开左侧抽屉时，FAB 同样压在抽屉内容上，**「释放显存」按钮的右端被切掉一块**（按钮右上/右下角被蓝按钮覆盖，白色按钮轮廓中断）。 | 1) 375×812 打开页面；2) 点顶栏 `#navToggle` 展开抽屉；3) 看抽屉左下角。 | `shots/12c-narrow375-drawer-foot-fab.png`（2× 裁切）、`shots/12-narrow375-drawer.png`。DOM 测量：抽屉 `#sidebar` rect=[0,44,300,812]（`z-index:940`，`web/responsive.css:224`）；FAB rect=[275,752,361,796]；两者交集 ox=25 oy=44。抽屉底栏 `.sidebar-foot` rect=[10,747,289,798] 与 FAB 交集 ox=14 oy=44；`#unloadBtn` rect=[16,755,283,790]。根因：FAB `z-index:960`（`responsive.css:149`）高于抽屉 `z-index:940`（`responsive.css:224`），FAB 是 fixed 全局层。 | 抽屉 `.open` 时隐藏或抬高 FAB（`.sidebar.open ~ .panel-fab{display:none}` 或按抽屉 z-index 提升 FAB 使其被抽屉盖住），或抽屉底栏右侧加 `padding-right` 避让 86px 的 FAB 宽度。 |
| **M1** | 中 | 375px 首屏 **67% 高度被筛选器占满**（`.jobs-head` 高 514px / `#jobsArea` 高 768px），真正媒体卡片只露出顶部一条，首屏几乎看不到一张完整卡片。 | 1) 375×812 打开页面（不滚动）；2) 观察首屏。 | `shots/10-narrow375-main.png`（首屏）、`shots/10c-narrow375-main-FULLPAGE.png`。测量：`div.jobs-head` rect=[12,56,351,514]，`main#jobsArea` rect=[0,44,375,768] → 514/768 = 67%。结构：7 个状态标签占 2 行（`div.jobs-tabs` 高 92px）+ 13 个「生成类型」筛选项占 6 行（`web/style.css:325-334` `.jobs-type-tabs`，`responsive.css:284-285` 强制 `width:100%` 整行换行）。 | ≤700px 时把 `.jobs-type-tabs` 收进「筛选」折叠面板（默认收起，只留已选项 chip），或改为横向滚动单行 + 底部「更多」按钮；也可让状态标签与类型筛选同处一行可滚动容器。 |
| **M2** | 中 | 媒体库卡片「生成参数」区的关键值被 `text-overflow:ellipsis` 截断成省略号：「MiniMax-H3 FL2VA · 剪枝 int8（默认）」只剩 `MiniMax-H3 FL2VA · ...`（sw=202 / cw=124，截掉 78px），「Turbo 4 步 · ckpt850 ×1.00」同样截断（sw=137 / cw=124）。**在 375px 下更严重**：卡片宽 171px，`.job-meta b` cw=67px，内容 sw=202px（截掉 135px，约 2/3），「分辨率 672p · 1216×672 · 16:9」sw=120 / cw=67。 | 1) 1440 宽打开，点「生成媒体库 → 已完成」；2) 看任一视频卡片的参数行。 | `shots/x-jobsgrid-detail.png`、`shots/x-jobmeta-row.png`（1440，2× DPR）。DOM：`div#jobsGrid .job-card .job-meta b` scrollWidth=202 > clientWidth=124（`web/style.css:509-512` `.job-meta .row b{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}`）。375px：`b` cw=67/sw=202、`b`「672p · 1216×672 · 16:9」sw=120/cw=67。 | 该行改为 2 行折行（`white-space:normal;overflow-wrap:anywhere`）或 `title` 属性 + 允许换行；模型名可去掉「MiniMax-H3 」冗余前缀（卡片区已在上方显示模式），并把「· 剪枝 int8（默认）」这类补充说明下沉到 tooltip。 |
| **M3** | 中 | 「长片拼接」面板的 **「合成长片」按钮是禁用态但白字落在浅灰底上，对比度仅 1.65:1**（远低于 WCAG AA 4.5:1），按钮文字几乎看不清。 | 1) 1440 宽打开，点左侧「视频 → 长片拼接 → minimax-h3 长片拼接」；2) 看面板底部按钮（未选片段时为禁用态）。 | `shots/02-mode-longfilm-panel.png`、`shots/x-longfilm-stitchbtn.png`（裁切）、`shots/02-mode-longfilm.png`。DOM：`button#stitchBtn` `disabled=true`，`color:rgb(255,255,255)`、`background:rgb(196,202,210)` → 对比度 1.65。CSS：`web/style.css:221` `.gen-btn:disabled { background: #c4cad2; }` —— 禁用态只改背景不改文字色，`color:#fff` 继承自 `.gen-btn`。 | 禁用态把文字改成深灰（如 `color:#5c636e`）而不是保留白字，或把禁用背景压深（`#aab1bb`）。这样 `rgba(255,255,255)` 白字与浅灰底的组合在禁用时不会读不出。 |
| **M4** | 中 | 1440×900 桌面下，**左侧导航有 8 个主要入口在首屏视口外**（需在 `.nav-scroll` 内滚动才能点到）：「素材库」「收藏」「智能」「内置 Agent」「系统」「交互流程图」「本地模型配置」「硬件参数」。 | 1) 1440×900 打开页面，不滚动；2) 看左侧栏底部。 | `shots/01c-main-topbar-sidebar.png`、`shots/01-main-default.png`。测量：`.nav-scroll` scrollHeight=1022 / clientHeight=750 → 272px 内容在视口外；「素材库」y=825、「收藏」y=861、「内置 Agent」y=929、「硬件参数」y=1069（>900）。 | 1440 高度常见笔记本（1366×768 / 1440×900）下应保证「素材库 / 收藏 / 内置 Agent」可达：可把「系统」分组默认折叠、或把 `素材/收藏/智能` 三个高频入口提到顶部，或给 `.nav-scroll` 加显式滚动条+滚动渐隐提示。 |

---

## 二、中低危 / 观察项

| # | 严重度 | 现象 | 证据 | 建议 |
|---|---|---|---|---|
| **L1** | 低 | 全站次要文字统一用 `--muted:#8a919c`，在白底/浅灰底上普遍只有 **2.94–3.18:1**，低于 WCAG AA 4.5:1。受影响面广：`span.tabs-label`「生成类型」(2.93)、`div.empty-hint`「当前没有排队或生成中的任务」(2.94)、`span.page-summary`(2.94)、`div#alStats.al-sub`「共 99 个素材 · 53.1 MB …」(2.94)、`div.agent-hint`(2.99)、`small.muted` 各类「推荐」「留空随机」(3.07)、`span.brand-tagline`「多模型图像 / 视频 / 音频创作工作台」(3.18)、`span.brand-version`「v1.00」(3.18)、`span#statusText`「空闲」(3.18)、`div.nav-label`「CREATE」(3.18)、`button.nav-group`「视频/文生图/图片编辑/音乐/素材/智能/系统」(3.18)、`span#queueSummary`「最近完成 12」(3.07)。 | `measurements.json` → 各页 `lowContrast`；跨页去重后 **59 个唯一选择器**低于阈值。代表截图：`shots/01-main-default.png`、`shots/04-assets-page.png`、`shots/05-agent-page.png`。 | 把 `--muted` 从 `#8a919c` 调到 `#6b7280`（≈4.83:1）或 `#71767f`（≈4.6:1），一次性修复全部 59 处；9–10px 的 hint 字号同时上调到 11px。 |
| **L2** | 低 | 「首尾帧 / 参考图」面板的虚线上传槽 **中心浅灰「+」字对比度仅 1.59:1**（`color:#c4cad2` on `background:#fafbfc`，24px 大字但仍属「非大字」判定），几乎看不见可点；`.up-slot .tag`（首帧/尾帧）另用 `rgba(0,0,0,.55)` 底白字，两者风格不统一。 | `shots/x-upslot-zoom-s.png`（3× 裁切）、`shots/02-mode-flf-panel.png`、`shots/02-mode-r2v-panel.png`。DOM：`div.up-slot` color=rgb(196,202,210) bg=rgb(250,251,252)，`web/style.css:286-290`（`color:#c4cad2; background:#fafbfc`），命中 6 个槽位（首帧/尾帧/参考视频 1-3/参考音乐 1-3）。 | 中心「+」提到 `#9aa3af` 以上（≥3:1）；或统一成 `.tag` 那样的深底白字方案。 |
| **L3** | 低 | 375px 下**触控目标 < 44px 的可点击元素共 156 个**（素材库 43 个、Agent 27 个）。最小的一批：`input#vidTwopassEnabled` **15×15**、`input#vidTwopassAllowContinuation` **13×15**、`button#alDrawerClose` **13×22**、`button#fdClose` **13×26**、`button.nav-cat-toggle` **32×32**（×4）、`button#navToggle` **36×36**、`button#agMenu` **34×34**、`button.img-lora-btn.{toggle,up,down,del}` **34×34**（×4）、`button#agAttachBtn`「📎 媒体」**64×27**、`button#agManageBtn`「⚙ 管理」**64×31**、`button#agNew`「新会话」**129×31**、`button#agArtifactsBtn`「本次会话产物」**129×31**、`select#alSort` **100×30**、`select#alSource` **99×30**。 | `measurements.json` → 各页 `smallTargets`；跨页去重后 **125 个唯一选择器**。`shots/14-narrow375-agent.png`、`shots/13-narrow375-assets.png`。注意：`responsive.css:180-193` 已把 `.jobs-tabs button` 提到 36px、`.jobs-pagination button` 提到 38px，但**漏掉了 `.nav-cat-toggle`、`.ag-*` 按钮组、`.al-sort/.al-source` 下拉**。 | ≤700px 媒体查询里补齐：`button.nav-cat-toggle{width:44px;height:44px}`、`.ag-new/.ag-attach-btn/.ag-manage-btn/.ag-artifacts-btn{min-height:44px}`、`select#alSort,select#alSource{height:40px}`；开关类 input 用透明 padding 扩大点击区到 44×44。 |
| **L4** | 低 | 素材库文件名 `.al-name` 被单行省略号截断（`v6s.mp4_replaced_a5993e34.mp4` → sw=185/cw=153），哈希后缀（最有区分度的部分）被吃掉。 | `shots/04-assets-page.png`、`shots/04b-assets-grid.png`、`shots/13c-narrow375-assets-loaded.png`（375px 下 `v1.mp4_7d9141c6.webm` 亦单行）。DOM：`div.al-name` scrollWidth=185 > clientWidth=153；CSS `web/assets.css:151-154`（`overflow:hidden;text-overflow:ellipsis;white-space:nowrap`）。 | 允许 2 行（`-webkit-line-clamp:2`）+ `title` 提示；或把哈希前缀移到文件名之前。 |
| **L5** | 低 | 图片/视频面板的 LoRA 名称与说明被截断：`span.img-lora-name`（「Minimax H3真实电影质感 — 去 AI 味电影质感 LoRA」）sw=283 / cw=25（截掉 258px）；`div.img-lora-meta`（「基础：Z-Image · 作者未给强度区间 → 按 LoRA 默认强度」）sw=441 / cw=277。 | `measurements.json` → `clipped`；`shots/02-mode-r2v-panel.png`、`shots/02-mode-t2i-panel.png`。CSS `web/style.css:1117-1120`。 | 同 L2/L4：允许 2 行折行 + `title` tooltip。 |
| **L6** | 低 | 「生成媒体库」顶部的「生成类型」筛选器在 1440 下也是 2 行 13 项，与上方 7 个状态标签、搜索框、排序/时长/比例下拉、「每页 N 个」选择器、「管理」按钮共处一个 `.jobs-head`，横向排布偏紧（右侧按钮组贴边）。 | `shots/01-main-default.png`、`shots/03-media-tab-all.png`、`.jobs-head` rect=[540,60,880,153]。 | 桌面端可把「生成类型」筛选改为可折叠；或给 `.jobs-tools` 右侧加 padding，避免贴边。 |
| **L7** | 低 | 375px 首屏顶栏显示「ComfyUI 未连接」时状态文字被压缩（`span#statusText` 对比度 3.18），且该状态是全局唯一的连接态指示，却用最浅的灰字。 | `shots/10-narrow375-main.png`、`shots/12-narrow375-drawer.png`；`span#statusText` color=rgb(138,145,156)。 | 状态文字单独用更深的灰（或按连接态染色），不要跟 `--muted` 共用。 |

---

## 三、已验证「不是问题」的项（避免误报）

- **横向溢出：0 处。** 58 个测量页面中 `document.documentElement.scrollWidth == window.innerWidth` 全部成立（1440 → 1440；375 → 375）。唯一「越界」元素是 `aside#alDrawer`（rect.left=375/1440），它是**关闭态的抽屉**（transform 移出视口），不是溢出。
- **元素重叠：0 处真实重叠。** 桌面端所有 `overlaps` 命中都是**祖先/后代关系**（`#jobsArea` ⊃ `#jobsHead` ⊃ `#jobsTabs`），不是视觉重叠。375px 下 `#panel` vs `#jobsGrid` 的重叠是 `#panel` 处于 `transform: translateY(100%)` 的收起态（rect.y=812 = 视口底），非真实叠加。**真实叠加只有 H1/H2 两处 FAB 相关问题。**
- **`err-text` 低对比度是误报。** 测量报 `color:rgb(255,156,160)` on 白底 2.0:1，但实测其父容器 `.job-preview.is-failed` 用的是**斜纹渐变底** `repeating-linear-gradient(135deg,#2a1418,#34191e)`（`web/style.css:473-478`），浅红字在深色斜纹上对比度充足。截图 `shots/x-media-failed.png` 确认清晰可读。
- **音乐面板 chip 的 `rgba(16,16,16,.3)` 低对比度是误报。** 复核（`verify_contrast.py`）显示选中态 chip 实际是 `rgb(79,110,247)` on `rgb(238,241,254)` = 3.80:1，未选中是 `rgb(0,0,0)` on 白 = 21:1。那条 2.0 的记录来自骨架屏占位态（`.job-skeleton .job-actions button{color:transparent}`），非真实渲染。
- **分页条未被 FAB 压住。** vision 一度判断「下一页」被遮，但 DOM 实测：`#jobsPagination` rect=[12,676,351,60] → 底边 y=736，FAB 顶边 y=752，中间有 16px 间距；像素裁切 `shots/15b-narrow375-bottom-zoom.png` 确认「下一页」「共 272 个作品」完整可见。
- **375px 抽屉导航文字无截断**，所有导航项单行完整显示；「释放显存」按钮本身可见（只是右端被 FAB 压，见 H2）。
- **375px 素材库 / Agent 页无横向溢出**（docSW=375 == innerW=375）。
- **375px 素材库「加载中…」不是缺陷。** 首轮 `13b-narrow375-assets-FULLPAGE.png` 抓到的是数据加载中的瞬时状态（`.al-grid` 有 60 个子节点但缩略图未渲染）。复采（等待 15s 后）`shots/13c-narrow375-assets-loaded.png` 显示网格正常渲染 2 列卡片、chips 换行整齐、无「加载中」残留，故不列入问题清单。
- **左右两栏（`.jobs-area` vs 卡片网格）在 375px 下无横向滚动**：`#jobsArea` scrollWidth=375 == clientWidth=375。

---

## 四、建议修复优先级

1. **H1** — FAB 遮挡卡片参数（375px 主场景，影响每一次浏览）
2. **H2** — FAB 遮挡抽屉底部「释放显存」（375px 导航场景）
3. **M3** — 禁用按钮白字/浅灰底 1.65:1（一行 CSS 改动，收益立竿见影）
4. **L1** — `--muted` 一处调色解决 59 处对比度问题
5. **M1 / M2** — 375px 首屏筛选器占 67% 高度、卡片参数截断
6. **M4 / L3** — 导航入口不可达、触控目标尺寸
