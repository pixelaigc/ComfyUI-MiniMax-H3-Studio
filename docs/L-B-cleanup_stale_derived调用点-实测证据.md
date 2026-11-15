# L-B 实测证据：`cleanup_stale_derived()` 补调用点 + 不误删有效派生文件

生成时间：2026-11-15
分支：local/feature/legacy-lo-cleanup

---

## 1. 定位（文件 + 行号证据）

仓��里**有两个**同名函数：

| 函数 | 位置 | 上一轮是否有调用点 |
|---|---|---|
| `lora_damping.cleanup_stale_derived(lora_dirs)` | `h3studio/lora_damping.py:236` | **有** —— `h3studio/loras.py:1492`（`list_z_image_loras()` 里） |
| `moss_lora_split.cleanup_stale_derived(lora_dirs)` | `h3studio/moss_lora_split.py:312` | **无** ← **这就是上一轮报告说的「无调用点」** |

live 线上实测（同源代码）：

```
$ grep -rn "cleanup_stale_derived" --include=*.py .
./h3studio/lora_damping.py:236:def cleanup_stale_derived(lora_dirs: list[Path] | None = None) -> int:
./h3studio/loras.py:1492:        lora_damping.cleanup_stale_derived(dirs)
./h3studio/moss_lora_split.py:312:def cleanup_stale_derived(lora_dirs=None) -> int:
```

→ `moss_lora_split` 的那个（**派生归一文件**的清理器）定义了但**没有任何调用点**。

### 预期调用时机

派生文件是「源文件的后代」：源文件被更新（重下 / 替换）后，旧派生文件带着**旧权重**，
ComfyUI 会照常加载它 → 用户以为加载了新 LoRA，实际生效的是旧副本。
所以清理必须发生在**派生文件生成成功之后**。

本机现状：48 个 `h3studio_mossnorm__*` 派生文件（全部在 `moss-effects/`），
它们是 48 条 MOSS LoRA「融合 qkv → 分离形态」转换已验证的**唯一证据**
（`conversion_status=derived` 靠它们判定）。一旦被误删，这 48 条立刻掉回
`unverified` → `compatible=false` → 用户可选列表凭空少 48 条。

---

## 2. 补上的调用点

| 调用点 | 文件 | 位置 | 时机 |
|---|---|---|---|
| ① 批量预热路径 | `h3studio/moss_lora_split.py` `prewarm_derived()` | 循环**之后** | 全部派生文件生成/复用完成 |
| ② 工作流提交路径 | `h3studio/moss_lora_split.py` `ensure_normalized()` | `r["ok"]` 分支内 | 单个派生文件生成/复用成功 |

两处都用 `try/except` 包住：清理失败（磁盘忙 / 权限）**不影响**派生结果 ——
派生文件已经就位，清理只是顺手去垃圾，失败下次再清。

**为什么 ① 放在循环之后而不是循环里**：批量转换期间源文件与派生文件的 mtime
关系是乱的（正在写的派生文件 mtime 可能比源文件更早），循环中途清理会把
**刚生成**的派生文件误删。

---

## 3. ⚠️ 补调用点时发现并修掉的更严重缺陷（旧实现会删光全部有效派生文件）

旧实现反解源文件用的是 **lora 根目录**：

```python
original = candidate.name[len(DERIVED_PREFIX):].replace("__", "/")
source = Path(directory) / original          # ← directory = lora 根目录
if not source.is_file() or source.mtime > candidate.mtime:
    candidate.unlink()
```

但派生文件**就放在源文件同一个子目录里**（`normalize_lora` 的 `target_dir = src.parent`），
源文件在 `moss-effects/` 子目录里 —— 用 lora 根目录去找**必然找不到**
→ 判定「派生文件过期」→ **把全部有效派生文件删光**。

**实测 dry-run（同一套目录、同一个函数）**：

```
OLD impl: total derived=48, would DELETE=48      ← 全删光
NEW impl: would DELETE=0                          ← 一个都不该删
```

修复：反解源文件改用**派生文件自己的目录**：

```python
original = candidate.name[len(DERIVED_PREFIX):]
source = candidate.parent / original
```

清理耗时实测 **0.004 s**（48 个派生文件）—— 放进 HTTP 提交路径也无压力。

---

## 4. 实测证据

### 4.1 真实磁盘场景（`scripts/verify_labc_cleanup_scenario.py`）

触发方式：**真实的派生文件生成路径** `ensure_normalized()`（不是直接调 cleanup 函数
—— 这样才证明调用点真的接上了）。

探针文件放在真实机器的 `/mnt/usb2t/comfyui-models/loras/_labc_probe/`，
因为 `ensure_normalized` 内部的 cleanup 调用**不传目录** → 它清的是
`comfyui_lora_dirs()`（真实机器目录）。探针目录跑完即删。

| 场景 | 触发前 | 触发后 | 判定 |
|---|---|---|---|
| A 有效派生（源文件在、mtime 相同） | 存在 | **仍存在** | ✅ 保留 |
| B 过期派生（源文件比派生文件新） | 存在 | **已删除** | ✅ 清理 |
| C 孤儿派生（源文件已删） | 存在 | **已删除** | ✅ 清理 |
| 真实机器 48 个有效派生文件 | 48 | **48** | ✅ 一个不少 |

机器可读证据：`/tmp/labc_cleanup_scenario.json`

```json
"verdict": {
  "valid_derived_kept": true,
  "stale_derived_removed": true,
  "orphan_derived_removed": true,
  "real_derived_kept": true,
  "real_before_count": 48,
  "real_after_count": 48,
  "all_pass": true
}
```

### 4.2 单测（`tests/test_labc_cleanup_stale_derived.py`，8 条全绿）

```
8 passed in 0.03s
```

覆盖：有效派生保留 / 派生比源新保留 / 过期派生删除 / 孤儿派生删除 /
源文件反解必须用自身目录（回归锁死）/ 两个调用点存在 / 公开 API 可达。

### 4.3 变异验证（证明断言真的咬得住）

把反解改回旧的 lora 根目录写法：

```
mutation1 applied
FAILED ...::test_derived_newer_than_source_is_kept
FAILED ...::test_source_lookup_uses_own_directory_not_lora_root
FAILED ...::test_valid_derived_is_not_deleted
3 failed, 5 passed
```

→ 精确命中「误删有效派生文件」这一行为，**无误伤**。

---

## 5. 已知限制

1. 清理**不保护「正在被某个排队任务使用的派生文件」**。
   现状判定是「源文件 mtime > 派生文件 mtime 才删」，而源文件在任务运行期间
   不会变 → 正在使用的派生文件不会被删。但若用户在任务运行期间**替换源文件**，
   该任务的派生文件会被删。已记录为已知边界（本轮不扩大范围改判定语义）。
2. 清理只覆盖 `h3studio_mossnorm__*`（MOSS 归一派生）。
   `h3studio_ctx_*`（Z-Image 阻尼派生）由 `lora_damping.cleanup_stale_derived`
   负责，它**本来就有调用点**（`loras.py:1492`），本轮未改动。
