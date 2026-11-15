# H3-Studio 前端代码审查报告（B 组 · 完整稿）

**范围**：`/root/projects/h3s-cr-20261115/web/`（30 个 JS/HTML/CSS 文件，前端 JS 合计 33,344 行）
**方式**：只读静态审查。**审查过程中未修改任何仓库文件、未执行任何 git 写操作、未重启/部署服务。** 所有结论均来自实际读到的代码并标注行号。
**审查基线**：commit `8bcc3d0`（`local/feature/code-review-20261115`）。

## 关于行号与「是否已被主线修掉」的说明

审查完成后，主线 agent 已在同一分支上继续改动并部署。本报告的**行号一律以审查基线 `8bcc3d0` 为准**（因为发现是在那份代码上做出的）；每条发现额外标注**当前 HEAD（`293e593` + 未提交改动）上的实际状态**，避免读者按当前行号找不到对应代码。

当前工作区相对基线的已发生改动（主线所为，非本审查所为）：

| 改动 | 内容 | 对本报告的影响 |
|---|---|---|
| `293e593` | `web/app.js:3168-3190` 不再用 `legacySize` 回写 `params.width/height`，只反算 `resolution` 标签 | **B-1 已被主线修掉**（保留描述，见下） |
| `293e593` | 骨架卡占位文案 / `model_label` / 骨架 `title` 属性清理；`index.html` 清空 `#prompt` 预填 fixture | 属于 A 组（可见文案污染），不在本报告范围 |
| 未提交（工作区） | `web/providers.js` 删除 649-659 的 12 行纯注释块 | **B-8 已被主线修掉**（保留描述，见下） |

---

## 一、缺陷清单（12 条）

### B-1 `app.js:3168-3172`（基线） — flf/r2v 提交尺寸被旧面板常量覆盖 · **Critical**

| 项 | 内容 |
|---|---|
| 位置 | `web/app.js:3168-3172`（基线 8bcc3d0）；关联 `2517-2550`、`128/132`、`3136-3137` |
| 问题 | `params.width/height` 先在 `3136-3137` 由共享面板写入用户真实选择，随即在 `3168-3172` 被 `currentLegacyVideoSize()` **无条件覆盖**。该函数读 `state.res`/`state.aspect`（`2549`），而这两个字段**全仓从未被写过**（仅 `128`/`132` 初始化为 `"544p"`/`"16:9"`），恒定返回 `864×480`。基线原文：`const legacySize = currentLegacyVideoSize(); if (legacySize) { params.width = legacySize.width; params.height = legacySize.height; }` |
| 严重级 | **Critical** |
| 用户影响 | 用户在共享面板选「608p · 16:9」（1088×608），首尾帧/参考生视频实际跑 **864×480**。面板显示与产物不符 —— 正是修复③ 想消灭的「面板说谎」。服务端 `h3studio/h3_video_size` 走的是「有显式 width/height 就用它」（`h3_video_modes.py:451-454`、`568-571`），所以前端传什么就跑什么，**服务端不会救**。 |
| 建议修复 | 删除基线 `3168-3172` 整段（连同 `3154-3167` 的注释一并删）。`3136-3137` 已是共享面板的唯一正确来源；`legacyVideoSize`/`currentLegacyVideoSize`（`2517-2550`）仅剩 `3149-3150` 一处兜底调用，可一并删除该函数。 |
| 当前状态 | **已被主线修掉**（`293e593`）。当前 `app.js:3186-3190` 只保留 `params.resolution = legacyTierForPixels(...)`，不再回写 width/height；兜底路径移到 `3142-3151` 的 else 分支。**本审查不再重复修。** |

### B-2 `app.js:3228-3240`（基线） — `asset_refs` 提交完全不看当前模式 · **Critical**

