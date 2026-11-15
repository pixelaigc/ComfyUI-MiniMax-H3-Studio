# 修复④ 交付报告 — minimax-h3 稀疏视频注意力 FinalLayer.forward() 参数不匹配

- **日期**：2026-11-15
- **缺陷编号**：修复④
- **仓库**：`/root/projects/comfyui-minimax-h3-studio`（工作台，本轮**无需代码改动**，见 §6）
- **缺陷实际所在仓库**：`/home/ubuntu/comfyui/custom_nodes/H3-Optimizations`（第三方插件，本机版本 0.2.45 / commit `8627749`）
- **开发 worktree / 分支**：
  - 插件：`/root/projects/h3opt-final-layer-fix`，分支 `local/feature/h3-final-layer-compat`（基于 `8627749`）
  - 工作台：`/root/projects/h3studio-fix4`，分支 `local/feature/h3-final-layer-compat`（基于 `262fd9b`）
- **线上部署**：ComfyUI `comfyui.service`（端口 8188）+ 工作台 `h3-studio.service`（端口 8190）

---

## 1. 用户可见症状

文生视频面板把「注意力机制」选成 **稀疏视频注意力（预算制）** 时必崩：

```
TypeError: FinalLayer.forward() missing 3 required positional arguments: 'sigma', 'sample_sigmas', and 'grids'
```

其它注意力档位（sageattention / sol-attn / comfy kitchen / pytorch）正常。

---

## 2. 根因（现场代码核实，文件:行号）

### 2.1 根因不在工作台，在 H3-Optimizations 插件的 FinalLayer 兼容层

| 位置 | 内容 |
|---|---|
| `custom_nodes/H3-Optimizations/h3_optimizations/memory/final_layer.py:19`（修复前） | `_CURRENT_FORWARD_PARAMETERS = frozenset({'sigma', 'sample_sigmas', 'shifts'})` |
| 同文件 `:131-137`（修复前） | `_accepts_current_contract()`：只有当签名里**同时**出现 `sigma` / `sample_sigmas` / `shifts` 才判定为「当前契约」 |
| 同文件 `:140-161`（修复前） | `_call_original()`：`current_contract` 为假时走 `return original_forward(x, t_emb, video_seg, audio_seg)` —— **只传 4 个参数** |
| `comfy/ldm/minimax/model.py:362`（ComfyUI 0.45.3） | `def forward(self, x, t_emb, video_seg, audio_seg, sigma, sample_sigmas, grids):` |
| `comfy/ldm/minimax/model.py:834-835` | `grids = (pdd_grid(...), pdd_grid(...))`；`v, a = self.final_layer(h, t_emb, video_seg, audio_seg, sigma_v, transformer_options.get("sample_sigmas"), grids)` |

**因果链**：

1. ComfyUI 在 0.45.3 把 `FinalLayer.forward` 的**第 7 个位置参数从 `shifts` 改名成 `grids`**（`shifts` = 两个 flow shift 浮点；`grids` = 每条流一条 `pdd_steps+1` 点的 sigma grid）。参数**个数没变（7 个）**，只是名字变了。
2. 插件的契约探测集合把名字**写死**成 `shifts` → 在 0.45.3 上探测不到 → 判定「这是旧四参数契约」。
3. 于是 `_call_original()` 只传 4 个参数调用上游 → 上游要 7 个 → `TypeError`。

### 2.2 为什么只有「稀疏视频注意力」会炸

稀疏档位是**唯一**会经过 FinalLayer 包装器的档位：

```
H3SparseAttention（工作台稀疏档插入的节点，h3studio/video_models.py:591）
  → apply_plan()
  → apply.py:1531-1534   if plan.sparse is not None: install_cube_order(patched, cube_shape)
  → cube_order.py:406-410  final_layer.install(model_patcher, chunk_rows, cube_state=state)
  → memory/final_layer.py:335-360  装上 FinalLayer 包装器
  → ComfyUI 调用包装器（7 个位置参数）
  → 包装器按「旧契约」只传 4 个 → TypeError
```

其余档位：

