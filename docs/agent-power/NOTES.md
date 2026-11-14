# 内置 Agent 能力增强 —— 持久工作笔记（NOTES）

> 用途：跨上下文窗口续接。**每完成一小步立即回写本文件**。
> 本文件只记「已完成 / 待办 / 关键数字」，证据放 `evidence/`，细节放能力清单文档。

## 仓库坐标（不动他人 WIP）

| 项 | 值 |
|---|---|
| 共享脏树（**禁止碰**） | `/root/projects/comfyui-minimax-h3-studio`（分支 `local/feature/mobile-responsive-fix`，他人 WIP） |
| 本任务 worktree | `/root/projects/h3studio-agent-power` |
| 本任务分支 | `local/feature/agent-power` |
| 基线 | `local/maintainer` @ `f4bc747` |
| 基线对照 worktree | `/tmp/h3s-baseline-verify`（detached @ f4bc747） |
| 线上部署 | `/root/projects/h3studio-live`（8190，systemd `h3-studio.service`）—— **禁止部署/重启** |

禁止动作：`git reset` / `git stash` / `git checkout -- .` / force push / 改 `local/maintainer` / 清理他人改动。

## 行尾约定（改动时必须保持）

- `server.py` / `web/index.html` / `web/style.css` / `web/app.js` = **CRLF**
- `h3studio/agent.py` / `h3studio/workflows.py` = **LF**

## 本轮（收尾轮）起点

分支 HEAD = `2037d89`（7 个 commit 已落地），工作树干净。

已落地 commit：

```
2037d89 docs(agent): 能力清单更新最终回归数字 + 补「本次引入并修掉的两个缺陷」取证表
d1a5f78 test(agent): 锁死「JS 注释里不得出现块注释定界符」
25e8189 fix(agent.js): 注释里的 "image/*" 打开了假块注释，把后半份 agent.js 全部吞掉
66162ad docs(agent): 能力清单补「假缺节点」缺陷修复记录 + 全量回归基线对比取证
e1912ad fix(server): 节点探测不完整时不得断言「本机缺节点」+ Agent 建视频任务强制刷新节点清单
6e43bbf test(agent): 补「粘贴支持音视频」断言
284bd64 feat(agent): 内置 Agent 全面增强 —— 50 步上限 / ffmpeg 音视频 / 剪贴板粘贴 / 可配置额外能力与搜索引擎 / t2v 默认参数
```

## 已完成清单（上一轮）

- [x] **1) 工具调用步数上限 8 -> 50**：`h3studio/agent.py` `MAX_STEPS 8->50`、
      `MAX_TOOL_CALLS 12->120`、`TOTAL_TIMEOUT 420->2400s`；前后端一致（管理面板 1~50 可调，硬顶 50）。
      验证：12 轮工具调用（>8）在 max_steps=50 下 `completed`；负控 max_steps=8 下 `truncated`。
- [x] **2) ffmpeg 音视频工具**（真实调用本机 ffmpeg 7.1.1）：`h3studio/agent_media_tools.py`
      提供 probe / concat / transcode / extract_frames / mix_audio / merge_av 六个工具，
      输入只接受 asset_id（不拼 shell），产物登记进素材库。
- [x] **3) 剪贴板粘贴媒体**：`web/agent.js` 在 `#agInput` 上处理
      `clipboardData.items` 的 file 项 + `clipboardData.files` + 图片 data-URL 兜底；
      `kindForFile` 共用，认 image/audio/video。
- [x] **4) 可配置额外能力 + 搜索引擎**：`h3studio/agent_settings.py`（配置单一来源，SQLite 持久化）、
      `h3studio/agent_websearch.py`（DDG / 9898 OpenWebUI 聚合 / 自定义 HTTP）、`h3studio/agent_codeexec.py`（Python 子进程执行，默认禁用）、
      `h3studio/agent_extra_tools.py`（9 个工具注册 + 能力->工具映射）；
      `web/agent-manage.js` 管理面板：步数上限 / 4 个能力开关 / 搜索引擎 CRUD / 连通性自检 / t2v 默认参数可改；
      服务端 `GET/PUT /api/agent/settings`、`POST /api/agent/settings/test-engine`。
- [x] **5) t2v 默认参数**：attention=`kitchen`、accel_lora=`dmad_4step_critic`、模型 `fl2va_pruned_int8`、
      16:9 / 0.4MP / 5s，全部可在管理面板改（`_merge_t2v_defaults`）。
      验证：生成的工作流含 `ModelAttentionBackend{comfy kitchen attention}` 与
      `MiniMaxH3TurboLoRA(dmad_minimax_h3_4step_full_critic_comfyui.safetensors)`。
- [x] **6) 能力清单文档**：`docs/内置Agent能力清单与配置说明.md`（32 工具全表 + 配置说明 + 证据 + 已知边界）。

## 关键数字（已实测）

