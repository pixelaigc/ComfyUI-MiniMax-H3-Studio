# 米哈游 LoRA 增量接入 — 断点续跑检查点

分支：`local/feature/mihoyo-lora`　worktree：`/root/projects/h3studio-mihoyo-lora`
基线：`local/maintainer`（`abfa4bb`）

## 已完成（跳过，不重做）

| 步骤 | 状态 | 产物 |
|---|---|---|
| 画风+场景 45 条落盘 | ✅ | `/mnt/usb2t/comfyui-models/loras/<族目录>/`，SHA256 45/45 逐位一致 |
| `h3studio/lora_catalog_mihoyo.py` 接入 45 条 | ✅ | commit `4674fb0` / `e503c4e` / `14c8225` |
| 角色/机体排除项解析 | ✅ | 角色 5 条（菲比串档 + 4 同名伪装）+ 邻族 10 条 |
| 85 条直链复探 | ✅ | `mihoyo_lora_probe_char_mech.json`：57 匿名可下 / 27 登录墙 / 1 无直链 |

## 待办（按序，每步完成即落盘 + 提交）

- [x] D1 下载 57 条匿名可下条目到族目录 —— ✅ 57/57 成功，8.49 GB，0 失败
- [ ] D2 回平台 API 取权威 SHA256 逐位比对（0 不一致）
- [ ] D3 提取权重头部触发词（ss_tag_frequency / trigger_word / trigger），提取不到标「触发词未标注」
- [ ] D4 生成元数据并接入 `h3studio/lora_catalog_mihoyo.py`（四类合并）
- [ ] D5 更新测试 + 跑全量
- [ ] D6 独立端口实测出图 A/B + API 勾选校验
- [ ] D7 更新中文报告
- [ ] D8 提交 + 汇报

## 抗 429 纪律

外部 API（Civitai / ModelScope / HuggingFace）：指数退避 base 2s / 上限 60s / 最多 5 次 + 请求间节流。
批量结果一律落盘为文件，终端只打印摘要。

## 铁律

- 不重复下载已完成的 45 条
- 不动 8190 线上服务与 systemd unit
- 不合并 local/maintainer
- 禁止 git reset / stash / checkout -- . / force push
