/* ============================================================
   提示词增强方式注册表 + 统一「增强」下拉菜单
   ------------------------------------------------------------
   【统一增强入口（2026-11-09 用户要求）】
   每个功能面板只有**一种**增强：一个增强按钮 + 一个列出可用增强方式的下拉。
   改造前 Z-Image 文生图面板有两个语义重叠的下拉：
     - #enhanceMenu  「增强方式」（普通 / 二次 / 结构化 / 官方PE）
     - #enhanceSpec  「增强模板」（Z-Image 官方PE / 物体细化 / SCALIST / bbox）
   两者列的是同一组东西，用户无从判断该选哪个 —— 已把 #enhanceSpec 下拉删除，
   模板选择**并入**本下拉（每个增强方式自带它用的模板）。

   增强方式注册表（ENHANCE_METHODS）是唯一真相来源：
     - 下拉按它渲染（每项都带它自己的模板与可用模式）
     - 主按钮按它路由
     - 面板可见性按它决定
   两条硬规则：
     1. **每个功能只有自己对应的增强能力** —— 别的功能的专属增强在别的面板被禁用
        （Z-Image 面板看不到 Qwen 官方 PE；Qwen 面板看不到 MOSS 结构化增强）。
     2. **二次增强是通用增强** —— 任何面板都能选，它按媒体类型自动用对应模板
        （图像/视频 = 逐个画面物体；音乐 = 逐个音乐要素）。

   【多选】下拉是复选列表，可同时选中多项：
     - 点「增强」= 按注册表顺序依次执行选中的增强方式（链式输入）
     - 各项的「推荐参数」是叠加语义
     - 各项的「提示词片段」合并追加，互不覆盖
   ============================================================ */
