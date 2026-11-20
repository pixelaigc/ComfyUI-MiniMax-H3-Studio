# 「参考素材右键移除」在 8190 线上的可用性核实报告

- **日期**：2026-11-21
- **核实对象**：ComfyUI-MiniMax-H3-Studio 工作台，线上 `http://127.0.0.1:8190`
- **线上代码目录**：`/root/projects/h3studio-live`（systemd `h3-studio.service`，`WorkingDirectory=/root/projects/h3studio-live`）
- **核实方式**：只读取证。**未改任何代码、未提交、未重启 8190、未动他人 worktree**（`web/app.js` / `web/index.html` / `web/favorites.js` 全程未触碰）

---

## 一、结论

**功能可用（四个入口全部实测通过），那条 `open + interrupted` 的任务记录是状态残留，不是功能未完成。**

| 判定项 | 结论 |
|---|---|
| 「参考素材右键移除」在 8190 线上是否可用 | ✅ **可用**（r2v 参考图槽位 / flf 首尾帧槽位 / Qwen 图片编辑参考图 / 参考生音乐参考音频，四个入口全部真实右键实测通过） |
| 线上部署的代码里有没有这个功能 | ✅ **有**。线上 5 个相关静态文件与仓库 sha256 **逐一 MATCH**，不是"分支上有、线上没有" |
| 那条 `open + interrupted` 记录 | ⚠️ **状态残留**。它在 2026-11-20 11:28:22 被创建、11:28:40 被 **abort**（运行 18 秒，仅做了 4 次 `search_files` 探查），随后 11:28:46 由 coding profile 的**另一个任务**（`90f3bf94`）接手并在 14:30:53 完成。功能由后者完整交付并部署。 |

---

## 二、实现位置（file:line）

### 2.1 共享菜单本体

| 位置 | 内容 |
|---|---|
| `web/ref-context-menu.js:70-99` | `open(event, opts)` —— 弹菜单、造按钮、贴边定位 |
| `web/ref-context-menu.js:109-112` | `attach(el, onRemove, label, enabled)` —— 绑定 `contextmenu` 事件 |
| `web/ref-context-menu.js:33-34` | 菜单挂载点 `#refContextMenu` |
| `web/ref-context-menu.js:79` | `enabled` 判据：**空对象不弹菜单**（避免"点不动的假移除"） |
| `web/ref-context-menu.js:141-143` | 导出 `window.RefContextMenu = { open, attach, close }` |

### 2.2 三个调用方（各自只提供"移除"语义）

| 位置 | 内容 |
|---|---|
| `web/app.js:1006-1017` | 视频面板槽位挂右键；`enabled` 读 `state.assetRefs[key].asset_id` |
| `web/app.js:844-862` | `removeSlotAsset(key)` —— 调 `RefSlots.planRemoval` → `applyRemoval` → `rewriteTags` |
| `web/qwen-refs.js:523-529` | Qwen/H3-Image 参考图缩略图挂右键，复用已有 `removeAt()`（已做重编号 + 占位清理） |
| `web/music.js:468-474` | 参考生音乐的参考音频卡片挂右键 → `refAudio = null; renderRefAudio()` |

### 2.3 纯逻辑模块（可单测）

| 位置 | 内容 |
|---|---|
| `web/ref-slots.js:105` | `mediaOrder()` —— 按 T8 规则排图片/视频/音频编号 |
| `web/ref-slots.js:173` | `applyRemoval()` —— 改状态 |
| `web/ref-slots.js:206` | `planRemoval()` —— 先算清"谁被移除、谁前移、序号怎么变" |
| `web/ref-slots.js:320` | `rewriteTags()` —— 重写提示词占位标记 |

### 2.4 挂载点

```
web/index.html:1148   <div class="ctx-menu" id="refContextMenu"></div>
web/index.html:1560   <script src="/static/ref-context-menu.js?v=refctx1"></script>
web/index.html:1564   <script src="/static/ref-slots.js?v=refslots1"></script>
```

---

## 三、引入 commit

