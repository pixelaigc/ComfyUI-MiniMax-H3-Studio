# H3 Studio 真实浏览器 E2E 测试集

> 这套测试用**真实 Chromium**（Playwright）驱动**真实界面**，按人的操作
> 流程点击 / 输入 / 右键 / 拖拽，验证「界面 → 后端 → ComfyUI → 结果回到界面」
> 整条链路是通的。
>
> 它**不是**接口测试：仓库里原有的 `tests/` 是契约测试（解析 HTML/JS 文本
> 断言 DOM「应该」长什么样），那层漏掉了「界面能渲染但按钮点不动」
> 「输入框能输入但提交的是旧值」「卡片永远运行中」这类问题。

---

## 一分钟跑起来

```bash
# 默认：隔离实例 + 假 ComfyUI（不占 GPU、时序可控，约 8 分钟）
scripts/run_e2e.sh

# 连线上 8190（真实部署 + 真实 ComfyUI，会占 GPU，耗时以分钟计）
scripts/run_e2e.sh --live

# 有头复验（没有 DISPLAY 时脚本会自己起 Xvfb）
scripts/run_e2e.sh --headed

# 只跑一组
scripts/run_e2e.sh --only test_image_flow.py
```

证据（截图 / 步骤日志 / 控制台 / 网络错误）落在：

```
evidence/e2e/<run-id>/
├── <case-id>/
│   ├── 01-xxx.png          # 真机截图（失败时含 final 状态）
│   ├── evidence.json       # 步骤日志 + 控制台 + 网络错误
│   └── console.log
└── studio-server.log       # 被测实例的服务端日志
```

---

## 两种运行模式

| 模式 | 命令 | Studio 实例 | ComfyUI | 用途 |
|---|---|---|---|---|
| 隔离（默认） | `scripts/run_e2e.sh` | 新起一个（独立端口 / 独立 data_dir / 独立素材库） | **假 ComfyUI** | 回归、CI、可重复复现 |
| 线上 | `scripts/run_e2e.sh --live` | 连 8190 现有部署 | 真 ComfyUI | 验收「线上那套真的能用」 |

隔离模式**绝不碰线上数据**：端口是系统分配的空闲端口、`data_dir` 在
临时目录、素材库在 `/dev/shm` 下的独立挂载点（素材库会拒绝写非独立
挂载点，见 `h3studio/assets.py:check_disk_mounted`）。

### 假 ComfyUI 是什么

`tests/e2e/fake_comfyui_e2e.py` 实现 ComfyUI 的三个端点，但比仓库里
原有的 `tests/fake_comfyui.py` 强：

1. **真的产出媒体字节** —— `/view` 按工作流里的请求尺寸生成真实 PNG，
   所以「卡片显示的实际像素 == 提交尺寸」这类断言才有意义；
2. **真的落盘潜空间** —— 看到工作流里的 `H3RelayLatentSave` 节点，就在
   `<output>/relay_kit/<run_id>/stage_<N>.safetensors` 写文件。没有它，
   服务端的「这一段能不能接续」永远判否，接续链路根本测不了；
3. **可注入故障** —— `/control/drop` 把任务从队列抹掉且永不返回，
   用来复现「ComfyUI 那边任务丢了」；
4. **可回读工作流** —— `/control/workflow/<prompt_id>` 返回原始 prompt
   JSON，用例据此断言「接续段真的挂了 `H3RelayLatentLoad` + `explicit_path`」。

---

## 目录结构

```
tests/e2e/
├── conftest.py              # pytest 装置：服务 / 页面 / 证据 / 素材
├── e2e_lib.py               # 基础设施：证据收集、页面助手、Provider 种子
├── fake_comfyui_e2e.py      # 可控的假 ComfyUI
├── test_image_flow.py       # 组 1：文生图（分辨率 / 批次）
├── test_video_flow.py       # 组 2：视频三模式 + 音轨三选一
├── test_continuation_flow.py# 组 3：接续链路 + 时长计算
├── test_enhance_flow.py     # 组 4：提示词增强
├── test_asset_flow.py       # 组 5：素材库 / 播放器 / 批次入库
└── test_agent_and_status.py # 组 6：Agent 默认参数 / 运行状态一致性
```

---

## 覆盖矩阵

