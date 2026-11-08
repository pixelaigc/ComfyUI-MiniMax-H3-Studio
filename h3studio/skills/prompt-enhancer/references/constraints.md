# 硬约束与校验规则

> 本文件是提示词增强 Agent 的**约束层**。`scripts/validate.py` 里每条检查都对应本文一节。
> 标注口径：【官方】= 一手官方文档/源码；【社区】= 社区经验。

---

## 1. 输出必须是纯自然语言

**【官方】InvokeAI 明确：Z-Image 不支持 Compel 权重语法。**
「Compel-style prompt syntax does not apply to Z-Image. Write prompts in plain language.」
— https://v7.invoke.ai/users-guide/models/local-models/z-image/#generation-settings

**【官方】InvokeAI 点名 Z-Image 属新一代 LLM 文本编码器模型：**
「Newer models with large language-model text encoders, such as FLUX, Qwen Image, **Z-Image** and Krea-2,
understand full sentences and long, detailed descriptions」
— https://v7.invoke.ai/users-guide/image-generation/prompting-guide/#write-for-the-model-you-are-using

**【官方】HuggingFace 官方要求：**
「structured narrative, not a keyword list」
— https://huggingface.co/Tongyi-MAI/Z-Image-Turbo

**【官方】HiDream-O1 官方 SCALIST 要求：**
「prompt 必须是一个英文的、连贯自然的单段落，像 Creative Director's Brief，
而不是关键词堆砌或 tag soup」

### 禁止的权重语法（校验器命中即报错）

| 形式 | 示例 |
|---|---|
| 括号权重 | `(word:1.2)`、`(word)` |
| 双层括号 | `((word))` |
| 方括号 | `[word]` |
| 增强标记 | `word++`、`word---` |

diffusers 官方倍率表仅供了解（**不适用于 Z-Image**）：
`(cat)`=1.1x、`((cat))`=1.21x、`(cat:1.5)`=1.5x、`(cat:0.5)`=0.5x
— https://huggingface.co/docs/diffusers/en/using-diffusers/weighted_prompts#prompt-weighting

---

## 2. 禁止质量元标签

**【官方】Z-Image 官方 PE 模板原文：**
「你的最终描述必须客观、具象，严禁使用比喻、情感化修辞，
也绝不包含"8K"、"杰作"等元标签或绘制指令。」

**【官方】FLUX.3 upsampler system prompt 同义表述：**
「no quality or emotion judgments」「must not include meta-tags or drawing instructions
such as "8K" or "masterpiece"」

**【官方】InvokeAI 官方把「长串通用质量词」列为常见错误：**
「Using long generic quality-word lists before you know the base prompt works」
— https://v7.invoke.ai/users-guide/image-generation/prompting-guide/#common-mistakes

### 校验器命中的词表

`masterpiece`、`best quality`、`8k`、`4k`、`uhd`、`hd`、`high quality`、`超清`、`高清`、`杰作`、`最佳质量`、`极致细节`

命中即**报错并拒收**（不是警告）——这类词对 Z-Image 无效，
真正的提升来自构图/光影/材质/镜头/文字排版等可执行视觉信息。

---

## 3. 禁止比喻与主观评价

**【官方】** 同上：严禁使用比喻、情感化修辞。

校验器命中的主观评价词（英文）：
`beautiful`、`stunning`、`gorgeous`、`amazing`、`spectacular`、`breathtaking`、`perfect`

> 注：`beautiful` 这类词在官方模板里被明确排斥，但它们比质量标签轻。
> 校验器对其**降级为警告**，对质量标签则是**拒收**。

---

## 4. 文字元素必须双引号逐字转录

**【官方】Z-Image 官方 PE 模板：**
「你必须一字不差地转录所有希望在最终画面中出现的文字，
并且必须将这些文字内容用英文双引号（""）括起来」

**【官方】FLUX.2 Guideline 3：**
「Put ALL text in quotation marks, matching the prompt's language.
Always provide explicit quoted text for objects that would contain text in reality
(signs, labels, screens, etc.) — without it, the model generates gibberish.」

**【官方】FLUX.3 upsampler：**
「Rendered text: transcribe every wording verbatim inside double quotes
in its original language and script」

---

## 5. thinking 模型必须关闭思考

