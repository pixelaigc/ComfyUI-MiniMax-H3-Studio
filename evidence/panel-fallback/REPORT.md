# 文生视频面板「下拉全部空白」修复报告

- **日期**：2026-11-11
- **分支**：`local/feature/panel-fallback`
- **worktree**：`/root/projects/h3studio-panel-fallback`
- **基线**：`local/deploy/qwen-ref-assets-live` @ `0d0d5d1`（**与线上 8190 同一提交**）
- **线上服务**：`h3-studio.service` @ 8190 —— **未重启、未部署**

---

## 一、结论速览

用户报的「文生视频面板前端崩了、下拉全部空白」**不是本任务上一轮改动引入的**
（上一轮的 `08e90eb` **没有上线**，线上不含它）。这是**既有代码的缺陷**，
2026-11-11 21:06 由用户在真实环境踩中。

根因是**三件事叠加**：

| # | 缺陷 | 后果 |
|---|---|---|
| 1 | `video-panel.js` 的 `loadVideoOptions()` 在 `/api/video-options` 失败时直接进 `catch`，**`renderVideoPanel()` 从未执行** | 尺寸预设 / 输出尺寸 / 模型 / 加速 LoRA / 注意力机制**全部空下拉**（控件在、选项没有） |
| 2 | `.img-warn` 自带橙色背景 + padding + 边框，**空内容也渲染成一条橙条** | 模型框下面挂着一条**空的**橙色告警条 |
| 3 | 服务端 `refresh_comfy_node_names()` **串行**问 6 个节点、每个 10 秒超时 | ComfyUI 卡住/正在启动时，`/api/video-options` 要 **66 秒**才返回 → 浏览器先放弃 |

**触发时机**：8190 在 **21:00:22** 重启过，ComfyUI 随后反复启动（21:01/21:05/21:06
日志连续 `object_info timed out`）。用户截图上传于 **21:06:18** ——
正好撞上 ComfyUI 还在启动、接口要 66 秒的窗口。

修复后：接口 **3.2 秒**返回，且**失败也渲染**（内置兜底清单 + 明确错误提示）。

---

## 二、根因（实测复现，非推测）

### 2.1 复现路径

用**线上同一提交**的代码起独立实例，把 ComfyUI 指向一个「接受连接但永不响应」
的假服务（模拟 ComfyUI 启动中），实测：

```bash
$ time curl -s -o /dev/null -w "http=%{http_code}\n" --max-time 200 \
    http://127.0.0.1:8194/api/video-options
http=200
real    0m1m6.106s          # ← 66 秒
```

浏览器实测（真实 Playwright，非模拟）：

```
vidSizePresetSelect : 0 options   ← 空
vidModelSelect      : 0 options   ← 空
vidAccelLoraSelect  : 0 options   ← 空
vidAttentionSelect  : 0 options   ← 空
warnVisible         : true        ← 空的橙条
ready               : false
```

截图与用户报的那张**逐像素一致**（含右上角「ComfyUI 未连接」状态）。

### 2.2 三个缺陷的具体位置

| 缺陷 | 文件:行 | 说明 |
|---|---|---|
| ① 失败即中断 | `web/video-panel.js` `loadVideoOptions()` 的 `catch` | catch 里只 `console.error` + 显示 banner，**没有** `renderVideoPanel()` |
| ② 空橙条 | `web/style.css` `.img-warn` | 自带 `background:#fff8ec; border:1px solid #f3e2c4; padding:5px 8px` —— 空内容也占位 |
| ③ 探测串行 | `server.py` `refresh_comfy_node_names()` | `for name in _VIDEO_PANEL_NODES: await s.get(...)`，每个 `ClientTimeout(total=10)` |

实测串行代价：2 个节点 21.4 秒 → 6 个节点 **≈ 64 秒**。

### 2.3 为什么「模型列表 API 失败不得阻断其它下拉」在原代码里做不到

原代码把**所有**下拉的渲染都放在 `renderVideoPanel()` 里，且它**只**在 try 块末尾
被调用一次。任何一个前置失败 → 整块面板一个下拉都不渲染。
所以「模型接口失败」在原结构下必然导致「其它下拉也空白」。

---

## 三、线上 8190 是否被污染

### 3.1 结论：**没有被污染**（但线上有该缺陷，且随时可能再次触发）

