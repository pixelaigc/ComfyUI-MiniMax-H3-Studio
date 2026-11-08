# 扩写示例对

> 每组给出：原始输入 → 合规扩写（通过校验器）→ 反例（会被校验器拒收或警告）。
> 反例标出**命中的是哪条约束**。

---

## 例 1｜基本场景（官方 PE 档）

**输入**

```text
一个女孩
```

**合规扩写**（示例）

```text
A young woman in a pale beige knitted sweater stands beside a window in a quiet apartment on a misty
morning, her long dark hair falling loosely over one shoulder as she rests a hand on the wooden frame
and looks out toward the street below. The room behind her holds a low table with an open book and a
ceramic cup, and leafy plants fill the corner near the window. The composition places her slightly left
of center in a medium shot, leaving open space on the right toward the rain-streaked glass. Soft
diffused daylight enters from the left and mixes with the warm glow of a small table lamp behind her,
rendering her features in gentle shadow. The image reads as a quiet observational portrait in muted
natural color, rendered with fine detail in the knit texture of the sweater and the grain of the wood.
```

六维覆盖：subject（young woman / 针织衫 / 长发）· action（resting a hand, looks out）·
location（apartment, misty morning）· composition（medium shot, left of center, 留白）·
lighting（diffused daylight + warm lamp, gentle shadow）· style（observational portrait, muted natural color）。

---

## 例 2｜反例：质量词 + 权重语法

**输入**

```text
一个女孩, masterpiece, 8k, best quality, (detailed face:1.4), [cinematic lighting]
```

**问题输出**

```text
A masterpiece portrait, 8k best quality, with (detailed face:1.4) and [cinematic lighting]...
```

**命中**

| 命中 | 判定 |
|---|---|
| `masterpiece` / `8k` / `best quality` | **拒收** —— Z-Image 官方 PE 明令禁止质量元标签 |
| `(detailed face:1.4)` | **拒收** —— InvokeAI 官方：Z-Image 不支持 Compel 权重语法 |
| `[cinematic lighting]` | **拒收** —— 方括号权重语法 |

**修正方向**：用可执行的视觉信息替代 —— 「soft diffused daylight from the left, gentle shadow
across the features, fine knit texture」而不是「masterpiece, 8k」。

---

## 例 3｜反例：thinking 泄漏

**问题输出**

```text
<think>用户想要一个女孩的场景，我应该先构思主体、场景、光线……</think>
A young woman standing in a sunlit park...
```

**命中**：`<think>` 段残留 —— **拒收**。
InvokeAI 官方：「models that always reason (with no way to turn it off) are **not suitable** for
prompt expansion.」

**修正方向**：请求层发 `enable_thinking=false`；解析层剥离 `<think>` 围栏；校验层兜底拒收。

---

## 例 4｜反例：主体词丢失

**输入**

```text
一只橘猫戴着红色项圈趴在窗台上
```

**问题输出**

```text
A small animal resting on a wooden surface near a window, illuminated by warm afternoon light...
```

**命中**：`橘猫` / `红色项圈` / `窗台` 全部丢失 —— 警告级（官方 FLUX.2「strictly preserving
their core subject and intent」、Krea 2 Rule 1「Faithfulness First」）。

**合规方向**：保留 `橘猫`、`红色项圈`、`窗台`，只补充外观与光影。

---

## 例 5｜文字元素（双引号规则）

**输入**

```text
一张咖啡店菜单海报
```

**合规扩写**（节选）

```text
...A vertical poster for a coffee shop hangs on a plastered wall, and the top of the layout carries
the shop name "NOVA COFFEE" in bold condensed sans-serif capitals, with the headline special
"ESPRESSO" set much larger below it and a list of smaller item names such as "Americano",
"Latte" and "Cold Brew" arranged in neat horizontal rows across the middle, printed in warm
cream ink on a deep espresso-brown background...
```

要点：文字**逐字**转录、**英文双引号**包裹、并说明字体与排版布局
（官方 PE 第 4 步 + FLUX.2 Guideline 3）。

---

## 例 6｜bbox 档（FLUX 3 官方格式）

**输入**

```text
一个女人坐在长椅上抱着狗
```

**合规输出**

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

要点：caption 里用尖括号引用 id；`bbox` 是 `[y_min, x_min, y_max, x_max]`（**y 在 x 前**）、
0–1000 归一化；`desc` 只写外观不写位置。

---

## 例 7｜生成式推理场景

**输入**

```text
设计一个咖啡品牌
```

官方 PE 第 2 步要求：这类需求不是直接的场景描述，需要先在脑中构想出一个完整、具体、
可被视觉化的方案（一个 logo 草案 / 一张品牌主视觉 / 一套包装）。

**合规扩写**（节选）

```text
A brand identity mockup laid out flat on a pale concrete surface, centered on a circular emblem
containing a simplified line drawing of a coffee bean rising above a curved horizon line, rendered
in warm terracotta on a cream background, with the brand name "AURORA BEAN" set beneath in a
modern geometric sans-serif and a short line of descriptive text below it...
```

要点：先定一个具体可视觉化的方案（圆形徽标 + 文字排版），再展开细节；
文字用双引号逐字转录。

---

## 例 8｜用户已详细 —— 只轻润不重写

**【官方】Krea 2 Rule 7**：
「If the user's prompt is already detailed, lightly polish and finalize rather than heavily
expanding — preserve their phrasing and direction.」

**输入**

```text
A weathered fishing boat at dawn, hull painted faded turquoise, resting on wet sand at low tide,
flat grey sky, no people.
```

**合规扩写**（节选）

```text
A weathered fishing boat at dawn, its hull painted faded turquoise, resting on wet sand at low tide
under a flat grey sky, with no people in the frame.
```

要点：保留用户原有措辞（weathered / faded turquoise / flat grey），只做句法整理，
不新增船锚、渔网之类用户没暗示的物件（Krea 2 Rule 5「Avoid Over-Specification」）。