**【官方】InvokeAI 明确记录：**
> 「Reasoning models such as Qwen3 or the DeepSeek-R1 distills normally write out a chain
> of thought before their answer. Prompt expansion returns the generated text as-is, so that
> reasoning would end up in your prompt. InvokeAI therefore renders the chat template with
> **thinking disabled**... models that always reason (with no way to turn it off) are
> **not suitable** for prompt expansion.」
> — https://v7.invoke.ai/users-guide/image-generation/prompt-tools/#reasoning-thinking-models

**结论**：Qwen3-8B / Qwen3-14B 是 thinking 模型，扩写时必须关 thinking。

三层防护（见 `h3studio/llm.py`）：

1. **请求层**：OpenAI 兼容路径优先发送 `chat_template_kwargs: {"enable_thinking": false}`；
   服务端不认这个参数时（报错文本含该参数名）自动去掉重试一次。
2. **解析层**：`_strip_thinking()` 剥离 `<think>` / `<thinking>` / `<reasoning>` 围栏内容，
   未闭合的起始标签之后全部丢弃。
3. **校验层**：`validate.py` 检查结果里是否残留 `<think>` 段，命中即报错。

---

## 6. 采样参数

**【官方】InvokeAI 实际取值**（源码 `invokeai/backend/text_llm_pipeline.py`）：

```python
generation_kwargs = dict(
    **inputs,
    max_new_tokens=max_new_tokens,
    do_sample=True,
    temperature=0.7,
    top_p=0.9,
    ...
)
```

**【官方】BFL 官方 upsampler 用 greedy decoding：**
「Expand Prompt samples its rewrite, so repeated expansions differ;
**Black Forest Labs' own pipeline decodes greedily.**」

因此本 Agent 提供两档：

| 档位 | temperature | top_p | do_sample | 用途 |
|---|---|---|---|---|
| `sample`（默认） | 0.7 | 0.9 | true | 每次扩写略有不同，InvokeAI 口径 |
| `greedy` | — | — | false | 复刻 BFL 官方行为，同输入必得同输出 |

**【官方】候选小模型自身默认值**（HF `generation_config.json`）：

| 模型 | temperature | top_p | top_k |
|---|---|---|---|
| `Qwen/Qwen3-8B` | 0.6 | 0.95 | 20 |
| `Qwen/Qwen3-14B` | 0.6 | 0.95 | 20 |
| `Qwen/Qwen2.5-7B-Instruct` | 0.7 | 0.8 | 20 |

---

## 7. 长度与 token 预算

**【官方】InvokeAI 官方口径：扩写输出 `max_new_tokens = 300`。**

**【官方】Z-Image 文本长度上限**（官方讨论串，作者 Cxxs / QJerry 回复）：
> 「we set text maximum length as 512 tokens, 600-1000 words may results in 800-1333 tokens
> roughly (**0.75 word per token** generally) ... or set `max_sequence_length` in pipeline
> calling to **1024** when running the code locally」
> — https://huggingface.co/Tongyi-MAI/Z-Image-Turbo/discussions/8

换算：1024 tokens × 0.75 word/token ≈ **768 词**上限。
按 0.75 折算，300 tokens ≈ **225 词**。

**token 计量口径**：目标模型是 Z-Image 时，用 **Qwen3-4B tokenizer** 计算
（Z-Image 的文本编码器就是 Qwen3-4B）。

### 本 Agent 的长度档

| 档 | max_new_tokens | 目标词数（÷0.75） | 适用 |
|---|---|---|---|
| `short` | 300（官方默认） | ~150-225 | 简述扩写，InvokeAI 默认档 |
| `medium` | 300（官方默认） | ~150-225 | 默认 |
| `long` | 512 | ~350-384 | 需要长描述时（仍远低于 1024 上限） |

**SCALIST 官方长度口径**：80-220 词。
**FLUX.3 官方 t2i 典型长度**：100-350 词。

---

## 8. 负向提示词

**【官方】Z-Image-Turbo 是蒸馏模型，推理时不依赖 CFG，因此不使用负向提示词：**
> 「this is a few-step distilled model that **does not rely on classifier-free guidance**
> during inference ... this model does not use negative prompts at all.」
> — https://huggingface.co/Tongyi-MAI/Z-Image-Turbo/discussions/8