| 检查项 | 结果 |
|---|---|
| 线上是否含本任务上一轮的 `08e90eb` | **否**（`git merge-base --is-ancestor` = false） |
| 线上 HEAD | `0d0d5d1`（= `local/deploy/qwen-ref-assets-live`） |
| 线上工作区 | 干净（`git status --porcelain` 空） |
| 服务返回的静态文件 vs 磁盘 | **sha256 逐个一致**（`video-panel.js` / `app.js` / `index.html`） |
| 线上 `video-panel.js` / `app.js` 语法 | 全部通过 `node --check` |
| 线上 `/api/video-options` | 200，**连续 10 次全部 200、~120ms** |
| 线上 ComfyUI | 正常（`/system_stats` 200） |

**为什么 21:06 会坏、现在又是好的**：因为故障是**瞬态**的
（ComfyUI 正在启动 → 接口 66 秒 → 浏览器放弃）。
ComfyUI 就绪后接口恢复到 120ms，面板也恢复正常。
**缺陷仍在代码里，下次 ComfyUI 重启/卡住时会原样复发。**

### 3.2 引入时间线

```
14:26  web/video-panel.js 最后一次改动（提交 321caf8「文生视频面板 4 项缺陷」）
       —— 该文件自 7f72606 引入起就带着「catch 后不渲染」的结构
21:00:22  h3-studio.service 重启
21:01 / 21:05 / 21:06  ComfyUI object_info 连续 timed out
21:06:18  用户上传故障截图
22:00 / 22:05 / 22:10  ComfyUI 又被重启 3 次（他人操作）
```

> 结论：缺陷是 `7f72606`（视频面板初版）引入的**长期存在**的结构问题，
> `321caf8` 只是让它更容易被触发（多了尺寸预设/注意力等更多下拉）。
> **与本任务上一轮改动无关**。

### 3.3 修复需要回灌哪些提交

| 提交 | 内容 | 能否独立回灌 |
|---|---|---|
| 本分支 `local/feature/panel-fallback` | ① 前端失败也渲染 + 兜底清单 ② 空橙条隐藏 ③ 服务端探测并发/短超时 ④ 24 项测试 | ✅ 基于线上同一提交 `0d0d5d1`，可直接合入 |
| `08e90eb`（上一轮，模型清单改用 ComfyUI API） | 模型清单以 ComfyUI API 为准 + `/api/comfy-models` 端点 + 前端数据驱动 | ⚠️ 基于更早的 `7e98645`，与 `0d0d5d1` 有重叠文件（`server.py` / `web/index.html` / `h3studio/video_models.py`），**需 rebase 后再合**，见 §六 遗留问题 |

---

## 四、改动清单

| 文件 | 改动 |
|---|---|
| `server.py` | `refresh_comfy_node_names()`：**并发**探测（`asyncio.gather`）+ 超时 10s→**3s** + 探测失败**保留上一次成功的缓存**。实测 66s → **3.2s** |
| `web/video-panel.js` | ① `catch` 分支也调用 `renderVideoPanel()`（置 `optionsReady=true`）② 新增 `applyFallbackManifest()` + `renderFallbackNotice()`：失败时用**内置兜底清单**渲染，并显示**明确错误提示** ③ `renderVideoPanel()` 逐区块独立 `try/catch` —— **一个区块失败不阻断其它** ④ `renderModelNote()` 空 `.img-warn` 设 `hidden` |
| `web/video-panel-fallback.js` | **新增**。静态兜底清单（与服务端 `h3studio/video_models.py` 同源实测数据生成）。⚠️ 形状必须与服务端**完全一致**（`size_presets` 是扁平 list） |
| `web/app.js` | 图片面板 `#imgModelWarn` 同样处理空内容隐藏（同一类缺陷） |
| `web/index.html` | 引入 `video-panel-fallback.js`（在 `video-panel.js` 之前） |
| `tests/test_panel_fallback.py` | **新增** 24 项：前端契约、服务端探测（源码 + **行为级耗时断言**）、Node+DOM shim **真跑** `video-panel.js` 验证失败场景 |
| `tests/verify_panel_fallback_live.py` | **新增**。真机浏览器验收：3 视口 × 3 场景 |
| `evidence/panel-fallback/` | **新增**。截图 + 接口耗时原始输出 + 验收报告 |

---

## 五、验证证据

### 5.1 接口耗时（ComfyUI 挂住时，同机对比）

```
=== ComfyUI 挂住时 GET /api/video-options 耗时 ===
修复前 (8194, 串行探测 6×10s):
  http=200 time=65.855890s
修复后 (8195, 并发探测 3s):
  http=200 time=3.198136s
```