| 项 | 内容 |
|---|---|
| 位置 | `web/app.js:3228-3240`（基线；当前 HEAD 为 `3236-3258`） |
| 问题 | `Object.entries(state.assetRefs)` 无 mode 判断地**全量提交**；`SLOT_FOR_KEY = { first: "first_frame", last: "last_frame" }`（基线 `3218-3220` / 当前 `3236-3238`）把 `first`/`last` 无条件映射成首尾帧槽位。 |
| 严重级 | **Critical** |
| 用户影响 | 用户在首尾帧模式选了素材库首帧 → 切到**参考生视频**（r2v）→ 提交时残留的 `first_frame` 仍被提交。服务端 `server.py:1335` + `_ASSET_SLOT_PARAM`（`server.py:1267-1269`）会把 `first_frame` 写进 `params`，而 `build_r2v`（`h3_video_modes.py:534`）只读 `ref_images/ref_videos/ref_audios/ref_video_audios` —— 残留首帧变成**静默丢弃的孤儿输入**；同时 `server.py:1341` 的 `_repo.add_ref` 让素材引用计数被错误 +1，删素材时触发不该有的 409 保护（`asset_repo.py:498`）。 |
| 建议修复 | 在 `Object.entries(...)` 循环体首行（当前 `3246-3247` 之间）加模式闸门：`if (mode !== "flf" && (key === "first" \|\| key === "last")) return;`。更彻底的做法是切模式时清理 `state.assetRefs`（见 B-3）。 |
| 当前状态 | **未修**（当前 HEAD 该循环仍无 mode 判断）。 |

### B-3 `app.js:495` + `589/615` — 槽位 DOM 每次切模式重建，但 `state.files`/`state.assetRefs` 不清 · **High**

| 项 | 内容 |
|---|---|
| 位置 | `web/app.js:495`（`row.innerHTML = ""`，当前 HEAD 仍为 495）；写入点 `589`（`state.files[key] = file`）、`615/616`（`state.files[key] = null` / `state.assetRefs[key] = {...}`） |
| 问题 | `renderMode()` 第 495 行清空 `#uploadRow` 并用 `mk()` 重建全新槽位；但 `state.files[key]` 与 `state.assetRefs[key]` **全仓没有任何一处被重置**。我用脚本扫描了 `state.files\s*=` / `state.assetRefs\s*=` / `\.files\s*=` / `\.assetRefs\s*=` / `delete state.files` / `Object.assign(state` 全部写形态：除 `589/590/615/616` 这四处「填槽位」写入外，**命中 0 处清理写入点**。 |
| 严重级 | **High** |
| 用户影响 | 切模式后界面槽位全空（新节点没有 `filled` class、没有缩略图），但 `state.assetRefs` 里旧素材仍在 → 提交时被静默带上（见 B-2）。用户在 flf 选的首帧，切到 r2v 后界面看不到，但任务里居然有首帧 —— 「界面与提交不一致」的另一种形态。 |
| 建议修复 | 在 `renderMode()` 的 `row.innerHTML = ""`（`495`）**之后**、重建槽位**之前**，按新模式清空不再适用的槽位键：`state.files = {}; state.assetRefs = {};`。（`168` 行 `files: { first: null, last: null, refs: [] }` 里的 `refs: []` 是无人读取的遗留字段 —— 全仓 grep `files.refs` 命中 0 处，可一并删。） |
| 当前状态 | **未修**。 |

### B-4 `app.js:494-495` — `#uploadRow` 重建导致槽位预览状态与界面脱节 · **Medium**

| 项 | 内容 |
|---|---|
| 位置 | `web/app.js:494-495`（重建点）、`529-547`（`openSlotPreview`）、`569`（`slot.dataset.previewUrl`） |
| 问题 | 重建后槽位无 `filled` class、无缩略图、无 `dataset.previewUrl`；而 `openSlotPreview` 只从 `state.assetRefs[key]` 或 `slot.dataset.previewUrl` 取 URL —— 重建后 `dataset.previewUrl` 已随旧 DOM 节点销毁。 |
| 严重级 | **Medium** |
| 用户影响 | 切模式再切回来，用户之前上传的本地文件槽位显示为空（视觉上正确），但 `state.files[key]` 仍持有 File 对象 → 若新模式有同名槽位（`refvid0`/`refaud0` 在 flf 与 r2v 都会渲染），提交时会把**上一次模式**的本地文件当本次素材发出去（`3290/3295/3321/3326` 的读取路径）。 |
| 建议修复 | 与 B-3 同处修复：`renderMode()` 重建前清空 `state.files` / `state.assetRefs`。若希望保留「同模式内切走再切回不丢素材」，则改为按模式分桶存储（`state.slotStateByMode[mode]`）而不是共用一份。 |
| 当前状态 | **未修**。 |

