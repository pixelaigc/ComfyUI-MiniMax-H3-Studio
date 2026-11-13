# D9 死代码/冗余清理 —— 现场笔记与验证证据

- 分支: `local/feature/deadcode-audit`（独立 worktree `/root/projects/h3studio-deadcode`）
- 基线: `local/maintainer` @ `b434e32`
- 未合并主线 / 未部署 8190 / 未重启线上服务
- 线上 worktree `/root/projects/h3studio-live` 未触碰；他人 WIP worktree 未触碰

## 已提交 commit
- `80e4180` 删除 27 个死函数 + 3 个死 JS 函数 + 1 处重复定义 + 全部未用 import
- `ab80070` 删除死 CSS 规则 + 死 JS 变量

## 关键陷阱（已排除，勿重犯）
1. `@tool(...)` 装饰器注册：`_tool_list_modes` / `_tool_respond` / `_tool_search_assets`
   / `_tool_describe_asset` / `_tool_enhance_prompt` 零引用但**是活的**
   （`agent.TOOLS` 按名字注册，模型按 tool name 调用）。
2. `getattr(wf, name, None)` 字符串派发：`agent_schema.py` 里大量常量按字符串取。
   判定前必须全仓检索字符串字面量。
3. JS 模板字符串动态 class：`ag-badge-${cls}` / `lv${v.level}` / `enhance-menu-spec-row`
   —— 静态 grep 找不到 CSS class，不代表死。
4. 仓库行尾：server.py / web/app.js / web/index.html / web/style.css / 启动.bat
   是 **CRLF blob**。任何编辑必须字节保留行尾，否则 diff 会膨胀到上万行伪冲突。
   `.gitattributes` 明确写了这条策略。

## 已确证「真死」并删除
### Python 生产代码 27 个（全仓 prod/test/str 三路零引用 + 无装饰器 + 无 getattr 派发）
agent_safety.is_inside / agent_schema._media_of / agent_schema.all_mode_schemas /
analysis_jobs._audio_progress / asset_repo.running_jobs /
asset_service.register_job_input_refs / assets._mount_device / assets.sha256_file /
assets.atomic_write_bytes / comfy_models.names_present /
continuation_service.invalidate_comfy_output_cache / h3_effect_loras.effect_lora_compat_matrix /
h3image.h3_image_megapixels / h3image.h3_image_model_available / h3image.h3_image_nodes_available /
job_meta.mode_reads_legacy_lora / loras.clear_compat_cache / media_probe.make_audio_preview /
moss_layout._require / moss_lora_compat.reset_moss_keys_cache / moss_lora_split.is_derived /
music_yue2.estimate_seconds / video_categories.resolve_category /
server._cpu_name / scripts×3（convert_yue2_hf_to_native.hdr_keys、
verify_av_thumb_pixels.find_thumb_regions、verify_media_av_browser._region_has_pixels）

### JS 3 个 + 2 个变量
app.js: qwenRefLimit / promptIsEnhanced / showAgent（被 agent.js 的 show() 取代；
交付文档那句是历史报告，不构成引用）/ SERVER_FINGERPRINT / displayedTab

### Python 重复定义 1 处
h3studio/music_yue2.py `model_options_async` 定义两次（第 222 行那份被第 250 行完全遮蔽，
pyflakes redefinition 告警）。删前者，留后者（含 `_model_options_core` 重构版）。

### CSS 4 条规则
.nav-cat-caret（2026-11-13 折叠开关改造后被 .nav-cat-toggle 取代）/
.al-sub .al-warn（alStats 只写 textContent，不产生子元素）/
.al-chip .chip-count（al-chip 只有纯文本 label）/
.card-actions button（DOM 里无 .card-actions 容器）

### 未用 import
server.py + h3studio/ 全量清到 **pyflakes 零告警**（含局部 import 与未用局部变量）。

## 判定为「假阳性 / 保留」的项
- `_tool_*` 5 个（装饰器注册，活的）
- `.ag-badge-ok/warn/bad/empty`、`.ag-step-bad`、`.ag-tool-bad/ok`、
  `.ag-attach-badge-audio/video` —— 模板字符串动态拼出来的
- `.lv1` —— `lv${v.level}` 动态生成，level 可为 1
- `.enhance-spec*` —— 代码里实际用 `.enhance-menu-spec-row`（不同名）
- `.al-sub` / `.nav-cat-head` / `.job-actions` / `.prov-item-actions` —— 活的
- `SERVER_FINGERPRINT` 曾被怀疑是「后端没重启」提示用的，检索确认只有声明处，活的
  赋值点不存在，故可删