| commit | 时间 | 作者 | 说明 |
|---|---|---|---|
| `67f2a3d` | 2026-11-20 14:22:29 | Hermes Agent | **功能引入**（`feat(参考素材): 补齐「右键 -> 移除」`），新增 `web/ref-context-menu.js` + `web/ref-slots.js` |
| `6b6dce2` | 2026-11-20 14:23:09 | Hermes Agent | `fix(deploy): 并发守卫改用祖先关系判据` |
| `b9a6424` | 2026-11-20 14:23:20 | cjxz | **上线到 live 的合并提交**（author=cjxz，即主线合并） |
| `bf5ef98` | 2026-11-20 14:29:36 | Hermes Agent | 交付说明文档 |

```
$ git merge-base --is-ancestor 67f2a3d e393e8a  →  YES（功能在 HEAD 历史里）
$ git merge-base --is-ancestor b9a6424 e393e8a  →  YES（上线提交也在 HEAD 历史里）
$ git log --oneline 67f2a3d..e393e8a -- web/ref-context-menu.js web/ref-slots.js  →  （空，此后无人改动过）
```

**引入路径**：`git log -S 'remove-ref' --all` 与 `git log --follow -- web/ref-context-menu.js` 均只命中 `67f2a3d` —— 该功能是**一次性引入**，无后续修补。

---

## 四、线上部署代码核实（不是分支上的）

### 4.1 systemd 跑的就是这个目录

```
$ systemctl cat h3-studio.service
WorkingDirectory=/root/projects/h3studio-live
ExecStart=/home/ubuntu/comfyui/venv/bin/python server.py
Active: active (running) since Fri 2026-11-20 23:42:15 CST

$ cd /root/projects/h3studio-live && git rev-parse HEAD
e393e8affedcc728dbcb3e9623d9dc104c3e4e47      # = local/maintainer
$ git rev-parse --abbrev-ref HEAD
local/deploy/qwen-ref-assets-live
```

静态资源挂载：`server.py:11641` → `app.router.add_static("/static", BASE / "web")`

### 4.2 线上 HTML 确实含菜单容器与脚本标签

```
$ curl -s http://127.0.0.1:8190/ | grep -nE 'refContextMenu|ref-context-menu\.js|ref-slots\.js'
1145:  <div class="ctx-menu" id="mediaContext"></div>
1148:  <div class="ctx-menu" id="refContextMenu"></div>
1560:<script src="/static/ref-context-menu.js?v=refctx1"></script>
1564:<script src="/static/ref-slots.js?v=refslots1"></script>
```

### 4.3 线上静态文件 = 仓库文件（sha256 逐一比对）

```
ref-context-menu.js      MATCH
  live=8d10671b1af9d7a7be9ebceda77c792a05b57d6d0ae58d8ea4264c0d3165e92d
  repo=8d10671b1af9d7a7be9ebceda77c792a05b57d6d0ae58d8ea4264c0d3165e92d
ref-slots.js             MATCH
  live=a3a5927e90b877e58e3c64515a9aded2071707858d4553c8a17fdf229178b98b
  repo=a3a5927e90b877e58e3c64515a9aded2071707858d4553c8a17fdf229178b98b
qwen-refs.js             MATCH
  live=7201eb222af793a40ea3bd5e913745b0f357b36a9bdde72f73320f8eec2f9a66
  repo=7201eb222af793a40ea3bd5e913745b0f357b36a9bdde72f73320f8eec2f9a66
app.js                   MATCH
  live=6134c458c20aecadeadce25bae06cbbabaf7528deedd38069e6411dbb7ecbeea
  repo=6134c458c20aecadeadce25bae06cbbabaf7528deedd38069e6411dbb7ecbeea
music.js                 MATCH
  live=66c5a1594fe401bc488782c4daf5e6466ce5b6c29aef119fd9cbcbad5ebe442e
  repo=66c5a1594fe401bc488782c4daf5e6466ce5b6c29aef119fd9cbcbad5ebe442e
```

线上 `index.html` 的 refctx 相关行与仓库 `web/index.html` **diff 完全一致**。

---

## 五、真实浏览器操作证据