| 项 | 值 |
|---|---|
| Agent 工具总数 | 32（启用 31，代码执行默认禁用） |
| 步数上限 | 50（上限硬顶，可调 1~50） |
| 总时限 | 2400s |
| 全量回归（改动树） | `3 failed, 2538 passed, 4 skipped` |
| 全量回归（基线同批文件） | 失败更多（18 > 3） |
| agent 相关测试 | 784 passed（上一轮记录） |
| `tests/test_agent_power.py` | 45 passed |
| `tests/test_agent_paste_browser.py` | 14 passed（含真 Chromium 粘贴 PNG 入库断言） |
| ffmpeg | `/usr/bin/ffmpeg` 7.1.1，`/usr/bin/ffprobe` |
| 9898 聚合引擎 | `http://127.0.0.1:9898`（LAN `http://192.168.3.78:9898`） |
| DDG 直连 | **本机被 anomaly challenge 拦截**（HTTP 202）—— 可用路径是「经 9898 的 DDG 路由」 |
| Qwen 桥 | `127.0.0.1:5000`，模型 `qwen3.8-flash-next-iq3_xxs` |

## 本轮待办（收尾）

- [x] **T1 节点探测三态语义判定 + 负向对照** —— 已完成（commit `a0be5d1`）
      **判定结论：新语义（三态）正确且更安全。**
      旧语义有**真实触发路径**：内置 Agent 不走面板（不调 /api/video-options），
      首次进来 `_COMFY_NODE_NAMES` 是空集 -> 旧判据 `node not in _COMFY_NODE_NAMES`
      报「本机 ComfyUI 里没有节点 ModelAttentionBackend」，
      而该节点**实测恒在**（`curl /object_info/ModelAttentionBackend` -> HTTP 200）。
      新语义**没有放松**「真缺节点」的拒绝路径 —— M3/M4 变异证明。
      证据：`docs/agent-power/evidence/T1_mutation_node_probe.txt`（5/5 变异全被抓住）
            `docs/agent-power/evidence/T1_three_state_tests.txt`（108 passed）
      本轮补的测试（4 条）：
        1. `test_allows_twopass_when_probe_incomplete` —— twopass 侧负控（原来**没有**）
        2. `test_rejects_attention_without_node_even_when_other_nodes_present` —— 正控加强
        3. `test_rejects_twopass_without_node_even_when_other_nodes_present` —— 正控加强
        4. `tests/test_node_probe_completeness.py`（新增 6 条）—— 探测定律**直接**测试
      **M5 变异暴露的真实缺口**：`_COMFY_PROBE_COMPLETE` 的**计算逻辑**
      从来没被直接测过（三态断言全靠手动赋值绕过）-> 标志恒为 False 时
      三态退化成「永远放行」，真缺节点也不再拒绝 = 静默失效。
      新文件用**可控的假 ComfyUI**（真 HTTP / 真超时）补上这一层。

- [x] **T2 完整 agent 回归 + 基线差集** —— 已完成
      改动树 `901 passed, 0 failed`；基线树 `845 passed, 0 failed`；
      **新增失败差集 = 0**。
      通过数差 56 逐项可解释：845 -> 850（`test_agent.py` +1 步数断言、
      `test_t2v_panel.py` +4）+ 新增 2 文件 51 条 = 901。
      证据：`docs/agent-power/evidence/T2_changed_tree.txt`
            `docs/agent-power/evidence/T2_baseline_tree.txt`
            `docs/agent-power/evidence/T2_diff_summary.txt`
      回归脚本：`scripts/run_agent_regression.sh`（**两边跑同一份清单**，
      基线不存在的文件显式 SKIP 并打印 -> 差集 apples-to-apples）

- [ ] **T3 六项目标逐项重跑真实证据**，落盘 `docs/agent-power/evidence/`
- [ ] **T4 能力清单文档补齐验证证据 + 已知边界**
- [ ] **T5 提交 + 给绝对路径清单**（不签验收结论）

## 本轮踩坑（不要再踩）

1. **变异脚本会原地改写 `server.py`** -> 不得与测试**并发**跑在同一棵树上。
   本轮第一次跑新测试时 3 条**假红**，原因就是另一个
   `mutation_check_capability.py` 在后台同时改同一个文件，
   测试读到了变异中的代码。单独跑即 6 passed。
2. **基线树没有本分支新增的测试文件** -> 直接跑会报 "file not found"
   （不是失败，是不存在）。脚本显式剔除并打印 SKIP。
3. Studio plan MCP 工具仍命中 harness `item` 注入 bug
   -> 用 `HERMES_PROFILE=coding /root/studio_plan.py <context_id> "<说明>" <plan.json>`
   （注意 profile 必须是 **coding**，默认的 research token 已失效）。

## 已知边界（沿用上一轮，需在文档复核）

- 步数上限 50 是**硬顶**，管理面板只能调低不能调高。
- 搜索引擎自检在 9898 不可达时 **skip** 而不是判红（外部服务，不是本仓库 bug）。
- 管理面板的搜索引擎类型下拉只列 3 种；新增类型要改 `h3studio/agent_settings.py::SEARCH_ENGINE_TYPES` + 前端下拉。
- DDG 直连在本机被 anomaly challenge 拦截 -> 默认走「经 9898 的 DDG 路由」。
- 全量套件 3 条基线既有失败（不在本次审查目标范围，按 `agent-minimal-change` 不擅修）。

## 取证纪律（沿用）

- 区分「设计意图」与「实际证据」；行为变更必须有**负控/变异**证据。
- 断言不可自指（不能拿被测函数自己的输出算期望值）。
- 测量点要挑常量巧合抓不到的位置。
- 基线对照必须用**独立 worktree + 同一批测试文件**。
