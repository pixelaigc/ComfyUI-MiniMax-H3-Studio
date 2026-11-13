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
