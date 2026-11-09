# MOSS 结构化增强（layout）说明

> 本节描述 H3 Studio 自带的 **MOSS-Image 结构化提示词增强** —— 在**我们自己的外部链路**里
> 复现 ComfyUI-Moss 插件 `MossPrompt` 节点的能力，让 MOSS-Image 拥有与官方同等能力的
> 结构化（layout）提示词强化。
>
> 实现文件：`h3studio/moss_layout.py`（后端）、`web/moss-layout.js`（前端）、
> `tests/test_moss_layout.py`（测试）、`POST /api/moss-layout`（接口）。

---

## 一、来源与取舍

### 1.1 来源（全部来自本机已安装的源码，非臆造）

参考插件：`/home/ubuntu/comfyui/custom_nodes/ComfyUI-Moss`（`Ksanadu/ComfyUI-Moss`）

| 文件 | 本项目从它取的什么 |
|---|---|
| `nodes.py` | `MossPrompt` 节点的输入定义（`text` / `resolution` / `output_form` / `seed_mode` / `seed_value` / `seed`） |
| `engine.py` | 调用哪套 API、草稿解析规则、失败行为、缓存键 |
| `config.py` | API 配置形态、Skill/编译脚本可编辑的设计 |
| `defaults/SKILL.md` | **官方 MOSS Skill** —— 文生图的提示词写法规则 |
| `defaults/layout.md` | **layout 草稿 schema** —— 字段的权威定义 |
| `scripts/compile_prompt.py` | **坐标编译算法** —— 草稿 → MOSS Layout 的逐条规则 |

官方 `MossPrompt` 节点的行为（实测阅读源码，非猜测）：

- **输入**：`text`（当前这一条图像需求）、`resolution`（画布宽×高）、`output_form`
  （`layer json` / `natural`）、`seed_mode`（`fixed`/`random`）、`seed_value`，以及可选的
  `seed` 输入口（接上后优先）。
- **调用哪个 API**：一个**兼容 OpenAI Chat Completions** 的服务，地址写在节点设置里，
  默认 `https://api.deepseek.com/v1` + `deepseek-chat`。
- **提示词模板**：system = 官方 `SKILL.md` 全文 + 一段运行时契约；user = **只有这一条**
  `text`（不带历史对话）。`layer json` 模式时，契约里附上 `defaults/layout.md` 的 schema。
- **输出**：两个 STRING 口 —— `text`（编译后的提示词）与 `debug info`（JSON，含
  `cache_hit` / `seed` / `model` / `usage` / `elapsed_seconds` 等）。
- **两种产物**：
  - `natural` → `{"caption":"..."}` → 编译后是一段**纯文本**提示词。
  - `layer json` → 完整 layout 草稿 → 编译后是**一行 MOSS Layout JSON**
    （`bbox_coord_space="abs_centralized"`、`bbox_format="yxyx"`）。
- **失败/空返回行为**：
  - API 不可达 → 抛 `RuntimeError`（中文原因），节点执行失败，**不产出任何结果**。
  - 返回非空但 `choices[0].message.content` 为空 → 抛错（"未返回非空 content"）。
  - 草稿 JSON 非法 → 抛错并指出**第几行**。
  - 草稿字段不合法 → 编译器 `compile_prompt.py` 以非零码退出，错误写 stderr。
  - 失败**不写缓存**；成功结果按 `seed + 文本 + 分辨率 + 输出格式 + 配置指纹` 持久缓存。

### 1.2 取舍

| 决策 | 理由 |
|---|---|
| **不接入 `MossPrompt` 节点本身** | 它需要连 OpenAI 兼容的**外部 API**，会把 MOSS 工作流与外部 API 耦合。本项目全程只走本地 provider。 |
| **复用本项目既有的 provider 配置体系** | 不新增第二套 provider 配置表。`h3studio/moss_layout.py` 只认「调用方传入的 provider 字典」，与 `h3studio/enhancer.py` 的边界一致。 |
| **复用既有 `h3studio/llm.py` 调用封装** | 四种 Provider 协议（openai / ollama / llamacpp / comfyui）与 thinking 防护都已在 `llm.py` 里实现，不重写。 |
| **保留官方的两段式** | 模型只出**草稿**（`abs`/`xyxy`，左上原点）；坐标转换与校验由本地代码做 —— 与官方 `compile_prompt.py` 算法**逐条对齐**（见下方「与官方的一致性」）。 |
| **两个产物都给** | 本机 ComfyUI 0.37.0 的 MOSS 工作流里只有普通 `CLIPTextEncode`，用户没有把 layout JSON 直接接进出图链路。所以同时给出：<br>① `layout_json` —— 官方 MOSS Layout JSON（可喂给支持 layout 的下游）<br>② `final_prompt` —— 由 layout **确定性翻译**出的自然语言提示词（送 `CLIPTextEncode`）<br>两者**同源**，描述的是同一张图。 |

