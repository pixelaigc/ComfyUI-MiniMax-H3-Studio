# 文生视频面板「获取不到模型」提示修复报告

- **日期**：2026-11-11
- **分支**：`local/feature/model-list-fix`
- **worktree**：`/root/projects/h3studio-model-list-fix`
- **基线**：`local/maintainer` @ `7e98645`
- **线上服务**：`h3-studio.service` @ 8190 —— **未重启、未部署**（仍是修复前代码）
- **验证实例**：8191（本分支代码，`H3STUDIO_PORT=8191`）

---

## 一、结论速览

用户报的那条红字**结论是错的**，根因是**一句写死的常量**：

> 「权重在外硬盘 /mnt/nvme1t_colorful —— 尚未挂载或 ComfyUI 下拉框确实没有这个单卡，提交会被拒绝。」

这句话来自 `h3studio/video_models.py` 里 `VIDEO_MODELS["fl2va_pruned_int8"]["warn"]`
的**硬编码字符串**，它：

1. **不参与任何判定** —— 跟面板上的 `available` 字段、与提交时的校验都**没有关系**，
   哪怕权重根本不存在，这句话也照样整句显示；
2. **从未调用过 ComfyUI 的模型列表 API** —— 可用性判定走的是 `model_available()`，
   它只扫本地目录（`extra_model_paths.yaml` 声明的路径 + 写死的默认目录），
   从没问过 ComfyUI「你的下拉框里到底有哪些模型」；
3. **前提在本机不成立** —— 外置盘已挂载、ComfyUI 已收录全部 5 个 H3 FL2VA 权重（见 §三）。

修复方向：**把「能不能选」改成问 ComfyUI 本人**，
并在连不上时如实降级（说明用的是磁盘扫描 + 原因），而不是笼统断言「获取不到模型」。

---

## 二、根因（只读定位）

### 2.1 代码位置

| 角色 | 文件 | 位置 |
|---|---|---|
| 静态红字常量 | `h3studio/video_models.py` | `VIDEO_MODELS[...]["warn"]` |
| 可用性判定（扫描盘） | `h3studio/video_models.py` | `model_available()` / `lora_available()` |
| 清单组装 | `h3studio/video_models.py` | `list_models()` / `options_payload()` |
| 接口 | `server.py` | `api_video_options()` → `GET /api/video-options` |
| 提交校验 | `server.py` | `normalize_t2v_params()` |
| 前端渲染 | `web/video-panel.js` | `renderModelSelect()` / `renderModelNote()` |
| 前端容器 | `web/index.html` | `#vidModelSelect` / `#vidModelNote` / `#vidModelWarn` |

### 2.2 原来的判定逻辑（两条互不相干的路径）

```
面板显示：  renderModelSelect() -> m.available  -> model_available() -> find_weight() 扫盘
红字文案：  model.warn  ->  写死常量，无条件显示              ← 与上面那条毫无关系
提交校验：  normalize_t2v_params() -> model_available() -> find_weight() 扫盘
```

后果：

- 红字与实际可用性**矛盾**（明明 `available: true`，红字却说「没有这个名字」）；
- 面板与提交**不共用** ComfyUI 的事实，存在「下拉显示 A、提交被拒」的结构性风险；
- ComfyUI 已经提供的模型清单能力**完全没被利用**。

### 2.3 用户引用的那句话与本次实现的对照

| 用户看到的（写死） | 修复后（运行时现算） |
|---|---|
| 「权重在外硬盘 …… 尚未挂载或 ComfyUI 下拉框确实没有这个单卡，提交会被拒绝。」 | ComfyUI 已收录 → `已确认在 ComfyUI 模型列表中可用。` |
| 同上（盘真的没挂时） | → `权重位于外置盘 /mnt/...，但 ComfyUI 的模型列表里没有它 —— 外置盘可能未挂载或未扫描到，当前不可选。`（该项**禁用**） |
| 同上（ComfyUI 连不上时） | → `未能连上 ComfyUI 模型列表（<原因>）—— 当前清单来自本机磁盘扫描…`（如实说明来源） |

