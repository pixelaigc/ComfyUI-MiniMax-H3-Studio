# A 段审查发现：前端用户可见文案污染

审查根目录：`/root/projects/h3s-cr-20261115`（独立 worktree，分支 `local/feature/code-review-20261115`，基线 `8bcc3d0`）
线上对照：`/root/projects/h3studio-live`（`local/deploy/qwen-ref-assets-live` @ `8bcc3d0`，systemd `h3-studio.service` → 8190）

## 审查方法（全部现场实测，非历史摘要）

1. **静态扫描**：自写 `audit/scan_visible_pollution.py`，抽取 `innerHTML` / `textContent` / `placeholder` / `title` / `aria-label` / `alert` / `confirm` / toast 类函数调用的字符串字面量与模板字符串静态片段，匹配污染标记（TODO/FIXME/XXX/HACK/debug/内部日期标记/交付项编号/未完成措辞/占位变量…）。命中 5 条，逐条人工判定。
2. **真实浏览器 DOM 抽取（ground truth）**：Playwright 打开 8190 首页，遍历 **15 个面板**（t2v/flf/r2v/longfilm/t2i/t2i_qwen/t2i_moss/t2i_h3image/i2i_qwen/i2i_h3image/t2a/a2a/assets/agent/system）+ **13 个模态框**（mediaManageModal/provModal/provMultimodal/enhanceModal/mossLayoutModal/promptVersionModal/fdModal/loraInfoModal/jobDetailModal/enhanceGateModal/promptModal/detailModal/agManageModal），采集全部可见文本节点 + `placeholder`/`title`/`aria-label`/`alt`。
3. **后端返回串检查**：对真实 API 响应 `curl http://127.0.0.1:8190/api/image-loras` 做机器字段名泄漏扫描。
4. **console.\* 调用点全量清点**：`grep -rn "console\." web/*.js`。

---

## A-1 【High】骨架卡把「骨架占位模型 · 8步」写进 DOM 文本 + title 原生 tooltip

**位置**
- `web/app.js:5480` — `prompt: "骨架占位提示词，用于让元信息区高度与真实卡片保持一致，长度对齐真实内容。"`
- `web/app.js:5524` — `model_label: "骨架占位模型"`

**现场证据**
浏览器实测（8190 live，页面加载后 0.30s 抓取）：
```
SKELETON AT BOOT: {"count":12,"detail":[
 {"tag":"B","title":"骨架占位模型 · 8步","color":"rgba(0, 0, 0, 0)"},
 {"tag":"B","title":"未使用 LoRA",...},{"tag":"B","title":"res_multistep",...},
 {"tag":"B","title":"simple",...},{"tag":"B","title":"8 · 1",...}, ...
 {"tag":"BUTTON","cls":"row prompt-row","title":"点击查看并复制完整提示词","color":"rgb(138, 145, 156)"} ]}
```
模态框扫描同样命中（`mossLayoutModal` / `agManageModal`）：
```
[mossLayoutModal] TEXT <B> :: 骨架占位模型 · 8步
[mossLayoutModal] TEXT <B> :: 骨架占位提示词，用于让元信息区高度与真实卡片保持一致，长度对齐真实内容。
[mossLayoutModal] ATTR:title <B> :: 骨架占位模型 · 8步
```

**为何会显示给用户**
`skeletonJob()` 造的假任务被 `jobCard()` **复用**渲染（这是刻意设计，为保证「骨架→真实卡片」盒模型逐像素一致）。`model_label` 与 `prompt` 原样进入 `<b>` 文本节点和 `title` 属性。
CSS（`web/style.css:384-389`）用 `color: transparent` 把文字涂掉 —— 但：
- `title` 是**浏览器原生 tooltip**，鼠标悬停即以浮层显示，与颜色无关；
- DOM 文本节点可被复制、可被读屏软件朗读、`aria-hidden="true"` 只影响可访问性树的暴露方式，不影响「复制页面内容」。

首屏加载即出现 12 张骨架卡，**这是最高频可见路径**。