### B-5 `video-panel.js:1824` — `submitParams()` 引用了本文件不存在的全局 `state` · **Medium**

| 项 | 内容 |
|---|---|
| 位置 | `web/video-panel.js:1824`（`duration: state.duration`）；真源 `web/app.js:121`（`const state = {`） |
| 问题 | `video-panel.js` 是 IIFE（`23` 行 `(function () {`），内部**没有**声明 `state`，只有 `videoState`（`59` 行）。`1824` 行裸引用 app.js 的全局 `const state`（script 作用域）。加载顺序还是反的：`index.html:1503` 先加载 video-panel.js，`1537` 才加载 app.js —— 因 `submitParams()` 只在运行时调用，实际不触发 TDZ。我扫描了全部非 app.js 模块的裸 `state.xxx` 引用（去注释后）：`video-panel.js` 只有 `state.duration` 这一处真依赖（`state.width`/`state.img` 只出现在注释里）。 |
| 严重级 | **Medium** |
| 用户影响 | 目前不崩，但这是**隐式跨模块耦合**：谁把 app.js 的全局 `state` 改名或包进 IIFE，`1824` 行立刻 `ReferenceError` → `submitParams()` 抛错 → `app.js:3075`/`3118` 的 `Object.assign(params, ...)` 抛异常 → 结合 B-10（提交 handler 无顶层 try/catch）→ **整个生成按钮白屏式无响应**，用户点「生成」没有任何提示。 |
| 建议修复 | 走已存在的窄接口：`duration: (window.__h3State && window.__h3State.duration) \|\| 5`（`window.__h3State` 由 `app.js:7073` 导出），或让 video-panel.js 自己维护 `videoState.duration` 并在 `setMode` 时同步。前者一行、风险最低。 |
| 当前状态 | **未修**（当前 HEAD 仍为 `duration: state.duration`）。 |

### B-6 `app.js:128/132` + `2549` — `state.res` / `state.aspect` 是死常量 · **Medium**

| 项 | 内容 |
|---|---|
| 位置 | `web/app.js:128`（`res: "544p"`）、`132`（`aspect: "16:9"`）、`2549`（`return legacyVideoSize(state.res, state.aspect, state.mode)`）、`2996-2997`（`resolution: state.res, aspect: state.aspect`）、`4684`（复用参数路径 `aspect: p.aspect \|\| state.aspect`） |
| 问题 | 与 B-1 同源但独立成条。全仓搜索所有写形态（`.res =`、`.aspect =`、`state[key] =`、`Object.assign(state`）**均未命中这两个顶层字段**；命中的 `.aspect =` 全部是 `state.img.aspect` / `state.imgQwen.aspect` 等子对象。唯一的通用写入器 `bindSeg()`（`2471`，`state[key] = b.dataset.v`）**全仓零调用点**（`grep -rn bindSeg` 只命中定义行）—— 旧面板 DOM 已在修复③ 中删除（`2479-2481` 注释明确说明「旧面板的 seg 绑定（#resSeg / #aspectChips / #modelCards / #loraCards）已随对应 DOM 一起删除」）。字段失去唯一写入源后成为**永久冻结的常量**，但仍被 `legacyVideoSize()` 用来算尺寸（→ B-1），并被 `2996` 作为 `params.resolution` 的初始值。 |
| 严重级 | **Medium** |
| 用户影响 | 本身不直接产生错误（B-1 修掉后 `3140`/`3188` 会用真实像素反算覆盖 `resolution`），但 `2996` 的 `resolution: "544p"` 会作为**兜底路径**（`3142-3151`，共享面板未就绪时）的落库档位标签，与 `3149-3150` 算出的 864×480 一起构成「面板没就绪时提交的是硬编码档位」。 |
| 建议修复 | 随 B-1 一并删除 `legacyVideoSize` / `currentLegacyVideoSize` 及 `state.res` / `state.aspect` 字段，并把 `2996-2997` 改成不预置（由模式分支自己写）。删除前需确认 `4684`（旧任务复用路径）改用 `p.aspect` 单一来源。 |
| 当前状态 | **部分修**：B-1 的覆盖行为已修；`state.res`/`state.aspect` 字段与 `2996` 的预置**仍在**，`legacyVideoSize`/`currentLegacyVideoSize` 也保留（现仅服务于 `3149-3150` 兜底与 `3186` 的档位标签反算）。 |

