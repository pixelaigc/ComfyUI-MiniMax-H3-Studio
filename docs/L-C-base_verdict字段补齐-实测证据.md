# L-C 实测证据：`base_verdict` 字段补齐

生成时间：2026-11-15
分支：local/feature/legacy-lo-cleanup

---

## 1. 定位（缺失的响应 + 文件/行号证据）

上一轮遗留项原文（`docs/内置多模态Agent-增强汇报-20261115.md`）：

> **`base_verdict` 目录字段未改**：84 条条目的目录里仍没有 `base_verdict`。
> 本轮改的是**前端兜底**（不再伪装成「原生」），没有去补目录数据。

上一轮验收报告（`docs/独立复验报告-deeff606-c1至c8与线上ABCD.md` L-6 行）：

> `cleanup_stale_derived()` 无调用点、`base_verdict` 目录字段未补

### 实际缺字段的位置

`base_verdict` 字段由**两条 payload 构造处**产出（`h3studio/loras.py`）：

| 位置 | 函数 | 改前 | 改后 |
|---|---|---|---|
| `h3studio/loras.py:1357` | `_list_loras_for_base()`（Qwen / MOSS 共用） | `"base_verdict": info.get("base_verdict", "")` | `"base_verdict": _resolve_base_verdict(info, compat)` |
| `h3studio/loras.py:1617` | `list_z_image_loras()` | `"base_verdict": info.get("base_verdict", "")` | `"base_verdict": _resolve_base_verdict(info, compat)` |

改前两处都**只从目录取**，目录没登记就是空串 → 接口下发的
`base_verdict` 有 **197 / 143 条为空**。

**前端消费点**（`web/app.js`）：

- `web/app.js:1165` —— 列表标签：`meta.base_verdict === "近似适用"` 才打「近似适用」标签
- `web/app.js:1338-1342` —— 详情面板「基座适用性」行：
  空值时显示「未标注（本机实测可用，但上游目录未登记基座判定结论）」

### 关于「预热/健康检查响应」

核实结论：仓库里**没有**名为「预热」或「健康检查」的 HTTP 端点。
`scripts/prewarm_moss_lora.py` 是**离线脚本**（产出 `/tmp/prewarm-moss/prewarm_moss_lora.json`），
不是服务路由；`/api/system`（`server.py:5233`）是队列/硬件状态，不含 LoRA 判定。

**base_verdict 缺失的真实落点就是上面那两处 payload 构造** —— 它们是
`/api/image-loras`（清单接口）以及 `/api/video-effect-loras`（视频效果 LoRA 清单）
里 LoRA 条目的 `base_verdict` 字段来源。本轮按这两处补齐。

---

## 2. 补法（按既有判定语义，绝不新增常量为 true 的假值）

新增 `h3studio/loras.py::_resolve_base_verdict(info, compat)`，
判定**完全落在既有三条实测结论**上：`compatible` / `attention_hit` / `conversion_status`。

取值（`h3studio/loras.py:1093-1130` 附近）：

| 取值 | 触发条件 |
|---|---|
| `不可用` | `conversion_status == "broken"`（文件本体损坏） |
| `不适配` | `compatible == false`，或 `attention_hit < 1.0`（半静默失效） |
| `原生` / `近似适用` | 目录登记过，**且**本机实测判定为可用 |
| `已适配` | 目录没登记，但本机实测判定为可用 |

### 优先级规则（关键）

1. **实测优先于目录**：`broken` / `compatible=false` 先判 → 直接给「不可用」/「不适配」，
   **即使目录登记了「原生」也不采信**（实测坏掉的不能写成「原生」）。
2. 实测可用时，目录登记过的结论（原生 / 近似适用）**保留**（它带上游声明，信息量更大）。
3. 目录没登记 → 按实测如实标「已适配」，不再一律「未标注」。

### 前端同步

`web/app.js:1338-1346` —— 详情面板区分三档：

- `近似适用` → 「⚠️ 近似适用（非该基座原生训练，依据见下方说明）」
- `不适配` → 「⚠️ 不适配（本机实测与当前底模结构对不上，已排除在可选列表之外）」
- `不可用` → 「⚠️ 不可用（权重文件本体损坏，无法使用）」
- `原生` / `已适配` → 如实显示

