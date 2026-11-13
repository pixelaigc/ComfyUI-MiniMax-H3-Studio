# 验收证据索引 — 文生视频接续（t2v）

> 本证据包验收的是「文生视频接续（t2v）」功能本身，不是 H3 视频面板的 7 项修复。

- 分支：`local/feature/t2v-continue`　仓库：`/root/projects/h3studio-t2v-continue`
- 生成时间：2026-11-13T19:07:35+08:00
- 机器可读版：`evidence/acceptance/INDEX.json`

## C1_兄弟分支落盘互不覆盖

**结论**：每个分支一个专属目录（run_id 由该段自己的 job_id 派生），目录不同 -> 结构上不可能互相覆盖

- **兄弟分支**：`evidence/acceptance/INDEX.json` 内同名键
- **全部段**：`evidence/acceptance/INDEX.json` 内同名键

## C2_接续首帧归属

**结论**：C1 首帧来自 B1（正确分支），与 B2 显著拉开

- **灰度MAE_C1首帧_vs_B1末帧**：5.785
- **灰度MAE_C1首帧_vs_B2末帧**：63.988
- **偏离倍数**：11.06
- **缺陷态对照_原始报告**：`evidence/acceptance/INDEX.json` 内同名键
- **执行图级主判据**：`evidence/acceptance/INDEX.json` 内同名键
- **对比图**：`evidence/acceptance/INDEX.json` 内同名键

## C3_链账本不按chain_id线性拼接

**结论**：服务端按 source_job 指针回溯血缘，每条分支只含自己的前序段；B2 不出现在 B1 分支的完整视频里

- **各分支**：`evidence/acceptance/INDEX.json` 内同名键

## C4_内容指纹机制对照nonebot

**结论**：与 nonebot 咏唱 h3 的 _latent_fingerprint 同一口径：文件大小 + 头8MB + 尾8MB 进 md5，取前8位；副本命名 stage_<N>_<指纹>.safetensors；内容变->指纹变->路径变->explicit_path 变->ComfyUI 节点缓存必然失效

- **本项目实现位置**：`evidence/acceptance/INDEX.json` 内同名键
- **nonebot参考实现位置**：`evidence/acceptance/INDEX.json` 内同名键
- **落盘制度差异_说明**：nonebot 是「一部片子一个目录 + 长视频级互斥锁」（链线性）；本项目链允许分叉，互斥锁挡不住「先后两次」的分叉，所以用「每段一个专属目录」= 类型层面的不变量
- **本机实际指纹副本**：`evidence/acceptance/INDEX.json` 内同名键

## C5_两轮真实接续

**结论**：A -> B1 -> C1 -> D1 四段真实跑通；第 1 轮完整视频 = A+B1+C1；第 2 轮对完整视频再接续 = A+B1+C1+D1

- **逐段**：`evidence/acceptance/INDEX.json` 内同名键
- **帧数账**：`evidence/acceptance/INDEX.json` 内同名键

## C6_selflift卡片无接续按钮

**结论**：二采（SelfLift）产物服务端判定 can_continue=false，卡片**不渲染**「接续」按钮；普通 t2v 产物则渲染

- **服务端判定**：`evidence/acceptance/INDEX.json` 内同名键
- **DOM按钮清单**：`evidence/acceptance/INDEX.json` 内同名键
- **截图**：`evidence/acceptance/INDEX.json` 内同名键
- **报告**：/root/projects/h3studio-t2v-continue/evidence/continuation-selflift/report.json

## C7_接续配置区块只能由生成库添加

**结论**：接续上下文区块里**只有「清除」**，没有任何「新增/添加」入口 —— 只能由生成库卡片点「接续」进来；点进来后配置被自动回填，提示词留空给用户写下一段

- **区块实测**：`evidence/acceptance/INDEX.json` 内同名键
- **代码位置**：`evidence/acceptance/INDEX.json` 内同名键
- **截图**：`evidence/acceptance/INDEX.json` 内同名键

## C8_pytest

**结论**：

- **接续相关命令**：cd /root/projects/h3studio-t2v-continue && /home/ubuntu/comfyui/venv/bin/python -m pytest tests/test_continuation_branch.py tests/test_continuation_server.py tests/test_continuation.py tests/test_continuation_retry.py tests/test_continuation_frontend.py tests/test_continuation_mixed_state.py tests/test_continuation_capability.py -q --no-header -p no:cacheprovider
- **接续相关结果**：121 passed in 60.65s (0:01:00)
- **全量命令**：cd /root/projects/h3studio-t2v-continue && /home/ubuntu/comfyui/venv/bin/python -m pytest tests/ -q --no-header -p no:cacheprovider
- **全量结果**：3 failed, 2289 passed, 4 skipped in 1515.35s (0:25:15)
- **全量失败的3项**：`evidence/acceptance/INDEX.json` 内同名键

## C9_浏览器截图与报错统计

**结论**：

- **两档宽度**：`evidence/acceptance/INDEX.json` 内同名键
- **截图目录**：`evidence/acceptance/INDEX.json` 内同名键
- **console_pageError统计**：`evidence/acceptance/INDEX.json` 内同名键
