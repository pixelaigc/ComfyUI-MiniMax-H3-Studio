# Agent 增强修复 —— 验证证据

> 对应分支：`local/feature/agent-enh-fix`
> 基线：`17c9adb`（worktree `/root/projects/h3studio-agent-enh-review-baseline`）
> 采集日期：2026-11-10
>
> **本目录里的每个文件都是真实产出的证据**，可直接打开核对。
> 采集方式：
> - 回归：真实 `python3 -m pytest tests/ -q -rfE`（两个 worktree **串行**跑，互不干扰）
> - 浏览器：真实 Chromium + 真实 `server.py` + 真实 `web/` 前端
>   （`scripts/verify_agent_contract_fix.py`，独立端口 8197 + 独立 data_dir，
>   **不碰 8190 线上服务，不碰任何 systemd unit**）

## 文件清单

| 文件 | 说明 |
|---|---|
| `fix_full_pytest.txt` | **本分支**全量 pytest 原始输出（`-rfE`）。末尾：`27 failed, 803 passed, 3 skipped, 134 errors` |
| `baseline_full_pytest.txt` | **基线 17c9adb**全量 pytest 原始输出（`-rfE`）。末尾：`27 failed, 517 passed, 3 skipped, 134 errors` |
| `fix_failures.txt` | 本分支 161 条 FAILED/ERROR 名单（**排序去重的规范形式**） |
| `baseline_failures.txt` | 基线 161 条 FAILED/ERROR 名单（**同一规范形式**） |
| `regression_md5.txt` | 两份名单的 md5 一致性证据 + 逐条 diff 结论 |
| `browser_contract_evidence.json` | 真实浏览器全量数据：3 档宽度 × 5 场景的 DOM 断言、console 全量输出、横向溢出实测、SQLite 落库、场景 D 的请求体取证 |
| `live_real_task.json` | **真实本地模型 + 真实 ComfyUI** 的完整链路实跑记录：job `08de768731df`（t2i / done / progress 1.0 / 产物 `images/i-zimage-000001.png`，Agent 耗时 32.9 秒），含 jobs 表与会话表落库内容。**这条实跑的 `steps[]` 里已经带 `summary` 与 `call_id`**（字段列表见下） |
| `screenshots/fix-{375,390,1280}-{01-toolchain,02-clarify,03-stream,04-injection,05-oneshot}.png` | 15 张真实渲染截图 |

## 回归结论

两份名单（排序去重后）**逐条一致**：

```
baseline_failures_canonical_md5 = 7aecedc45b683dc99d7d13389e01db0c
fix_failures_canonical_md5      = 7aecedc45b683dc99d7d13389e01db0c
baseline_count = 161
fix_count      = 161
only_in_baseline = 0
only_in_fix      = 0
identical        = True
```

**零新增回归。** passed 从基线 517 → 本分支 803（+286：新增注入防护单测、
跨侧契约测试、媒体语义用例等）。

> **为什么名单用「排序去重」的规范形式**
> pytest 的用例收集顺序在不同运行之间不固定（同一份代码两次跑，
> FAILED/ERROR 名单的**行序**会变）。用原始行序比 md5 会得到假差异。
> 排序去重后是**集合语义**的稳定表示，适合做「逐条 diff + md5」。

## 浏览器场景

| 场景 | 文件 | 断言 |
|---|---|---|
| A 工具链 + 任务卡 | `fix-*-01-toolchain.png` | 工具块「参数」「结果」两行都在；任务卡渲染参数 |
| B 澄清式提问 | `fix-*-02-clarify.png` | DOM 里有澄清 chip + hint + 输入框 |
| C 流式增量 | `fix-*-03-stream.png` | SSE 分块到达（chunkCount ≥ 2）+ 事件顺序 progress→tool_call→done |
| D U+200B 注入 | `fix-*-04-injection.png` | 前端 `injection_detected` + warning；记忆写入是失败态且显示拒绝原因；**模型真实收到的 system 提示词里没有载荷**；**长期记忆库里没有 `injected_pref`** |
| E 一次性降级路径 | `fix-*-05-oneshot.png` | `.ag-tool-result` 节点数 ≥ 1；每个工具块「参数」「结果」两行都有；失败工具显示「未知工具」错误文案；真实后端 `steps[]` 带 `summary` 与 `call_id` |

三档宽度（375 / 390 / 1280）全部：**console 报错 0 条**、**横向溢出 0px**。

## 真实模型 + 真实 ComfyUI 实跑

`live_real_task.json` 是一次**完整链路**实跑：真实本地模型（Qwen 多模态）
理解意图 → 调工具建任务 → **真实 ComfyUI 真的出图** → 产物落库 →
SQLite 有记录。

其中 `agent_response.steps[]` 的字段列表实测为：

```
['args', 'call_id', 'error', 'ms', 'ok', 'step', 'summary', 'tool']
```

—— 即本轮 B 项修复后，一次性 JSON 响应的 `steps[]` **确实带
`summary` 与 `call_id`**（与 `docs/内置多模态Agent-接口契约.md` §1.2.1 一致）。

## 复现方式

```bash
# 回归（两个 worktree 串行跑）
cd /root/projects/h3studio-agent-enh-fix
python3 -m pytest tests/ -q -rfE

cd /root/projects/h3studio-agent-enh-review-baseline
python3 -m pytest tests/ -q -rfE

# 浏览器
cd /root/projects/h3studio-agent-enh-fix
python3 scripts/verify_agent_contract_fix.py /tmp/agent-contract-evidence
```