**环境**：Playwright + Chromium，`/home/ubuntu/comfyui/venv/bin/python`，访问真实 `http://127.0.0.1:8190/`，用**真实素材库**素材（`/api/refs/assets`）。全部截图在 `evidence/refctx-20261121/`。

### 5.1 r2v 参考生视频 —— 参考图槽位（核心场景）

**脚本**：`verify_refctx.py` → `browser_result.json`
**截图**：`01-r2v-three-refs-filled.png` → `02-rightclick-menu-open.png` → `03-after-remove.png`

DOM 断言（真实输出）：

```
[step0] module: {"hasRefContextMenu": true, "hasAttach": true, "menuElExists": true,
                  "menuElId": "refContextMenu",
                  "scriptTags": ["/static/ref-context-menu.js?v=refctx1",
                                 "/static/ref-slots.js?v=refslots1"]}

[step2] dom_before: refKeys=["ref0","ref1","ref2"]
                      prompt="A=<Picture 1> B=<Picture 2> C=<Picture 3>"
                      slots: ref0/ref1/ref2 = filled=true

[step3] menu_state: {"exists": true, "open": true, "classes": "ctx-menu open",
                      "rect": {"x":356,"y":488,"w":180,"h":45}, "visible": true,
                      "buttons": [{"text":"移除这个参考素材","act":"remove-ref","cls":"danger"}]}

[step4] dom_after:  refKeys=["ref0","ref1"]          ← 3 条变 2 条
                      refIds=["asset_971a71830dea","asset_af06007eed9a"]
                      prompt="A=<Picture 1> B= C=<Picture 2>"   ← 被移除那条标记已删，其余重编号
                      slots: ref0/ref1=filled=true, ref2=filled=false
                      menuStillOpen=false            ← 菜单已自动关闭
```

**视觉独立核对**（`vision_analyze` 读 `02-rightclick-menu-open.png`）：确认参考素材网格上**弹出了白色右键菜单**，按钮文案为 **「移除这个参考素材」**，带垃圾桶图标，与彩色缩略图背景对比清晰可辨。

### 5.2 提交体不再包含被移除素材（独立验证，不依赖仓库自带测试）

**脚本**：`verify_refctx_submit.py` → `browser_result_submit.json`
**截图**：`12-before-submit-remove.png` → `13-after-submit-captured.png`

真实拦截 `/api/jobs` POST，抓到的 `FormData`：

```json
{
  "removedId": "asset_971a71830dea",
  "keptId": "asset_6bb6ee9324a4",
  "stillInState": ["asset_6bb6ee9324a4"],
  "removedStillInState": false,
  "keptStillInState": true,
  "removedInPayloadText": false,          ← 被移除素材不在提交体里
  "keptInPayloadText": true,
  "payloadKeys": {
    "mode": "r2v",
    "asset_refs": "[{\"asset_id\":\"asset_6bb6ee9324a4\",\"slot\":\"ref_image\",\"ordinal\":0}]"
  }
}
```

`asset_refs` 只剩 1 条 —— **这才是"移除生效"的落点**，不只是界面上少了一张图。

### 5.3 Qwen 图片编辑 —— 参考图缩略图

**脚本**：`verify_refctx2.py` → `browser_result2.json`
**截图**：`04-qwen-ref-rightclick.png` → `05-qwen-after-remove.png`

```
[A] items: {"count": 2, "items": [{"idx":"0","badge":"1"},{"idx":"1","badge":"2"}]}
[A] menu:  {"exists":true,"open":true,"visible":true,
            "buttons":[{"text":"移除这张参考图","act":"remove-ref"}]}
[A] after: {"count": 1,
            "refs": ["asset_6bb6ee9324a4"],        ← 只剩第 2 张
            "prompt": "A= B=<image1>",             ← <image2> 已删、<image1> 保留
            "badges": ["1"],                      ← 序号徽标重排为 1
            "menuStillOpen": false}
```

### 5.4 参考生音乐 —— 参考音频卡片（走**真实素材库选择器 UI**）

**脚本**：`verify_refctx4.py` → `browser_result4.json`
**截图**：`07-music-asset-picker.png` → `09-music-ref-audio-filled.png` → `10-music-ref-rightclick.png` → `11-music-after-remove.png`

