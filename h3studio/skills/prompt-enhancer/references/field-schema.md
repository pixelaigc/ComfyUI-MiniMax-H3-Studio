# 字段定义 —— SCALIST 六维 + bbox 元素表

> 本文件说明各模板的字段含义与官方依据。
> 约束层（禁什么、必须什么）见 `constraints.md`。

---

## 一、SCALIST 六维（HiDream 官方字段划分）

**【官方】出处**：https://github.com/HiDream-ai/HiDream-O1-Image/blob/main/prompt_agent.py

HiDream-O1 官方 SCALIST 原文有 7 个字段：

| 官方字段 | 官方要求摘录 |
|---|---|
| **Subject** | 主体的身份、外观、颜色、材质、纹理、动作、表情、服饰 |
| **Composition** | 镜头景别、视角、主体位置、前景/中景/背景层次、留白和视觉焦点 |
| **Action** | 主体正在做什么，动作方向、姿态、互动关系 |
| **Location** | 场景地点、室内/室外、时代、天气、时间段、环境细节 |
| **Image style** | photorealistic / cinematic / oil painting / watercolor / anime / 3D render，并匹配光线与色彩氛围 |
| **Specs** | 摄影/渲染参数（85mm lens, low-angle shot, shallow depth of field, soft diffused light, dramatic backlighting, matte texture, sharp focus） |
| **Text rendering** | 准确文字放在英文双引号中，并说明字体风格、颜色、大小、材质、精确位置 |

### 本 Agent 的键名映射

需求指定的键名是 `subject` / `action` / `location` / `composition` / `lighting` / `style`。

| 本 Agent 键名 | 对应 SCALIST 官方字段 | 差异说明 |
|---|---|---|
| `subject` | Subject | — |
| `action` | Action | — |
| `location` | Location | — |
| `composition` | Composition | — |
| `lighting` | **Specs**（光照部分） | Z-Image 官方 PE 第 3 步把「光影氛围」单列为一项，比 SCALIST 的 Specs 更聚焦；本 Agent 拆出独立维度 |
| `style` | Image style | 镜��参数（焦段/光圈/景深）并入 style 段落，不单设 `specs` 键 |

**立场说明**（引自主笔记 §2.4）：
「划分本身与 HiDream 官方 SCALIST 高度吻合，**有官方先例**；但**没有任何官方来源用这一组精确
JSON 键名** → 属『合理但无同名先例』。」

因此本 Agent 的做法是：**键名沿用需求指定的六维**，
但**输出形态仍是单段自然语言**（SCALIST 官方原文要求「连贯自然的单段落」），
而不是输出带这些键的 JSON。

> SCALIST 的字段划分原本是为 JSON 输出设计的，但 HiDream 官方同时要求最终 prompt 是
> 「一个英文的、连贯自然的单段落」。本 Agent 采用**内部按六维组织、外部输出一段自然语言**的
> 折中：既落实了字段覆盖，又满足 Z-Image「纯自然语言」的硬约束。

### SCALIST 官方的长度与格式约束（原文）

- 「prompt 必须是一个英文的、连贯自然的单段落，像 Creative Director's Brief，
  而不是关键词堆砌或 tag soup」
- 「长度通常为 **80-220 词**」
- 「最重要的主体和画面意图放在**开头**」
- 「prompt 必须**自包含**，仅凭 prompt 本身就能准确生成图片」

---

## 二、bbox 元素表（BFL FLUX 3 官方格式）

**【官方】出处**：https://huggingface.co/black-forest-labs/flux-3-image-klein-pup/raw/main/system_prompts.json
（`t2i_bbox` system prompt，12602 bytes）

### JSON 结构

```json
{
  "id": "animal_1",
  "bbox": [500, 320, 750, 520],
  "desc": "A small Pomeranian with reddish-brown fur, dark eyes, and a black nose, looking toward the camera."
}
```

### 字段规则

| 字段 | 规则 |
|---|---|
| `id` | 裸类名词 + 从 1 开始的索引，按**先左到右、再上到下**编号（`person_1`, `dog_2`）；文本块用两字母语言码 + `_Text_` + 索引（`En_Text_1`）；**id 必须唯一** |
| `bbox` | `[y_min, x_min, y_max, x_max]`，**0–1000 归一化网格**，原点左上角，**y 在 x 之前** |
| `desc` | 一个自洽段落，**只描述外观**（形状/材质/颜色/状态/纹理），**不含位置** |

### 聚焦人物的 desc 要求（官方原文）

聚焦的人物要写：**族裔、性别、年龄区间、肤色、体型、头发、面部、每件衣物、配饰、姿态、表情**。

### caption 中的 id 引用

caption 里用尖括号引用 id：

```text
An outdoor photograph under overcast winter light. A woman <person_1> sits in the left foreground,
facing right, holding a small Pomeranian <animal_1> in her lap. Spectators <crowd_1> are softly
blurred behind her.
```

### 官方完整示例