**修复方式**
1. `model_label` 改为空字符串或中性占位（如 `""`），`prompt` 改为不含开发者措辞的中性占位；
2. `renderGridSkeleton()` 里对骨架卡整体移除 `title` 属性（骨架卡的 title 全是占位，无信息价值）；
3. 保持 `color: transparent` 与 `aria-hidden="true"` 不变（它们解决的是布局抖动与可访问性，不是文案污染）。

**无测试依赖**：`grep -rn "骨架占位模型\|骨架占位提示词" tests/` → 0 命中，改文案不会打破任何现有测试。

---

## A-2 【High】提示词输入框出厂预填一份原始内部 payload fixture

**位置**
`web/index.html:719`
```
<textarea id="prompt" rows="6" placeholder="描述画面…支持多镜头分镜写法">integrated_multimodal_description: [Shot 1] Live-action, cinematic Chinese wuxia period film. At night in a moonlit bamboo forest, ... His thumb slowly pushes the sword guard forward, exposing a narrow glint of steel before the video ends at 5.00 seconds.

overall_soundscape: Night wind rustles dense bamboo leaves while insects call faintly in the distance. ...

non_diegetic_music: Sparse low guqin notes over a sustained bowed-string drone, ending on a single deep drumbeat as the blade is exposed.</textarea>
```

**现场证据**
```
PROMPT TEXTAREA: {"value_len":1464,
 "value_head":"integrated_multimodal_description: [Shot 1] Live-action, cinematic Chinese wuxia period film. At night in a moonlit bamboo forest, a tight close-up frames an adult swordsman in black robes (S1)...",
 "value_tail":"...e metallic scrape of a sword beginning to leave its scabbard.\n\nnon_diegetic_music: Sparse low guqin notes over a sustained bowed-string drone, ending on a single deep drumbeat as the blade is exposed."}
```
全仓扫描「带默认内容的 textarea」：脚本遍历所有 `<textarea>`，命中 **仅 1 条**（就是这条）。

**为何会显示给用户**
textarea 的默认内容 = 输入框里的值，首屏直接可见、可编辑。用户不删就点「生成」，这份 fixture 会被当提示词提交到 ComfyUI。

**额外风险**
该 fixture 里的字段名 `integrated_multimodal_description` 正是 `h3studio/job_meta.py:755-808` 记录的历史故障源 —— 它曾被当成 **LoRA 名**显示在媒体库卡片 LoRA 行（`docs/H3模型参数注释与工作流对照-交付说明.md:49`），并留有专门回归测试（`tests/test_job_meta_c_fix.py:33`、`tests/test_artifact_meta.py:414`、`tests/test_lora_whitelist_all_modes.py:50`）。把它放在出厂默认位，等于把这个已知易错串的字段名重新端到用户面前。

**溯源**
`git log --oneline --all -S "integrated_multimodal_description: [Shot 1]" -- web/index.html` → `b45f7f0 sync: merge the local D:/h3s v1.00 code`（初次导入代码时带入）。
全仓**没有**「示例提示词」按钮 / 加载示例功能（`grep -niE "示例提示词|样例|loadSample|sampleBtn" web/` → 0 命中），说明这不是有意设计的示例功能，而是导入时残留的测试/演示数据。

**修复方式**
清空 textarea 默认内容，只保留 `placeholder="描述画面…支持多镜头分镜写法"`。

---

## A-3 【Medium】LoRA 下拉 / 详情 tooltip 泄漏上游机器字段名