---

## 三、ComfyUI 模型列表 API —— 实际接口与返回结构

### 3.1 实测可用接口（ComfyUI 0.37.0）

| 接口 | 实测 | 返回结构 |
|---|---|---|
| `GET /models/diffusion_models` | 200，7596 B | **扁平字符串数组**，151 条，含子目录前缀（`h3/xxx.safetensors`） |
| `GET /models/loras` | 200，39708 B | 同上，686 条 |
| `GET /object_info/UNETLoader` | 200，8122 B | `{"UNETLoader":{"input":{"required":{"unet_name":[[…151 项…],{…}]}}}}` |
| `GET /models/unet` | **404** | `unet` 只是 `diffusion_models` 的旧别名，HTTP 路由**不做**映射 |

**两个视角交叉验证**：目录视角（`/models/*`）与节点视角
（`/object_info/<Node>.input.required.<slot>`）实测**完全一致**（各 151 条）——
后者正是 ComfyUI 界面下拉框的数据源（`folder_paths.get_filename_list()`，同源）。

### 3.2 H3 权重实际存放路径（对应要求 4）

**结论：权重全部真实存在，ComfyUI 已收录，无需修配置。**

```bash
$ mount | grep nvme1t_colorful
/dev/nvme2n1p1 on /mnt/nvme1t_colorful type ext4 (rw,noatime)      # 已挂载

$ df -h /mnt/nvme1t_colorful
/dev/nvme2n1p1  938G  604G  287G  68%  /mnt/nvme1t_colorful

$ ls -la /mnt/nvme1t_colorful/comfyui-models/diffusion_models/ | grep minimax_h3
-rw-r--r-- 1 root root 66280487368 Sep  9 06:52 minimax_h3_fl2va_bf16.safetensors
-rw-r--r-- 1 root root 20970379616 Aug  5 09:14 minimax_h3_fl2va_pruned_int8_convrot.safetensors
-rw-r--r-- 1 root root 12540858008 Aug 13 10:37 minimax_h3_fl2va_pruned_w4a8_mixed.safetensors
-rw-r--r-- 1 root root 20970379632 Aug  5 14:50 minimax_h3_hybrid_fl2va_ref2va_b30-49-int8.safetensors
-rw-r--r-- 1 root root 34038894550 Aug  6 22:14 minimax_h3_ref2va_int8_convrot.safetensors

$ ls -la /home/ubuntu/comfyui/models/diffusion_models/ | grep minimax_h3
-rw-r--r-- 1 root root 34038892334 Aug  6 22:15 minimax_h3_fl2va_int8_convrot.safetensors   # 唯一在本地盘

$ grep -A2 nvme1t_models /home/ubuntu/comfyui/extra_model_paths.yaml
nvme1t_models:
    base_path: /mnt/nvme1t_colorful/comfyui-models/
```

外置盘已挂载、`extra_model_paths.yaml` 已正确声明、ComfyUI 已收录 ——
所以那句「盘未挂载 / ComfyUI 下拉框确实没有这个单卡」**前提不成立，结论错误**。

---

## 四、改动清单