(function () {
  "use strict";

  /**
   * 增强方式注册表。
   *
   * id          内部标识（路由用）
   * label       下拉里显示的名字
   * hint        一句话说明（悬停/副标题）
   * modes       该方式在哪些工作模式下可用；空数组 = 全部模式
   * specId      该方式用的**增强模板** id（后端 TEMPLATE_SPECS 的键）
   * busyFlag    该方式运行时把哪个 busy 标记置起来
   * buttonText  选中后主按钮上显示的文字
   * recommended 标注「推荐」
   * fields      该项自带的可编辑字段
   * kind        "template"（走 /api/enhance 模板扩写）| "moss-layout" | "qwen-pe"
   *             | "yue2-struct"（音乐结构化：风格字段 + 歌词）
   * write       该方式的**写回契约**（审查 P0-1 要求「按能力契约」而非硬编码 id）：
   *             - 不写 = 文本路线，写回当前面板的文本输入框
   *             - kind:"structured" = 结构化路线，产物是结构化对象
   *               （由该方式自己注册契约，见 music-enhance.js）
   * recommended 标注「推荐」（界面上显示 Recommended 徽标）
   * owner       该增强能力归属哪个功能；不在本面板可用时用它说明「为什么没有」
   * exclusive   true = 与其它项互斥（不能同时选中）
   * fields      该项自带的可编辑字段（渲染在展开区里）

   */
  const ENHANCE_METHODS = [
    {
      id: "basic",
      label: "普通增强",
      hint: "按所选模板把提示词扩写成完整描述",
      modes: [],
      specId: "",
      busyFlag: "enhance",
      buttonText: "增强",
      kind: "template",
      // 【写回契约 · 文本路线】产物是一段文本，写回当前面板的文本输入框。
      // 目标由 targets() 按能力解析（先 #prompt，再兼容 #musicPrompt/#musicStyle）。
      write: { kind: "text" },
    },
    {
      id: "refine",
      label: "二次增强",
      hint: "把提示词里的每一个物体/要素逐个枚举并逐个细化（通用能力，各面板都可用）",
      modes: [],
      // 模板按媒体类型动态决定（服务端 refine_specs 下发）——
      // 图像/视频 = 逐个画面物体；音乐 = 逐个音乐要素。
      specId: "",
      busyFlag: "refine",
      buttonText: "二次增强",
      kind: "template",
      // 【写回契约 · 文本路线】
      write: { kind: "text" },
    },
    {
      id: "director",
      label: "视频导演提示词",
      hint: "按 MiniMax-H3 官方三段式（画面时间线 / 整体声景 / 非剧情音乐）写导演稿",
      // 只对视频模式开放：H3 的三段式结构是视频模型专属
      modes: ["t2v", "flf", "r2v", "longfilm"],
      busyFlag: "enhance",
      buttonText: "导演提示词",
    },
    {
      id: "layout",
      label: "结构化增强（layout）",
      hint: "生成 MOSS Layout JSON（对象清单 + 位置坐标），并翻译成最终文本提示词",
      // 只对 MOSS-Image 开放：layout 坐标是 MOSS 的能力，其它模型不认
      modes: ["t2i_moss"],
      specId: "",
      busyFlag: "layout",
      buttonText: "结构化增强",
      kind: "moss-layout",
      // 【写回契约 · 文本路线】
      // layout 的产物最终仍翻译成**文本提示词**写回提示词框
      // （坐标 JSON 本身由 moss-layout.js 渲染成弹窗）。
      write: { kind: "text" },
      // 归属功能（禁用原因的唯一真相源，见 unavailableReason）
      owner: "MOSS-Image",
    },
    {
      // 【统一增强体系 · 音乐路线】
      // 音乐的增强**不是**文本扩写，而是产出「结构化风格 + 歌词」
      // （YuE2 官方流程：12 个结构化风格字段 + 一段歌词）。
      // 所以它是**另一条路线**（kind: "yue2-struct"），
      // 走 POST /api/music-enhance，结果由 music-enhance.js 的
      // **结构化写回契约**落到结构化风格区与歌词区。
      //
      // 【为什么放在同一份注册表里】审查 P0-2 的根治要求：
      // 「targets() 与音乐增强方法注册表并入同一份增强体系」。
      // 两份注册表各自渲染同一个菜单会互相覆盖（实测：图像注册表
      // 把音乐菜单内容覆盖成 basic/refine/layout/qwen_pe）。
      id: "yue2_struct",
      label: "YuE2 结构化风格 + 歌词",
      hint: "把描述拆成 YuE2 的结构化风格字段（语言/曲风/情绪/人声/乐器/BPM/能量/意象）并生成配套歌词",
      // 只在音乐模式可用（图像/视频不认音乐 style 串）
      modes: ["t2a", "a2a"],
      specId: "",
      busyFlag: "enhance",
      buttonText: "生成风格+歌词",
      recommended: true,
      kind: "yue2-struct",
      // 【写回契约 · 结构化路线】
      // 产物是「12 个风格字段 + 歌词」这个**结构化对象**，塞不进文本框。
      // 契约由 music-enhance.js 注册（window.MusicEnhance.getWriteContract），
      // 这里只声明「走结构化路线」，具体落地交给契约的 apply()。
      write: { kind: "structured" },
      // 归属功能（禁用原因的唯一真相源，见 unavailableReason）
      owner: "YuE2 音乐",
    },
    {
      id: "qwen_pe",
      label: "qwen21图片官方增强PE",
      hint: "用 Qwen-Image 2.1 官方 PE 权重与官方 system prompt 扩写（文生图 / 图片编辑各一档）",
      // 只对 Qwen-Image 2.1 开放：官方 PE checkpoint 是 Qwen-Image 2.1 专用的
      modes: ["t2i_qwen", "i2i_qwen"],
      specId: "",
      busyFlag: "enhance",
      buttonText: "官方PE扩写",
      // 官方最佳实践 —— 界面上标 Recommended
      recommended: true,
      kind: "qwen-pe",
      // 【写回契约 · 文本路线】官方 PE 的产物是扩写后的提示词文本。
      write: { kind: "text" },
      // 归属功能（禁用原因的唯一真相源，见 unavailableReason）
      owner: "Qwen-Image 2.1",
      // 该项自带的可编辑字段：官方 PE 的 system prompt 与档位参数。
      // systemPrompt 走「扩写 + 出图」同一条链路；params 是 PE 节点参数。
      fields: [
        {
          key: "systemPrompt",
          label: "官方 PE system prompt",
          kind: "textarea",
          hint: "官方 PE 扩写所用的指令原文。编辑后，扩写与出图共用这一份文本。",
        },
        {
          key: "presencePenalty",
          label: "presence_penalty",
          kind: "number",
          hint: "官方 PE 档位参数：文生图 1.5 / 图片编辑 0。选中即自动套用，可再改。",
        },
        {
          key: "maxNewTokens",
          label: "max_new_tokens",
          kind: "number",
          hint: "官方 PE 档位参数：文生图 16256 / 图片编辑 24000。不能压到 4096（会静默返回空串）。",
        },
      ],
    },
  ];

  // 当前选中的增强方式 id 集合（多选）。默认只选「普通增强」。
  let selectedIds = ["basic"];

  const byId = {};
  ENHANCE_METHODS.forEach(m => { byId[m.id] = m; });

  /** 当前选中的方式（按注册表顺序 = 执行顺序）。 */
  function selectedMethods() {
    return ENHANCE_METHODS.filter(m => selectedIds.indexOf(m.id) >= 0);
  }

  /** 兼容旧调用方：返回「当前主方式」（多选时取第一个选中的）。 */
  function currentMethod() {
    const sel = selectedMethods();
    return sel[0] || byId.basic;
  }

  /** 当前模式的媒体类型（image / video / audio）—— 服务端 mode_media 下发。 */
  function currentMedia() {
    if (typeof provState === "undefined" || !provState) return "";
    const mode = (typeof state !== "undefined" && state) ? (state.mode || "") : "";
    return provState.modeMedia[mode] || "";
  }

  /**
   * 某增强方式在当前模式下用的**模板 id**。
   *
   * - 二次增强：按媒体类型取（服务端 refine_specs 下发）——
   *   图像/视频 = image-refine（逐个画面物体）；音乐 = music-refine（逐个音乐要素）。
   * - 普通增强：取当前上下文的默认模板（服务端 mode_defaults 下发）。
   * - 其它方式（layout / 官方 PE）：不用模板，返回空串。
   */
  function methodSpecId(method) {
    if (!method) return "";
    const options = methodSpecOptions(method);
    if (!options.length) return method.specId || "";
    // 用户在该方式下选过的模板优先（模板行里切换过）。
    // 注意 options 是**当前上下文**的清单 —— 跨模式残留的旧选择
    // 不在 options 里，自然被丢弃（这就是「切换模式时失效旧选择」）。
    const picked = selectedSpecByMethod[specKey(method.id)];
    if (picked && options.indexOf(picked) >= 0) return picked;
    // 否则用当前上下文的默认模板（服务端 mode_defaults）
    const def = currentSpecId();
    if (def && options.indexOf(def) >= 0) return def;
    return options[0];
  }

  /**
   * 某增强方式在当前模式下**可用的模板 id 列表**。
   *
   * 改造前模板选择是一个独立下拉（#enhanceSpec）；现在它**并入**本下拉：
   * 每个增强方式自带它的模板清单，用户在菜单里就地切换。
   *
   *   - 普通增强：本面板可用的全部模板（按媒体类型 + 模型族过滤，
   *     即服务端 filter_specs 的同一套规则）——
   *     Z-Image 面板 = Z-Image 官方 PE / SCALIST 结构化 / bbox 元素表；
   *     Qwen 面板 = Qwen 官方 PE 两档；
   *     MOSS 面板 = MOSS 官方 Skill 档；
   *     音乐面板 = YuE2 专用档；视频面板 = MiniMax-H3 档。
   *   - 二次增强：只有 1 条（按媒体类型选定的通用细化模板）。
   *   - 结构化增强 / 官方 PE：不用模板（各自有专属链路），返回空。
   */
  function methodSpecOptions(method) {
    if (!method) return [];
    if (method.id === "refine") {
      const id = refineSpecId();
      return id ? [id] : [];
    }
    if (method.id === "layout" || method.id === "qwen_pe") return [];
    // 音乐结构化增强：走自己的专属链路（/api/music-enhance），不用模板
    if (method.kind === "yue2-struct") return [];
    const specs = currentContextSpecs();
    if (specs.length) return specs.map(s => s.id);
    const def = currentSpecId();
    return def ? [def] : [];
  }

  /** 本面板可用的模板清单（按当前模式 + 模型过滤）。 */
  function currentContextSpecs() {
    if (typeof enhanceSpecsForContext !== "function") return [];
    const mode = (typeof state !== "undefined" && state) ? (state.mode || "") : "";
    let modelKey = "";
    if (typeof enhanceCurrentModelKey === "function") {
      modelKey = enhanceCurrentModelKey();
    }
    return enhanceSpecsForContext(mode, modelKey) || [];
  }

  /** 记下「某方式下选了哪个模板」的键（按模式分，避免跨面板串味）。 */
  function specKey(methodId) {
    const mode = (typeof state !== "undefined" && state) ? (state.mode || "") : "";
    return mode + "::" + methodId;
  }

  /** 记录某个方式下用户选中的模板（菜单里就地切换时调用）。 */
  function setMethodSpec(methodId, specId) {
    if (!methodId || !specId) return;
    selectedSpecByMethod[specKey(methodId)] = specId;
    saveSelection();
  }

  /**
   * 当前媒体类型下「二次增强」的模板 id（服务端下发，前端不硬编码）。
   *
   * 【2026-11-09】去掉了硬编码兜底 "image-refine"：
   * 服务端加了新媒体类型后，前端回落到写死的旧 id 会拿错模板。
   * 取不到时返回空串，由调用方提示「配置未就绪，请刷新」。
   */
  function refineSpecId() {
    if (typeof provState === "undefined" || !provState) return "";
    const mode = (typeof state !== "undefined" && state) ? (state.mode || "") : "";
    const byMode = provState.refineSpecs || {};
    if (byMode[mode]) return byMode[mode];
    const byMedia = provState.refineSpecByMedia || {};
    const media = currentMedia();
    return byMedia[media] || "";
  }

  /**
   * 当前上下文的默认增强模板 id（普通增强用）。
   *
   * 【2026-11-09 统一状态来源】原来这里是
   *     defaults[mode] || provState.specId || "image"
   * 其中 provState.specId 是**全局单值**状态，与
   * selectedSpecByMethod（按 mode::method 存）**两套状态并存** ——
   * 切到别的模式再切回来时，可能命中一个属于别的模式的模板 id。
   *
   * 现在只认两处：
   *   1. 服务端下发的 modeDefaults[mode]（该上下文的权威默认）
   *   2. 用户在该上下文下显式选过的模板（selectedSpecByMethod）
   * 兜底改为「空串 + 提示刷新配置」，不再硬编码模板 id ——
   * 服务端加了新模板后，前端不应回落到一个写死的旧 id。
   */
  function currentSpecId() {
    if (typeof provState === "undefined" || !provState) return "";
    const mode = (typeof state !== "undefined" && state) ? (state.mode || "") : "";
    const defaults = provState.modeDefaults || {};
    return defaults[mode] || "";
  }

  /**
   * 当前上下文的 DOM 目标 —— **按能力抽象**，不硬编码某个 id。
   *
   * 【2026-11-09 重构】原来这里是硬编码的：
   *     input: music ? "#musicStyle" : "#prompt"
   * 问题：yue2-flow 分支把音乐输入框从 #musicStyle 改名成 #musicPrompt
   * （且结构化风格字段是另一组 id），两边合并后
   * `document.querySelector("#musicStyle")` 返回 null ->
   * 后面 `.value` 抛 TypeError -> **音乐增强静默崩溃**。
   *
   * 现在改成「能力契约」：
   *   - input      —— 增强的**输入**载体（一个可写文本的控件）
   *   - outputs    —— 增强的**产物**写回目标（可以是多个：
   *                  文本框 + 一组结构化字段 + 歌词框）
   *   - badge      —— 「已增强」徽标元素
   *   - versions   —— 版本历史是否适用于本面板
   *   - label      —— 面板里这个输入框叫什么（提示文案用）
   *
   * 契约是**能力**而不是 id：具体 id 由 resolveTarget() 按「先新后旧」
   * 的顺序探测，探测不到就返回 null，由调用方给出可读提示，
   * **绝不静默抛 TypeError**。
   */
  function targets() {
    const music = isMusicContext();
    return {
      btn: music ? "#musicEnhanceBtn" : "#enhanceBtn",
      btnText: music ? "#musicEnhanceBtnText" : "#enhanceBtnText",
      menuBtn: music ? "#musicEnhanceMenuBtn" : "#enhanceMenuBtn",
      menu: music ? "#musicEnhanceMenu" : "#enhanceMenu",
      hint: music ? "#musicEnhanceHint" : "#enhanceHint",
      applied: music ? "#musicEnhanceAppliedParams" : "#enhanceAppliedParams",
      split: music ? "#musicEnhanceSplit" : "#enhanceSplit",
      current: music ? "#musicEnhanceCurrent" : "#enhanceCurrent",
      // 【模板选择行已删除】模板选择收进增强菜单项内（renderSpecPicker），
      // 所以不再有平级的 specRow / specLabel / specSelect 元素。
      // 这里保留空串，避免调用方 querySelector("") 抛错。
      specRow: "",
      specLabel: "",
      specSelect: "",
      // 【能力契约】见上。inputSelectors 是**候选列表**（按优先级）：
      //   图像/视频 = 提示词框
      //   音乐      = 音乐描述框（yue2-flow 改名后的 #musicPrompt）
      //               与 enh-unify 的 #musicStyle（同一语义的两个名字）
      inputSelectors: music
        ? ["#musicPrompt", "#musicStyle"]
        : ["#prompt"],
      // 产物写回目标（候选列表，同上语义）
      outputSelectors: music
        ? ["#musicPrompt", "#musicStyle"]
        : ["#prompt"],
      // 徽标与版本历史：音乐面板有独立的徽标；版本历史目前只对图像面板生效
      badgeSelectors: music ? ["#musicPromptBadge"] : ["#promptEnhancedBadge"],
      supportsVersions: !music,
      label: music ? "音乐描述" : "提示词",
    };
  }

  /**
   * 按能力契约解析出实际的 DOM 元素。
   *
   * 返回 { el, selector } —— 找不到时 el = null（**不抛异常**）。
   * 调用方必须处理 null，给出可读提示，而不是让 TypeError 冒到顶层。
   */
  function resolveTarget(selectors) {
    const list = Array.isArray(selectors) ? selectors : [selectors];
    for (const sel of list) {
      if (!sel) continue;
      const el = document.querySelector(sel);
      if (el) return { el, selector: sel };
    }
    return { el: null, selector: list[0] || "" };
  }

  /**
   * 当前的增强输入元素（取它当前的文本）。
   * 找不到时返回空串 —— 调用方据此提示「本面板没有可增强的输入」。
   */
  function currentInputValue() {
    const { el } = resolveTarget(targets().inputSelectors);
    return el ? String(el.value || "").trim() : "";
  }

  /**
   * 把增强结果写回到本面板的输入元素。
   * 找不到目标时返回 false（调用方给出可读提示，不抛异常）。
   *
   * 【按能力契约写回 · 审查 P0-1】
   * 目标不再由调用点硬编码，而是由**增强方式自己声明的 write 契约**决定：
   *   - write.kind === "structured" -> 结构化路线（音乐）：交给契约的 apply()
   *   - write.kind === "text"（或未声明）-> 文本路线：写回当前面板的文本输入框
   *
   * @param {string} text   写回的文本
   * @param {object} method 增强方式（取它的 write 声明）；不传则按文本路线
   * @returns {boolean} 是否写回成功
   */
  function writeBackInput(text, method) {
    const decl = method && method.write ? method.write : { kind: "text" };
    if (decl.kind === "structured") {
      const t = resolveStructuredWriteTarget();
      if (!t.ok) return false;
      if (typeof t.apply === "function") t.apply(text);
      return true;
    }
    const { el } = resolveTarget(targets().outputSelectors);
    if (!el) return false;
    el.value = text == null ? "" : String(text);
    return true;
  }

  /**
   * 取某个增强方式的**写回契约**（供外部调用方使用）。
   *
   * 返回结构与 EnhanceMenuShared.resolveWriteTarget 的输入一致：
   *   { kind: "text"|"structured", targets?, target?, describe?, apply? }
   *
   * 文本路线的 targets 是**候选列表**（按优先级探测，先新后旧）：
   *   图像/视频 = #prompt
   *   音乐      = #musicPrompt（yue2-flow 改名后的），
   *                并兼容 enh-unify 的 #musicStyle（同一语义的两个名字）
   */
  function methodWriteContract(method) {
    const decl = method && method.write ? method.write : { kind: "text" };
    if (decl.kind === "structured") {
      const music = window.MusicEnhance
        && typeof window.MusicEnhance.getWriteContract === "function"
        ? window.MusicEnhance.getWriteContract() : null;
      if (music) return music;
      return { kind: "structured", describe: () => "音乐写回契约未注册" };
    }
    return {
      kind: "text",
      targets: targets().outputSelectors,
      describe: () => `本面板找不到可写回的${targets().label}输入框`,
    };
  }

  /**
   * 当前上下文的**结构化写回契约**（音乐路线）。
   *
   * 文本路线（图像/视频）沿用 enh-unify 的 outputSelectors 候选列表
   * （#prompt / #musicPrompt / #musicStyle 任一存在即可）。
   * 结构化路线（音乐）：产物是「12 个风格字段 + 歌词」，
   * 由 music-enhance.js 注册契约并负责落地。
   *
   * 找不到目标时返回 { ok:false, reason } —— **不抛异常**，
   * 调用方给可读提示（审查 P0-1）。
   */
  function resolveStructuredWriteTarget() {
    const music = window.MusicEnhance
      && typeof window.MusicEnhance.getWriteContract === "function"
      ? window.MusicEnhance.getWriteContract() : null;
    if (!music) return { ok: false, reason: "音乐写回契约未注册" };
    const shared = window.EnhanceMenuShared;
    if (shared && typeof shared.resolveWriteTarget === "function") {
      return shared.resolveWriteTarget(music);
    }
    // 共用组件没加载时的兜底：直接用契约自己的 apply
    if (typeof music.apply === "function") {
      return { ok: true, kind: "structured", apply: music.apply };
    }
    return { ok: false, reason: "增强共用组件未就绪（enhance-menu-shared.js 未加载）" };
  }

  /** 当前是否在音乐模式（文生音乐 / 参考生音乐）。 */
  function isMusicContext() {
    if (typeof state === "undefined" || !state) return false;
    const mode = state.mode || "";
    return mode === "t2a" || mode === "a2a";
  }

  /** 某方式的模板在界面上的显示名（用于「当前增强方式」回显）。 */
  function methodSpecLabel(method) {
    if (!method) return "";
    if (method.id === "layout") return "MOSS Layout";
    if (method.id === "qwen_pe") return "Qwen-Image 2.1 官方 PE";
    const specId = methodSpecId(method);
    if (!specId) return "";
    const specs = (typeof provState !== "undefined" && provState
                   && provState.agent && provState.agent.specs)
      ? provState.agent.specs : [];
    const spec = specs.find(s => s.id === specId);
    return spec ? spec.label : specId;
  }

  /**
   * 某增强方式在当前模式下是否可用。
   *
   * 【音乐模式下的关键规则】文本扩写类（普通增强 / 二次增强）
   * 在音乐面板**不可用**：
   *   - 它们的产物是「一段文本」，而音乐面板的写回目标是
   *     **结构化契约**（12 个风格字段 + 歌词）—— 文本塞不进去；
   *   - 而且音乐有**专属**的结构化增强项（yue2_struct），
   *     它才是 YuE2 官方流程对应的产物形态。
   * 所以音乐面板只提供「YuE2 结构化风格 + 歌词」这一项。
   */
  function isAvailable(method) {
    if (!method) return false;
    // 音乐路线：只认结构化增强（产物形态与面板的写回契约一致）
    if (isMusicContext()) {
      return method.kind === "yue2-struct";
    }
    if (!method.modes || !method.modes.length) return true;
    const mode = (typeof state !== "undefined" && state) ? (state.mode || "") : "";
    return method.modes.includes(mode);
  }

  /** 可用且被选中的方式（真正会被执行的那些）。 */
  function runnableMethods() {
    return selectedMethods().filter(isAvailable);
  }

  // ------------------------------------------------------------------ 状态

  const STATE_KEY = "h3studio.enhanceSelection.v1";

  /** 把选中集合与可编辑字段持久化到 localStorage（刷新后不丢）。 */
  function saveSelection() {
    try {
      localStorage.setItem(STATE_KEY, JSON.stringify({
        selected: selectedIds,
        fields: fieldValues(),
        touched: Array.from(fieldTouched),
        specs: selectedSpecByMethod,
      }));
    } catch (e) { /* 隐私模式下忽略 */ }
  }

  /** 读取持久化状态；损坏/缺失时退回默认值。 */
  function loadSelection() {
    try {
      const raw = localStorage.getItem(STATE_KEY);
      if (!raw) return;
      const data = JSON.parse(raw);
      if (data && Array.isArray(data.selected)) {
        // 只保留注册表里真实存在、且当前模式下可用的 id
        const valid = data.selected.filter(id => byId[id]);
        if (valid.length) selectedIds = valid;
      }
      if (data && data.fields && typeof data.fields === "object") {
        Object.keys(data.fields).forEach(key => {
          if (fieldValues()[key] !== undefined) fieldState[key] = data.fields[key];
        });
      }
      if (data && Array.isArray(data.touched)) {
        data.touched.forEach(key => fieldTouched.add(key));
      }
      if (data && data.specs && typeof data.specs === "object") {
        Object.keys(data.specs).forEach(key => {
          selectedSpecByMethod[key] = data.specs[key];
        });
      }
    } catch (e) { /* 忽略 */ }
  }

  // 「某方式下选了哪个模板」的持久化表。key = "<mode>::<methodId>"
  // （按模式分 —— 同一项在 Z-Image 与 Qwen 面板可能对应不同模板）
  const selectedSpecByMethod = {};

  // 各增强项的可编辑字段当前值。key = "<methodId>.<fieldKey>"
  const fieldState = {};

  // 用户**动过**的字段（哪怕改成空串）。
  // 动过的字段用用户的值（不静默回落默认值）；没动过的用默认值。
  const fieldTouched = new Set();

  /** 展开成扁平表：{"qwen_pe.systemPrompt": "...", ...} */
  function fieldValues() {
    const out = {};
    Object.keys(fieldState).forEach(k => { out[k] = fieldState[k]; });
    return out;
  }

  /** 某项某字段的当前值（未设置过 = undefined）。 */
  function fieldValue(methodId, fieldKey) {
    return fieldState[`${methodId}.${fieldKey}`];
  }

  /** 某项某字段是否被用户动过。 */
  function fieldIsTouched(methodId, fieldKey) {
    return fieldTouched.has(`${methodId}.${fieldKey}`);
  }

  /** 记录一次字段编辑（值 + 动过标记）。 */
  function setFieldValue(methodId, fieldKey, value) {
    fieldState[`${methodId}.${fieldKey}`] = value;
    fieldTouched.add(`${methodId}.${fieldKey}`);
  }

  /**
   * 某项某字段的**生效值**：
   *   动过 -> 用用户的值（哪怕是空串）
   *   没动 -> 用默认值（官方 PE 用官方档位 / 官方 system prompt）
   */
  function effectiveFieldValue(method, field) {
    if (fieldIsTouched(method.id, field.key)) {
      const v = fieldValue(method.id, field.key);
      return v === undefined || v === null ? "" : v;
    }
    const d = defaultFieldValue(method, field);
    return d === undefined ? "" : d;
  }

  /** 某项某字段的默认值：官方 PE 用官方档位；其余项 = undefined。 */
  function defaultFieldValue(method, field) {
    if (method.id === "qwen_pe" && field.key === "presencePenalty") {
      const p = currentQwenPeParams();
      return p.presence_penalty;
    }
    if (method.id === "qwen_pe" && field.key === "maxNewTokens") {
      const p = currentQwenPeParams();
      return p.max_new_tokens;
    }
    if (method.id === "qwen_pe" && field.key === "systemPrompt") {
      return officialPeSystemPrompt();
    }
    return undefined;
  }

  /** 当前模式对应的官方 PE 档位参数（presence_penalty / max_new_tokens）。 */
  function currentQwenPeParams() {
    const opts = (typeof QWEN_OPTIONS !== "undefined" && QWEN_OPTIONS)
      ? QWEN_OPTIONS : null;
    const table = opts && opts.peParams ? opts.peParams : null;
    const mode = (typeof state !== "undefined" && state) ? (state.mode || "") : "";
    const tier = mode === "i2i_qwen" ? "i2i" : "t2i";
    if (table && table[tier]) {
      return {
        presence_penalty: table[tier].presence_penalty,
        max_new_tokens: table[tier].max_new_tokens,
        label: table[tier].label,
      };
    }
    // 后端还没下发（未重启）时的兜底：官方卡值
    const fallback = tier === "i2i"
      ? { presence_penalty: 0, max_new_tokens: 24000, label: "PE-I2I（图片编辑）" }
      : { presence_penalty: 1.5, max_new_tokens: 16256, label: "PE-T2I（文生图）" };
    return fallback;
  }

  /**
   * 官方 PE 的 system prompt 原文。
   * 来源：/api/enhancer 下发的模板清单（enhancer.py 的 TEMPLATE_SPECS 里
   * qwen21-pe-t2i / qwen21-pe-i2i 两档的 builtin_system_prompt，
   * 用户在「本地模型配置」里改过则为 custom_system_prompt）。
   * 取不到就返回空串 —— 前端据此提示「后端未就绪」，不编造。
   */
  function officialPeSystemPrompt() {
    const mode = (typeof state !== "undefined" && state) ? (state.mode || "") : "";
    const specId = mode === "i2i_qwen" ? "qwen21-pe-i2i" : "qwen21-pe-t2i";
    const specs = (typeof provState !== "undefined" && provState
                   && provState.agent && provState.agent.specs)
      ? provState.agent.specs : null;
    if (!specs) return "";
    const spec = specs.find(s => s.id === specId);
    if (!spec) return "";
    return spec.custom_system_prompt || spec.builtin_system_prompt || "";
  }

  // ------------------------------------------------------------------ 菜单渲染

  /**
   * HTML 转义 —— 走**共用组件**（web/enhance-menu-shared.js），
   * 避免同一个转义实现有两份（同一常量写两处必然走偏）。
   */
  function esc(s) {
    const shared = window.EnhanceMenuShared;
    if (shared && typeof shared.esc === "function") return shared.esc(s);
    return String(s == null ? "" : s).replace(/[&<>"']/g,
      c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function renderMenu() {
    const t = targets();
    const menu = document.querySelector(t.menu);
    if (!menu) return;
    const shared = window.EnhanceMenuShared;
    if (!shared || typeof shared.renderEnhanceMenu !== "function") {
      // 共用组件没加载（脚本顺序问题）—— 明确报错，不静默失败
      setHint("增强组件未就绪（enhance-menu-shared.js 未加载），请刷新页面", "error");
      return;
    }
    // 渲染走**共用组件**（web/enhance-menu-shared.js）——
    // 图像面板与音乐面板用的是同一套实现与视觉，不另造一套。
    shared.renderEnhanceMenu(menu, {
      methods: ENHANCE_METHODS,
      selectedIds: selectedIds,
      isAvailable: isAvailable,
      onToggle: toggleMethod,
      onFieldEdit: (methodId, fieldKey, value) => {
        setFieldValue(methodId, fieldKey, value);
        saveSelection();
        applySelectedParams();      // 参数即时套用并回显
        updateButton();
      },
      // 不可用时说明**为什么**不可用（属于哪个功能）
      unavailableText: unavailableReason,
      // 模板选择**收进菜单项内**（审查要求：不再有第二个下拉）。
      // 多档时该项自带一个下拉，单档时只显示名字。
      specHtml: renderSpecPicker,
      onSpecChange: (methodId, specId) => {
        setMethodSpec(methodId, specId);
        renderMenu();
        updateButton();
        updatePanelVisibility();
      },
      valueOf: (method, field) => effectiveFieldValue(method, field),
    });
  }

  /**
   * 菜单项里的**模板选择器**（可点，就地切换）。
   *
   * 【2026-11-10 改回「菜单项内」】
   * 改造前（2026-11-09）把模板选择提升成增强栏里的**平级一行**
   * （#enhanceSpecRow / #musicEnhanceSpecRow）—— 那是**第二个下拉**，
   * 与「增强方式」下拉语义重叠：用户看到两个下拉，却不知道该选哪个、
   * 哪个才是「增强入口」。审查要求统一为
   *   **一个增强按钮 + 一个下拉（列出可用增强方式）+ 当前方式回显**，
   *   模板选择**收进增强菜单项内**。
   *
   * 现在每个增强方式自带它的模板清单（多档时是下拉，单档时只显示名字）：
   *   - 一条下拉搞定「用哪种增强方式」+「这一项用哪个模板」
   *   - 不再有第二个下拉
   *   - 只有一条模板时不显示下拉（无意义的单选控件）
   *
   * 模板状态仍由 selectedSpecByMethod 承担（单一来源），
   * 跨模式残留的旧选择不在 options 里，自然被丢弃。
   */
  function renderSpecPicker(method) {
    const options = methodSpecOptions(method);
    if (!options.length) return "";
    const specs = (typeof provState !== "undefined" && provState
                   && provState.agent && provState.agent.specs)
      ? provState.agent.specs : [];
    const byIdMap = {};
    specs.forEach(s => { byIdMap[s.id] = s; });
    const current = methodSpecId(method);
    if (options.length === 1) {
      const one = byIdMap[options[0]];
      const name = one ? one.label : options[0];
      return `<span class="enhance-menu-spec" title="该项使用的增强模板">${esc(name)}</span>`;
    }
    const opts = options.map(id => {
      const s = byIdMap[id];
      const name = s ? s.label : id;
      return `<option value="${esc(id)}"${id === current ? " selected" : ""}>${esc(name)}</option>`;
    }).join("");
    return `<label class="enhance-menu-spec-row" title="该项使用的增强模板">
      <span class="enhance-menu-spec-label">模板</span>
      <select class="enhance-menu-spec" data-spec-method="${esc(method.id)}">${opts}</select>
    </label>`;
  }

  /**
   * 某方式在当前模式不可用的原因（说清楚「它属于哪个功能」）。
   * 规则 1 的可见表达：别的功能的专属增强在本面板被禁用并说明来历。
   */
  /**
   * 增强栏里的**模板选择行** —— **已移除**。
   *
   * 【2026-11-10】模板选择收进增强菜单项内（renderSpecPicker），
   * 所以不再有「第二个下拉」。本函数保留为空实现，避免调用方
   * （updateButton / 导出接口）报错；模板状态改由菜单项内的
   * select[data-spec-method] 承担。
   *
   * 保留空实现的原因：外部（providers.js / 测试）可能仍调用
   * renderSpecRow()，让它静默无操作比抛异常安全。
   */
  function renderSpecRow() {
    // 模板行元素已删除（模板收进菜单项内），这里无需任何 DOM 操作。
    // 注意不要写 document.querySelector(t.specRow) —— t.specRow 现在是空串，
    // querySelector("") 会抛 SyntaxError。
    return;
  }

  /**
   * 某方式在当前模式下不可用的原因（说清楚「它属于哪个功能」）。
   */
  function unavailableReason(method) {
    if (!method) return "本面板不可用（未登记该增强方式）";
    // 音乐模式：文本扩写类不可用 —— 说清楚**为什么**（产物形态不匹配）
    if (isMusicContext() && method.kind !== "yue2-struct") {
      if (method.kind === "template") {
        return "文本扩写类，YuE2 音乐需结构化风格+歌词";
      }
      return "图像/视频专属，音乐面板不可用";
    }
    // 归属信息以**注册表**的 owner 字段为唯一真相源
    // （合并 Agent 分支时两侧各有一份 unavailableReason，JS 里后者会
    //   静默覆盖前者 —— 现在只留这一份，owner 从注册表读）
    const owner = method.owner || "其它功能";
    return owner + " 专属，本面板不可用";
  }

  // ------------------------------------------------------------------ 选中与路由

  /**
   * 选中/取消一种增强方式（多选）。
   * @param {string} id
   * @param {boolean} on  true=选中，false=取消
   */
  function toggleMethod(id, on) {
    const method = byId[id];
    if (!method) return;
    if (on && !isAvailable(method)) return;   // 不可用的方式不选中
    if (on) {
      if (selectedIds.indexOf(id) < 0) selectedIds.push(id);
    } else {
      if (selectedIds.length <= 1) {
        // 至少留一项：取消最后一项时保持原状
        // （否则用户点「增强」没反应 —— 比多留一项更糟）
        renderMenu();
        updateButton();
        const hint = document.querySelector(targets().hint);
        if (hint) {
          hint.textContent = "至少要保留一项增强方式";
          hint.className = "enhance-hint error";
        }
        return;
      }
      selectedIds = selectedIds.filter(x => x !== id);
    }
    saveSelection();
    renderMenu();
    updateButton();
    updatePanelVisibility();
    applySelectedParams();
    const hint = document.querySelector(targets().hint);
    if (hint) {
      hint.textContent = on ? "已选中：" + method.label : "已取消：" + method.label;
      hint.className = "enhance-hint ok";
    }
  }

  /**
   * 切换模式 / 模型后**失效**不属于当前上下文的模板选择。
   *
   * 【2026-11-09】原来 selectedSpecByMethod（按 mode::method 存）
   * 与 provState.specId（全局单值）两套状态并存：
   * 切到别的模式再切回来时，可能命中一个属于别的模式的模板 id
   * （因为键里虽然带了 mode，但**模型**变了而 mode 没变时，
   * 同一 mode 下的模板集合已经变了 —— 例如 Qwen 面板切到 Z-Image 模型）。
   *
   * 现在：模式/模型变化时，把该上下文**不可用**的模板选择直接清掉。
   * 保留「该上下文仍然可用」的选择 —— 用户不必每次重选。
   */
  function invalidateSpecSelection() {
    const mode = (typeof state !== "undefined" && state) ? (state.mode || "") : "";
    if (!mode) return false;
    const valid = new Set(currentContextSpecs().map(s => s.id));
    let changed = false;
    Object.keys(selectedSpecByMethod).forEach(key => {
      // key = "<mode>::<methodId>"
      const keyMode = key.split("::")[0];
      if (keyMode !== mode) return;               // 别的模式的先不动
      const methodId = key.split("::")[1];
      const method = byId[methodId];
      if (!method) { delete selectedSpecByMethod[key]; changed = true; return; }
      const options = methodSpecOptions(method);
      // 二次增强的模板按媒体类型选，不在普通模板清单里 —— 单独判
      if (methodId === "refine") {
        const want = refineSpecId();
        if (!want || selectedSpecByMethod[key] !== want) {
          delete selectedSpecByMethod[key];
          changed = true;
        }
        return;
      }
      if (options.length && options.indexOf(selectedSpecByMethod[key]) < 0) {
        delete selectedSpecByMethod[key];
        changed = true;
      }
    });
    if (changed) saveSelection();
    return changed;
  }

  /**
   * 当前选中的方式在当前模式下是否仍可用；不可用则剔除。
   *
   * 模式切换时必须做这件事 —— 否则主按钮会停在一个「当前模式不可用」的
   * 方式上（比如在 Qwen 面板选了官方 PE 扩写，切到 Z-Image），
   * 用户点「增强」却什么都没发生（run() 直接 return）。
   */
  function ensureAvailable() {
    const before = selectedIds.length;
    // 模式/模型变了 -> 先失效不属于当前上下文的模板选择
    invalidateSpecSelection();
    selectedIds = selectedIds.filter(id => {
      const m = byId[id];
      return m && isAvailable(m);
    });
    // 兜底：当前模式下没有任何可用的方式时，选一个**该模式可用**的。
    // 音乐模式的可用项是 yue2_struct（不是 basic）—— 否则主按钮会停在一个
    // 不可用的方式上，用户点「增强」没反应。
    if (!selectedIds.length) {
      const fallback = ENHANCE_METHODS.find(m => isAvailable(m));
      selectedIds = fallback ? [fallback.id] : [];
    }
    if (selectedIds.length !== before) {
      saveSelection();
      renderMenu();
      updateButton();
      updatePanelVisibility();
      return true;
    }
    return false;
  }

  /**
   * 主按钮的文字 + 「当前所选增强方式」的显式回显。
   *
   * 规则 4：所有面板统一为「一个增强按钮 + 一个下拉（列出可用增强方式）
   * + 明确显示当前所选增强方式」。
   * 所以除了按钮文字，还要有一处**始终可见**的当前方式标注
   * （按钮文字在多选时只能写「增强（N 项）」，看不出具体是哪几项）。
   */
  function updateButton() {
    const t = targets();
    const text = document.querySelector(t.btnText);
    const sel = runnableMethods();
    const method = sel[0] || byId.basic;
    if (text) {
      text.textContent = sel.length > 1 ? `增强（${sel.length} 项）` : method.buttonText;
    }
    const btn = document.querySelector(t.btn);
    if (btn) {
      btn.title = sel.length > 1
        ? "按选中顺序依次执行：" + sel.map(m => m.label).join(" → ")
        : method.hint;
    }
    updateCurrentIndicator();
    // 模板行跟着主方式走（主方式变了，模板行要换清单）
    renderSpecRow();
    // 对外同步「当前方式」：多选时取第一个选中的（兼容旧的单选调用方）
    if (typeof window !== "undefined") {
      window.enhanceMethods = window.enhanceMethods || {};
      window.enhanceMethods.current = (sel[0] || byId.basic).id;
      window.enhanceMethods.selected = selectedIds.slice();
    }
  }

  /**
   * 「当前所选增强方式」的可见回显。
   * 音乐模式用另一套 id（#musicEnhanceCurrent）—— 同一时刻只显示其中一个。
   */
  function updateCurrentIndicator() {
    const sel = runnableMethods();
    const parts = sel.map(m => {
      const spec = methodSpecLabel(m);
      return spec ? `${m.label}（${spec}）` : m.label;
    });
    const text = parts.length ? "当前增强：" + parts.join(" + ") : "";
    const ids = ["#enhanceCurrent", "#musicEnhanceCurrent"];
    ids.forEach(sel2 => {
      const el = document.querySelector(sel2);
      if (!el) return;
      el.textContent = text;
      el.hidden = !text;
    });
  }

  /**
   * 面板可见性：不同增强方式需要显示不同的辅助控件。
   *   - 结构化增强（layout）-> 显示「画面意图补充」栏
   *   - 二次增强           -> 显示「作用对象」切换
   *   - 其它方式           -> 隐藏它们
   *
   * 「版本历史」按钮是**所有增强方式共用**的（在 #refineBar 里），
   * 所以只藏 #refineTargetGroup 这一组，不能藏整个 #refineBar。
   */
  function updatePanelVisibility() {
    const sel = selectedIds;
    const layoutBar = document.querySelector("#mossLayoutBar");
    if (layoutBar) layoutBar.hidden = sel.indexOf("layout") < 0;
    const targetGroup = document.querySelector("#refineTargetGroup");
    if (targetGroup) targetGroup.hidden = sel.indexOf("refine") < 0;
  }

  // ------------------------------------------------------------------ 参数套用

  /**
   * 把选中各项的「推荐参数」叠加套用，并回显到界面。
   *
   * 叠加语义：后一项只覆盖它自己声明的键，不会清掉前一项已套用的参数。
   * 最终提交的参数 = 各项叠加后的结果。
   *
   * 官方 PE 的参数落到 state.imgQwen.peParams（出图时随 params 提交）；
   * 其它项目前没有独立参数，声明为空 —— 后续新增增强项时在这里加分支即可。
   */
  function applySelectedParams() {
    if (typeof state === "undefined" || !state || !state.imgQwen) return;
    const merged = {};
    const sel = runnableMethods();
    const hasPe = sel.some(m => m.id === "qwen_pe");
    sel.forEach(m => {
      if (m.id !== "qwen_pe") return;
      const dflt = currentQwenPeParams();
      // 生效值：用户动过用用户的，没动过用官方档位
      const ppRaw = effectiveFieldValue(m, { key: "presencePenalty" });
      const mnRaw = effectiveFieldValue(m, { key: "maxNewTokens" });
      if (ppRaw !== "" && ppRaw !== undefined && ppRaw !== null) {
        merged.presence_penalty = Number(ppRaw);
      } else if (dflt.presence_penalty !== undefined) {
        merged.presence_penalty = dflt.presence_penalty;
      }
      if (mnRaw !== "" && mnRaw !== undefined && mnRaw !== null) {
        merged.max_new_tokens = Number(mnRaw);
      } else if (dflt.max_new_tokens !== undefined) {
        merged.max_new_tokens = dflt.max_new_tokens;
      }
    });
    // 官方 PE 未被选中时清掉（不残留上一轮的参数）
    if (hasPe) {
      state.imgQwen.peParams = merged;
      // system prompt：用户改过就用用户的；没改过用官方内置的
      // （出图链路与手动扩写共用同一份 -> 扩写与出图走同一套规则）
      const peMethod = byId.qwen_pe;
      const sp = effectiveFieldValue(peMethod, { key: "systemPrompt" });
      state.imgQwen.peSystemPrompt = sp || "";
    } else {
      state.imgQwen.peParams = null;
      state.imgQwen.peSystemPrompt = "";
    }
    updateAppliedParamsNote();
  }

  /** 已套用参数的可见回显（「选中即自动应用参数」要让用户看得见）。 */
  function updateAppliedParamsNote() {
    const note = document.querySelector(targets().applied);
    if (!note) return;
    const sel = runnableMethods();
    const parts = [];
    sel.forEach(m => {
      if (m.id === "qwen_pe") {
        const p = (typeof state !== "undefined" && state && state.imgQwen
                   && state.imgQwen.peParams) ? state.imgQwen.peParams : null;
        if (p) {
          parts.push(`官方PE：presence_penalty=${p.presence_penalty}、max_new_tokens=${p.max_new_tokens}`);
        }
      }
    });
    note.textContent = parts.length ? ("已套用推荐参数 → " + parts.join("；")) : "";
    note.hidden = !parts.length;
  }

  // ------------------------------------------------------------------ 菜单开合

  /**
   * 菜单的「宿主」—— 打开时挂到 body，关闭时挂回原位。
   *
   * 【2026-11-09 窄屏弹到视口外的根因修复】
   * 症状：375 / 390 视口下点开增强下拉，菜单整体位于视口上方之外
   *      （实测 menuTop=-979 / menuBottom=-350，menuOnScreen=false），
   *      屏幕上一个选项都看不到。
   *
   * 根因：`.enhance-menu` 是 `position: fixed`，但它挂在 #enhanceSplit 里，
   *      而窄屏下 #panel 是 `position: fixed` + `transform: translateY(0)`
   *      （web/responsive.css）。**CSS 规定：带 transform 的祖先会成为
   *      fixed 定位的包含块** —— 于是菜单的 top 不再相对视口，
   *      而是相对那个被 transform 的 #panel 的滚动内容盒，
   *      于是落到视口上方 −979px。
   *
   * 修法（采用「挂到 body」）：打开时把菜单 append 到 document.body，
   *      它就不在任何 transform 祖先里 —— fixed 重新相对视口定位。
   *      关闭时挂回原来的父节点，恢复 DOM 结构。
   *
   * 为什么不用另一个方案（去掉 #panel 的 transform）：
   *      那会破坏底部抽屉的 translateY 动画（审查报告实测过的方案 C）。
   *
   * 为什么不用 position:absolute + offsetParent 定位：
   *      那样菜单会被 #panel 的 overflow-y:auto 裁剪（审查报告 P1-1
   *      明确要求「不受父级 overflow 裁剪」），且要重写一套定位逻辑。
   */
  const MENU_HOME = new WeakMap();   // menu -> 原父节点

  /** 把菜单挂到 body（脱离 transform 包含块）。 */
  function mountMenuToBody(menu) {
    if (!menu) return;
    if (menu.parentElement === document.body) return;
    MENU_HOME.set(menu, menu.parentElement);
    document.body.appendChild(menu);
    // 挂到 body 后不再受原父级的 overflow / z-index 上下文约束，
    // 给一个足够高的层级（高于面板抽屉的 950）。
    menu.style.zIndex = "1300";
  }

  /** 把菜单挂回原来的位置。 */
  function restoreMenuHome(menu) {
    if (!menu) return;
    const home = MENU_HOME.get(menu);
    if (!home || menu.parentElement === home) return;
    home.appendChild(menu);
    menu.style.zIndex = "";
    MENU_HOME.delete(menu);
  }

  function openMenu() {
    const t = targets();
    const menu = document.querySelector(t.menu);
    const btn = document.querySelector(t.menuBtn);
    if (!menu || !btn) return;
    renderMenu();
    menu.hidden = false;
    btn.setAttribute("aria-expanded", "true");
    // 先挂到 body 再定位 —— 否则算出来的 top 是相对 transform 祖先的
    mountMenuToBody(menu);
    positionMenu(menu, btn);
  }

  function closeMenu() {
    const t = targets();
    const menu = document.querySelector(t.menu);
    const btn = document.querySelector(t.menuBtn);
    if (!menu) return;
    menu.hidden = true;
    // 清掉定位时临时设的高度约束 —— 下次打开要按新的视口重新算。
    menu.style.maxHeight = "";
    if (btn) btn.setAttribute("aria-expanded", "false");
    restoreMenuHome(menu);
  }

  function toggleMenu() {
    const menu = document.querySelector(targets().menu);
    if (!menu) return;
    if (menu.hidden) openMenu(); else closeMenu();
  }

  /**
   * 菜单定位在按钮下方；靠近视口右/下边缘时翻转，避免出屏。
   *
   * 【2026-11-09】菜单挂到 body 后，getBoundingClientRect() 拿到的
   * 是**视口坐标**，与 position:fixed 的参照系一致 —— 这正是原来
   * 在窄屏下算错的原因（坐标是相对 transform 祖先的）。
   */
  function positionMenu(menu, btn) {
    const r = btn.getBoundingClientRect();
    const mw = menu.offsetWidth || 260;
    // 【窄屏】菜单高度被 CSS 的 max-height 限制在视口内（超高时内部滚动）。
    // 这里必须用**实际高度**做定位判定 —— 否则会拿「内容高度」去比视口，
    // 得出「放不下」的结论，把菜单翻到按钮上方，但按钮上方也不够放，
    // 结果菜单仍然溢出视口（实测 375x500 视口下整块菜单底部在 643px，
    // 视口只有 500px —— 最后几项被裁掉）。
    const maxH = Math.max(120, window.innerHeight - 16);
    const mh = Math.min(menu.offsetHeight || 160, maxH);
    let left = r.left;
    if (left + mw > window.innerWidth - 8) {
      left = Math.max(8, window.innerWidth - mw - 8);
    }
    if (left < 8) left = 8;

    // 下方优先；下方放不下就翻到上方；**两边都放不下**时贴着视口顶，
    // 并把高度压到视口内（菜单内部滚动，见 style.css 的 max-height/overflow-y）。
    let top = r.bottom + 6;
    if (top + mh > window.innerHeight - 8) {
      const above = r.top - mh - 6;
      if (above >= 8) {
        top = above;
      } else {
        top = 8;
        menu.style.maxHeight = Math.max(120, window.innerHeight - 16) + "px";
      }
    }
    // 兜底：任何情况下底部都不越过视口。
    if (top + mh > window.innerHeight - 8) {
      top = Math.max(8, window.innerHeight - 8 - mh);
    }
    menu.style.left = left + "px";
    menu.style.top = top + "px";
  }

  // ------------------------------------------------------------------ 路由

  /**
   * 当前出图尺寸 —— layout 的坐标是绝对像素，必须与画布一致。
   *
   * 画布尺寸的**权威是后端** workflows.normalize_moss_image_size()
   * （16 倍数 + 单边 16~16384 + 总像素 0.25M~4M）。
   * 前端 app.js 的 normalizeImgSize() 只是它的一份副本 ——
   * 两份各写一遍必然走偏（这正是本项目反复出现过的「同一常量写两处」问题）。
   *
   * 所以前端**不复制算法**：直接把当前尺寸原样传给后端，
   * 由后端用权威规则归一后返回生效尺寸（响应里的 width/height）。
   * 前端只做最低限度兜底（取不到就退回 1024×1024）。
   */
  function currentCanvasSize() {
    let w = 1024, h = 1024;
    if (typeof imgCurrentSize === "function") {
      const s = imgCurrentSize();
      if (s && s.length >= 2) { w = Number(s[0]) || w; h = Number(s[1]) || h; }
    }
    return [w, h];
  }

  /**
   * 执行当前（或指定）的增强方式。
   *
   * 每个方式各自调自己的后端接口，各自处理成功/失败，
   * 但**回填语义一致**：结果进同一个弹窗、同一个「替换/追加」按钮组，
   * 回填后都标记为「已增强」并进版本历史。
   *
   * 【多选】按注册表顺序依次执行选中的每一项：
   *   - **链式输入**：后一项的输入 = 前一项的产出（真正的「依次应用」）
   *   - 片段合并：每一项产出的「提示词片段」合并追加，互不覆盖
   *   - 参数叠加：每一项的推荐参数叠加套用，后一项不清掉前一项
   */
  async function run(methodId) {
    // 兼容旧调用：run("layout") = 只跑这一项
    if (methodId) {
      return runOne(byId[methodId] || currentMethod());
    }
    const sel = runnableMethods();
    if (!sel.length) {
      setHint("请先在「增强」下拉里选中至少一项", "error");
      return;
    }
    if (sel.length === 1) return runOne(sel[0]);

    // 多项：按注册表顺序依次执行，链式输入 + 片段合并 + 参数叠加
    const merged = [];
    const metas = [];
    const versions = [];
    let anyPe = false;
    // 链式输入：第一项吃输入框里的原文，后一项吃前一项的产出
    const originalInput = currentInputValue();
    let input = originalInput;
    for (const method of sel) {
      const piece = await runOne(method, { collect: true, input });
      if (!piece) continue;
      if (piece.expanded) {
        merged.push(piece.expanded);
        input = piece.expanded;      // 下一项接着这一项的产出继续
      }
      if (piece.meta) metas.push(piece.meta);
      if (piece.isPe) anyPe = true;
      if (piece.version) versions.push(piece.version);
    }
    if (!merged.length) return;
    const combined = mergeFragments(merged);
    document.querySelector("#enhanceResult").value = combined;
    document.querySelector("#enhanceMeta").innerHTML = metas.join("<br>");
    document.querySelector("#enhanceModal").hidden = false;

    // 【写回标记】合并结果里若含官方 PE 的产出，写回提示词框后
    // 出图就不该再二次扩写（提示词框里已经是官方 PE 结果）。
    // 这个标记必须在**所有项都跑完之后**统一设置 —— 否则先跑的普通增强
    // 会把后跑的官方 PE 标记冲掉（两处生效 = 出图扩写两次）。
    if (anyPe && typeof provState !== "undefined") {
      provState.lastEnhanceKind = "qwen_pe";
    }

    // 【2026-11-09 修复】多选路径写版本历史 + 打「已增强」徽标。
    //
    // 症状：勾选「普通增强 + 二次增强」执行后，徽标不显示、
    //       版本历史为空，点生成还会弹「提示词尚未增强」确认框。
    //
    // 根因：pushPromptVersion 写在 runBasic()/runRefine() 的
    //       「非 collect 分支」里 —— 多选走 collect=true，
    //       提前 return，版本历史一次都没写。
    //
    // 修法：各步骤把版本信息**回报**给 run()（piece.version），
    //       由 run() 在**全部步骤跑完之后**统一写：
    //         - 原始输入 -> level 0
    //         - 每个步骤的产出 -> 它自己的 level（普通增强=1，二次增强=2）
    //         - 合并结果 -> 取最高 level（多选里含二次增强就是 2）
    //       这样徽标等级与单选路径语义一致。
    if (targets().supportsVersions && typeof pushPromptVersion === "function") {
      // 原始输入（level 0）—— 只有当有产出时才记
      if (originalInput) {
        pushPromptVersion(0, originalInput, "原始提示词");
      }
      // 每一步的产出
      versions.forEach(v => {
        if (v && v.text) pushPromptVersion(v.level, v.text, v.source, v.specId);
      });
      // 合并结果本身也要是一条版本 —— 用户真正写回的是它
      const topLevel = versions.reduce((acc, v) => Math.max(acc, (v && v.level) || 0), 0);
      pushPromptVersion(
        topLevel || 1, combined,
        `合并 ${merged.length} 项增强结果`, "");
      // 徽标：等级由版本谱系判定（currentPromptLevel）。
      // 注意：这里**不**调 markPromptEnhanced() —— 那是「内容已写回输入框」
      // 的标记，由 providers.js 的「替换/追加」写回按钮负责。
      // 这里只刷新徽标显示与版本列表。
      if (typeof updatePromptBadge === "function") updatePromptBadge();
      if (typeof renderPromptVersions === "function") renderPromptVersions();
    }

    setHint(`已按顺序应用 ${merged.length} 项增强，合并后 ${combined.length} 字`, "ok");
  }

  /**
   * 合并多项增强的提示词片段。
   *
   * 规则（与 LoRA 触发词合并同一套思路）：
   *   - 各片段互不覆盖、不互相截断
   *   - 已存在的片段不重复追加
   *   - 顺序 = 执行顺序
   */
  function mergeFragments(parts) {
    const seen = new Set();
    const out = [];
    parts.forEach(p => {
      const text = String(p || "").trim();
      if (!text) return;
      if (seen.has(text)) return;
      seen.add(text);
      out.push(text);
    });
    return out.join("\n\n");
  }

  /**
   * 执行单个增强方式。
   * @param {object} method
   * @param {object} opts collect=true 时不直接写弹窗，只返回产物（供多选合并）
   */
  async function runOne(method, opts) {
    opts = opts || {};
    if (!isAvailable(method)) return null;
    if (method.kind === "yue2-struct") {
      return runYue2Struct(method, opts);
    }
    if (method.id === "layout") {
      return runLayout(method, opts);
    }
    if (method.id === "qwen_pe") {
      return runQwenPe(method, opts);
    }
    if (method.id === "refine") {
      return runRefine(method, opts);
    }
    if (method.id === "director") {
      return runDirector(method, opts);
    }
    return runBasic(method, opts);
  }

  // ---- 音乐：YuE2 结构化风格 + 歌词（走 /api/music-enhance）----
  //
  // 【为什么委托给 music-enhance.js】这条路线由音乐模块实现
  // （web/music-enhance.js 里有完整的请求/回填/契约逻辑）。
  // 这里只做**路由**，保证「一份注册表、一个主按钮、一套组件」。
  async function runYue2Struct(method, opts) {
    const music = window.MusicEnhance;
    if (!music || typeof music.run !== "function") {
      setHint("音乐增强模块未加载，请刷新页面", "error");
      return null;
    }
    // 多选时 collect 语义不适用于结构化产物（结构化对象没法和文本片段合并），
    // 所以音乐路线**只支持单选**。
    if (opts && opts.collect) {
      setHint("音乐结构化增强不能与其它增强同时勾选（产物是结构化对象，无法合并）", "error");
      return null;
    }
    await music.run({});
    return { expanded: "", meta: "" };
  }

  // ---- 视频导演提示词（H3 官方三段式） ----
  //
  // 走既有 /api/enhance，只是把 spec_id 换成 "video-director" ——
  // 模板正文在服务端 h3studio/skills/prompt-enhancer/assets/video-director.txt，
  // 依据本机 ComfyUI 的官方 skill h3-prompt-writing/references/base-en.txt。
  async function runDirector(method, opts) {
    const btn = document.querySelector("#enhanceBtn");
    const text = document.querySelector("#enhanceBtnText");
    const prompt = (opts && opts.input !== undefined)
      ? String(opts.input || "").trim()
      : document.querySelector("#prompt").value.trim();
    if (!prompt) {
      setHint("请先输入要写成导演稿的提示词", "error");
      document.querySelector("#prompt").focus();
      return null;
    }
    btn.disabled = true;
    const originalLabel = text.textContent;
    text.textContent = "正在写导演稿…";
    promptEnhance.busy = true;
    updateActionButtons();
    setHint("正在按 H3 官方三段式写导演稿…", "busy");
    try {
      const d = await apiJSON("POST", "/api/enhance", {
        prompt,
        mode: "replace",
        spec_id: "video-director",
        mode_ctx: enhanceCurrentMode(),
        model_key: enhanceCurrentModelKey(),
      });
      if (!opts.collect) provState.lastEnhanceKind = "";
      const enhanced = d.enhanced || "";
      if (!enhanced) throw new Error("本地模型返回了空结果");
      if (opts.collect) {
        return { expanded: enhanced, meta: buildEnhanceMeta(d) };
      }
      document.querySelector("#enhanceResult").value = enhanced;
      document.querySelector("#enhanceMeta").innerHTML = buildEnhanceMeta(d);
      document.querySelector("#enhanceModal").hidden = false;
      pushPromptVersion(0, prompt, "原始提示词");
      pushPromptVersion(1, enhanced, "视频导演提示词（H3 三段式）", "video-director");
      setHint(`导演稿已生成（${d.provider}）：${enhanced.length} 字`, "ok");
      return { expanded: enhanced, meta: buildEnhanceMeta(d) };
    } catch (e) {
      const detail = e.payload && e.payload.error ? e.payload.error : e.message;
      const who = e.payload && e.payload.provider ? `（${e.payload.provider}）` : "";
      setHint(`导演提示词生成失败：${detail}`, "error");
      alert(`视频导演提示词生成失败${who}\n\n${detail}\n\n请检查「本地模型配置」里的 Provider 是否可用。`);
      return null;
    } finally {
      promptEnhance.busy = false;
      btn.disabled = false;
      text.textContent = originalLabel;
      updateActionButtons();
    }
  }

  // ---- 普通增强（沿用既有 /api/enhance + 模板下拉） ----
  async function runBasic(method, opts) {
    const btn = document.querySelector(targets().btn);
    // 多选时用链式输入（上一项的产出）；单选时用提示词框原文
    const prompt = (opts && opts.input !== undefined)
      ? String(opts.input || "").trim()
      : currentInputValue();
    if (!prompt) {
      const { el } = resolveTarget(targets().inputSelectors);
      if (!el) {
        setHint(`本面板没有可增强的${targets().label}输入框，无法执行增强`, "error");
        return null;
      }
      setHint(`请先输入要增强的${targets().label}`, "error");
      el.focus();
      return null;
    }
    btn.disabled = true;
    btn.classList.add("busy");
    promptEnhance.busy = true;
    updateActionButtons();
    setHint(isMusicContext() ? "正在扩写音乐风格描述…" : "正在增强…", "busy");
    try {
      const d = await apiJSON("POST", "/api/enhance", {
        prompt,
        mode: "replace",
        // 模板由注册表解析（当前上下文的默认模板）——
        // 不再读那个已被删除的模板下拉的 provState.specId。
        spec_id: methodSpecId(method) || currentSpecId(),
        mode_ctx: enhanceCurrentMode(),
        model_key: enhanceCurrentModelKey(),
      });
      provState.lastEnhance = d;
      // 只有**单独**跑普通增强时才清标记；多选时由 run() 统一设置，
      // 否则先跑的普通增强会把后跑的官方 PE 标记冲掉。
      if (!opts.collect) provState.lastEnhanceKind = "";   // 普通增强，不是官方 PE
      // 【2026-11-09】不再写 provState.specId ——
      // 那是「本地模型配置」页用来决定「当前编辑哪条模板」的全局单值，
      // 与本文件的 selectedSpecByMethod（按 mode::method 存）**双状态并存**，
      // 切模式时会互相污染。模板选择状态统一由 selectedSpecByMethod 承担。
      const enhanced = d.enhanced || "";
      if (opts.collect) {
        // 【多选】把「这一步的版本信息」回报给 run()，
        // 由 run() 统一写版本历史 + 打徽标。
        // 为什么不在这里直接写：多选时后面还有别的增强项要跑，
        // 每一步都写一次会留下一堆中间态版本，而且徽标会在
        // 最后一步之前就被「提前」标记 —— 与单选路径的语义不一致。
        return {
          expanded: enhanced,
          meta: buildEnhanceMeta(d),
          version: {
            level: 1,
            text: enhanced,
            source: `模板：${d.spec_id || "image"}`,
            specId: d.spec_id || "",
          },
        };
      }
      // 【单选】写版本历史（徽标由「替换/追加」写回时的 markPromptEnhanced 驱动）
      if (d.enhanced) {
        pushPromptVersion(0, prompt, "原始提示词");
        pushPromptVersion(1, d.enhanced, `模板：${d.spec_id || "image"}`, d.spec_id);
      }
      document.querySelector("#enhanceResult").value = enhanced;
      document.querySelector("#enhanceMeta").innerHTML = buildEnhanceMeta(d);
      document.querySelector("#enhanceModal").hidden = false;
      setHint(`已用「${d.provider}」扩写，${d.enhanced ? d.enhanced.length : 0} 字`, "ok");
      return { expanded: enhanced, meta: buildEnhanceMeta(d) };
    } catch (e) {
      const detail = e.payload && e.payload.error ? e.payload.error : e.message;
      const who = e.payload && e.payload.provider ? `（${e.payload.provider}）` : "";
      setHint(`增强失败：${detail}`, "error");
      alert(`提示词增强失败${who}\n\n${detail}\n\n请检查「本地模型配置」里的 Provider 是否可用。`);
      return null;
    } finally {
      btn.disabled = false;
      btn.classList.remove("busy");
      promptEnhance.busy = false;
      updateActionButtons();
    }
  }

  // ---- 二次增强（沿用既有 /api/enhance + image-refine 模板） ----
  async function runRefine(method, opts) {
    const btn = document.querySelector(targets().btn);
    const text = document.querySelector(targets().btnText);
    // 多选时用链式输入（上一项的产出）；单选时用既有语义
    // （refineTarget=1 时取「一次增强」版本，否则取提示词框原文）
    const source = (opts && opts.input !== undefined)
      ? String(opts.input || "").trim()
      : refineSourcePrompt();
    if (!source) {
      const { el } = resolveTarget(targets().inputSelectors);
      if (!el) {
        setHint(`本面板没有可细化的${targets().label}输入框，无法执行二次增强`, "error");
        return null;
      }
      setHint(`请先输入要细化的${targets().label}`, "error");
      el.focus();
      return null;
    }
    const targetLevel = promptVersions.refineTarget === 1 ? 1 : 0;
    btn.disabled = true;
    text.textContent = "正在细化…";
    promptVersions.busy = true;
    updateActionButtons();
    setHint("正在逐个物体细化…", "busy");
    try {
      const d = await apiJSON("POST", "/api/enhance", {
        prompt: source,
        mode: "replace",
        // 「二次增强」是通用能力，模板按媒体类型自动选：
        // 图像/视频 = image-refine（逐个画面物体）；音乐 = music-refine（逐个音乐要素）。
        // 不再硬编码 image-refine —— 硬编码会让音乐模式拿到画面物体向的模板。
        spec_id: methodSpecId(method) || refineSpecId(),
        mode_ctx: enhanceCurrentMode(),
        model_key: enhanceCurrentModelKey(),
      });
      // 【2026-11-09】不再写 provState.specId（见 runBasic 的同名注释）
      if (!opts.collect) provState.lastEnhanceKind = "";   // 二次增强，不是官方 PE
      const refined = d.enhanced || "";
      if (!refined) throw new Error("本地模型返回了空结果");
      if (opts.collect) {
        // 【多选】回报版本信息，由 run() 统一写版本历史 + 打徽标
        return {
          expanded: refined,
          meta: buildEnhanceMeta(d),
          version: {
            level: 2,
            text: refined,
            source: `模板：${d.spec_id || refineSpecId()}`,
            specId: d.spec_id || "",
          },
        };
      }
      // 写回输入框（按能力契约；找不到目标时给可读提示，不抛异常）
      if (!writeBackInput(refined, method)) {
        throw new Error(`本面板找不到可写回的${targets().label}输入框，增强结果未写入`);
      }
      pushPromptVersion(2, refined, `模板：${d.spec_id || refineSpecId()}`, d.spec_id);
      markPromptEnhanced();
      updatePromptBadge();
      renderPromptVersions();
      pushPromptVersion(targetLevel, source,
        targetLevel === 1 ? "一次增强（本次二次增强的输入）" : "原始提示词（本次二次增强的输入）");
      document.querySelector("#enhanceResult").value = refined;
      document.querySelector("#enhanceMeta").innerHTML = buildEnhanceMeta(d);
      document.querySelector("#enhanceModal").hidden = false;
      setHint(`二次增强完成（${d.provider}）：逐个物体细化 ${refined.length} 字`, "ok");
      return { expanded: refined, meta: buildEnhanceMeta(d) };
    } catch (e) {
      const detail = e.payload && e.payload.error ? e.payload.error : e.message;
      const who = e.payload && e.payload.provider ? `（${e.payload.provider}）` : "";
      setHint(`二次增强失败：${detail}`, "error");
      alert(`二次增强失败${who}\n\n${detail}\n\n请检查「本地模型配置」里的 Provider 是否可用。`);
      return null;
    } finally {
      promptVersions.busy = false;
      btn.disabled = false;
      updateButton();   // 多选：按钮文字取决于选中项数，不能只取第一项
      updateActionButtons();
    }
  }

  // ---- 结构化增强（layout）—— 调既有 /api/moss-layout ----
  async function runLayout(method, opts) {
    const btn = document.querySelector(targets().btn);
    const text = document.querySelector(targets().btnText);
    const prompt = currentInputValue();
    if (!prompt) {
      const { el } = resolveTarget(targets().inputSelectors);
      if (!el) {
        setHint(`本面板没有可增强的${targets().label}输入框，无法执行结构化增强`, "error");
        return null;
      }
      setHint(`请先输入要增强的${targets().label}`, "error");
      el.focus();
      return null;
    }
    const intentEl = document.querySelector("#mossLayoutIntent");
    const intent = intentEl ? intentEl.value.trim() : "";
    const [w, h] = currentCanvasSize();
    btn.disabled = true;
    text.textContent = "生成中…";
    promptEnhance.busy = true;
    updateActionButtons();
    setHint("正在生成 MOSS Layout…", "busy");
    try {
      const d = await apiJSON("POST", "/api/moss-layout", {
        prompt, intent, width: w, height: h,
      });
      // 产物渲染交给 moss-layout.js（弹窗的归属方）——
      // 这里只管「取数据 -> 交给视图 -> 显示弹窗」
      const view = window.mossLayoutUI;
      if (!view || typeof view.renderResult !== "function") {
        throw new Error("结构化增强视图未就绪，请刷新页面后重试");
      }
      view.renderResult(d);
      view.openModal();
      setHint(`结构化增强完成（${d.provider}）：${d.layout.items.length} 个元素`, "ok");
      return { expanded: "", meta: "" };
    } catch (e) {
      const detail = e.payload && e.payload.error ? e.payload.error : e.message;
      const who = e.payload && e.payload.provider ? `（${e.payload.provider}）` : "";
      setHint(`结构化增强失败：${detail}`, "error");
      alert(`MOSS 结构化增强失败${who}\n\n${detail}\n\n`
        + `请检查「本地模型配置」里的 Provider 是否可用。`);
      return null;
    } finally {
      promptEnhance.busy = false;
      btn.disabled = false;
      updateButton();   // 多选：按钮文字取决于选中项数，不能只取第一项
      updateActionButtons();
    }
  }

  // ---- Qwen-Image 2.1 官方 PE 扩写 —— 调 /api/qwen-pe/expand ----
  //
  // 与其它增强方式的**回填语义完全一致**：
  //   结果进同一个弹窗（#enhanceResult）+ 同一个「替换/追加」按钮组，
  //   替换后标记为「已增强」并进版本历史，用户可继续编辑提示词。
  //
  // 差异只有两点：
  //   1. 不走 Provider 配置，走官方 PE checkpoint（ComfyUI TextGenerate）；
  //   2. 图片编辑模式下要把参考图一起送去 —— 官方 PE-I2I 工作流就是这么接的
  //      （BatchImagesNode -> TextGenerate.image），PE 才能「看着图」改写，
  //      并在输出里写 <image1>/<image2>。
  //
  // 【system prompt 与档位参数】
  //   这两项来自该增强项自带的可编辑字段（用户改过就以用户的为准），
  //   没改过就用官方内置值（后端 /api/enhancer 下发的模板 + 官方卡值）。
  //   同一份 system prompt 也随出图参数提交 -> 扩写与出图走同一套规则。
  async function runQwenPe(method, opts) {
    const btn = document.querySelector(targets().btn);
    const text = document.querySelector(targets().btnText);
    // 多选时用链式输入（上一项的产出）；单选时用提示词框原文
    const prompt = (opts && opts.input !== undefined)
      ? String(opts.input || "").trim()
      : currentInputValue();
    if (!prompt) {
      const { el } = resolveTarget(targets().inputSelectors);
      if (!el) {
        setHint(`本面板没有可增强的${targets().label}输入框，无法执行官方 PE 扩写`, "error");
        return null;
      }
      setHint(`请先输入要增强的${targets().label}`, "error");
      el.focus();
      return null;
    }
    const mode = (typeof state !== "undefined" && state) ? (state.mode || "") : "";
    if (mode !== "t2i_qwen" && mode !== "i2i_qwen") {
      setHint("官方 PE 扩写只在 Qwen-Image 2.1 的文生图 / 图片编辑模式下可用", "error");
      return null;
    }
    // 参考图：素材库来源，顺序即 <imageN> 顺序（与出图链路同一份 payload）
    let refs = [];
    if (mode === "i2i_qwen" && window.QwenRefs
        && typeof window.QwenRefs.toPayload === "function") {
      refs = window.QwenRefs.toPayload();
    }
    // 可编辑字段：用户改过就带上，没改过就不传（后端用官方内置值）
    const peMethod = byId.qwen_pe;
    const sp = effectiveFieldValue(peMethod, { key: "systemPrompt" });
    const peParams = {};
    const pp = effectiveFieldValue(peMethod, { key: "presencePenalty" });
    const mn = effectiveFieldValue(peMethod, { key: "maxNewTokens" });
    if (pp !== "" && pp !== undefined && pp !== null) peParams.presence_penalty = Number(pp);
    if (mn !== "" && mn !== undefined && mn !== null) peParams.max_new_tokens = Number(mn);
    const payload = { prompt, mode, refs };
    if (sp !== undefined && sp !== null && String(sp).trim()) {
      payload.system_prompt = String(sp);
    }
    if (Object.keys(peParams).length) payload.pe_params = peParams;

    btn.disabled = true;
    text.textContent = "官方PE扩写中…";
    promptEnhance.busy = true;
    updateActionButtons();
    setHint("官方 PE 扩写中（9B 模型 + thinking，约需半分钟）…", "busy");
    try {
      const d = await apiJSON("POST", "/api/qwen-pe/expand", payload);
      const expanded = d.expanded || "";
      if (!expanded) throw new Error("官方 PE 返回了空结果");
      const meta = buildQwenPeMeta(d);
      if (opts.collect) {
        // 多选：只回报「这次是官方 PE 的」，写回标记由 run() 统一设置
        return { expanded, meta, isPe: true };
      }
      // 回填到弹窗（与其它增强方式同一个弹窗 / 同一组按钮）
      document.querySelector("#enhanceResult").value = expanded;
      document.querySelector("#enhanceMeta").innerHTML = meta;
      document.querySelector("#enhanceModal").hidden = false;
      // 记录「这次结果是官方 PE 的」—— 真正**写回提示词框**时
      // （用户在弹窗里点「替换提示词框 / 追加到原提示词」）
      // 才把出图时的二次扩写关掉。providers.js 那边消费这个标记。
      // （不能在拿到结果时就关：那只是弹窗里的预览，用户可能直接取消。）
      if (typeof provState !== "undefined") {
        provState.lastEnhanceKind = "qwen_pe";
      }
      // 进版本历史（与普通增强一致）：
      //   level 0 = 原始提示词，level 1 = 官方 PE 扩写结果。
      // 不做这一步的话，写回提示词框后「已增强」徽标不会出现
      // （徽标靠版本谱系判定级别，不是靠 markPromptEnhanced）。
      if (expanded) {
        pushPromptVersion(0, prompt, "原始提示词");
        pushPromptVersion(1, expanded,
          `官方 PE：${d.tier_label || d.tier || ""}`.trim(), "qwen21-pe");
      }
      setHint(`官方 PE 扩写完成（${d.tier_label || d.tier}）：${expanded.length} 字；`
        + `点「替换提示词框」写回后，出图将不再二次扩写`, "ok");
      return { expanded, meta };
    } catch (e) {
      const detail = e.payload && e.payload.error ? e.payload.error : e.message;
      setHint(`官方 PE 扩写失败：${detail}`, "error");
      alert(`Qwen 官方 PE 扩写失败\n\n${detail}\n\n`
        + `请确认 ComfyUI 正在运行，且官方 PE 权重（qwen3.5_qwen_image_2.1_pe_*.safetensors）已就位。`);
      return null;
    } finally {
      promptEnhance.busy = false;
      btn.disabled = false;
      updateButton();   // 多选：按钮文字取决于选中项数，不能只取第一项
      updateActionButtons();
    }
  }

  /** 官方 PE 扩写结果的 meta 行（与其它增强方式同一块区域）。 */
  function buildQwenPeMeta(d) {
    const parts = [];
    parts.push(`<b>官方 PE</b>：${esc(d.tier_label || d.tier || "-")}`);
    if (d.clip_name) {
      parts.push(`checkpoint：<code>${esc(d.clip_name)}</code>`);
    }
    if (d.ref_count) parts.push(`参考图：${Number(d.ref_count)} 张`);
    if (d.pe_params) {
      parts.push(`已套用参数：presence_penalty=${esc(d.pe_params.presence_penalty)}、`
        + `max_new_tokens=${esc(d.pe_params.max_new_tokens)}`);
    }
    parts.push(`字数：${(d.expanded || "").length}`);
    return parts.join(" · ");
  }

  function setHint(text, kind) {
    const hint = document.querySelector(targets().hint);
    if (!hint) return;
    hint.textContent = text;
    hint.className = "enhance-hint" + (kind ? " " + kind : "");
  }

  // ------------------------------------------------------------------ 绑定

  function bind() {
    // 两个面板各有一套增强元素（图像/视频一套、音乐一套）——
    // 两边都绑一遍，切换模式时不需要重新绑定。
    [["#enhanceBtn", "#enhanceMenuBtn", "#enhanceMenu", "#enhanceSplit"],
     ["#musicEnhanceBtn", "#musicEnhanceMenuBtn", "#musicEnhanceMenu", "#musicEnhanceSplit"]]
    .forEach(([btnSel, menuBtnSel, menuSel, splitSel]) => {
      const btn = document.querySelector(btnSel);
      if (btn) btn.addEventListener("click", () => run());
      const menuBtn = document.querySelector(menuBtnSel);
      if (menuBtn) {
        menuBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          // 只有当前上下文那一套的菜单能开
          if (menuBtnSel !== targets().menuBtn) return;
          toggleMenu();
        });
      }
    });
    // 【模板选择已收进增强菜单项内】
    // 模板下拉现在由共享组件渲染在菜单项内部（select[data-spec-method]），
    // 共享组件已经绑好 change 事件（onSpecChange -> setMethodSpec）。
    // 所以这里**不再**需要绑定已删除的平级模板行。
    // 点别处关菜单
    document.addEventListener("click", (e) => {
      const t = targets();
      const menu = document.querySelector(t.menu);
      if (!menu || menu.hidden) return;
      if (menu.contains(e.target)) return;
      if (e.target.closest && e.target.closest(t.split)) return;
      closeMenu();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") closeMenu();
    });
    // 窗口尺寸变化时菜单位置会失效 -> 关掉（下次打开会重新定位）
    window.addEventListener("resize", () => closeMenu());
  }

  // ------------------------------------------------------------------ 启动

  function boot() {
    bind();
    loadSelection();
    ensureAvailable();
    updateButton();
    renderMenu();
    renderSpecRow();
    updatePanelVisibility();
    applySelectedParams();
    if (typeof window !== "undefined") {
      window.enhanceMethods = {
        current: selectedIds[0] || "basic",
        selected: selectedIds.slice(),
        list: ENHANCE_METHODS,
        toggle: toggleMethod,
        run: run,
        renderMenu: renderMenu,
        updatePanelVisibility: updatePanelVisibility,
        ensureAvailable: ensureAvailable,
        refreshButton: updateButton,
        mergeFragments: mergeFragments,
        getSelected: () => selectedIds.slice(),
        // 【写回契约 · 审查 P0-1】按增强方式取它自己声明的写回契约。
        // 外部调用方（providers.js 的「替换/追加」写回）用它，
        // 而不是各自硬编码某个 id。
        getWriteContract: (methodId) => {
          // 注意：默认取**当前可运行的**方式（runnableMethods），
          // 不能直接用 currentMethod() —— 后者不按可用性过滤，
          // 在音乐面板可能取到已失效的文本扩写类（basic 排在注册表最前），
          // 那样会拿到文本契约而漏掉结构化契约。
          const m = methodId
            ? byId[methodId]
            : (runnableMethods()[0] || currentMethod());
          return methodWriteContract(m);
        },
        // 统一增强体系对外接口（供测试与其它面板使用）
        getMethods: () => ENHANCE_METHODS.map(m => ({
          id: m.id, label: m.label, modes: m.modes || [], kind: m.kind,
          specId: methodSpecId(m), specLabel: methodSpecLabel(m),
          write: m.write || { kind: "text" },
        })),
        getMethodSpec: (methodId) => {
          const m = byId[methodId];
          return m ? methodSpecId(m) : "";
        },
        setMethodSpec: setMethodSpec,
        getRefineSpecId: refineSpecId,
        getSpecOptions: (methodId) => {
          const m = byId[methodId];
          return m ? methodSpecOptions(m) : [];
        },
        // 【2026-11-10】模板选择已收进增强菜单项内（renderSpecPicker），
        // 不再有平级的模板行；本接口保留但返回「无模板行」状态。
        renderSpecRow: renderSpecRow,
        invalidateSpecSelection: invalidateSpecSelection,
        getSelectedSpecByMethod: () => Object.assign({}, selectedSpecByMethod),
        getSpecRowState: () => {
          const t = targets();
          // 注意：specSelect/specRow 现在是空串（元素已删除），
          // querySelector("") 会抛 SyntaxError —— 必须先判空。
          const sel = t.specSelect ? document.querySelector(t.specSelect) : null;
          const row = t.specRow ? document.querySelector(t.specRow) : null;
          return {
            hidden: row ? !!row.hidden : null,
            value: sel ? sel.value : "",
            options: sel ? Array.from(sel.options).map(o => o.value) : [],
            method: currentMethod() ? currentMethod().id : "",
            // 模板现在在菜单项内，用这个字段告诉调用方去哪里找
            inMenu: true,
          };
        },
        isAvailable: (methodId) => {
          const m = byId[methodId];
          return m ? isAvailable(m) : false;
        },
        getFieldValue: fieldValue,
        getEffectiveFieldValue: (methodId, fieldKey) => {
          const m = byId[methodId];
          if (!m) return "";
          const f = (m.fields || []).find(x => x.key === fieldKey);
          return f ? effectiveFieldValue(m, f) : "";
        },
      };
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
