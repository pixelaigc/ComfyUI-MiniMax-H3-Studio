# 第 3 轮收口审查发现（2026-11-16）

**分支**：`local/feature/code-review-20261115` · commit `c6676e6`
**基线**：`9843b47`（live HEAD，进入本轮时的线上版本）
**线上部署**：live HEAD = `c6676e6`，systemd `h3-studio.service`，端口 8190

本轮解决任务书里的 3 项未收口事项 + 1 项同族缺陷：
A-6（assets.js 注释漏修）、D-2（静态资源版本号过期）、B-2（跨模式槽位串台），
以及把「槽位状态跨模式残留」从「未证实」推进到「已复现 → 已修复」。

---

## A-6 `web/assets.js` 两处开发 HTML 注释进 DOM · **High** · 已修

| 项 | 内容 |
|---|---|
| 位置 | `web/assets.js:387-392`（`assetCardHtml()` 模板）、`web/assets.js:1016-1021`（`showAssetContext()` 右键菜单模板） |
| 问题 | 模板字面量里嵌了 `<!-- 【2026-11-15 A3 补漏】多选勾选框… -->` 与 `<!-- 【A2】添加到播放器清单… -->` 两段 HTML 注释 |
| 为何会显示给用户 | `assetCardHtml()` 的返回值经 `fresh.innerHTML = assetCardHtml(asset)`（`assets.js:462`/`:469`）注入；`showAssetContext()` 的模板经 `menu.innerHTML = ...`（`assets.js:1016`）注入。HTML 注释不渲染但**进 DOM 注释节点** —— 用户「查看源代码」「复制页面内容」都能带走，内容是内部日期标记 + 需求编号（A2 / A3 补漏）等纯开发语境 |
| 修复方式 | 把两段注释移到模板字符串**外面**的 JS 注释（`//`），DOM 结构与用户可见文案一字未动 |
| 修复 commit | `c6676e6` |
| 实测证据 | 修复后媒体库页 DOM 注释节点数 **109**，与 `index.html` 静态注释数 **109** 完全相等 ⇒ JS 侧注入的注释归零；右键菜单触发后仍为 109（未新增） |

**上一轮为什么漏了**：第 1 轮的断言只扫 `app.js` 的 jobCard 一个模板
（`tests/test_visible_text_pollution.py::test_task_card_has_no_html_comment_in_template`），
没覆盖 `web/` 下其它 `.js`。

---

## D-2 14 个静态资源 `?v=` 版本号过期 · **High** · 已修

| 项 | 内容 |
|---|---|
| 位置 | `web/index.html`（仅版本参数，**未改任何资源内容**） |
| 问题 | 14 个 `/static/*` 引用，其 `?v=` 值在设置之后该资源文件内容又变过 |
| 为何是缺陷 | `server.py:8862` 的 `app.router.add_static("/static", BASE / "web")` 走 aiohttp 默认静态处理，**没有 `Cache-Control`**（实测只有 `Etag` + `Last-Modified`）。浏览器于是套用「启发式新鲜度」＝ 10% × 资源年龄 —— 实测 `assets.js` 距今 26.5 小时 ⇒ **2.7 小时内根本不回源**。而 `index.html` 自己是 `Cache-Control: no-cache, must-revalidate`（`server.py` 里单独设的），每次都拿新的 `?v=` 清单 —— 但清单里的版本号没跟着内容升，老用户拿到的仍是旧文件 |
| 修复方式 | 14 个资源的 `?v=` 统一升到 `cr20261115b` |
| 修复 commit | `c6676e6` |
| 实测证据 | 部署后浏览器加载的 script/link 标签中 **14 个** 带 `?v=cr20261115b` |

### 过期清单（14 项）

判据：该资源文件内容相对其 `?v=` **引入点**是否发生过变化。
用「逐 commit 走历史 + 找 token 引入点 + 内容 diff」确定（详见 `scripts/` 与本文件末「口径说明」）。