模型卡 model zoo：Z-Image-Turbo 的 CFG 列为 ❌，Z-Image Base 为 ✅。

→ **增强器不产出负向提示词**。用户如需负向，单独维护。

---

## 9. 保留用户原文的核心要素

**【官方】FLUX.2：**
「strictly preserving their core subject and intent」

**【官方】Krea 2 Rule 1：**
「Faithfulness First: Preserve all original subjects, actions, colors, and spatial
relationships. Do not add new objects, props, characters, or animals unless the user
clearly implies them.」

**【官方】FLUX.3 upsampler（表述最完整）：**
「keep every explicit subject, attribute, count, spatial constraint, style cue and piece
of wording; never contradict, substitute, drop or narrow the request... Decide everything
left open as ONE concrete, consistent plan」

**【官方】Krea 2 Rule 5：**
「Avoid Over-Specification: Do not invent highly specific clothing, colors, materials, or
scene details unless the input supports them.」

**【官方】Krea 2 Rule 7：**
「Respect Existing Detail: If the user's prompt is already detailed, lightly polish and
finalize rather than heavily expanding」

**【官方】FLUX.3 / Qwen-Image 2512 禁止编造专有名词：**
「Do not invent names, brands, logos or celebrities; keep user-named ones.」
「Never modify proper nouns」

→ 校验器检查用户原文的**主体名词**是否仍出现在扩写结果里（见 validate.py 的主体保留检查）。

---

## 10. 不得改变用户指定的媒介

**【官方】Krea 2 Rule 9：**
「Respect User Medium: When the user explicitly requests a medium (e.g. "photo of",
"photograph of", "illustration of", "painting of", "sketch of", "3D render of"), honor it.
Do not pivot to a different medium to avoid difficulty」

---

## 11. bbox 元素表规则（可选档）

**【官方】BFL FLUX 3 upsampler `t2i_bbox` system prompt：**

- `id` = 裸类名词 + 从 1 开始的索引，按**左到右、再上到下**编号
  （如 `person_1`, `dog_2`）；文本块用两字母语言码 + `_Text_` + 索引（如 `En_Text_1`）；
  id 必须唯一。
- `bbox` = `[y_min, x_min, y_max, x_max]`，**0–1000 归一化网格**，原点左上角，
  **y 在 x 之前**。
- `desc` = 一个自洽段落，**只描述外观**（形状/材质/颜色/状态/纹理），**不含位置**。
- caption 中每个 id 用尖括号引用：`a wicker table <table_1> holding a tiered stand <stand_1>`。

> ⚠️ 注意：官方 `docs.bfl.ml` 布局页写的是 `[40, 200, 100, 800]` 这类，
> 但 upsampler system prompt 明确 "bbox: [y_min, x_min, y_max, x_max]"。
> **以 upsampler system prompt 为准**（本 Agent 校验器按此判定）。

---

## 12. 来源清单

**Z-Image 官方**
- https://huggingface.co/spaces/Tongyi-MAI/Z-Image-Turbo/raw/main/pe.py （PE 模板，2371 bytes）
- https://huggingface.co/Tongyi-MAI/Z-Image-Turbo/discussions/8 （长度上限 / 负向 / thinking 讨论）
- https://huggingface.co/Tongyi-MAI/Z-Image-Turbo （模型卡，CFG 表）

**InvokeAI 官方**
- https://v7.invoke.ai/users-guide/image-generation/prompting-guide/
- https://v7.invoke.ai/users-guide/image-generation/prompt-tools/
- https://v7.invoke.ai/users-guide/models/local-models/z-image/
- https://github.com/invoke-ai/InvokeAI/blob/main/invokeai/backend/text_llm_pipeline.py

**其他官方模板**
- https://github.com/black-forest-labs/flux2/blob/main/src/flux2/system_messages.py
- https://huggingface.co/black-forest-labs/flux-3-image-klein-pup/raw/main/system_prompts.json
- https://raw.githubusercontent.com/krea-ai/krea-2/main/docs/expansion.txt
- https://github.com/HiDream-ai/HiDream-O1-Image/blob/main/prompt_agent.py

**Skill 规范**
- https://agentskills.io/specification
- https://docs.claude.com/en/docs/claude-code/skills