**位置（节选，全部 105 处由 A-3-扫 汇总）**
| 文件:行 | 原文（节选） |
|---|---|
| `h3studio/lora_catalog_ea.py:69` | `基座判定：原生（Data.BaseModel=['Tongyi-MAI/Z-Image-Turbo']; vision_foundation=Z_IMAGE_TURBO）。` |
| `h3studio/lora_catalog_ea.py:1382` | `基座判定：原生（HF 镜像 key_map 命中判定：{'attn_hit': 128, 'attn_total': 128, 'attn_ratio': 1.0, ...}）` |
| `h3studio/lora_catalog_ea.py:935` | `基座判定：原生（README frontmatter vision_foundation: Z_IMAGE_TURBO；AigcAttributes.SubVisionFoundation=...）` |
| `h3studio/lora_catalog_ea.py:144` | `基座判定：原生（Civitai modelVersions[].baseModel = ZImageTurbo）。` |
| `h3studio/lora_catalog_nsfw.py:258` | `作者未给触发词（README instance_prompt 为 null）。` |
| `h3studio/lora_catalog_nsfw.py:349` | `作者未给触发词（instance_prompt 为 null）。示例 prompt 为 'masterpiece, best quality, anime coloring, anime screencap'。` |
| `h3studio/lora_catalog_nsfw.py:544` | `EasonnLi 10 件套 pack 成员。该版本 trainedWords 为空。作者未给强度。` |

**现场证据**
浏览器实测（LoRA 下拉 option 的 title / 文本）：
```
{'text': 'zitronstyle（动漫涂色 / 动画截图） — 作者未给触发词（instance_prompt 为 nu',
 'title': "作者未给触发词（instance_prompt 为 null）。示例 prompt 为 'masterpiece, best quality, anime coloring, anime screencap'。"}
```
后端 API 扫描（`curl /api/image-loras` → 逐条扫 `scene`/`strength_source`/`note`/`base`）：
```
TOTAL raw machine-field leaks: 105
```
token 计数：`Data.BaseModel` 91 / `vision_foundation` 70 / `frontmatter` 25 / `key_map`+`attn_hit`+`attn_total`+`all_hit`+`all_total` 各 10 / `modelVersions[` 5 / `instance_prompt` 3 / `lora_A`,`lora_B` 3 / `lokr_w` 2 / `trainedWords` 2 / `AigcAttributes`,`BaseModelRelation` 1 / `ss_output_name` 1。

**为何会显示给用户**
- `web/app.js:1170` — `const title = l.scene ? \` title="${esc(l.scene)}"\` : "";` → 整段 scene 进 `<option title>`，悬停即显示；
- `web/app.js:1168-1169` — 下拉**选项文本本身**取 `scene` 第一句（`split(/[。；\n]/)[0]`），所以 `作者未给触发词（instance_prompt 为 null）` 直接出现在**下拉列表可见文本**里；
- `web/app.js:1264` — 「近似适用」标签 `title="${esc(meta.scene || "")}"`；
- `web/app.js:1458` — LoRA 详情模态框 `<div class="lora-scene">${esc(meta.scene || "（上游未提供场景说明）")}</div>` 渲染全文。

**修复方式**
在 catalog 出站前统一清洗 `scene`（新增一个 `sanitize_scene()` 之类的出站函数），把机器字段名改写成自然语言：
`Data.BaseModel` → 基座模型 / `vision_foundation` → 视觉基座 / `instance_prompt` → 触发词字段 / `trainedWords` → 训练词 / `key_map`+`attn_hit`/`attn_total`/`all_hit`/`all_total` → 兼容命中统计的自然语言 / `frontmatter` → 模型卡头部 / `modelVersions[].baseModel` → 平台标注的基座 / `lora_A`/`lora_B`/`lokr_w` → 权重结构描述。
清洗只动**机器字段名**，不动「方向：…」「触发词：…」「来源平台：…」这些用户真正需要的语义。

---

## A-4 【Medium】任务卡片 innerHTML 里嵌了开发用 HTML 注释块

**位置**
- `web/app.js:4364` — `<!-- 【2026-11-13】骨架卡：补上与真实卡片同形的「下载 / 新窗口打开」两个占位按钮（真实卡片有产物所以有，骨架没有产物所以缺）。缺这两个 -> 动作行少两个 -> 从 2 行缩到 1 行 -> 骨架比真实卡片矮 17px -> 网格高度跳 68px（test_grid_flash 抓的）。 -->`
- `web/app.js:4373` — `<!-- 【2026-11-13 需求 C3】详情按钮：卡片区域有限，完整生成参数（模型/步数/调度器/采样器/shift/LoRA 列表+强度/尺寸/时长/seed/完整提示词/注意力机制/任务来源与工作流名/创建时间）放到模态框里。 -->`