### B-7 `app.js:3093-3095` — `_debug` 导出被生产代码当作正常 API 使用 · **Low**

| 项 | 内容 |
|---|---|
| 位置 | `web/app.js:3093-3095`（`window.VIDEO_PANEL._debug.currentSize()`）；导出点 `web/video-panel.js:2189-2194`（注释写着「供验收脚本直接读状态」） |
| 问题 | 这是**唯一**的 `_debug` 生产调用点（其余 4 处 `_debug` 定义只被测试脚本读取：`video-panel.js:2190`、`continuation.js:369`、`video-effect-lora.js:568`；全仓 grep `_debug` 共 5 处 = 4 定义 + 1 生产调用）。 |
| 严重级 | **Low** |
| 用户影响 | 不产生用户可见缺陷（`_debug` 不泄漏到任何用户可见文案）。但生产逻辑依赖「调试接口」是设计倒置：将来清理 `_debug` 会**静默打断**接续的尺寸一致性闸门（`Continuation.checkSubmitConsistency`），而那条闸门防的是 Relay-Kit「拷贝桥禁止跨分辨率」的硬失败。 |
| 建议修复 | 把 `currentSize` 提升为正式公开 API：在 `video-panel.js:2188` 附近（与 `accelOptions` 同级）加 `currentSize,`，`app.js:3093-3095` 改读 `window.VIDEO_PANEL.currentSize()`。`_debug` 保留给验收脚本。 |
| 当前状态 | **未修**。 |

### B-8 `providers.js:649-659`（基线） — 修复① 的死代码确实已删除，但注释块自身是残留 · **Low**

| 项 | 内容 |
|---|---|
| 位置 | `web/providers.js:649-659`（基线 8bcc3d0，12 行纯注释） |
| 问题 | 一整段**纯注释**，描述「原先这里有个 `applyQwenPeWritebackFlag()`……现在整段删除」。我全仓 grep `usePe` / `use_pe` / `applyQwenPeWritebackFlag` 覆盖 `web/` 全部 30 个 JS：**无任何可执行残留**（33 处命中全部位于注释）—— 就「死代码是否删干净」这一点上，修复① 是干净的。 |
| 严重级 | **Low** |
| 用户影响 | 无功能影响。但 12 行注释解释「已删除的代码为什么被删」，对读代码的人是噪音，且每次搜索 `usePe` 都会命中，制造「这里还有 PE 开关」的错觉。 |
| 建议修复 | 压缩为 1-2 行说明，或直接删除。**注意**：`app.js:2324-2329` 另有一段 6 行注释仍在描述 `usePe=true/false` 两态语义（`2325` 行「默认（没做过手动官方 PE 扩写）：usePe=true」），那段描述的行为**已不存在**，属于**过期文档**、比 providers.js 这段更容易误导 —— 优先清理 `app.js:2324-2332`（见 B-9）。 |
| 当前状态 | **已被主线修掉**（工作区未提交改动删除了这 12 行；当前 `providers.js:649` 起是「增强结果写回」的章节注释）。`app.js:2324-2329` 那段**仍在**。 |

### B-9 `app.js:2324-2329` — 过期注释描述已删除的行为 · **Low**