| 文件 | 改动 |
|---|---|
| `h3studio/comfy_models.py` | **新增**。ComfyUI 模型清单客户端：同步 + 异步双版本、目录视角 + 节点视角枚举、TTL 缓存、明确的 `ComfyModelListError` |
| `h3studio/video_models.py` | 新增 `ComfyModelSnapshot`（快照 = 唯一判据）、`model_status()` / `model_status_note()`、`refresh_comfy_snapshot[_async]()`；`model_available()` / `lora_available()` 改为以 ComfyUI 为准；`list_models()` 改为数据驱动（新增 `status` / `comfy_listed` / `on_external_disk` / `status_note`）；`options_payload()` 新增 `model_list_source` / `model_list_error` / `model_list_counts`；**删除写死的那句红字** |
| `server.py` | `api_video_options` 改用**异步**快照（避免阻塞事件循环）；提交校验 `normalize_t2v_params` 改用与面板**同源**的 `model_status()`；**新增** `GET /api/comfy-models` 诊断端点 |
| `web/video-panel.js` | 下拉改为**数据驱动**（按 `status` 决定禁用与文案）；红字区改为「现算的 status_note + 固有提醒」；新增**清单来源说明**（正常显示 API + 条目数，降级显示原因） |
| `web/index.html` | 新增 `#vidModelSourceNote` 容器 |
| `tests/test_model_list_api.py` | **新增** 40 项：静态文案禁令、API 为权威、面板与提交同源、同步/异步客户端、降级路径、前端数据驱动 |
| `tests/test_video_panel_model_dom.py` | **新增** 12 项：**Node + DOM shim 真跑 `video-panel.js`**，断言渲染结果（不是查字符串） |
| `tests/verify_model_list_live.py` | **新增**：三档视口实机浏览器验收脚本 |
| `scripts/compare_pytest_files.sh` | **新增**：逐文件跑 pytest，对比 baseline 与分支失败名单 |
| `evidence/model-list-fix/` | **新增**：截图 + 接口返回原始 JSON |

### 关键设计点

1. **单一判据**：`model_status()` 是唯一真相源，面板与提交都走它 → 结构上杜绝「下拉显示 A、提交用 B」。
2. **异步优先**：ComfyUI 的 `/models/diffusion_models` **首次调用实测 43 秒**（它要递归扫
   `extra_model_paths.yaml` 里所有盘）。同步 HTTP 调用会**卡死整个 aiohttp 事件循环**，
   所以服务端一律用 aiohttp 异步版，且三个请求**并发**发出（实测总耗时 4.7 秒 ≈ 最慢那个）。
3. **超时留足余量**：客户端超时设 60 秒而非 10 秒 —— 盘未挂载恰好会触发全量重扫，
   超时太短会把「ComfyUI 正在扫盘」误判成「连不上」，那正好复现了本次要修的病。
4. **降级不撒谎**：连不上 ComfyUI 时退回扫盘，但 payload 带 `model_list_source` +
   `model_list_error`，界面如实说明「当前清单来自本机磁盘扫描」+ 原因。
5. **两个 unet 视角取并集**：目录视角漏了但节点枚举有 → 仍算可选。

---

## 五、验证证据

### 5.1 接口返回（8191 分支实例）

```bash
$ curl -s http://127.0.0.1:8191/api/video-options | python3 -c "
import json,sys; d=json.load(sys.stdin)
print(d['model_list_source'], d['model_list_counts'], repr(d['model_list_error']))
for m in d['models']: print(' ', m['key'], m['status'], m['comfy_listed'])"
```
```
comfyui_api {'diffusion_models': 151, 'unet_enum': 151, 'loras': 686} ''
  fl2va_pruned_int8       available True
  fl2va_int8              available True
  fl2va_pruned_w4a8       available True
  fl2va_bf16              available True
  hybrid_fl2va_ref2va     available True
```

```bash
$ curl -s http://127.0.0.1:8191/api/comfy-models | python3 -c "
import json,sys; d=json.load(sys.stdin)
print(d['ok'], d['folder'], d['count'])
for m in d['panel_models']: print(' ', m['key'], 'listed=', m['listed'])"
```
```
True diffusion_models 151
  fl2va_pruned_int8 listed= True
  fl2va_int8 listed= True
  fl2va_pruned_w4a8 listed= True
  fl2va_bf16 listed= True
  hybrid_fl2va_ref2va listed= True
```

### 5.2 参数链路（下拉 → 提交 → 工作流 → ComfyUI）

`evidence/model-list-fix/curl/param-chain-verified.json`，5 个模型逐个验证：

