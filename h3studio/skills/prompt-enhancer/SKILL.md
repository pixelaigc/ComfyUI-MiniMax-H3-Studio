---
name: prompt-enhancer
description: 把一句粗略描述扩写成可直接喂给 Z-Image 等扩散模型的完整自然语言提示词。当用户需要扩写/润色图像提示词、需要给文生图模型写符合 SCALIST 结构的描述、或需要检查提示词里是否混进了质量词与权重语法时使用本 skill。
license: Apache-2.0
metadata:
  target-model: Z-Image
  provider-source: 本地模型配置页的默认多模态 Provider
  templates: pe-zimage-official / scalist-structured / bbox-elements
---

# 提示词增强 Agent

把用户的粗略描述扩写成一段**忠实于原意、细节饱满、可直接被 Z-Image 使用**的完整自然语言提示词。

本 skill 是 **H3 Studio 内置的迷你 Agent**，通过左侧「本地模型配置」里指定的
**默认多模态 Provider** 调用本地模型完成扩写。Provider 配置持久化在服务端 SQLite，
局域网全部客户端共用一份。

---

## 何时用

| 场景 | 用法 |
|---|---|
| 「一个女孩」→ 想要能出图的提示词 | 图像页提示词框旁「增强」按钮 |
| 提示词里有 `masterpiece` / `(word:1.2)` | 先跑校验器清理 |
| 需要带元素定位表（bbox） | 选 `bbox` 档 |
| 复用到视频/音频提示词 | 新增一个模板文件，见「扩展到其它模态」 |

---

## 三档模板

| 档 | 模板文件 | 输出形态 | 适用 |
|---|---|---|---|
| **官方 PE**（默认） | `assets/pe-zimage-official.txt` | 单段自然语言 | Z-Image 首选，官方原版 |
| **SCALIST 结构化** | `assets/scalist-structured.txt` | 单段自然语言（内部按六维组织） | 需要显式覆盖 subject/action/location/composition/lighting/style |
| **bbox 元素表** | `assets/bbox-elements.txt` | caption 段落 + JSON 数组 | 需要精确元素定位（FLUX 3 官方格式） |

模板原文与出处见 `references/field-schema.md`。

---

## 四步工作流（官方 PE 模板的逻辑序列）

1. **锁定不可变核心要素** —— 主体、数量、动作、状态、专有名词、颜色、文字。绝对保留。
2. **判断是否需要「生成式推理」** —— 需求不是直接场景描述时（回答"是什么"、做"设计"、
   展示"如何解题"），先在脑中构思出完整可视觉化的方案。
3. **注入美学与真实感细节** —— 构图、光影氛围、材质质感、色彩方案、层次空间。
4. **精确处理文字元素** —— 一字不差转录，用英文双引号 `""` 括起来；海报/菜单/UI 还要
   详述字体与排版布局。

---

## 硬约束（校验器强制，逐条对应 references/constraints.md）

| 约束 | 判定 | 依据 |
|---|---|---|
| 纯自然语言，禁止权重语法 | **拒收** | InvokeAI 官方：Z-Image 不支持 Compel 语法 |
| 禁止 `masterpiece`/`8k`/`best quality` 等质量词 | **拒收** | Z-Image 官方 PE 模板明令 |
| 禁止比喻、主观评价 | 警告 | 官方 PE 模板「严禁使用比喻、情感化修辞」 |
| 画面文字必须双引号包裹 | 警告 | 官方 PE / FLUX.2 / FLUX.3 一致要求 |
| 不得残留 `<think>` 段 | **拒收** | InvokeAI：thinking 模型不适合做扩写 |
| 保留用户原文主体名词 | 警告 | 官方 FLUX.2 / Krea 2 Rule 1 |
| 不产出负向提示词 | — | Z-Image-Turbo 蒸馏模型无 CFG，负向不生效 |
| 不编造专有名词 | 警告 | 官方 FLUX.3 / Qwen-Image 2512 |

完整规则、词表与出处见 **`references/constraints.md`**。

---

## 采样与长度

| 档 | temperature | top_p | do_sample | 说明 |
|---|---|---|---|---|
| `sample`（默认） | 0.7 | 0.9 | true | InvokeAI 官方口径，每次略有差异 |
| `greedy` | — | — | false | 复刻 BFL 官方，同输入同输出 |

**长度**：`max_new_tokens` 默认 **300**（InvokeAI 官方口径）。
Z-Image 文本上限本地 `max_sequence_length=1024`，官方换算 **0.75 word/token**，
用 **Qwen3-4B tokenizer** 计量 —— 300 tokens ≈ 225 词。

---

## 本地模型选型

**【官方】InvokeAI**：Qwen3-8B / Qwen3-14B 这类 8B 级足够做通用扩写。

⚠️ **Qwen3 是 thinking 模型**，扩写时必须关闭 thinking，否则思维链会混进 prompt。
本 Agent 有三层防护：请求层发 `enable_thinking=false`、解析层剥离 `<think>` 围栏、
校验层拒绝残留。

---

## 扩展到其它模态

新增一档模板只需两步，**不需要改动本模块的调用逻辑**：

1. 在 `assets/` 放一个新模板文件（如 `video-official.txt`），用 `{prompt}` 占位用户原文。
2. 在 `h3studio/enhancer.py` 的 `TEMPLATES` 注册该模板（id / 模板文件 / 档位参数）。

Provider 调用、校验、落地策略（replace / append）全部复用。

---

## 目录

```text
prompt-enhancer/
├── SKILL.md                        # 本文件（导航层）
├── references/
│   ├── constraints.md              # 硬约束 + 校验规则 + 官方出处
│   └── field-schema.md             # 字段定义（SCALIST 六维 + bbox 规则）
├── assets/
│   ├── pe-zimage-official.txt      # Z-Image 官方 PE 模板（中文原文）
│   ├── scalist-structured.txt      # SCALIST 结构化模板
│   ├── bbox-elements.txt           # caption + bbox 元素表模板
│   └── examples.md                 # 输入 → 扩写输出 示例对（含反例）
└── scripts/
    └── validate.py                 # 校验器（质量词 / 权重语法 / think 段 / 主体保留 / 长度）
```

调用方（`h3studio/enhancer.py`）在运行时读取 `assets/` 下的模板文件，
用户在「本地模型配置」页的编辑会覆盖并持久化到服务端 SQLite。

---

## 相关文件

- Provider 配置：`h3studio/providers.py`（服务端 SQLite，`model_providers` 表）
- 模型调用封装：`h3studio/llm.py`（四种 Provider 协议 + thinking 防护）
- 增强 Agent 主体：`h3studio/enhancer.py`（模板装载、参数、校验、落地策略）