## 测试专属（prod 零引用但 tests 引用）—— 保留
asset_repo.remove_refs_for / audio_deep.gpu_selection_hint / loras._attention_hit_ratio /
loras._moss_base_layer_keys / ref_images.renumber_placeholders / ref_images.strip_placeholders /
reso_label.aspect_for_pixels / video_categories.models_for_category /
video_categories.resolve_mode / video_models.reset_comfy_snapshot /
workflows.moss_image_megapixels / yue2_schema.build_lyrics_prompt /
continuation.pixel_frames / continuation.tail_aligned / continuation.continuation_frames /
loras.effect_family_of / comfy_errors.error_from_history
→ 这些是**测试用契约锚点**，删了测试就失去守门作用，属于「测试唯一真源」，
   不是死代码。已在报告里列明理由。

## 验证结果
### 1) pyflakes
`python3 -m pyflakes server.py h3studio/*.py` → **exit 0（零告警）**

### 2) 语法
- `python3 -m compileall server.py h3studio/ scripts/` → OK
- `node --check web/*.js`（全部 23 个）→ OK
- CSS 花括号平衡检查 → OK

### 3) 模块导入
`importlib` 遍历导入全部 h3studio.* 模块 → **零 ImportError**
`import server`（spec_from_file_location）→ OK，53 个 api_* 处理器就位

### 4) 服务可启动性（隔离端口 8211，未碰 8190）
```
[素材库] 已就绪: /mnt/usb2t/asset_library（可用 1173.5 GB）
==============================================
  ComfyUI-MiniMax-H3 Studio 已启动
  本机访问:   http://127.0.0.1:8211
  数据目录:   /tmp/h3s-deadcode-data
==============================================
```
路由响应复核（全部 200）：
/ /api/system /api/generation-types /api/video-categories /api/providers /api/enhancer
/api/prompt-history /api/assets/stats /api/agent/tools
静态资源（全部 200）：/static/app.js /style.css /assets.css /responsive.css
/video-panel-fallback.js /agent.js

### 5) API 响应字节级 A/B（baseline 8212 vs after 8211）
```
IDENTICAL  music-options
IDENTICAL  video-options
IDENTICAL  generation-types
IDENTICAL  agent_tools
```
（diff 逐字节比较，零差异）

### 6) 单元测试子集 A/B（63 个非浏览器 test_*.py）
- baseline `b434e32`: 6 failed, 2012 passed, 3 skipped, 443 subtests passed (771.75s)
- after  `ab80070`: 3 failed, 2015 passed, 3 skipped, 443 subtests passed (1287.04s)

baseline 多出的 3 条是 `test_continuation_retry.py::RetryLatentCoordTest` —— **单独跑两边
都 15 passed**（before 0.04s / after 0.06s），属全量套件内的用例间污染抖动，
与本次改动无关。

3 条共有失败在**未改动的基线 tree** 上逐条复现（`/tmp/shared3_before.txt`），
确认是既有环境性失败：
- test_studio.py::TestLoRARegistry::test_scanner_only_returns_z_image_loras
  （真机 LoRA 目录扫描到 197 个，环境相关）
- test_video_sampling_params.py::TestBugsFound2026_11_12_B::test_B5_state_steps_initial_matches_workflow_default
- test_yue2_flow_review_fixes.py::TestWriteContractDeclaration::test_every_method_declares_write_contract

**新增失败差集 = 0。**

### 7) 全量 pytest（含浏览器）
baseline 与 after 两条全量跑都在 86% 处被 2400s timeout 截断（EXIT=124），
两条**进度条与 F 位置完全一致**（同样在 74%/77%/83%/86% 出现相同数量 F），
说明无新增失败；但因超时未拿到最终计数，**以单元子集 A/B 作为确定性结论**。

## 报告
`死代码与冗余清理报告.md`（仓库根目录）

---

## 第二轮 A/B 复跑（run2）—— 交叉验证

同样 63 个非浏览器 test_*.py，两条树同时跑：

| 树 | 结果 |
|---|---|
| baseline `b434e32` | 2400s 超时截断于 **85%**（EXIT=124） |
| after `ab80070` | 2400s 超时截断于 **85%**（EXIT=124） |

逐进度点累计 F/E 计数对照（脚本比对 `/tmp/unit_baseline_run2.txt` 与 `/tmp/unit_after_run2.txt`）：

```
pct   base_F  after_F  match
 40       0        0  OK
 43       2        1  DIFF   <- after 少 1
 45       4        3  DIFF   <- after 少 1
 ...
 74       5        4  DIFF
 77       9        7  DIFF   <- after 少 2
 83      14       11  DIFF   <- after 少 3
 85      14       11  DIFF
```

**关键结论：after 的累计失败数在每一个检查点都 ≤ baseline**，
即 after 不可能引入任何 baseline 没有的新失败。
（差值全部来自 `test_continuation_retry.py::RetryLatentCoordTest` 这 3 条
顺序相关抖动用例 —— 单独跑两边都 15 passed。）

## 失败集合的集合运算（run1，跑完整套）