```
[picker_open] maskOpen=true, cards=[{id:"asset_029bfc8ec7dd", name:"a1.mp3", meta:"音频 2s"}, ...]
[picked]       pickedCards=1, foot="已选 1 / 1 个  取消  确定使用"
[ref_card]     cardExists=true, cardText="a1.mp3预览移除", cardVisible=true
[menu]         {"exists":true,"open":true,"visible":true,
                "buttons":[{"text":"移除这个参考音频","act":"remove-ref"}]}
[after]        cardExists=false, boxText="尚未选择参考音频",
                note="从素材库选 1 段音频（wav/mp3/flac/m4a/ogg）..."
```

### 5.5 flf 首尾帧 —— 首帧槽位

**脚本**：`verify_refctx_flf.py` → `browser_result_flf.json`
**截图**：`14-flf-two-frames-filled.png` → `15-flf-rightclick-first-frame.png` → `16-flf-after-remove.png`

```
[before] refs=[["first","asset_fa99e11b5f44"],["last","asset_d88cdf616bd6"]]
         prompt="F=<Picture 1> L=<Picture 2>"
         slots: first/last = filled=true
[menu]   {"exists":true,"open":true,"visible":true,
          "buttons":[{"text":"移除这个参考素材","act":"remove-ref"}]}
[after]  refs=[["last","asset_d88cdf616bd6"]]      ← 首帧没了，只剩尾帧
         prompt="F= L=<Picture 1>"                ← <Picture 1> 已删、尾帧前移为 1
         slots: first=filled=false, last=filled=true
```

### 5.6 反向用例：空槽位右键**不**弹菜单（避免"假动作"）

**截图**：`06-empty-slot-no-menu.png`

```
[B] empty menu: {"exists":true,"open":false,"rect":{"x":0,"y":0,"w":0,"h":0},
                 "visible":false}      ← 容器在但未打开、尺寸为 0
```

（`buttons` 里残留上一轮 Qwen 菜单项的 DOM 是因为 `innerHTML` 未清空 —— 但 `open` class 未加、`visible=false`、尺寸 0，用户看不到也点不到。属渲染残留，非功能缺陷。）

### 5.7 控制台

| 脚本 | JS 报错数 |
|---|---|
| `verify_refctx.py`（r2v） | **0** |
| `verify_refctx2.py`（Qwen + 空槽位） | **0** |
| `verify_refctx4.py`（音乐） | **0** |
| `verify_refctx_flf.py`（flf） | **0** |
| `verify_refctx_submit.py`（提交体） | 2 条 `Failed to load resource: 404` —— **是脚本故意返回假 job id `fake-job` 导致前端轮询 `/api/jobs/fake-job` 的资源噪声**，与右键移除功能无关 |

---

## 六、仓库自带回归测试（只读运行，未改代码）

```
$ cd /root/projects/h3studio-live && python -m pytest tests/test_ref_context_remove.py -q
```

**结果不稳定** —— 实测多轮：

| 轮次 | 结果 |
|---|---|
| RUN 1 | `5 failed, 14 passed` |
| RUN 2 | `19 passed` |
| RUN 3 | `19 passed` |
| RUN 4–7 | `19 passed` ×4 |
| RUN 8 | `1 failed, 18 passed` |
| RUN 9 | `19 passed` ×3（`-v` 全绿） |

失败用例集中在 `TestRefContextRemoveBrowser`，报错**全部**是测试脚手架层面的：

```
playwright._impl._errors.TimeoutError: Page.goto: Timeout 30000ms exceeded.
  - navigating to "http://127.0.0.1:37185/", waiting until "networkidle"
playwright._impl._errors.Error: It looks like you are using Playwright Sync API inside the asyncio loop.
```

**根因（已定位到源码）**：`tests/test_ref_context_remove.py:389` 用 `wait_until="networkidle"` 打开页面，但 H3 Studio 每 2 秒轮询一次任务列表（`web/app.js` 的 `setInterval(scheduleRefresh("jobs", refreshJobs), 2000)`），**networkidle 永远达不到** → 30 秒超时。超时后 Playwright 的 asyncio loop 状态被污染，后续用例连带报 "Sync API inside the asyncio loop"。