**现场证据**
```
DOM COMMENT NODES: {"count":133, "sample":[
 "左侧导航（窄屏折叠为抽屉，见 responsive.css）",
 "【2026-11-13 左侧栏折叠 + 滚动】\n 导航内容放进独立滚动容器 .nav-scroll：...",
 ...]}
```
index.html 的静态注释是主体（130 个），但 `jobCard()` 的 innerHTML 模板注释会**随每次卡片渲染进 DOM**。

**为何会显示给用户**
HTML 注释本身不渲染，但进入 DOM 后：1) 用户「查看源代码」能看到；2) 「复制页面内容」会带走；3) 内容含内部日期标记、`test_grid_flash` 测试名、`->` 箭头等纯开发语境，出现在用户可见产物里。

**修复方式**
把 `jobCard()` 模板里的两段 HTML 注释移到**模板字符串外**的 JS 注释（`//`），DOM 中不再产生注释节点。

---

## A-5 【Low / 无需修】控制台日志文案

**位置**：`web/app.js:6868`、`web/interaction-guard.js:266`、`web/video-nav.js:306`、`web/video-panel.js:1155,1555,1558,1649,1743,1761`

**现场证据**：`grep -rn "console\." web/*.js` → 共 9 处，全部是 `console.error` / `console.warn`，带 `[启动]` / `[交互守卫]` / `[视频导航]` / `[视频面板]` 前缀。**无 `console.log` 调试打印**。

**判定**：不进 DOM，用户界面看不到；属正常可观测性日志。**保留，不修**。

---

## 已排查但确认无问题（false positive 清单）

| 命中项 | 判定 |
|---|---|
| `Mystic-XXX V6/V7/v4`（option 文本 + title） | LoRA **真实模型名**（成人向风格系列），不是污染标记。→ 但同行 title 里的 `instance_prompt` 是真泄漏，归入 A-3 |
| `assets.js:815/1138/1147` 命中 foo/bar/baz | 命中的是 class 名 `up-bar` / `up-name` 里的 `bar`，非占位变量 |
| `app.js:1185` `官方暂未发布 H3-Image 的 LoRA` | 面向用户的明确说明文案（需求原文要求「界面需留位或明确说明」），**不是**污染 |
| `app.js:265/331/356/1127/1180/1481/3067/4763` 「官方暂未发布」 | JS 注释或面向用户的说明文案，不进 DOM |
| `agent_sessions.js:211/213` 「待补充」 | JS 注释，描述会话收尾状态枚举 |
| `video-panel.js:2190` / `continuation.js:369` / `video-effect-lora.js:568` 的 `_debug` 导出 | `window` 调试接口，不渲染到 DOM；`app.js:3093` 确实读取 `VIDEO_PANEL._debug.currentSize()`，接口存在且健壮 |
| `provLlmUrl` placeholder「ComfyUI 节点内部去连的 LLM 地址」 | 「内部」指节点内部连接，非开发者痕迹，是正常产品文案 |
| `web/app.js:4325` 起的 `console.error` 启动日志 | 正常错误日志 |

---

## A 段小结

| 编号 | 严重级 | 问题 | 是否修复 |
|---|---|---|---|
| A-1 | High | 骨架卡占位文案进 DOM 文本 + 原生 tooltip（首屏 12 张） | 待修 |
| A-2 | High | 提示词框出厂预填内部 payload fixture（1464 字符） | 待修 |
| A-3 | Medium | LoRA tooltip 泄漏上游机器字段名（105 处） | 待修 |
| A-4 | Medium | 任务卡片 innerHTML 内嵌开发 HTML 注释 | 待修 |
| A-5 | Low | console.* 日志 | 无需修（保留） |