**未改动**前端「空 base_verdict 不得兜底成原生」那条纪律
（`tests/test_d11_defect_a_conversion.py::test_frontend_does_not_fake_native_verdict`
仍然要求 `meta.base_verdict || "原生"` 不存在，且「未标注……」文案仍在）——
本轮只是**服务端不再下发空值**，前端兜底分支保留作为防御。

---

## 3. 实测证据

### 3.1 真实请求接口（`scripts/verify_labc_base_verdict.py`）

实例：独立端口 8199（跑本分支代码），`http://127.0.0.1:8199/api/image-loras?base=...`
证据：`/tmp/labc_base_verdict_8199.json`

| base | available | incompatible | base_verdict 分布 | **空值数** |
|---|---|---|---|---|
| Z-Image | 195 | 0 | 已适配 112 / 原生 77 / 近似适用 6 | **0** |
| Qwen-Image 2.1 | 67 | 0 | 已适配 53 / 原生 14 | **0** |
| MOSS-Image | 139 | 0 | 已适配 85 / 原生 52 / 近似适用 2 | **0** |

### 3.2 三类真实输入 → 三类结论（证明非常量）

| 类别 | 真实输入 | `conversion_status` | `compatible` | `base_verdict` |
|---|---|---|---|---|
| **可适配** | `Kook_Zimage_如梦似幻.safetensors`（Z-Image，目录没登记） | — | true | `已适配` |
| **可适配** | `moss/moss_hires_lora_v0.1_qkv_split.safetensors`（MOSS，direct） | `direct` | true | `已适配` |
| **不可适配** | `compatible=false` 的条目（实测不命中底模） | — | false | `不适配` |
| **broken** | 人为截断副本（真实 25,529,772 B / 声明 51,059,544 B） | `broken` | false | **`不可用`** |

### 3.3 不变量（脚本自动核对）

```json
"invariants": {
  "total_items": 401,
  "empty_base_verdict": 0,
  "declared_native": 143,
  "declared_near": 8,
  "derived_verified": 250,
  "violations": [],
  "verdict_universe": ["不可用", "原生", "已适配", "近似适用"],
  "verdict_universe_size": 4,
  "not_a_constant": true
}
```

- `empty_base_verdict = 0` —— 401 条**一条都不空**
- `violations = []` —— 没有「broken 却没标不可用」「compatible=false 却标原生」这类矛盾
- `verdict_universe_size = 4` —— 判定**随真实状态变化**，不是常量

### 3.4 单测（`tests/test_labc_base_verdict.py`，10 条全绿）

```
10 passed in 0.04s
```

覆盖：六种输入都非空 / 可适配映射 / 不可适配映射 / broken 映射 /
**broken 压过目录「原生」** / 目录结论被保留 / 部分命中判不适配 /
判定是纯函数 / **三类输入三类结论** / payload 必须走 `_resolve_base_verdict`
且不得再直接取目录值。

### 3.5 fail-closed 逻辑未被破坏

`broken → compatible=false` 保持：人为截断文件仍判
`compatible=false` + `attention_hit=0.0` + 如实原因
「权重文件被截断（实际 25,529,772 字节，头部声明需要 51,059,544 字节，缺 25,529,772 字节）」。

---

## 4. 已知限制

1. **`不适配` / `不可用` 两档在当前线上列表里计数为 0** —— 因为
   `/api/image-loras` 的 `available` 列表**只下发 compatible=true 的条目**
   （不兼容的走 `incompatible` 列表，而 MOSS 的 incompatible 列表按需求 15
   **不再下发**）。所以这两档的真实值要靠「人为截断副本」这条真实输入验证
   （已在 3.2 / 3.3 完成），接口列表里看不到是**既有过滤语义**决定的，
   不是本轮遗漏。
2. `已适配` 这一档的含义是「**本机实测可用**，但上游目录未登记基座判定结论」，
   它**不声称**该 LoRA 是上游原生训练 —— 与「原生」严格区分，界面文案也已分开。
3. 目录里 84 条（MOSS）/ 112 条（Z-Image）/ 53 条（Qwen）的**上游声明**仍未逐条核实。
   本轮补的是「本机实测结论」，**不是**上游调研结论 —— 后者属于独立一轮调研，
   本任务不擅自扩大范围。