| 资源 | 旧 `?v=` | 新 `?v=` | 引入点 | 引入点后的内容增量 |
|---|---|---|---|---|
| `assets.css` | uxbatch1 | cr20261115b | `90d4cb0` | +159/-0 |
| `responsive.css` | agentpower1 | cr20261115b | `367a169` | +29/-1 |
| `qwen-refs.js` | fix3preview1 | cr20261115b | `90a8914` | +14/-2 |
| `video-panel-fallback.js` | vpfb1 | cr20261115b | `41226db` | +644/-79 |
| `continuation.js` | continuation1 | cr20261115b | `1ef90f0` | +91/-7 |
| `job-meta.js` | jobmeta3 | cr20261115b | `ad9841f` | +9/-3 |
| `providers.js` | h3image1 | cr20261115b | `84da697` | +6/-18 |
| `assets.js` | uxbatch1 | cr20261115b | `2725e3f` | +305/-2 |
| `asset-player.js` | uxbatch1 | cr20261115b | `ab95dfd` | +412/-117 |
| `enhance-menu-shared.js` | shared2 | cr20261115b | `055db97` | +15/-3 |
| `enhance-menu.js` | uxbatch1 | cr20261115b | `f99588f` | +14/-22 |
| `music-enhance.js` | uxbatch1 | cr20261115b | `90d4cb0` | +16/-1 |
| `agent.js` | agent4 | cr20261115b | `284bd64` | +34/-3 |
| `agent_sessions.js` | agtsess2 | cr20261115b | `407f6e0` | +511/-29 |

### 明确**不算**过期（任务书特别点名，本轮实测确认）

- `app.js?v=ac1steps1` 与 `video-panel.js?v=ac1steps1` —— `cd7f175`（升版号）本身就是 `50fc0e2`（C-2）与 `d6a51fb`（AC-1）**之后**的 commit，`git diff cd7f175 HEAD -- web/app.js` 为空 ⇒ 内容未变，不需再升。
- `style.css?v=cr20261115a`、`favicon.svg?v=cr20261115a` —— 第 1 轮 D-1 已修，内容此后未变。
- `media-manage.js?v=uxbatch1` —— 引入点 `b7c8bdc` 之后内容未变。

### 口径说明（为什么任务书说 13、实测是 14）

任务书列了 13 项。本轮用**逐 commit 走历史**（不是 `git log -S`）独立复核，得到 **14**：
多出来的是 `responsive.css`。

原因：`git log -S 'responsive.css?v=agentpower1'` **返回空** ——
该 `?v=` 值在 `index.html` 里从 `83a2260` 起就存在，后续 commit 只是整行搬移，
pickaxe 判不出「引入点」。改用「逐个 commit 检查 token 是否首次出现（父提交没有、本提交有）」
的口径后，引入点定位到 `367a169`，此后 `63473af` 又改了 `web/responsive.css`（+12 行媒体查询）⇒ 过期。

**同时验证了 `git log -S` 口径的误报**：用 `-S '?v=uxbatch1'` 会把
`assets.css`/`assets.js`/`asset-player.js`/`enhance-menu.js`/`media-manage.js`/
`music-enhance.js` 的引入点都误判到 `293e593`（那是第 1 轮升 `style.css` 版号时
顺带碰到的），从而漏掉真正的过期判定。**结论：以「内容 diff」为准，`-S` 只作辅助。**

---

## B-2 跨模式切换后提交值与面板显示值不一致 · **Critical** · 已修（本轮新复现）

| 项 | 内容 |
|---|---|
| 位置 | `web/app.js:3269`（`Object.entries(state.assetRefs \|\| {}).forEach(...)` 提交处） |
| 问题 | 提交处**无模式判断地全量提交** `state.assetRefs`。而 `state.assetRefs` 是全局的，切模式时从不清空（`renderMode` 只重建 DOM，不重置 state） |
| 为何是缺陷 | `SLOT_FOR_KEY`（`app.js:3259`）把 `first`/`last` 无条件映射成 `first_frame`/`last_frame` 槽位，与当前模式无关 |
| 修复方式 | 提交处按当前模式白名单过滤槽位 key，口径与 `renderMode` 里真正 `mk()` 出来的槽位严格一致（`app.js:747-768`） |
| 修复 commit | `c6676e6` |

### 真机复现（8190 现场，修复前）

步骤序列：

1. 切到「首尾帧生视频」
2. 在首帧/尾帧槽位各选一个素材
3. 切到「参考生视频」
4. 点「生成」

r2v 界面实测：`uiSlots = ["参考 1".."参考 9", "参考视频 1..3", "参考音乐 1..3"]`，
`hasFirstSlotInUI = false`（**界面上根本没有首帧槽位**）。

抓到的真实提交体（拦截 `fetch` + `FormData`）：

```
mode       = r2v
asset_refs = [{"asset_id":"FAKE-1","slot":"first_frame","ordinal":0},
              {"asset_id":"FAKE-2","slot":"last_frame","ordinal":0}]
```

后果链（实测）：