| 档位 | 插入节点 | 是否经 FinalLayer 包装器 | 结果 |
|---|---|---|---|
| 稀疏视频注意力 | `H3SparseAttention` | **是**（装 cube-order + FinalLayer patch） | **崩** |
| SageAttention | `MiniMaxH3MemoryEfficientSageAttentionPatch` | 否 | 正常 |
| Sol-Attn | `MiniMaxH3MemoryEfficientSolAttentionPatch` | 否 | 正常 |
| Comfy Kitchen | `ModelAttentionBackend` | 否 | 正常 |
| 默认（PyTorch） | 不插节点 | 否 | 正常 |

### 2.3 这是一个「升级 ComfyUI 引入、但此前未被发现」的潜在雷

工作台仓库 `evidence/h3image-upgrade-20261114/交付说明-H3-Image出图能力上线.md` §9 早已记录同一根因，
但当时的结论是「本工作台不受影响」（因为当时用的是 `MiniMaxH3ImageToVideo` / `MiniMaxH3SigmaShift`，
**没挂 `H3SparseAttention`**）。本轮用户实际在面板上选到稀疏档位，把它变成了**现网故障**。

上游 `Zironic/H3-Optimizations` 最新 `f195ca4`（0.3.0）**仍未适配 `grids`**
（源码里还是 `shifts`），所以「升级到上游最新版」不能解决，必须自己打补丁。

### 2.4 现场实测复现（真实上游签名，不是 mock）

`evidence-fix4/real_upstream_contract_check.log`：

```
params: ['self', 'x', 't_emb', 'video_seg', 'audio_seg', 'sigma', 'sample_sigmas', 'grids']
'shifts' present: False
'grids' present: True

pre-fix _CURRENT_FORWARD_PARAMETERS = ['sample_sigmas', 'shifts', 'sigma']
pre-fix accepts_current_contract(live) = False
-> pre-fix code takes the 4-argument branch ...
   reproduced -> TypeError: FinalLayer.forward() missing 4 required positional arguments:
                  'audio_seg', 'sigma', 'sample_sigmas', and 'grids'
```

---

## 3. 修复内容

全部改在插件仓 `local/feature/h3-final-layer-compat` 分支，**工作台无需任何代码改动**。

### 3.1 契约探测不再依赖参数名（`final_layer.py:35`）

```python
_CURRENT_FORWARD_PARAMETERS = frozenset({'sigma', 'sample_sigmas'})
```

只保留**跨版本没改过**的两个名字。用途从「决定传哪几个参数」降级为
「判断上游签名有没有尾部参数」。

### 3.2 参数名在调用时从**绑定的真实签名**里解析（`final_layer.py:278-353`）

```python
_TRAILING_ARGUMENT_ALIASES = {
    'sigma': 'sigma',
    'sample_sigmas': 'sample_sigmas',
    'shifts': 'trailing',     # ComfyUI <= 0.44
    'grids': 'trailing',      # ComfyUI >= 0.45.3
}
_LEADING_ARGUMENT_COUNT = 4
```

- `_resolve_trailing_arguments()`（`:290`）读 `inspect.signature(original_forward)`，
  把「前 4 个前导参数 + 其余尾部参数」按**位置**切分（前导参数**不按名字**跳，
  否则像 `_t_emb` 这种写法会串位），再按别名表把值映射到上游真实参数名。
- 未知的新名字（如上游将来再改名）落回 `trailing` 兜底 → **不丢参数、不改 arity**。
- 签名不可读时（`TypeError` / `ValueError`）退回历史位置传参行为。

### 3.3 `_call_original()` 改为按名字派发（`final_layer.py:356-379`）

```python
if not current_contract:
    return original_forward(x, t_emb, video_seg, audio_seg)
resolved = _resolve_trailing_arguments(original_forward, sigma, sample_sigmas, shifts)
return original_forward(x, t_emb, video_seg, audio_seg, **resolved)
```

### 3.4 包装层同时接受 `shifts` / `grids` 两个关键字（`final_layer.py:410-430`）

```python
def forward(x, t_emb, video_seg, audio_seg,
            sigma=None, sample_sigmas=None, shifts=None, grids=None, **kwargs):
    trailing = shifts if shifts is not None else grids
```

调用方无论说哪一方言都不会被 `unexpected keyword argument` 拒掉。

### 3.5 chunked 路径补上 `grids` 语义 + 修正 PDD head 数

