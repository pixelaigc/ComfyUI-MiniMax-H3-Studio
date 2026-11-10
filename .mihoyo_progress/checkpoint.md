# 米哈游 LoRA 任务 —— 断点续跑检查点

> 分支：`local/feature/mihoyo-lora`　基线：`local/maintainer`（`abfa4bb`）
> worktree：`/root/projects/h3studio-mihoyo-lora`
> 线上 worktree `/root/projects/h3studio-live` 与 8190 服务**全程未动**。

## 已完成

- [x] **D0** 画风 + 场景首批 45 条下载（9.22 GB），SHA256 与平台权威值逐位一致
- [x] **D1** 角色 / 机体 57 条下载（8.49 GB），57/57 成功，0 失败
      - 首次跑因 429 中断留下 4 个 `.part`；重跑（幂等）后全部完成
- [x] **D2** 触发词提取：读权重头部 `ss_tag_frequency` / `trigger_word`
- [x] **D3** 元数据生成 + 接入「可选效果 LoRA」列表（99 条：Z35 / Q19 / M45）
- [x] **D4** 集成改动提交（`d9dbff8` / `624ce29` / `e62fe33`）
- [x] **D5** 测试：首批 25 项 + 增量 24 项 = 49 passed
- [x] **D6** SHA256 权威核验：Civitai by-hash 反查 36 条逐位一致、0 不一致
- [x] **D7** 实测生成（Z-Image 族）：界面勾选 + ComfyUI 三层证据 + 同 seed 像素对照
- [x] **D8** 中文报告产出

## 下载落盘

| 批次 | 条数 | 体积 | 状态 |
|---|---|---|---|
| 画风 + 场景 | 45 | 9.22 GB | ✅ |
| 角色 + 机体 | 57 | 8.49 GB | ✅（2 条后因基座不符移出，见下） |
| 合计落盘 | 102 | ~17.7 GB | ✅ |

目录收录 99 条（首批 45 + 增量 54）：

- 增量批次里 1 条 `firefly26710_Moss` 首批已收录 → 去重
- 2 条基座实为 Qwen-Image 1 代 → 移至 `qwen1-effects/`，不进目录

## 未能下载（28 条，如实列出）

- 27 条 Civitai 登录墙（直链返回 `text/html` 登录页）
- 1 条无直链（`CIV-2009185 qwen ronova`）

## 实测中发现的 4 个真问题（已修）

1. **基座误标**：2 条 Qwen 权重元数据自报 `ss_base_model_version=qwen_image`（1 代），
   层名 `add_q_proj`，2.1 底模是 `to_q`，注意力层仅 128/240 命中 → 移出 2.1 族目录
2. **清单 copyright 写错**：11 条鸣潮（库洛游戏 IP）被写成「角色 IP 归米哈游所有」
   → 改为按游戏本体判定
3. **清单 strength 栏填触发词串**：13 条（如 `` `Cissia (zenless zone zero)` ``）
   → 排除后按「作者未给区间」处理，避免解析出 3.0 这种假强度
4. **版本号污染**：`Moss_danica` 的 strength 是 `v2.0 = 1.0；v1.0 = 0.6–0.8`，
   直接平均得 1.5 → 改为按版本号取最高版本对应档位（1.0）

## 抗 429 纪律

- 外部 API 全部串行 + 指数退避（base 2s / 上限 60s / 最多 5 次）
- 遇 429 不硬刚：立即落盘已完成进度并停该步
- 每完成一个原子步骤即 git commit

## 待办

- [ ] 补测 Qwen-Image 2.1 / MOSS-Image 两族的实测生成（进行中）
- [ ] 报告补上两族证据后定稿