1. 前端守卫（`app.js:3355`）按「有素材引用」判定为「已提供参考素材」→ **放行**；
2. 服务端（`server.py:3715`）按 slot 判定（只认 `ref_image`/`ref_video`/`ref_audio`）→ **拒绝**，
   报「参考模式需要至少 1 个参考素材（图片 / 视频 / 音频）」。

用户看到的是：界面一片空白（自己没选任何参考素材），点生成却被告知「缺少参考素材」——
**界面与报错互相矛盾，且无从修正**（界面上根本没有能选参考素材的地方被指出来）。

### 修复后验证（8190 现场）

| 场景 | 修复前提交体 | 修复后提交体 |
|---|---|---|
| flf 选首尾帧 → 切 r2v → 提交 | `asset_refs` 含 `first_frame` + `last_frame` | **空**（前端守卫直接拦下，提示「参考模式至少需要 1 个参考素材（参考图 / 参考视频 / 参考音乐）」—— 与空界面一致） |
| flf 选首尾帧 → 提交（自己模式） | `asset_refs` 含 `first_frame` + `last_frame` | **仍含**（功能未被破坏） |

修复前后的差异点正是那两条幽灵 `first_frame`/`last_frame`。

---

## 本轮新增回归测试

| 文件 | 用例数 | 覆盖 |
|---|---|---|
| `tests/test_frontend_assets_hygiene.py` | 12 | A-6：web/ 下**全部** .js 不得含 `<!--`；A-6：注释必须移到模板外而非被删；D-2：14 项表驱动 + 「不得等于旧值」；D-2：不过期资源不得被顺手改；D-2：每个 `/static/` 引用都得有 `?v=`；D-2：引用不得指向不存在的文件 |
| `tests/test_mode_switch_slot_isolation.py` | 8 | B-2：**用 node 真跑 app.js 里的真实实现**（不是断言源码里有没有某行字）；覆盖 flf↔r2v 双向、共享槽位、t2v 零素材、以及「提交 slot 集合 == 界面槽位集合」的护栏断言 |

### 变异自证结果

| 套件 | 脚本 | 结果 |
|---|---|---|
| A-6 / D-2 | `scripts/mutation_assets_hygiene.py` | **11 个变异，捕获 11，漏网 0** |
| B-2 | `scripts/mutation_mode_switch_slots.py` | **8 个变异，捕获 8，漏网 0** |

原始输出：`docs/audit-evidence/code-review-20261115/mutation_assets_hygiene.txt`、
`docs/audit-evidence/code-review-20261115/mutation_mode_switch_slots.txt`

### 变异测试过程中发现并修掉的两个「测试自身有洞」

1. **CRLF 导致变异被 SKIP（假通过）**：`web/app.js` / `web/index.html` 是 CRLF 文件，
   变异脚本的 needle 按 LF 写 ⇒ 永远匹配不上 ⇒ 变异被跳过 ⇒ 测试「假通过」。
   已在两个脚本里加 CRLF 归一（`mutation_assets_hygiene.py` / `mutation_mode_switch_slots.py`）。
2. **抽取器把源码写死 ⇒ 变异跑不到被测代码**：`test_mode_switch_slot_isolation.py` 的
   `extract_asset_refs_builder()` 原本把 `const slotKeyAllowed = ...` 这一行**硬编码在
   Python 里**。改源码里的这一行（`|| (() => false)` → `|| (() => true)`）测试根本跑不到
   被改的代码 ⇒ B2-e/B2-f 两个变异漏网。改为**从源码抽取该行**后，两个变异立即被捕获。

---

## 部署与验证（全部现场实测）

### 合并与部署

- 我的分支 `c6676e6` 是 live HEAD `9843b47` 的直接后代 ⇒ live worktree 内
  `git merge --ff-only c6676e6` **零冲突快进**，tracked 文件改动 0 个。
- `systemctl restart h3-studio.service`：PID `629827` → `1161047`，状态 `active`。

### 探活（部署后）

- 首页 `/` → **200**
- 静态资源全量 33 个（`web/*.js` + `web/*.css` + `web/*.svg`）→ **非 200 = 0**
- 真实 API 11 个（`/api/jobs`、`/api/image-loras`、`/api/comfy-models`、
  `/api/generation-types`、`/api/image-options`、`/api/hardware`、`/api/agent/sessions`、
  `/api/agent/tools`、`/api/agent/settings`、`/api/enhancer`、`/api/continuation/state`）
  → **全部 200**

### 浏览器验证（真实浏览器，8190）

- 12 个模式面板逐一访问（t2v / flf / r2v / longfilm / t2i / t2i_qwen / i2i_qwen /
  t2i_moss / t2i_h3image / i2i_h3image / t2a / a2a）→ **console 报错 0**