```text
An outdoor photograph under overcast winter light. A woman <person_1> sits in the left foreground,
facing right, holding a small Pomeranian <animal_1> in her lap. Spectators <crowd_1> are softly
blurred behind her.

[
  {"id": "person_1", "bbox": [500, 350, 950, 650],
   "desc": "A seated woman in a dark wool coat, three-quarter view, hands folded around the dog in her lap."},
  {"id": "animal_1", "bbox": [500, 320, 750, 520],
   "desc": "A small Pomeranian with reddish-brown fur, dark eyes, and a black nose, looking toward the camera."},
  {"id": "crowd_1", "bbox": [300, 0, 700, 1000],
   "desc": "A softly blurred group of standing onlookers in winter coats."}
]
```

### ⚠️ bbox 顺序的官方矛盾

- 官方 `docs.bfl.ml/guides/prompting_layout` 写的是 `[40, 200, 100, 800]` 这类
- upsampler `system_prompts.json` 明确写 `"bbox": [y_min, x_min, y_max, x_max]`

**本 Agent 以 upsampler system prompt 为准**（校验器按 y_min/x_min/y_max/x_max 判定，
且校验值域 0–1000）。

### 长度

FLUX.3 官方 t2i 典型长度 **100-350 词**。

---

## 三、Z-Image 官方 PE 模板的四步逻辑

**【官方】出处**：https://huggingface.co/spaces/Tongyi-MAI/Z-Image-Turbo/raw/main/pe.py
（实测 2371 bytes）

| 步骤 | 官方原文要点 |
|---|---|
| 1 | 锁定不可变更的核心要素：主体、数量、动作、状态、IP 名称、颜色、文字 |
| 2 | 判断是否需要"生成式推理"——需要构思方案（回答"是什么"、"设计"、"如何解题"）时先构想完整方案 |
| 3 | 注入专业级美学与真实感细节：构图、光影氛围、材质质感、色彩方案、层次空间 |
| 4 | 精确处理文字元素：一字不差转录 + 英文双引号；海报/菜单/UI 详述字体与排版 |

末尾硬约束（原文）：
「你的最终描述必须客观、具象，严禁使用比喻、情感化修辞，
也绝不包含"8K"、"杰作"等元标签或绘制指令。」
「仅严格输出最终的修改后的prompt，不要输出任何其他内容。」

---

## 四、FLUX.3 官方 caption 模板（槽位结构）

**【官方】出处**：https://docs.bfl.ml/guides/prompting_guide_flux2

```text
[Subject], [location], [style], [camera settings], [lighting], [colors], [effect], [additional elements]
```

FLUX 3 官方 caption 的实际顺序是「medium and style first, then the subjects and where they
sit, then light, color, and background」。

---

## 五、FLUX.2 / Krea 2 官方 system prompt（备选模板蓝本）

**FLUX.2**（https://github.com/black-forest-labs/flux2/blob/main/src/flux2/system_messages.py）：

```text
You are an expert prompt engineer for FLUX.2 by Black Forest Labs. Rewrite user prompts to be more
descriptive while strictly preserving their core subject and intent.

Guidelines:
1. Structure: Keep structured inputs structured (enhance within fields). Convert natural language to detailed paragraphs.
2. Details: Add concrete visual specifics - form, scale, textures, materials, lighting (quality, direction, color), shadows, spatial relationships, and environmental context.
3. Text in Images: Put ALL text in quotation marks, matching the prompt's language. Always provide explicit quoted text for objects that would contain text in reality (signs, labels, screens, etc.) - without it, the model generates gibberish.

Output only the revised prompt and nothing else.
```

**Krea 2**（https://raw.githubusercontent.com/krea-ai/krea-2/main/docs/expansion.txt）9 条 Rules 要点：

1. Faithfulness First —— 保留原始主体/动作/颜色/空间关系，不加用户没暗示的新物件
2. Practical T2I Structure —— 主体带自己的属性与动作，用扎实的短语写姿态/互动/空间布局
3. Style Planning Stays Internal —— 风格/媒介/取景/光线的选择过程留在内部，不输出规划标签
4. Text Rendering —— 可见文字用双引号精确写出
5. Avoid Over-Specification —— 不凭空编造服装/颜色/材质/场景细节
6. Structure —— 一段连贯文字，不要 bullet / JSON / markdown
7. Respect Existing Detail —— 已经详细的提示词只轻润，不重写
8. Respect the Human Form —— 人物描绘保持得体
9. Preserve User Medium —— 用户指定了媒介就照做，不擅自换

---

## 六、字段与约束的对应关系

| 检查项 | 约束来源 | 本 Agent 实现位置 |
|---|---|---|
| 无权重语法 | InvokeAI Z-Image 页 | `h3studio/enhancer.py` → `validate_output()` |
| 无质量词 | Z-Image 官方 PE | 同上 |
| 无 `<think>` 段 | InvokeAI prompt-tools | `h3studio/llm.py` → `_strip_thinking()` + 同上 |
| 主体词保留 | FLUX.2 / Krea 2 Rule 1 | 同上 |
| 双引号包裹文字 | Z-Image PE / FLUX.2 Guideline 3 | 同上（警告级） |
| 长度区间 | SCALIST 80-220 词 | 同上（警告级） |
| bbox y 在 x 前 | FLUX.3 upsampler | 同上（bbox 档专用） |
| 采样参数 | InvokeAI text_llm_pipeline | `h3studio/enhancer.py` → `resolve_params()` |