### 1.3 本机约束（如实记录）

- **ComfyUI 版本 0.37.0**（`/home/ubuntu/comfyui/comfyui_version.py`：`__version__ = "0.37.0"`）。
- **没有原生 MOSS 节点**。MOSS 能力来自 `custom_nodes/moss-image` 插件，它只提供
  **加载器**（`MOSSImageHFLoader` 等）—— 出图链路是
  `MOSSImageHFLoader → ModelSamplingSD3 → SkipLayerGuidanceDiTSimple → KSampler → VAEDecode → SaveImage`，
  正向条件是**普通 `CLIPTextEncode`**。
- 结论：**当前工作流只支持纯文本提示词**。因此本模块以**文本落地** ——
  `final_prompt` 是送进 `CLIPTextEncode` 的那一份；同时保留 `layout_json`，
  供将来接入支持 layout 的下游（或手动粘到 ComfyUI 的 MOSS layout 模板里）。
- 另注：官方 OpenMOSS 的 USERGUIDE 与 InvokeAI 文档都确认 **MOSS-Image 本身支持把
  layout JSON 直接放进 prompt**（`bbox_coord_space="abs_centralized"` + `bbox_format="yxyx"`）。
  本项目的 `layout_json` 就是这个格式；只是本机工作流尚未把它接到 `CLIPTextEncode`。

---

## 二、layout schema

来自官方 `defaults/layout.md` 与 `scripts/compile_prompt.py`，逐字对齐。

### 2.1 草稿（模型产出，本项目校验的对象）

根对象**恰好**这六个字段，多一个少一个都不合法：

| 字段 | 类型 | 约束 |
|---|---|---|
| `width` | int | 正整数，**必须是 16 的倍数**，且等于目标出图宽度 |
| `height` | int | 正整数，**必须是 16 的倍数**，且等于目标出图高度 |
| `bbox_coord_space` | str | 字面量 `"abs"`（左上原点绝对像素） |
| `bbox_format` | str | 字面量 `"xyxy"`（`[x_min,y_min,x_max,y_max]`） |
| `summary` | object | 整体构图描述，见下 |
| `items` | array | **非空**的**扁平**数组（不允许 `children` 嵌套） |

`summary`：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `caption` | str | ✅ | 非空。整体画面描述（构图/镜头/光线/材质/配色/氛围） |
| `meta` | list[str] | — | 媒介提示，如 `["poster"]` |
| `palette` | list[str] | — | 整体配色 |
| `canvas` | str | — | `"single"` / `"multi_panel"` / `"collage"` |

`items[]`：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `label` | str | ✅ | `"object"` / `"character"` / `"text"` |
| `caption` | str | ✅ | 非空，该元素的外观/材质/字体描述 |
| `bbox` | list[4] | ✅ | `[x_min,y_min,x_max,y_max]`，左上原点像素，四边有限数，**在画布内**，宽高为正 |
| `text` | str | `label=="text"` 时必填 | 图内文字**逐字**内容 |
| `tags` | list[str] | — | 关键词 |
| `palette` | list[str] | — | 该元素配色 |

**示例**（本模块的测试基准）：

```json
{
  "width": 720, "height": 1280,
  "bbox_coord_space": "abs", "bbox_format": "xyxy",
  "summary": {
    "caption": "A minimalist vertical poster with a deep navy night sky.",
    "meta": ["poster"], "canvas": "single",
    "palette": ["deep navy", "warm gold"]
  },
  "items": [
    {"label": "object", "caption": "A pale cream full moon with soft crater texture.",
     "bbox": [220, 180, 500, 540], "tags": ["moon", "luminous"], "palette": ["cream"]},
    {"label": "text", "caption": "Bold serif title characters in warm gold.",
     "text": "月光 MOSS", "bbox": [160, 820, 560, 960]}
  ]
}
```

### 2.2 编译后（MOSS Layout，本模块的 `layout` 产物）

坐标从**左上原点绝对像素**转成**画布中心原点**，且**竖坐标在前**：

```
bbox_out = [floor(y1) - height//2,
            floor(x1) - width//2,
            ceil(y2)  - height//2,
            ceil(x2)  - width//2]
```