| 项 | 内容 |
|---|---|
| 位置 | `web/app.js:2324-2329`（当前 HEAD 同样位置） |
| 问题 | `2325-2328` 行讲「usePe=true → 出图时链路内部再扩写一次 / usePe=false → 不再二次扩写」，而修复① 已把整条路删掉（`app.js:3050-3053` 注释确认「这里**不再**提交 `params.use_pe`」；`app.js:204` 注释确认「usePe 已删除」）。同文件内两处注释互相矛盾。 |
| 严重级 | **Low** |
| 用户影响 | 无功能影响。风险在于**未来的维护者**照着这段注释去实现 `usePe` 开关，或误以为出图链路仍受 PE 开关控制。 |
| 建议修复 | 删除 `2324-2329`（「两态」那段），保留 `2331-2332` 那句「原先读取档位的辅助函数已无调用点」的说明即可。 |
| 当前状态 | **未修**。 |

### B-10 `app.js:2928` — `#genBtn` 的 click handler 是 async 且无顶层 try/catch · **Medium**

| 项 | 内容 |
|---|---|
| 位置 | `web/app.js:2928`（`$("#genBtn").addEventListener("click", async () => {`）；未包裹区间 `2935`–`3219`；最近的 try 在 `3220`（只覆盖 asset_refs / 文件上传段），catch 在 `3371` |
| 问题 | 从 `2935` 到 `3219` 之间有大量**未包裹**的 DOM 读取与状态操作（`2981` 的提示词读取、`3075`/`3118` 的 `Object.assign(params, submitParams())`、`3136-3141` 的 `shared.width` 读取、`3212` 的 `window.QwenRefs.validate`），只有 `3220` 之后才进 `try`。音乐分支同理：`2941` 的 `window.MusicUI.collect()` 在 `2967` 的 try **之外**。 |
| 严重级 | **Medium** |
| 用户影响 | 若 try 之前任一行抛异常（如 B-5 的 `state.duration` ReferenceError，或 `window.QwenRefs.validate` 在模块未就绪时抛），异常在 try 块**之外**逃逸 → async handler 的未捕获 rejection → **无任何用户提示**，用户点「生成」后按钮无反应、不知发生了什么。这是「异常未捕获导致白屏」类的真实路径。 |
| 建议修复 | 把 `2928` 的 handler 整体包进 `try { ... } catch (e) { alert("提交失败：" + e.message); }`，或至少把 `2935-3219` 段纳入 try。注意保持现有 `return` 语义（早退分支不能被 catch 变成错误提示）。 |
| 当前状态 | **未修**。 |

### B-11 `video-panel.js:1826` + `app.js:139/2999` — `state.steps` 与视频面板 `videoState.steps` 双轨并存 · **Low**

| 项 | 内容 |
|---|---|
| 位置 | `web/app.js:139`（`steps: 4`）、`2999`（`steps: state.steps` 无条件预置）、`3073-3075`（t2v 用 `submitParams()` 覆盖）、`3116-3151`（flf/r2v 分支）、`web/video-panel.js:1828`（`steps: videoState.steps`） |
| 问题 | `app.js:2999` 在构造 params 时**无条件**写 `steps: state.steps`（全局，恒为 4）。随后 t2v 分支用 `Object.assign(params, window.VIDEO_PANEL.submitParams())` 覆盖为 `videoState.steps`。但 **flf/r2v 的兜底分支**（`3142-3151`）只写 sampler/scheduler/shift/width/height，**不写 steps** → 若 `VIDEO_PANEL.isReady()` 为 false（清单接口失败），flf/r2v 会用全局 `state.steps=4` 而不是面板值。steps 是该兜底分支里**唯一**没有对齐的采样参数。另外 `state.steps` 的唯一外部写入者是 `continuation.js:190`（`appState.steps = params.steps`，`window.__h3State` 与 `state` 是同一对象），而它回填的 DOM 目标 `#steps`/`#stepsVal` 已不存在（见「怀疑点」第 9 条）。 |
| 严重级 | **Low** |
| 用户影响 | 当前 t2v 路径正确（后者覆盖前者）。仅在「共享面板未就绪」的降级路径下，flf/r2v 的步数会退回硬编码 4；同时接续回填的步数不会反映到面板控件上。 |
| 建议修复 | 在 `3145-3148` 的兜底分支补一行 `params.steps = Number(state.vidSteps) \|\| state.steps;`，或把 `2999` 的 `steps: state.steps` 从公共 params 里移除、改由各模式分支显式写（消除双轨真源）。 |
| 当前状态 | **未修**。 |