| 用户要求 | 用例 | 文件 |
|---|---|---|
| 1. 文生图分辨率输入框（2560x1400）+ 按模型倍率自动校正 | `test_image_resolution_input_accepts_and_snaps`、`_snapping_respects_model_multiple`、`_submit_sends_snapped_size`、`_output_matches_requested_size` | `test_image_flow.py` |
| 2. 视频面板（文生视频 / 首尾帧 / 参考生视频 + 三选一轨道） | `test_video_t2v_panel_and_submit`、`_flf_panel_has_first_last_slots`、`_r2v_panel_has_three_media_tracks`、`_audio_track_options_depend_on_material`、`_r2v_submit_carries_asset_refs`、`_t2v_size_and_model_controls_present` | `test_video_flow.py` |
| 3. 接续链路（点接续 → 新提示词 → 增强 → 生成）+ 时长计算 | `test_continuation_button_appears_on_done_video`、`_refills_panel_and_clears_prompt`、`_second_segment_total_duration`、`_third_segment_total_duration`、`_workflow_uses_relay_bridge`、`_prompt_is_not_carried_over` | `test_continuation_flow.py` |
| 4. 提示词增强携带此前段落提示词 | `test_enhance_produces_result_in_dialog`、`test_enhance_carries_previous_segment_prompts`（**已知缺陷 DEF-01，strict xfail**） | `test_enhance_flow.py` |
| 5. 素材库（上传 / 标签 / 分析 / 被引用 / 右键入库 / 播放器） | `test_asset_upload_multiple_types_without_transcode`、`_tags_saved_and_filterable`、`_analyze_records_model_result`、`_used_by_generation_panel`、`test_media_right_click_adds_to_asset_library`、`test_player_playlist_from_manual_selection` | `test_asset_flow.py` |
| 6. 媒体库批次 / 非批次 | `test_asset_batch_and_non_batch_media_cards` | `test_asset_flow.py` |
| 7. 内置 Agent 按生成类型配置默认参数 | `test_agent_manage_lists_every_generation_type`、`test_agent_gen_defaults_saved_and_applied` | `test_agent_and_status.py` |
| 8. 卡片「运行中」与 ComfyUI 实际任务一致 | `test_running_card_is_consistent_with_comfyui`、`test_running_truth_state_is_reported`、`test_queue_panel_matches_server_queue` | `test_agent_and_status.py` |
| 9. 播放器模式播放列表 | `test_player_playlist_from_manual_selection` | `test_asset_flow.py` |

---

## 关于「时长计算」的口径（重要）

题面口径是「Σ各段请求时长 −(段数−1)×1 秒（5+5→9 秒，再接 8 秒→16 秒）」。

系统实现是**帧级精确**的：每段时长先向上吸附到 H3 的 **17k+5 帧网格**
（5 秒 → 124 帧 = 5.167 秒，不是 120 帧），接续段再减掉 22 帧钉住区。

所以实测值是：

| 段 | 累计帧数 | 累计秒数 | 题面口径 |
|---|---|---|---|
| 5 + 5 | 226 | **9.417 s** | 9 s |
| 5 + 5 + 8 | 396 | **16.500 s** | 16 s |

差的 0.417 / 0.500 秒全部来自帧网格取整（每段最多 +5 帧 = 0.208 秒）。

**这是真值，不是 bug**：线上真实产物 `v-h3-studio-000451.mp4` 经 `ffprobe`
实测 **226 帧**，与系统显示的 `total_frames` 完全一致 —— 视频文件本身
就是 226 帧、9.417 秒。

因此用例断言的是**帧级精确**（`total_frames` 必须等于按网格算出的值），
秒级则允许「每段 ≤ 0.208 秒」的网格容差。详见
`test_continuation_flow.py:_expected_frames` 的注释。

---

## 依赖

```bash
# playwright + chromium
/home/ubuntu/comfyui/venv/bin/pip install playwright
/home/ubuntu/comfyui/venv/bin/playwright install chromium

# ffmpeg（素材样例 + 视频产物探测）
apt install ffmpeg
```

缺 playwright / chromium 时，用例会 **skip 并明确说明原因** ——
绝不静默降级成接口测试（那样就等于没测）。

---

## 与既有 pytest 套件的关系

```bash
# 只跑 E2E
python3 -m pytest tests/e2e -q

# 全量（既有契约测试 + E2E）
python3 -m pytest tests/ -q
```

E2E 用例带 `@pytest.mark.e2e` 标记，可用 `-m "not e2e"` 排除。

**注意**：E2E 会在隔离实例里真的跑生成任务（假 ComfyUI，不占 GPU），
全量跑会比原来慢几分钟。要在 CI 里排除就用 `-m "not e2e"`。