| 下拉 key | 面板 status | 提交归一 | 工作流 `unet_name` | == 清单 | 在 ComfyUI 下拉里 |
|---|---|---|---|---|---|
| `fl2va_pruned_int8` | available | 无错 | `minimax_h3_fl2va_pruned_int8_convrot.safetensors` | ✅ | ✅ |
| `fl2va_int8` | available | 无错 | `minimax_h3_fl2va_int8_convrot.safetensors` | ✅ | ✅ |
| `fl2va_pruned_w4a8` | available | 无错 | `minimax_h3_fl2va_pruned_w4a8_mixed.safetensors` | ✅ | ✅ |
| `fl2va_bf16` | available | 无错 | `minimax_h3_fl2va_bf16.safetensors` | ✅ | ✅ |
| `hybrid_fl2va_ref2va` | available | 无错 | `minimax_h3_hybrid_fl2va_ref2va_b30-49-int8.safetensors` | ✅ | ✅ |

### 5.3 实机浏览器验收（三档视口）

`evidence/model-list-fix/report.json` + 截图：

| 视口 | 下拉==提交 | 切换后仍一致 | 横向溢出 | 控件可见 | console 错误 | 页面异常 | 错误断言命中 |
|---|---|---|---|---|---|---|---|
| 375 | ✅ | ✅ | **0 px** | ✅ | **0** | **0** | **0** |
| 390 | ✅ | ✅ | **0 px** | ✅ | **0** | **0** | **0** |
| 1280 | ✅ | ✅ | **0 px** | ✅ | **0** | **0** | **0** |

「错误断言命中」= 界面文本里出现
`获取不到` / `尚未挂载` / `根本没有这个名字` / `下拉框确实没有` / `没有这个单卡` / `提交会被拒绝`
的次数 —— **三档均为 0**。

375px 实机截图（模型区）：

![375px 文生视频面板模型区](/root/projects/h3studio-model-list-fix/evidence/model-list-fix/model-list-375-zoom.png)

界面实际渲染：
- 来源说明：`清单来源：ComfyUI 模型列表 API（151 个扩散模型 / 686 个 LoRA）`
- 下拉选中：`MiniMax-H3 FL2VA · 剪枝 int8（默认）`
- 下方提示：`已确认在 ComfyUI 模型列表中可用。`

### 5.4 全量回归（baseline vs 分支，逐文件对比）

对比方式：新建一个**未改动**的 baseline worktree
（`/root/projects/h3studio-model-list-baseline`，detached @ `7e98645`），
用 `scripts/compare_pytest_files.sh` **逐文件**并行跑两边
（整套跑一轮要 25+ 分钟且撞 1500s 超时，逐文件才拿得到完整失败名单）。

- 公共测试文件：**56 个**
- 分支新增测试文件：**2 个**（`test_model_list_api.py`、`test_video_panel_model_dom.py`）

**baseline 既有失败（4 项，两边完全一致）**：

```
test_music_flow_browser :: test_regen_uses_current_content_as_base
test_music_flow_browser :: test_structured_fields_are_editable
test_qwen_pe_enhance    :: test_dropdown_lists_four_methods
test_yue2_flow_review_fixes :: test_every_method_declares_write_contract
```

**分支新增失败：0 项。**

#### 关于 `test_page_size`（对比脚本曾报 2 项差异，已查清）

对比脚本首轮报分支多 2 项 `test_page_size` 失败。**逐条复核后确认是既有的时序 flake，
不是本次改动造成的**，证据如下：

| 复现方式 | baseline | 分支 |
|---|---|---|
| 整文件跑 `tests/test_page_size.py` | `1 failed, 21 passed`（`test_pager_boundary_buttons_disabled`） | `1 failed, 21 passed`（**同一项**） |
| 单独跑 `test_filters_still_work` | 2 轮均 `1 passed` | 同上 |
| 两项一起跑 ×3 轮 | `2 failed` / `1 failed, 1 passed` / `1 failed, 1 passed` | `2 failed` / `1 failed, 1 passed` / `1 failed, 1 passed` |

**两边的失败模式逐轮完全一致** —— 说明这是该测试文件自身的
「用例间顺序/时序依赖」问题（浏览器端 `wait_for_timeout` + localStorage 状态），
与本次改动无关（本次改动不碰 `web/app.js` 的分页逻辑，也不碰 `index.html` 的任务列表）。