### B-12 `app.js:3153` — `state.audioTrackSource` 跨面板残留 · **Medium**

| 项 | 内容 |
|---|---|
| 位置 | `web/app.js:176`（初始化）、`3153`（`params.audio_track_source = state.audioTrackSource \|\| ""`，只在 flf/r2v 分支执行）、`915-946`（`audioTrackAvailability()`）、`949-977`（`renderAudioTrackSelect()`）、`829`（`renderMode()` 无条件调用它） |
| 问题 | 用户在 flf 选了「使用参考音乐作为成片音轨」（`audioTrackSource="reference"`）→ 切到 r2v。`renderAudioTrackSelect` 的 `968-971` 会把不可用的选择回落成 `""`，看似自愈；**但** `audioTrackAvailability()`（`916`、`940-944`）读的是 `state.assetRefs` / `state.files`，而这两者切模式时**没有被清**（B-3）→ `hasRefAudio` 仍为 true → 「使用参考音乐作为成片音轨」选项在**新模式**里错误地出现，而新模式界面里并没有对应素材。 |
| 严重级 | **Medium** |
| 用户影响 | 用户在新模式看到一个「其实没有素材支撑」的音轨选项并可选中；提交后服务端 `h3_audio_track.normalize_track_source`（`h3studio/h3_audio_track.py:63-79`，被 `server.py:1729`/`h3_video_modes.py:386` 调用）会把它归回「生成的音乐」且不报错 —— 于是表现为「我选了用参考音乐，成片却是生成的音乐」，没有任何解释。 |
| 建议修复 | 与 B-3 同处修复：切模式时清 `state.files` / `state.assetRefs`，`audioTrackAvailability()` 自然只反映当前模式的素材。若想更稳，可在 `renderAudioTrackSelect` 里对「选项可见但素材来自上一次模式」的情况禁用该 option 而不是让它可选。 |
| 当前状态 | **未修**。 |

---

## 二、已排查但确认无问题的怀疑点（13 项）