（先 `round(x, 9)` 消数值噪声，再 `floor`/`ceil` 包住像素边 —— 与官方
`compile_prompt.py` 的 `centered_box()` 完全一致。）

编译后的根对象：

```json
{"summary": {...}, "items": [...],
 "bbox_coord_space": "abs_centralized", "bbox_format": "yxyx"}
```

**不含 `width`/`height`** —— 官方编译器就是这样（画布尺寸留在外面，与出图尺寸对齐）。

### 2.3 与官方的一致性（不是自证）

`tests/test_moss_layout.py::test_compiled_output_matches_official_compiler_byte_for_byte`
会**真的跑一遍** `/home/ubuntu/comfyui/custom_nodes/ComfyUI-Moss/scripts/compile_prompt.py`，
拿它的 stdout 与本模块的 `compile_layout()` 输出做**逐字节**比较。

额外做过 60 组随机草稿的对照，零差异。

---

## 三、翻译规则（layout → 文本提示词）

`layout_to_text()` 是**纯函数**：同一份 layout 永远得到同一段文本，**不调用任何模型**。

翻译步骤：

1. **首段** = `summary.caption`（整体构图/镜头/光线/材质/配色/氛围 —— 模型的草稿里已覆盖）。
2. **整体信息**：
   - `summary.canvas` → `multi_panel` / `collage` 的说明句；
   - `summary.meta` → 「媒介与形式：…」；
   - `summary.palette` → 「整体配色：…」。
3. **逐个元素**（按 `items` 数组顺序）：
   - 从**编译后的 yxyx 坐标**反算框的中心点与尺寸 → 方位短语 + 尺度短语；
   - 接上 `caption`（外观/材质/字体）；
   - `label=="text"` 时，把 `text` **逐字**带出（英文用双引号，中文用书名号）；
   - 附 `tags`（关键词）与 `palette`（配色）。
4. **语言一致性**：整段提示词跟随 `summary.caption` 的语言（CJK 字符占比 ≥ 20% 判中文，
   否则英文）。图内文字（`item.text`）**始终**保持原语言原样，不翻译。

方位/尺度词表：

| 位置（框中心点） | 英文 | 中文 |
|---|---|---|
| 水平 < 34% / 34~66% / > 66% | on the left side / in the horizontal center / on the right side | 位于画面左侧 / 位于画面水平中央 / 位于画面右侧 |
| 垂直 < 34% / 34~66% / > 66% | in the upper area / in the vertical center / in the lower area | 处于画面上部 / 处于画面垂直中央 / 处于画面下部 |
| 面积 ≥ 35% / ≥ 15% / ≥ 4% / 其余 | filling most of the frame / occupying a large part of the frame / of moderate size / small within the frame | 占据画面大部分 / 占据画面较大区域 / 尺寸适中 / 在画面中较小 |

**为什么用编译后的坐标反算**：草稿是左上原点，编译后是中心原点，而真正送进出图的是
编译后的那一份 —— 位置描述必须与它一致，否则文本说的位置和 layout 说的位置对不上。

---

## 四、失败与回退行为

**原则：绝不静默失败，绝不返回半成品。** 任何一步失败都不会产出 layout 产物，
用户可以退回原提示词继续用。

| 失败点 | 表现 | HTTP | 错误信息 |
|---|---|---|---|
| 提示词为空 / 超长 | 参数校验 | 400 | 「请先输入要增强的提示词」/「提示词过长（上限 4000 字）」 |
| 画布宽高非法 | 参数校验 | 400 | 「画布宽高必须是 16 的倍数（当前 W×H）」 |
| 没有可用 Provider | 配置缺失 | 400 | 「尚未配置可用的本地模型 Provider。请先在「本地模型」页添加并启用一个 Provider。」 |
| 指定的 Provider 不存在 / 已停用 | 配置错误 | 404 / 400 | 「指定的 Provider 不存在」/「该 Provider 已停用」 |
| **本地模型不可达 / 超时** | `llm.ProviderError` | **502** | Provider 的可读原因 + `degraded: true` |
| 模型返回空内容 | 空返回 | **502** | 「模型返回了空内容，未生成 layout 草稿」 |
| 模型返回的不是 JSON | 解析失败 | **502** | 「模型未返回有效的 JSON（第 N 行第 M 列）：…」 |
| JSON 合法但不符合 schema | schema 失败 | **502** | 「layout 草稿不符合 schema：…」+ `errors[]` **逐条**列出哪一条不合法 |