原始对比数据：`evidence/model-list-fix/regression/{base.txt,branch.txt,diff.txt}`。

### 5.5 新增测试

```
$ /home/ubuntu/comfyui/venv/bin/python -m pytest tests/test_model_list_api.py tests/test_video_panel_model_dom.py -q
56 passed
```

**变异验证**（把实现改坏，测试必须变红）：

| 变异 | 抓到的测试数 |
|---|---|
| 把写死的红字常量加回去 | 3 |
| `model_available()` 恒返回 True（无视 ComfyUI） | 2 |
| 前端退回只看 `m.available`（忽略 `status`） | 1 |
| `external_missing` 被当成 `available` | 5 |

> 变异 2 最初**没被抓到** —— 因为 `model_available()` 当时已成死代码。
> 已重构为 `model_status()` 的薄封装并补上直接覆盖它的测试，现已能抓住。

---

## 六、遗留问题（明确列出）

1. **8190 线上服务仍是修复前代码** —— 按要求未部署、未重启。
   线上「文生视频」面板**目前仍会显示那句错话**。合并后需重启 `h3-studio.service` 才生效。
2. **ComfyUI 首次列目录要 43 秒** —— ComfyUI 自己的 `folder_paths` 缓存问题
   （扫 `extra_model_paths.yaml` 里所有盘）。本次用「异步 + 20 秒快照缓存 + 60 秒超时」
   规避了对服务端的阻塞，但**首次打开面板**仍可能等十几秒。
   若要根治，需要调 ComfyUI 侧的目录扫描策略（不在本任务范围）。
3. **ComfyUI 在本轮验证期间被外部重启过一次** —— 不是我操作的（`systemctl` 未被我调用）。
   重启期间接口短暂不可达，降级路径实测正常。
4. **加速 LoRA 的可用性也改成了以 ComfyUI 为准**，但**未做逐项浏览器验收**
   （本次范围是底模下拉）。LoRA 清单在 ComfyUI 侧实测 686 条，
   面板登记的 8 个名字全部命中（见 §三 curl 输出）。
5. **`/api/comfy-models` 未做鉴权** —— 与本项目其它 `/api/*` 端点一致
   （局域网共用设计），未单独加权限。
6. **快照缓存 20 秒** —— 用户在 ComfyUI 里手动换模型后，面板最多 20 秒才反映出来
   （刷新页面或等 20 秒）。这是刻意的取舍，避免每次打开面板都全量扫盘。
7. **`tests/test_page_size.py` 有既有的时序 flake** —— 整文件跑时
   `test_pager_boundary_buttons_disabled` 稳定失败、`test_filters_still_work` 时好时坏。
   **baseline 与分支表现完全一致**（见 §5.4 表格），不是本次改动引入的，
   但确实会让「全量回归看起来有新增失败」。本任务范围内未修（不在本次范围）。

---

## 七、复现与验收

```bash
# 起一个分支实例（不动 8190）
cd /root/projects/h3studio-model-list-fix
H3STUDIO_PORT=8191 H3STUDIO_DATA_DIR=/tmp/h3-model-list-data \
  PYTHONPATH=$PWD /home/ubuntu/comfyui/venv/bin/python server.py

# 接口
curl -s http://127.0.0.1:8191/api/video-options
curl -s http://127.0.0.1:8191/api/comfy-models

# 浏览器验收（三档视口）
/home/ubuntu/comfyui/venv/bin/python tests/verify_model_list_live.py http://127.0.0.1:8191

# 新增测试
/home/ubuntu/comfyui/venv/bin/python -m pytest \
  tests/test_model_list_api.py tests/test_video_panel_model_dom.py -q

# 回归对比（baseline vs 分支）
bash scripts/compare_pytest_files.sh \
  /root/projects/h3studio-model-list-baseline \
  /root/projects/h3studio-model-list-fix /tmp/cmp-out
```