### 5.2 真机浏览器验收（3 视口 × 3 场景 = 9 组）

`evidence/panel-fallback/report.json` + 截图：

| 场景 | 视口 | 尺寸预设/模型/LoRA/注意力 选项数 | chips | 宽×高 | page error | console error | 空橙条 | banner | 溢出 |
|---|---|---|---|---|---|---|---|---|---|
| **正常** | 375 | 10 / 5 / 7 / 6 | 8 | 864×480 | 0 | 0 | 无 | 不显示 | 0px |
| 正常 | 390 | 10 / 5 / 7 / 6 | 8 | 864×480 | 0 | 0 | 无 | 不显示 | 0px |
| 正常 | 1280 | 10 / 5 / 7 / 6 | 8 | 864×480 | 0 | 0 | 无 | 不显示 | 0px |
| **后端不可达** | 375 | 10 / 5 / 7 / 6 | 8 | 864×480 | 0 | 2 | 无 | **显示** | 0px |
| 后端不可达 | 390 | 10 / 5 / 7 / 6 | 8 | 864×480 | 0 | 2 | 无 | **显示** | 0px |
| 后端不可达 | 1280 | 10 / 5 / 7 / 6 | 8 | 864×480 | 0 | 2 | 无 | **显示** | 0px |
| **ComfyUI 卡住** | 375 | 10 / 5 / 7 / 6 | 8 | 864×480 | 0 | 2 | 无 | **显示** | 0px |
| ComfyUI 卡住 | 390 | 10 / 5 / 7 / 6 | 8 | 864×480 | 0 | 2 | 无 | **显示** | 0px |
| ComfyUI 卡住 | 1280 | 10 / 5 / 7 / 6 | 8 | 864×480 | 0 | 2 | 无 | **显示** | 0px |

- **console error 的 2 条**是浏览器对「资源加载失败」的资源级日志
  （`ERR_CONNECTION_REFUSED` + 应用的 `[视频面板] 载入清单失败`）——
  属于**预期的失败记录**，不是未捕获异常（page error 全为 0）。
- 兜底态 banner 文案：
  > 视频面板清单载入失败：**Failed to fetch**。已切换到**内置兜底清单**（下拉仍可选择），但服务端无法校验本机权重 —— 提交可能失败。请确认后端与 ComfyUI 已就绪（`systemctl status h3-studio comfyui`）。

### 5.3 视觉确认（375px · 后端不可达）

![375px 后端不可达 — 下拉仍有选项](/root/projects/h3studio-panel-fallback/evidence/panel-fallback/backend_down-375.png)

修复前（ComfyUI 卡住，与用户截图一致）vs 修复后：
- 修复前：全部空下拉 + 空橙条
- 修复后：尺寸预设 `480p · 864×480`、宽高 `864`/`480`、模型
  `MiniMax-H3 FL2VA · 剪枝 int8（默认）`、加速 LoRA、注意力机制全部有值，
  橙色条消失，顶部有明确错误 banner。

### 5.4 测试

```
$ /home/ubuntu/comfyui/venv/bin/python -m pytest tests/test_panel_fallback.py -q
24 passed
```

**变异验证**（改坏实现，测试必须变红）：

| 变异 | 抓到 |
|---|---|
| A. 删掉 catch 里的 `renderVideoPanel()` | 3 项 |
| B. 服务端探测改回串行 | 1 项（**行为级耗时断言**：实测 18.0s > 10s 阈值） |
| C. 去掉 `.img-warn` 的 `hidden` | 2 项 |
| D. 兜底清单 `size_presets` 形状写错（dict 而非 list） | 由 `test_fallback_size_presets_match_server` + DOM 测试的「not iterable」检查抓住 |

> 变异 B 最初**没被抓到** —— 源码里只要别处还有一句 `asyncio.gather`，
> 「有没有 gather」的字符串检查就会通过。已补**行为级耗时断言**
> （真起一个「接受连接但不响应」的假 ComfyUI，量 `refresh_comfy_node_names` 的耗时）。

---

## 六、遗留问题（明确列出）

1. **8190 线上未部署** —— 按要求未重启、未改动。线上**此刻是好的**
   （ComfyUI 就绪、接口 120ms），但**缺陷仍在代码里**，
   下次 ComfyUI 重启/卡住时会原样复发。需用户拍板后合并 + 重启。