| 函数 | 位置 | 作用 |
|---|---|---|
| `_pdd_head_count()` | `:46` | ComfyUI 0.45.3 把烘焙的 PDD head bank 按**全宽**建（`Linear(hidden, video_dim * pdd_steps)`）并记录 `FinalLayer.video_dim`；旧版本是长过 `out_features`。按 `video_dim` 取才对，否则 head 数算错、PDD 混合区间全错 |
| `_pdd_grid_indices()` | `:63` | 按上游 `grids[0]`（递减 sigma grid）定位本步消费的 head 区间 |
| `_is_sigma_grid()` | `:85` | 区分两种方言：`shifts` 是 float 对，`grids` 是 grid 序列 —— 判别无歧义 |
| `_pdd_head_from_grid()` | `:140` | 按上游 grid 的 dt 加权混合（0.45.3 公式），旧的 `_pdd_head()`（flow shift 公式）保留给旧方言 |
| `chunked_final_layer()` | `:168` | 参数名从 `shifts` 改成中性的 `trailing`，按 `_is_sigma_grid()` 分派到两套数学 |

---

## 4. 测试结果

### 4.1 新增回归测试

| 文件 | 用例数 | 覆盖 |
|---|---|---|
| `tests/test_final_layer_contract.py` | 14 | 契约探测、别名映射、按位置跳前导参数、关键字兜底、未知名字兜底、grids/shifts 双方言 |
| `tests/test_sparse_final_layer_e2e.py` | 5 | 走**真实安装链路**（`install_cube_order` → `final_layer.install` → `ModelPatcher` 派发）驱动**真实上游** `FinalLayer`（grids 契约），复现用户报的 TypeError |

**修复前 / 修复后对照**（同一套新测试）：

```
# 修复前（安装目录 0.2.45 原始代码）
12 failed, 7 passed

# 修复后
19 passed
```

即：**新测试在缺陷代码上确实红，在修复代码上确实绿**。

### 4.2 插件全量测试对跑（同一 GPU、同一命令）

```
CUDA_VISIBLE_DEVICES=1 PYTHONPATH=/home/ubuntu/comfyui python -m pytest tests/ -q

基线（安装目录 0.2.45 原始代码）  : 802 passed, 3 failed, 9 skipped
修复后                             : 816 passed, 3 failed, 9 skipped
```

**失败集完全相同**，且与本轮改动无关：

| 失败用例 | 性质 |
|---|---|
| `test_embedding_memory.py::test_real_upstream_auto_policy_matches_supported_comfy_generation` | 既存失败（上游 `_forward` 源码摘要不在插件的白名单里） |
| `test_triton_bf16_inplace_gpu.py::test_dense_partial_slab_matches_separate_output_bitwise` | GPU 显存竞争导致的 OOM flake |
| `test_triton_bf16_inplace_gpu.py::test_dense_to_sparse_boundary_matches_separate_output_bitwise` | 同上 |

> 注：在 GPU0（被 Strata 占满 46.7/49 GB）上跑会额外出现 `no CUDA device` 类失败，
> 这是**资源**问题不是回归 —— 换到 GPU1 后基线与修复后的失败集完全一致。

### 4.3 变异验证（确认测试能抓住回归）

脚本：`evidence-fix4/mutation_verify.py`，日志：`evidence-fix4/mutation_verify.log`

```
total=6 caught=6 survived=0 skipped=0
All applied mutations were caught by the regression suite.
```

| 变异 | 结果 | 抓住它的测试 |
|---|---|---|
| 契约探测又写死回 `shifts` | CAUGHT | `test_final_layer_contract.py::FinalLayerContractTests` |
| 尾部参数被丢弃（退回 4 参数调用） | CAUGHT | `test_final_layer.py` + `test_final_layer_contract.py` |
| 删掉 `grids` 别名 | CAUGHT | `test_final_layer_contract.py::FinalLayerContractTests` |
| 包装层不再接受 `grids` 关键字 | CAUGHT | `test_final_layer.py` + `test_final_layer_contract.py` |
| 包装层优先取 `shifts`（错误方言胜出） | CAUGHT | `test_final_layer_contract.py::FinalLayerContractTests` |
| grid 方言被误判成 shift 方言 | CAUGHT | `test_final_layer.py::FinalLayerTests` |