| # | 怀疑点 | 如何验证 | 结论 |
|---|---|---|---|
| 1 | `providers.js:651-658` 的 `state.imgQwen.usePe` 是否仍有残留写入 | 全仓 `grep -E 'usePe\|use_pe\|applyQwenPeWritebackFlag'` 覆盖 `web/` 全部 30 个 JS，逐条读命中行上下文 | **无残留**。33 处命中全部位于注释（`providers.js:15-18`、`app.js:204`、`2325-2329`、`3050-3053`），无任何可执行语句写 `usePe`。修复① 干净；残留只是注释噪音（B-8/B-9） |
| 2 | `app.js:3093` 的 `window.VIDEO_PANEL._debug` 是否存在、是否健壮 | 追 `video-panel.js:2162-2195` 的导出对象，确认 `_debug.currentSize` 存在（`2193`）；再读 `currentSize()` 实现（`253-258`） | **存在且健壮**。`currentSize()` 有 null 兜底（`if (videoState.width && videoState.height)` 否则走 `autoSize`）；`app.js:3093` 也做了 `_debug` 存在性检查。唯一问题是「生产代码依赖调试接口」的设计倒置（B-7），非缺陷 |
| 3 | `video-panel.js:2190` / `continuation.js:369` / `video-effect-lora.js:568` 的 `_debug` 是否泄漏到用户可见文案 | 逐条检查 4 个 `_debug` 对象的全部成员（`selectableAccelLoras`/`presetsForAspect`/`currentSize`、`current`/`capability`、`filterCandidates`/`recommendedStrength`/`renderClearButton`）；再全仓 grep `_debug` 确认只有 5 处（4 定义 + 1 生产调用） | **无泄漏**。所有成员均为函数或数据，不进任何 `innerHTML` / `textContent` 路径；用户可见文案由 `toast()`（`continuation.js:51-66`）与 `setEnhanceHint()` 独立产出 |
| 4 | 面板切换后媒体库过滤条件是否串台（修复② 是否真的修好） | 追 `refreshJobs()`（基线 `5678` / 当前 `5697`）的 query 构造：`5736-5744` 确认只剩 `state.doneFilters`（用户自己设的）；`5749-5766` 确认 `media=video` 的页面级覆盖已被注释删除；再查 `stitchMediaOverride` 的全部写读点（`166` 声明、`6283`/`6329`/`6720` 只被清空、**从未被读进 query**）；拼接的「只收视频」由自己的片段池 `collectStitchCandidates()` 实现 | **已修好**。共享媒体库只反映用户自己的筛选，长片拼接不再污染它 |
| 5 | `state.page` 是否还会被 Agent 页污染 | 追 `agent.js:2246-2247` → `bridge.notifyPageChange("agent")` → `app.js:6716`（`notifyPageChange`）；并 grep `window.state\s*=` 全仓 | **已修好**。旧写法 `if (window.state) window.state.page = "agent"` 确实永不执行（`window.state =` 命中 0 处业务赋值，仅 2 处注释：`agent.js:2228/2230`、`app.js:6698-6700`）；新代码走窄接口，单一真源 |
| 6 | 是否有 `addEventListener` 绑到已被 `innerHTML` 重写替换掉的节点（失效绑定） | 用脚本对全部 30 个 JS 做「`innerHTML=` 赋值 → 后续 90 行内的 `addEventListener`」邻近扫描，再逐条人工复核 | **未发现真正的失效绑定**。命中的模式全部是「先 `innerHTML=""` 再重新 append 元素并对**新**元素绑事件」的正确写法（`app.js:1105-1113`、`1579-1597`、`3370-3383`；`assets.js:546-568`、`1189-1218`；`video-effect-lora.js:162-200`）。`asset-player.js:484-524` 还有专门注释说明如何避免重写打断播放。`assets.js:792` 的 `bindDrawer(asset)` 每次重建后重新绑定，节点是全新的，无重复绑定 |
| 7 | 首屏初始化是否有 try/catch 缺失导致白屏 | 逐条检查 `app.js:6858-6964` 的 boot 序列 | **无白屏风险**。`boot(step, fn)`（`6883-6886`）把每个启动项单独包在 try/catch 里，失败只 `console.error` 不阻断后续；注释明确记录了历史事故（`esc()` 未定义抛 ReferenceError 中断整个顶层脚本）。各 IIFE 模块（`assets.js:7`、`agent.js`、`video-panel.js:23`）也各自独立作用域 |
| 8 | 是否存在顶层（模块加载时立即执行）的 DOM 查询缺失导致整脚本崩溃 | 脚本提取全部 JS 中「列 0 缩进」的 `$$("#id")` / `getElementById("id")` 调用，与 `index.html` 的全部 `id=` 集合做差集 | **无**（重跑差集：0 个缺失）。列 0 缩进的 DOM 引用全部命中真实存在的 id；函数体内的引用（`providers.js` 的 `#agentSpec`/`#agentSave`/`#agentSys`/`#agentUser`/`#agentMaxTokens`/`#agentLength`/`#agentSampling`）也安全 —— 它们由 `renderAgentConfig()` 的 innerHTML 动态生成后立即绑定，顺序正确 |
| 9 | `continuation.js` 写的 `#steps` / `#stepsVal` 是否失效 | 对 `index.html` 全量 id 集合比对 | **是失效的**（`#steps`/`#stepsVal` 在 index.html 中不存在，`#vidSteps`/`#vidStepsVal` 才是新的）。但 `continuation.js:189-195` 的写法带 null 检查（`const steps = document.getElementById("steps"); if (steps) ...`），**不会抛异常**。后果只是「接续回填的步数只写进 `window.__h3State.steps`，面板控件不更新」—— 功能性遗漏而非崩溃，且 `state.steps` 本身已无人消费（见 B-11），所以归入无问题项 |
| 10 | `#duration` / `#durVal` 回填是否生效 | 检查 `app.js:2611-2614` 的绑定与 `continuation.js:182-188` 的回填，并对 id 集合比对 | **生效**。两个 id 在 index.html 中均存在，`state.duration` 仍是视频时长真源（`app.js:2998` 提交、`video-panel.js:1824` 也读它） |
| 11 | 音乐面板状态是否会串到其它面板 | 追 `music.js:94` 的 `let st = loadState()`（localStorage 持久化）与 `116-120` 的 `currentMode()`（优先读 `window.__h3state()` 的实时 mode，`app.js:304` 导出） | **不会串**。音乐侧有独立的 `st`，`currentMode()` 用实时 mode 覆盖持久化值（`779-783` 注释明确说明）。去注释后扫描：`music.js` 内**零**处引用 app.js 的全局 `state`（`6`/`783` 两处只是注释里提到 `state.mode`） |
| 12 | 各模块是否隐式依赖 app.js 的全局 `state`（潜在 ReferenceError） | 对全部非 app.js 的 JS 去注释后扫描裸 `state.xxx` 引用，逐条确认是否为本地声明 | **只有 1 处真依赖**：`video-panel.js:1824`（B-5）。其余带 `state.` 的文件全部有自己的局部 `const state`（`agent.js`、`agent_sessions.js`、`assets.js`、`sidebar-fold.js`、`prompt-history.js`、`agent-manage.js`）。`enhance-menu.js` 的 12 处 `state.mode` 中 11 处带 `typeof state !== "undefined"` 守卫（`226/286/296/316/341/548/669/693/919/1869/1980`），唯独 `512` 行 `const mode = state.mode \|\| "";` 缺守卫 —— 但它在 `isMusicContext()`（`510-514`）内部，`511` 行已先做 `typeof state === "undefined"` 检查并返回 false，所以 `512` 只在 `state` 存在时可达。**安全** |
| 13 | `enhance-menu.js` 与 `enhance-menu-shared.js` 的 `esc` / `positionMenu` 重复定义 | 对两个文件的函数名集合做交集 | **重复但无害**。`esc` 是 6 行纯函数（HTML 转义），两处各自实现、语义一致；`positionMenu` 各自有独立作用域（两个文件都在 IIFE 内）。不构成正确性缺陷，仅维护性重复（`esc` 在 web/ 里共有 15 份独立实现，属同一类噪音） |