`errors[]` 里的每一条都指名道姓，例如：

```
items[1].bbox 超出 720×1280 画布范围
items[0] 含未知字段：children
summary.caption 必须是非空文本
items[1] 是 text 类型，必须带 text 字段（图内文字逐字内容）
```

**降级说明**：本机**有**可用的本地 provider（本机 Qwen 多模态，
`http://127.0.0.1:5000`，`qwen3.8-flash-next-iq3_xxs`），因此本功能可用。
若某天本机没有可用 provider：

- 接口返回 400 并给出明确配置指引；
- 前端弹出明确错误、提示栏变红、**不会**写入提示词框；
- 用户可以直接用原提示词出图（既有的「增强」功能也不受影响）。

---

## 五、用法

### 5.1 界面

1. 左侧模式选 **MOSS-Image 文生图**。
2. 提示词框下方会出现 **「结构化增强（layout）」** 一栏（其它模式不显示）。
3. 可选：在「画面意图补充」里写一句补充要求（如「海报风格，标题要醒目」）。
4. 点 **「结构化增强」** → 弹窗出现，含三个页签：
   - **最终文本提示词** —— 要送进 `CLIPTextEncode` 的那一份（可编辑）
   - **Layout JSON** —— 官方 MOSS Layout JSON（可编辑）
   - **原始草稿** —— 模型产出的草稿，便于排查
5. 「复制 Layout JSON」/「复制文本提示词」一键复制；「回填到提示词框」把文本提示词写进
   提示词框（并标记为「已增强」，进入版本历史）。

> 画布尺寸自动取当前出图尺寸（MOSS 面板的分辨率档位/手填宽高）。
> **换分辨率后要重新跑一次结构化增强** —— layout 的坐标是绝对像素，与画布绑定。

### 5.2 HTTP API

```bash
curl -X POST http://127.0.0.1:8190/api/moss-layout \
  -H 'Content-Type: application/json' \
  -d '{
    "prompt": "一张极简竖版海报：深蓝夜空中的月亮，标题文字为“月光 MOSS”。",
    "intent": "海报风格，标题要醒目",
    "width": 720, "height": 1280
  }'
```

响应：

```json
{
  "ok": true,
  "provider": "本机 Qwen 多模态",
  "width": 720, "height": 1280,
  "draft":        { "...草稿原样（abs/xyxy）..." },
  "layout":       { "...编译后（abs_centralized/yxyx）..." },
  "layout_json":  "{\"summary\":...}",
  "final_prompt": "A minimalist vertical poster ...\n整体配色：...\n物件位于画面水平中央，..."
}
```

可选参数 `provider_id` 指定 Provider；不给则用默认 Provider。

### 5.3 与既有「增强 / 二次增强」的关系

三者互不干扰，产物不同：

| 入口 | 产物 | 特点 |
|---|---|---|
| 增强 | 一段自然语言 | 描述式扩写（官方 PE / SCALIST / bbox 等模板） |
| 二次增强 | 一段自然语言 | 把提示词里的物体逐个枚举 + 逐个细化 |
| **结构化增强（layout）** | **Layout JSON + 翻译出的文本** | 显式对象清单 + 位置坐标；仅 MOSS-Image 面板可见 |

「结构化增强」的产物会作为**第 1 级版本**进入「版本历史」，可以回退。

---

## 六、测试

```bash
python3 -m pytest tests/test_moss_layout.py -v
```

52 个用例，覆盖三块：

1. **schema 校验**（22 例）—— 根字段恰好六个、未知字段、坐标空间/格式字面量、
   16 倍数、正整数、`summary` 可选项与枚举、`items` 非空扁平、`label` 枚举、
   `text` 类型必带 `text`、拒绝 `children` 嵌套、bbox 正面积/在画布内/四个有限数、
   草稿画布与目标分辨率一致。
2. **layout → 文本翻译**（10 例）—— 确定性、summary 领起、方位/尺度/属性/材质落地、
   图内文字逐字保留、tags/palette 带上、**提示词里不残留 JSON/bbox**、
   中文 caption → 中文方位短语、**英文 caption + 中文图内文字仍保持英文**
   （这条是实测踩坑后加的回归）。
3. **错误回退**（11 例）—— 空返回、纯空白、非 JSON（且报错带行列位置）、
   JSON 数组而非对象、重复字段、schema 违规（且报错指名道姓）、
   ```json 围栏能被吃下、空提示词、非 16 倍数、成功时两个产物都在。

外加一条**上游一致性**用例：真的跑官方编译器，逐字节比对输出。