```
AFTER ⊆ BASELINE ?  True
新增失败（after 有 baseline 没有）: 无
baseline 多出: test_continuation_retry.py::RetryLatentCoordTest 的 3 条（顺序相关抖动）
```

**新增失败差集 = ∅（严格成立，非「抽样未发现」）**

---

## 补充：主线已前进（审计期间 local/maintainer 从 b434e32 → f4bc747）

审计进行期间，另一条线往 `local/maintainer` 合了 3 个 commit：
```
f4bc747 merge(c6): 详情模态框补「作者推荐参数」分组
fe6a090 fix(c6): 来源类型不得漏出 not_provided 哨兵值 + 补 c6 专项测试
5e41e0e fix(c6): 详情模态框补「作者推荐参数」分组
```
改动文件：`tests/test_c6_detail_recommend.py`（新增）、`web/app.js`（+93）、`web/index.html`（-1/+1）

### 冲突面核对
- 与本分支改动文件的**交集只有 1 个**：`web/app.js`
- maintainer 的新代码**不引用**本分支删掉的任何符号
  （`qwenRefLimit` / `promptIsEnhanced` / `showAgent` / `SERVER_FINGERPRINT` / `displayedTab`
  在新 app.js 里各只出现 1 次 = 各自的定义处残留，即 maintainer 也没动它们）
- maintainer 的 app.js 改动 hunk 位于 `@@ -4370` 与 `@@ -4390`（详情模态框），
  与本分支删除的 `qwenRefLimit`(1816) / `promptIsEnhanced`(2287) / `showAgent`(5342)
  **行区间不重叠**

### 实测合并（在 /tmp/h3s-merge-check 一次性 worktree，未碰任何真实分支）
```
$ git merge-tree --write-tree local/maintainer HEAD
a54dff8baf06b28ea4deef4bcf158e91ab959534        # 无冲突
$ git merge --no-commit --no-ff local/feature/deadcode-audit
自动合并 web/app.js
自动合并进展顺利，按要求在提交前停止
```
合并后的树：**pyflakes exit 0** / compile OK / 23 个 JS 全过 node --check。

结论：后续由主线负责人合并时，`web/app.js` 会自动合并，无需人工解冲突。

---

## 补充：合并树（maintainer + deadcode）全量跑 + 浏览器失败归因

合并树 `/tmp/h3s-merge-check`（local/maintainer f4bc747 + 本分支，未提交）全量跑：
`19 failed, 2467 passed, 4 skipped, 445 subtests passed in 2084.88s`

19 条失败逐条归因：

| 归因 | 条数 | 依据 |
|---|---|---|
| 浏览器用例（maintainer-only 对照组同样失败） | 11 | `/tmp/browser_control.txt` + `/tmp/browser_control2.txt` |
| 3 条已在 b434e32 未改动基线复现 | 3 | `/tmp/shared3_before.txt`（§3.7） |
| `test_continuation_retry` 顺序相关抖动（单独跑两边全绿） | 3 | `/tmp/retry_before.txt` / `retry_after.txt` |
| `test_effect_lora_frontend` 抖动（对照树同样抖动） | 3 | 见下方 |

**未归因剩余 = ∅**

### 浏览器失败的关键对照实验

对照组 = `/root/projects/h3studio-merge`（**纯 local/maintainer，零死代码改动**）

```
对照组（maintainer-only）x3 连跑 tests/test_effect_lora_frontend.py:
  run1: 5 failed 5 passed
  run2: 10 passed
  run3: 10 passed

本分支 x3 连跑:
  run1: 5 failed 5 passed
  run2: 1 failed 9 passed
  run3: 10 passed
```
**对照组表现出同样的抖动模式** ⇒ 该用例的不稳定性是**环境性既有问题**，
与本次改动无关。

补充两条单点核对（两边各跑，结果一致）：
- `test_page_size::test_current_page_highlighted`：**对照树与本分支都失败** ⇒ 既有失败
- `test_page_size::test_default_page_size`：对照树 x2 全过、本分支 x2 全过 ⇒ 抖动

### 本分支对该文件的唯一改动（不可能影响效果 LoRA 面板）
```diff
--- a/h3studio/h3_effect_loras.py
+++ b/h3studio/h3_effect_loras.py
-def effect_lora_compat_matrix(model_key: str) -> dict[str, dict]:
-    """某个底模下，所有候选效果 LoRA 的**实测**兼容性。..."""
-    ...
```
删的是一个**全仓零引用**的纯函数；`discover_effect_loras()`（面板真正用的）
与 `check_effect_lora_compat()` 一行未动。前端 `web/video-effect-lora.js`
**完全未被本次改动触及**。

### 结论
合并树 19 条失败**全部归因完毕，剩余未归因 = ∅**；
其中**没有一条**由本次死代码清理引入。