2. **`08e90eb`（上一轮模型清单改动）尚未上线**，且它基于更早的 `7e98645`，
   与线上 `0d0d5d1` 在 `server.py` / `web/index.html` / `h3studio/video_models.py`
   上有重叠，**需要 rebase 后再合**。本分支 `local/feature/panel-fallback`
   与它**互不依赖**，可独立先合。
3. **兜底清单是静态快照** —— 服务端登记的模型变了，前端兜底清单不会自动跟着变
   （需要重新生成 `web/video-panel-fallback.js`）。已加测试
   `test_fallback_covers_all_server_models` / `test_fallback_size_presets_match_server`
   在不一致时会**变红**，不会静默漂移。
4. **兜底态下模型可用性显示为 `unknown`** —— 兜底清单里的模型项
   `status: "unknown"`，界面显示「未能确认，可用性未知」。
   这是**如实**的：兜底数据不能保证权重真的在本机，提交时服务端仍会再校验一次。
5. **ComfyUI 启动期间的其它面板未做同样加固** —— 本次只修了文生视频面板
   （用户报的就是它）。图像 / 音乐面板的清单加载失败仍是「不渲染」的老结构，
   但它们的清单来自本地扫描、不依赖 ComfyUI 实时接口，风险低。
6. **兜底态的 banner 会覆盖 `staleVideoBanner`** —— 该元素是视频面板专用的
   过期提示位，兜底态复用它展示加载失败原因；不与其它面板冲突。
7. **ComfyUI 本轮验证期间被外部重启过多次**（22:00 / 22:05 / 22:10）——
   不是我操作（未调用 `systemctl`）。这与用户报告的故障窗口高度吻合，
   说明**触发条件就是 ComfyUI 重启**。

---

## 七、复现与验收

```bash
# 起分支实例（不动 8190）
cd /root/projects/h3studio-panel-fallback
# config.json: port=8195, comfyui_url 指向一个「接受连接但不响应」的假 ComfyUI
PYTHONPATH=$PWD /home/ubuntu/comfyui/venv/bin/python server.py

# 接口耗时对比（修复前 8194 / 修复后 8195）
curl -s -o /dev/null -w "%{time_total}s\n" http://127.0.0.1:8195/api/video-options

# 真机浏览器验收（3 视口 × 3 场景）
/home/ubuntu/comfyui/venv/bin/python tests/verify_panel_fallback_live.py http://127.0.0.1:8195

# 测试
/home/ubuntu/comfyui/venv/bin/python -m pytest tests/test_panel_fallback.py -q

# 回归对比（baseline vs 分支）
bash scripts/compare_pytest_files.sh \
  /root/projects/h3studio-panel-baseline \
  /root/projects/h3studio-panel-fallback /tmp/cmp-panel
```

---

## 附：全量回归（baseline vs 分支，逐文件对比）

对比方式：新建**未改动**的 baseline worktree
（`/root/projects/h3studio-panel-baseline`，detached @ `0d0d5d1` = 线上同一提交），
`scripts/compare_pytest_files.sh` 逐文件**并行**跑两边。

**baseline 既有失败（7 项 + 1 个超时，两边完全一致）**：

```
test_music_flow_browser :: test_regen_uses_current_content_as_base
test_music_flow_browser :: test_structured_fields_are_editable
test_qwen_pe_enhance    :: test_dropdown_lists_four_methods
test_qwen_refs_browser  :: test_desc_tag_renders_placeholder_text
test_qwen_refs_browser  :: test_remove_middle_drops_dangling_placeholder
test_yue2_flow_review_fixes :: test_every_method_declares_write_contract
TIMEOUT test_studio (exceeded 240s)
```

**分支新增失败：0 项。**

### 关于 `test_grid_flash::test_grid_height_stable_across_switch`

对比脚本曾报分支多这一项失败。**逐条复核后确认是既有的时序 flake，
与本次改动无关**：

| 单跑该项（3 轮） | baseline | 分支 |
|---|---|---|
| 轮 1 | passed | passed |
| 轮 2 | **failed** | **failed** |
| 轮 3 | **failed** | **passed** |

两边都是「时好时坏」。且该测试**与本次改动零交集** ——
它测的是**图像面板**的任务网格高度抖动（`#jobsGrid` / `.job-card`），
源码里不出现 `vidModelSelect` / `video-panel` / `VIDEO_PANEL` 任何一个。

原始对比数据：`evidence/panel-fallback/regression/{base.txt,branch.txt,diff.txt}`。