**判定：这是测试脚手架的竞态，不是产品缺陷。** 依据：
1. 失败是**随机**的（同一份代码、同一台机器，19 passed / 5 failed 交替出现）；
2. 失败点在 `page.goto` 阶段（**还没点到右键菜单**），此时任何 DOM 断言都还没执行；
3. 本报告第五节的 5 个独立脚本（自己写、用 `domcontentloaded` + `wait_for_selector`）**全部 100% 通过，0 JS 报错**；
4. 同一批断言在通过轮次里全部为绿。

> 建议（**未实施**，因为本任务是只读核实）：把 `tests/test_ref_context_remove.py:389` 的 `wait_until="networkidle"` 改成 `wait_until="domcontentloaded"` + `page.wait_for_selector('.nav-item[data-mode="r2v"]')`，即可消除该 flaky。

---

## 七、那条 `open + interrupted` 记录的来源判定

### 7.1 两条记录的身份

数据源：`POST http://127.0.0.1:8648/api/studio/person/tasks/query`（Studio 服务端 person-task 台账）

| 字段 | `9873a137`（interrupted） | `90f3bf94`（completed） |
|---|---|---|
| 标题 | 参考素材右键移除功能 | 参考素材右键移除功能 |
| **summary** | **逐字相同** | **逐字相同** |
| profile | `research` | `coding` |
| agent | 网络调研用agent | 主力编程用agent |
| reasoningEffort | `xhigh` | `max` |
| created_at | **2026-11-20 11:28:22** | **2026-11-20 11:28:46** |
| updated_at | 2026-11-20 11:28:40 | 2026-11-20 14:30:53 |
| completed_at | **null** | 2026-11-20 14:30:53 |
| status | **open** | completed |
| execution_state | **interrupted** | finished |
| execution_id | `94777768-886a-4fb9-8929-9efc8c315a35` | `8dd5b91d-e5ba-455d-88c9-08c133b0f869` |
| completion_evidence | **""（空）** | 有（完整交付记录） |

### 7.2 interrupted 那条实际做了什么（服务端日志原文）

`/root/.hermes-web-ui/logs/server.log` + `bridge.log`：

```
11:28:23 | loaded session person-task-94777768-... from DB (0 messages)
11:28:23 | starting CLI bridge run for session person-task-94777768-...
11:28:26 | recording CLI tool result tool=tool_describe
11:28:26 | recording CLI tool result tool=terminal
11:28:28 | recording CLI tool result tool=mcp__ekko_studio_interaction__ekko_studio_list_session_tasks
11:28:28 | recording CLI tool result tool=terminal
11:28:32 | recording CLI tool result tool=mcp__ekko_studio_interaction__ekko_studio_update_plan
11:28:34 | recording CLI tool result tool=search_files  (×2)
11:28:37 | recording CLI tool result tool=search_files  (×2)
11:28:39 | recording CLI tool result tool=search_files  (×2)
11:28:40 | [chat-run-socket][abort] started   {"runId": "7c3fe24de3d243908ebdc25de0e6a965"}
11:28:41 | [chat-run-socket][abort] completed {"runId": "7c3fe24de3d243908ebdc25de0e6a965", "synced": true}
11:28:47 | loaded session person-task-8dd5b91d-... from DB (0 messages)   ← 另一个任务接手
11:28:47 | starting CLI bridge run for session person-task-8dd5b91d-...
```

会话记录：`end_reason = "abort"`、`message_count = 32`、execution `error = "Task stopped before completion"`。

**即：这条任务跑了 18 秒，只做了 6 次 `search_files` 探查 + 2 次 plan 写入，就在 11:28:40 被显式 abort。** 它从未写过一行代码。

### 7.3 判定结论

**这是「同一条需求被两次派发，第一次被中止、第二次完成」的残留状态，不是功能未完成。**

支撑证据：