- 可见文本 + `title`/`aria-label`/`placeholder`/`alt` 全量扫描
  → 开发者标记（A2 / A3 / 补漏 / 骨架占位 / TODO / FIXME / XXX / HACK / 待实现 / 暂不支持）**0 命中**
- DOM 注释节点 109 个 == `index.html` 静态注释 109 个 ⇒ JS 注入归零
- 截图：`docs/audit-evidence/code-review-20261115/round3/`

### 针对性回归（不跑全仓）

| 范围 | 结果 |
|---|---|
| 本轮受影响 8 个测试文件（候选分支） | `7 failed, 172 passed, 1 skipped, 4 subtests passed` |
| 同 6 个既有测试文件（基线 `9843b47` 干净 worktree） | `7 failed, 152 passed, 1 skipped, 4 subtests passed` |

失败清单**逐条完全相同**（`test_video_sampling_params.py` 6 条 + `test_panel_fallback.py` 1 条，
均为第 1 轮已记录的既有失败）⇒ **新增失败 0 条**。

### 并发安全自证

操作 `local/maintainer` 所在 worktree 前后各记一次 index 指纹：

| 时点 | `local/maintainer` | index sha256（前 24 位） | 工作区状态 |
|---|---|---|---|
| 操作前 | `c3f0771b9f60300f4e04d308a285761e95fbbfcb` | `8086c68beaeecd2c06fec4f9` | `D=201, M=55, ??=3` |
| 操作后 | `c3f0771b9f60300f4e04d308a285761e95fbbfcb` | `8086c68beaeecd2c06fec4f9` | `D=201, M=55, ??=3` |

**完全一致** —— 未触碰该分支、未推进其 ref、未影响其 index。

---

## 清理

本轮真机复现时向 8190 提交了 1 个测试任务（`387bdcf9134d`，flf，failed，
错误含 `REAL-1`）。复现完成后已从生产库
`/root/projects/comfyui-minimax-h3-studio/data/jobs.db` 删除该行及其批次记录，
线上 `/api/jobs` 已不再显示。

---

## 本轮未做 / 未覆盖（如实说明）

1. **C-2 未动**（按任务书要求）。`50fc0e2` 已在**前端**修（t2v 分支补显式 `accel_lora` 键），
   后端 `h3studio/video_models.py` 的判据**刻意不动**（「无加速信息 → 默认 ckpt850」
   是老任务重放契约）。本轮未触碰该文件。
2. **`renderMode()` 的槽位生命周期未重构**（按任务书要求）。本轮只在**提交处**加了
   模式白名单，**没有**改「切模式时清空 `state.files`/`state.assetRefs`」。
   理由：`state.files` 的清空会影响「连续提交多个任务」的既有设计
   （`app.js:3380-3389` 的注释明确说参考帧槽位要保留以便连做）；
   提交处过滤已能杜绝「界面与提交不一致」这个用户可见症状，风险面小得多。
   `state.files` 里的残留本地文件在 r2v 模式下会被 `refs`/`refVids`/`refAuds` 三个
   分组循环按 key 精确取用（`app.js:3338-3350`），**不会**串台 —— 只有 `assetRefs`
   是「全量遍历」因而串台。**这一条是静态取证，本轮未做真机验证。**
3. **`web/index.html` 里的 109 段静态 HTML 注释未清理**。
   理由：它们是**源码里的注释**（用户看不到渲染效果），且 `index.html` 是 CRLF 文件、
   全文 1590 行，批量改注释有整文件伪 diff 风险；而它们**不含**用户可见文案污染
   （DOM 注释节点确实存在，但内容是维护者上下文）。
   **如需彻底清理，建议单独一轮做**（见下方建议）。
4. **未跑全仓回归**（按任务书要求，额度仅 23%）。本轮只跑了受影响文件的针对性回归 +
   与基线的失败清单逐条比对。

---

## 建议（下一轮可选）

- **清 `web/index.html` 的 109 段静态注释**：这是 D-2 之外的另一类「DOM 注释进用户视图」。
  建议用脚本按「段落级注释块」批量转成 JS 注释或直接删除，并在 `.gitattributes`
  里固定 CRLF 以避免伪 diff。
- **服务端 `/static/*` 加 `Cache-Control: no-cache`**：比逐个升 `?v=` 更根本。
  现在每次都要人工维护 33 个资源的版本号，漏一个就是一次「老用户拿旧文件」。
  本轮已加了一条断言锁住「服务端不得发 `immutable`」，但没锁「必须发 `no-cache`」。