> **第一轮变异验证发现了一个真实的覆盖漏洞**：「删掉 `grids` 别名」这个变异
> **存活**了 —— 因为「未知名字兜底」正好把它的行为掩盖了。已补
> `test_grids_alias_is_explicitly_mapped_to_the_trailing_value` 等 5 条
> 直接锁定别名表的用例，重跑后 6/6 全部被抓。

### 4.4 真实上游契约核对（不是 mock）

脚本：`evidence-fix4/real_upstream_contract_check.py`，日志：`evidence-fix4/real_upstream_contract_check.log`

```
fixed accepts_current_contract(live) = True
resolved trailing mapping: {'sigma': 'Tensor', 'sample_sigmas': 'Tensor', 'grids': 'tuple/list'}

calling non-chunked wrapper with the live 7-arg contract ...
  -> OK, output shapes: [(10, 4), (10, 2)]
  -> matches stock upstream: True

calling chunked wrapper with the live 7-arg contract ...
  -> OK, output shapes: [(10, 4), (10, 2)]
  -> matches stock upstream: True
```

额外核对了 **H3-Image 路径**（`audio_out=None`、只有一条 grid，模型文件 `model.py:909`）：
`OK, outputs: [(32, 4)]`，`matches stock upstream: True`。

---

## 5. 真机实测（8190 各注意力档位）

脚本：`evidence-fix4/live_attention_matrix.py`
结果：`evidence-fix4/live_attention_matrix_results.json`
逐档位 ComfyUI 日志：`evidence-fix4/comfyui-log-<mode>.log`

<!--LIVE_TABLE-->

---

## 6. 工作台侧为什么不需要改代码

- 面板的注意力清单与节点映射在 `h3studio/video_models.py:543-600`
  （`VIDEO_ATTENTION_MODES`），稀疏档映射到 `H3SparseAttention`（`:591`）—— **映射本身是对的**。
- 插入节点的逻辑在 `h3studio/video_workflow.py:161-179`（`_apply_attention`）—— **是对的**。
- 崩溃发生在 **插件内部**（`final_layer.py` 的兼容层），工作台只是把 ComfyUI 的
  真实报错透传出来。所以修复点唯一，就是插件。

---

## 7. 遗留 / 未修完项

| 项 | 状态 |
|---|---|
| 上游 `Zironic/H3-Optimizations` 仍未适配 `grids` | **未修**（第三方上游仓库，本轮打的是本机补丁；建议把本补丁 PR 回上游） |
| `h3_optimizations/memory/embedding.py:243-246` 里的 `final_layer` 调用仍写死 `(shift_v, shift_a)` | **未修**：该文件属于「embedding memory release」实验路径，本机未启用（`UPSTREAM_FORWARDS` 白名单只覆盖 0.34/0.35，当前 0.45.3 直接拒绝安装），且它调用的是 `model.final_layer` 而非包装器，不在本次崩溃链路上 |
| `h3_optimizations/memory/embedding.py` 的 `UPSTREAM_FORWARDS` 白名单不含 0.45.3 | **未修**（既存问题，与本缺陷无关） |
| ComfyUI 侧 `MiniMaxH3ImageModel`（H3-Image）也用 grids | **已覆盖**：本补丁对 grids/shifts 双方言都成立，已实测 |

---

## 8. 证据索引

| 路径 | 内容 |
|---|---|
| `evidence-fix4/mutation_verify.py` / `.log` | 变异验证脚本与日志（6/6 caught） |
| `evidence-fix4/real_upstream_contract_check.py` / `.log` | 真实上游签名核对（修复前复现 TypeError / 修复后对齐 stock） |
| `evidence-fix4/live_attention_matrix.py` | 8190 各注意力档位真机实测脚本 |
| `evidence-fix4/live_attention_matrix_results.json` | 各档位实测结果 |
| `evidence-fix4/comfyui-log-*.log` | 各档位运行时 ComfyUI 日志 |
| `evidence-fix4/restart_comfyui_and_probe.sh` | 部署后重启 + 探活脚本 |
| `evidence-fix4/final_layer.py.installed-backup` | 部署前的插件原文件备份（可回滚） |

插件仓提交：

```
e76fff8 fix(final-layer): 支持 ComfyUI 0.45.3 的 grids 契约，修掉稀疏注意力 TypeError
8c95c5e test(final-layer): 补稀疏注意力端到端回归（真实上游签名 + 真实安装路径）
```