1. **两条任务 summary 逐字相同** —— 是同一份需求的两次派发，不是两个不同任务。
2. **interrupted 那条只活了 18 秒**，全程只有 `search_files`（探查）+ `update_plan`（写计划卡），**没有任何 `terminal` 写操作、没有 commit**；`completion_evidence` 为空。
3. **它被显式 abort**：`[chat-run-socket][abort] started` → `completed`，`end_reason: "abort"`。这是人为/调度侧的停止，不是崩溃、不是超时失败、不是 OOM。
4. **功能由 24 秒后创建的 `90f3bf94` 完整交付**：14:22:29 提交 `67f2a3d`（+`6b6dce2`）→ 14:23:20 合并上线 `b9a6424` → 14:29:36 写交付文档 `bf5ef98` → 14:30:53 任务 completed。
5. **这是本会话的普遍模式**，不是孤例。同一批台账里还有两组同名任务，同样是「research profile 先起、18~200 秒后 abort；coding profile 后起、正常完成」：

   | 标题 | interrupted 那条 | completed 那条 |
   |---|---|---|
   | 参考素材右键移除功能 | `9873a137` 11:28:22 → 11:28:40（**18 秒**） | `90f3bf94` 11:28:46 → 14:30:53 |
   | 看门狗接回执行：修 params 通道绕过素材库闸门安全缺陷 | `8ba83163` 06:43:16 → 06:46:49（**3 分 33 秒**） | `5364e75c` 06:43:56 → 10:26:23 |
   | 看门狗接回执行：第 7 轮前后端代码审查 | `835040a2` 11-19 15:47:03 → 15:47:27（**24 秒**） | `297488b9` 16:02:11 → 23:30:23 |

   三组的形态完全一致 —— **research profile 那一路被中止后状态没被关闭，coding profile 那一路接手并完成**。第一组「参考素材右键移除」现在还是 `open`（因为 research 那条没关），另外两组分别是 `closed` 和 `open`。

6. **线上功能实测可用**（第五节）—— 这是最直接的判定依据：功能在线上跑得好好的，那条 `open` 记录没有对应的未完成工作。

### 7.4 残留状态的性质与影响

- **性质**：台账状态残留（stale row），不是代码/部署缺陷。
- **影响**：仅 UI 上多一条 `open` 状态的任务卡（`SessionTasksPanel` 里显示为「已中断」）。**对 8190 线上功能零影响。**
- **为什么没被自动关闭**：`interrupted` 的 execution 被 abort 时，`person/tasks/apply` 只把 execution 标成 `interrupted`；把 task 本身置为 `closed`/`completed` 需要另行调用，而当时没有（或调用失败）。这也解释了那批台账里 `open`+`interrupted`、`closed`+`interrupted`、`open`+`failed` 三种组合并存 —— 关闭动作本就不统一。

> **本任务未做任何状态修改**（只读取证纪律）。如需收口，应由宿主对 `9873a137-9d9a-44be-8583-3628f5a69c10` 调 `POST /api/studio/person/tasks/apply` 置为 `closed`（理由：同需求已由 `90f3bf94` 完成交付并上线）。

---

## 八、遗留与建议（均未实施）

| 项 | 说明 |
|---|---|
| 测试 flaky（**建议修**） | `tests/test_ref_context_remove.py:389` 的 `wait_until="networkidle"` 在 2 秒轮询的应用上必然超时。改成 `domcontentloaded` + `wait_for_selector` 即可。同类隐患可能存在于其它用 `networkidle` 的浏览器测试。 |
| 菜单按钮 DOM 残留 | 空槽位右键后，`#refContextMenu` 的 `innerHTML` 仍留着上一轮按钮（但 `open` class 未加、尺寸 0、不可见不可点）。不影响交互，可顺手在 `closeMenu()` 里清 `innerHTML`。 |
| 状态残留 | `9873a137` 应关闭；同批台账里还有 `835040a2`（open+interrupted）等同类。 |

---

## 九、证据文件清单

目录：`/root/.hermes-web-ui/.ekko/workspace/default/person-main-505e35f9-2826-4d4c-af53-d3dc5de6f957/evidence/refctx-20261121/`