---

## 三、修复优先级建议

1. **先修 B-2 + B-3 + B-4 + B-12**（槽位状态清理）—— 四条根因同一处：`renderMode()` 重建 DOM 时没清 `state.files`/`state.assetRefs`。改动集中在 `app.js:494-495` 附近 + `3246` 循环加模式闸门。这是当前唯一会让「界面与提交不一致」的活跃缺陷族。
2. **B-10**（提交 handler 顶层 try/catch）—— 防御性加固，防止 B-5 或未来任一改动引发「点生成没反应」。
3. **B-5**（`state.duration` 跨模块隐式依赖）—— 消除定时炸弹，一行改动。
4. **B-11**（steps 双轨）—— 只在降级路径生效，优先级低但改动很小。
5. **B-7 / B-9** —— 清理项，可合并成一次提交。
6. **B-1 / B-6 / B-8** —— 主线已修或部分修（见每条的「当前状态」与文首改动表）。B-6 剩下的部分（`state.res`/`state.aspect` 字段、`app.js:2996` 的 `resolution: state.res` 预置、`legacyVideoSize` 保留）建议在 B-1 的后续清理里一并收掉；删除前先确认 `app.js:4684`（旧任务复用路径）不依赖 `state.aspect`。

**本审查未做任何仓库修改。** 上述所有「当前状态」均为只读复核结论（`git diff` / `git show` / `grep` 实测），不是推测。