### 截图（15 张）

| 文件 | 内容 |
|---|---|
| `01-r2v-three-refs-filled.png` | r2v 三个参考图槽位已填（before） |
| `02-rightclick-menu-open.png` | **右键菜单弹出，按钮「移除这个参考素材」** |
| `03-after-remove.png` | 移除后：3 条 → 2 条，菜单已关 |
| `04-qwen-ref-rightclick.png` | **Qwen 参考图右键，菜单「移除这张参考图」** |
| `05-qwen-after-remove.png` | 移除后：count 2→1，徽标重排为 1 |
| `06-empty-slot-no-menu.png` | 空槽位右键**不**弹菜单（反向用例） |
| `07-music-asset-picker.png` | 真实素材库选择器（音频类，2 个素材） |
| `09-music-ref-audio-filled.png` | 参考音频卡片已填（a1.mp3） |
| `10-music-ref-rightclick.png` | **参考音频右键，菜单「移除这个参考音频」** |
| `11-music-after-remove.png` | 移除后回到「尚未选择参考音频」 |
| `12-before-submit-remove.png` | 提交体验证：移除前 |
| `13-after-submit-captured.png` | 提交体验证：抓到的 FormData（asset_refs 只剩 1 条） |
| `14-flf-two-frames-filled.png` | flf 首帧+尾帧已填 |
| `15-flf-rightclick-first-frame.png` | **首帧槽位右键，菜单「移除这个参考素材」** |
| `16-flf-after-remove.png` | 移除后：首帧空、尾帧前移、提示词同步 |

### DOM 断言输出

| 文件 | 内容 |
|---|---|
| `browser_result.json` | r2v 参考图槽位：模块加载 / 槽位 / 菜单 / 移除后 DOM |
| `browser_result2.json` | Qwen 参考图 + 空槽位反向用例 |
| `browser_result4.json` | 参考生音乐参考音频（走真实选择器 UI） |
| `browser_result_flf.json` | flf 首尾帧 |
| `browser_result_submit.json` | **提交体不含被移除素材的断言** |

### 可复现脚本（只读，不改代码）

| 文件 | 作用 |
|---|---|
| `verify_refctx.py` | r2v 参考图槽位全流程 |
| `verify_refctx2.py` | Qwen 参考图 + 空槽位反向用例 |
| `verify_refctx3.py` | 音乐面板结构探查（中间探查脚本） |
| `verify_refctx4.py` | 参考生音乐参考音频（真实选择器） |
| `verify_refctx_flf.py` | flf 首尾帧 |
| `verify_refctx_submit.py` | 提交体断言 |

**复现命令**：

```bash
cd /root/.hermes-web-ui/.ekko/workspace/default/person-main-505e35f9-2826-4d4c-af53-d3dc5de6f957/evidence/refctx-20261121
for s in verify_refctx.py verify_refctx2.py verify_refctx4.py verify_refctx_flf.py verify_refctx_submit.py; do
  /home/ubuntu/comfyui/venv/bin/python "$s"
done
```

---

## 十、核实纪律自检

| 纪律 | 执行情况 |
|---|---|
| 只读，不改代码 | ✅ 未改任何仓库文件。`git status --short web/app.js web/index.html web/favorites.js web/ref-context-menu.js web/ref-slots.js web/qwen-refs.js web/music.js` **全部为空** |
| 不提交 | ✅ 未执行任何 `git commit` / `git add` |
| 不重启 8190 | ✅ 未执行 `systemctl restart`。服务进程 PID 504745 自 2026-11-20 23:42:14 起持续运行未变 |
| 不动他人 worktree | ✅ 只读访问 `/root/projects/h3studio-live`；未进入或修改 `h3studio-t2v-continue` 等其它 worktree |
| 不新建额度探针 / 不跑 GPU 作业 | ✅ 未创建任何 quota 探针任务；未提交任何生成作业（提交体验证是**拦截** `/api/jobs` POST 并返回假响应，**没有真任务产生**） |
| 结论必须带真实输出 | ✅ 第五/六/七节全部引用真实命令输出、JSON 断言与截图路径 |
