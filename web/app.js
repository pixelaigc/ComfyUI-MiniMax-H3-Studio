/* H3 Studio 前端逻辑 */
const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];

// ---------- 页面显隐（统一切页契约） ----------
//
// 所有主页面（jobsArea / panel / stitchPanel / systemPage / providersPage /
// assetsPage / agentPage）都靠 .page-hidden 这个类切换显隐，
// **不再用内联 style.display**。
//
// 为什么必须这样：内联 style.display 的优先级高于任何 CSS 规则。
// 只要有人写了 style.display="block"，.agent-page{display:flex} 就被覆盖 ——
// flex 布局塌掉后 .ag-messages 的 flex:1 + min-height:0 全部失效，
// 消息区按内容撑高、composer 被顶出视口，而 body{overflow:hidden} 让整页滚不动。
// 结果是窄屏下对话一变长，输入框就彻底不可达（实测 390px 下
// inputTop=3068 / reachable=false，滚轮与触摸滑动都无效）。
//
// 显隐写成类之后，任何一条 CSS 规则都能覆盖它，不会再出现「一个人改坏了整条链」。
//
// 显示时**不**套一个统一的 .page-visible{display:flex} ——
// 各页的布局不同（.jobs-area/.system-page 是块级，.assets-page/.agent-page 是 flex），
// 统一成 flex 会改变它们的内部排版。这里只管「隐藏」，显示时把内联 display
// 整个移除，让该页自己的 CSS 规则决定布局。
function showPage(el, visible) {
  if (!el) return;
  el.classList.toggle("page-hidden", !visible);
  el.style.removeProperty("display");
}

// 页面当前是否可见（按计算样式判断，不依赖内联 display —— 现在已经没有内联 display 了）
function pageIsVisible(el) {
  if (!el) return false;
  if (el.classList.contains("page-hidden")) return false;
  return getComputedStyle(el).display !== "none";
}

const MODE_INFO = {
  t2v: { name: "文生视频", kind: "video" },
  flf: { name: "首尾帧", kind: "video" },
  r2v: { name: "参考生成", kind: "video" },
  t2i: { name: "Z-IMAGE 文生图", kind: "image" },
  // Qwen-Image 2.1：文生图与图片编辑共用一套参数面板，
  // 差别在「有没有参考图」和「PE 编码器用 t2i 还是 i2i 版」
  t2i_qwen: { name: "Qwen-Image 2.1 文生图", kind: "image", engine: "qwen" },
  i2i_qwen: { name: "Qwen-Image 2.1 图片编辑", kind: "image", engine: "qwen" },
  // MOSS-Image：OpenMOSS 的 dense text-to-image transformer，底模是
  // diffusers 目录（transformer/ + text_encoder/ + tokenizer/ + vae/）。
  t2i_moss: { name: "MOSS-Image 文生图", kind: "image", engine: "moss" },
  longfilm: { name: "长片", kind: "video" },
  // 音乐（YuE2）：文生音乐 / 参考生音乐
  t2a: { name: "文生音乐", kind: "audio", engine: "music" },
  a2a: { name: "参考生音乐", kind: "audio", engine: "music" },
};

// 图像类模式：共用「图像」参数面板
const IMAGE_MODES = ["t2i", "t2i_qwen", "i2i_qwen", "t2i_moss"];
// Qwen-Image 2.1 专用模式（参数清单与 Z-Image 不同）
const QWEN_MODES = ["t2i_qwen", "i2i_qwen"];
// MOSS-Image 专用模式（参数清单与 Z-Image / Qwen 都不同）
const MOSS_MODES = ["t2i_moss"];

function isQwenMode(mode) {
  return QWEN_MODES.includes(mode || state.mode);
}

function isMossMode(mode) {
  return MOSS_MODES.includes(mode || state.mode);
}

// 默认模式：整个应用只有这一个来源（导航高亮 / 参数面板 / state.mode 都用它）
const DEFAULT_MODE = "t2i";

// ---------- 生成库：每页作品数 ----------
// 可选值刻意覆盖「宽屏一行能铺满」的档位：22:9 超宽屏下单行常能放 8~12 张，
// 每页 12/16/24 才能把宽度用起来；8 是窄屏/16:9 下的保守档。
const PAGE_SIZE_OPTIONS = [8, 12, 16, 24, 32, 48];
const PAGE_SIZE_DEFAULT = 12;
const PAGE_SIZE_KEY = "h3studio.pageSize";

// localStorage 在隐私模式 / 禁用存储时会抛错，读取失败一律退回默认值。
function loadPageSize() {
  try {
    const raw = localStorage.getItem(PAGE_SIZE_KEY);
    const value = Number(raw);
    // 只接受重定义列。NaN/Infinity 之类的值不能当数字字面量用，
    // page_size=NaN 会让服务端返回 400，整个列表都渲染不出来。
    if (Number.isInteger(value) && PAGE_SIZE_OPTIONS.includes(value)) return value;
  } catch (e) { /* 存储不可用，用默认值 */ }
  return PAGE_SIZE_DEFAULT;
}

function savePageSize(value) {
  try { localStorage.setItem(PAGE_SIZE_KEY, String(value)); } catch (e) { /* 忽略 */ }
}

const state = {
  // 默认进入「Z-IMAGE 文生图」。这里只是兜底初值 —— 启动时 activateMode()
  // 会用 DEFAULT_MODE 再赋一次，避免两处各写一份字面量后走偏。
  mode: DEFAULT_MODE,
  tab: "all",
  genType: "",
  page: "create",
  res: "544p",
  model: "lora",
  lora: "ckpt850",
  loraStrength: 1.0,
  aspect: "16:9",
  duration: 5,
  steps: 6,
  donePage: 1,
  // 每页作品数（可配置，持久化在 localStorage）
  pageSize: loadPageSize(),
  doneFilters: { keyword: "", media: "", aspect: "", duration: "" },
  stitchClips: [],
  files: { first: null, last: null, refs: [] },
  // 素材库引用：{ 槽位: {asset_id, name, category} } —— 只存 id，不复制文件
  assetRefs: {},
  timer: null,
  genBusy: false,
  // Qwen-Image 2.1 专属状态（与 Z-Image 分开，避免两个模型的参数互相污染）
  imgQwen: {
    tier: "1mp",
    aspect: "16:9",
    // 自定义宽高（与 Z-Image 同一套逻辑）
    custom: false,
    width: null,
    height: null,
    rawWidth: null,
    rawHeight: null,
    model: "qwen_image_2.1_bf16",
    // 步数随「模型版本」联动：基座 40 步 / Turbo 8 步（蒸馏）
    // 初始值对齐默认模型（基座）的推荐步数
    steps: 40,
    cfg: 1,
    shift: 0.69,
    sampler: "euler",
    scheduler: "simple",
    negative: "",
    batchCount: 1,
    // 官方 PE 提示词扩写：出图时**始终**走官方 PE 接线（官方最佳实践）。
    // 「手动扩写」入口已并入「增强」下拉菜单（「qwen21图片官方增强PE」），
    // 不再是面板上的独立勾选框 —— 避免两处开关语义打架。
    usePe: true,
    // 官方 PE 的档位参数（presence_penalty / max_new_tokens）：
    // 由「增强」多选列表里官方 PE 那一项**自动套用**（选中即套用，可再改）。
    // 多项共存时是叠加结果 —— 后一项不会清掉前一项已套用的参数。
    peParams: null,
    // 官方 PE 的 system prompt（用户在「增强」多选列表里编辑过的那份）：
    // 与手动扩写用同一份文本 -> 扩写与出图走同一套规则。
    peSystemPrompt: "",
    loras: [],
    // 参考图（图片编辑用）**已搬到 web/qwen-refs.js**（window.QwenRefs）——
    // 参考图强制来自素材库，状态由那个模块自己管。
  },
  // MOSS-Image 文生图专属状态（与 Z-Image / Qwen 分开，避免参数互相污染）
  imgMoss: {
    tier: "1mp",
    aspect: "16:9",
    // 自定义宽高：custom=true 表示用户手填过（哪怕只填了一边）
    custom: false,
    width: null,
    height: null,
    // 用户**原始输入**（未经吸附）—— 用来算「原始值 vs 生效值」的差异说明
    rawWidth: null,
    rawHeight: null,
    model: "moss_image_base",
    // 官方 USERGUIDE：50 步 / cfg 3 / euler / sgm_uniform / shift 5
    steps: 50,
    cfg: 3,
    shift: 5,
    sampler: "euler",
    scheduler: "sgm_uniform",
    // 官方推荐的负面提示词（首次进入时由服务端默认值预填）
    negative: "",
    batchCount: 1,
    // 效果 LoRA 配置：[{name, strength, enabled}]，服务端持久化
    loras: [],
    // SLG（SkipLayerGuidance）skip layer —— 官方推荐 22；空 = 关
    slgSingle: "22",
  },
  // 图像模式（Z-Image 文生图）专属状态
  img: {
    tier: "1mp",
    aspect: "16:9",
    // 自定义宽高：custom=true 表示用户手填过（哪怕只填了一边）
    custom: false,
    width: null,
    height: null,
    // 用户**原始输入**（未经吸附）—— 用来算「原始值 vs 生效值」的差异说明。
    // 吸附后的值存在 width/height 里。
    rawWidth: null,
    rawHeight: null,
    model: "z_image_turbo_bf16",
    steps: 8,
    cfg: 1,
    shift: 3,
    sampler: "res_multistep",
    scheduler: "simple",
    negative: "",
    batchCount: 1,
    // LoRA 配置：[{name, strength, enabled}]，服务端持久化
    loras: [],
  },
};

// 生成类型清单，由 /api/generation-types 下发（后端真实注册的模式），
// 前端不硬编码任何类型名。
const GEN_TYPES = { types: [], registeredModes: [] };

// 音乐模式（YuE2）：文生音乐 / 参考生音乐
const MUSIC_MODES = ["t2a", "a2a"];
function isMusicMode(mode) {
  return MUSIC_MODES.includes(mode || state.mode);
}

// music.js 需要读当前模式 —— 只暴露一个只读 getter，不把整个 state 交出去
window.__h3state = () => ({ mode: state.mode });

// 图像模式的档位 / 比例 / 模型清单，由 /api/image-options 下发（服务端与本机
// ComfyUI 的实际权重、官方推荐参数一致），前端不硬编码模型文件名。
const IMG_OPTIONS = { tiers: [], aspects: [], models: {}, sizeMultiple: 16,
                    maxDirectMp: 3, sizeMin: 16, sizeMax: 16384 };
// Qwen-Image 2.1 的独立一套清单（服务端 /api/image-options 的 qwen 字段）
const QWEN_OPTIONS = {
  tiers: [], aspects: [], models: {}, sizeMultiple: 16, maxDirectMp: 4,
  sizeMin: 16, sizeMax: 16384,
  defaultTier: "1mp", defaultModel: "qwen_image_2.1_bf16",
  // 参考图上限：模型官方能力 10 张（ComfyUI 节点 16 槽 -> 取 10）
  maxRefImages: 10, minRefImages: 1, nodeRefSlots: 16,
  // 官方 PE 双档参数（presence_penalty 不可互换）
  peParams: {}, peDefaultMaxLength: 4096,
};
// MOSS-Image 的独立一套清单（服务端 /api/image-options 的 moss 字段）
const MOSS_OPTIONS = {
  tiers: [], aspects: [], models: {}, sizeMultiple: 16, maxDirectMp: 4,
  minMp: 0.25, sizeMin: 16, sizeMax: 16384,
  defaultTier: "1mp", defaultModel: "moss_image_base",
  // 官方推荐的负面提示词（首次进入该模式时预填）
  defaultNegative: "",
};
// 本机实际存在的 Z-Image LoRA（服务端下发），以及当前配置
// available = 效果类 LoRA（用户可选）
// acceleration = 加速/蒸馏类（工作流固定使用，不进下拉）
// available = 效果类 LoRA（用户可选，且已通过兼容性检测）
// acceleration = 加速/蒸馏类（工作流固定使用，不进下拉）
// incompatible = 效果类但与 Z-Image 底模不兼容（会出噪点，已排除）
const IMG_LORAS = { available: [], acceleration: [], incompatible: [], config: [] };
// Qwen-Image 2.1 的 LoRA 清单（与 Z-Image 分开：底模结构不同，
// 兼容性要各自比对，混在一起会让用户选到不匹配的权重）
const QWEN_LORAS = { available: [], acceleration: [], incompatible: [], config: [] };
// MOSS-Image 的 LoRA 清单（与其它族分开：底模结构不同，不能混用）
const MOSS_LORAS = { available: [], acceleration: [], incompatible: [], config: [] };
// 服务端下发的功能位（后端重启后才有新位）
let SERVER_FEATURES = {};
// 后端是否已支持 MOSS-Image（/api/image-options 有 moss 字段才算）
let MOSS_BACKEND_READY = false;
// 后端代码指纹（用于提示「后端没重启」）
let SERVER_FINGERPRINT = "";
const IMG_ASPECT_NUM = {
  "1:1": [1, 1], "2:3": [2, 3], "3:2": [3, 2], "3:4": [3, 4],
  "4:3": [4, 3], "9:16": [9, 16], "16:9": [16, 9], "21:9": [21, 9],
};

// ---------- 侧栏切换 ----------
// 选中态与面板内容始终保持一致：同一个入口负责「打高亮 + 切面板」，
// 启动时、手动点击时都走它，避免两者不同步。
function activateMode(mode, navEl) {
  $$(".nav-item").forEach(x => x.classList.remove("active"));
  if (navEl) navEl.classList.add("active");
  state.mode = mode;
  showCreate();
  renderMode();
}

$$(".nav-item[data-mode]").forEach(el => el.addEventListener("click", () => {
  activateMode(el.dataset.mode, el);
}));

function renderMode() {
  // 模式切换后二次增强的作用对象 chips 状态要跟着刷新
  queueMicrotask(() => { try { renderPromptVersions(); } catch (e) { /* 忽略 */ } });
  // 模式切换后「增强」下拉菜单的可用性要跟着刷新
  // （结构化增强只在 MOSS-Image 面板可用、官方 PE 扩写只在 Qwen 面板可用 ——
  //   切走时该选项自动禁用，当前方式若不可用则回落到普通增强）
  queueMicrotask(() => {
    try {
      if (window.enhanceMethods) {
        window.enhanceMethods.renderMenu();
        // 当前方式在新模式下不可用 -> 回落普通增强
        // （否则主按钮会停在一个「当前模式不可用」的方式上，
        //   用户点「增强」却什么都没发生）
        if (typeof window.enhanceMethods.ensureAvailable === "function") {
          window.enhanceMethods.ensureAvailable();
        }
        window.enhanceMethods.updatePanelVisibility();
        // 「当前增强方式」回显要跟着模式换（音乐面板是另一套 DOM id）
        if (typeof window.enhanceMethods.refreshButton === "function") {
          window.enhanceMethods.refreshButton();
        }
      }
      // 音乐面板有**自己的一套**增强菜单（音乐侧注册表）—— 切到音乐模式时
      // 也要刷新它的按钮文字与菜单（否则按钮还显示上一个模式的文案）。
      if (window.musicEnhance) {
        window.musicEnhance.updateButton();
        window.musicEnhance.renderMenu();
      }
    } catch (e) { /* 忽略 */ }
  });
  const mode = state.mode;
  const info = MODE_INFO[mode];
  // 上传区
  const zone = $("#uploadZone"), row = $("#uploadRow");
  row.innerHTML = "";
  const mk = (key, label) => {
    const slot = document.createElement("div");
    slot.className = "up-slot";
    slot.innerHTML = `+<span class="tag">${label}</span>`;
    const inp = document.createElement("input");
    inp.type = "file"; inp.accept = "image/*";
    const fillSlot = (file, previewUrl) => {
      const url = previewUrl || URL.createObjectURL(file);
      slot.replaceChildren();
      const image = document.createElement("img");
      image.src = url;
      image.alt = label;
      const tag = document.createElement("span");
      tag.className = "tag";
      tag.textContent = label;
      slot.append(image, tag);
      slot.classList.add("filled");
      state.files[key] = file;
      delete state.assetRefs[key];
    };
    // 用素材库里的素材填这个槽位：只存 asset_id，不复制文件
    const fillFromAsset = (asset) => {
      slot.replaceChildren();
      const image = document.createElement("img");
      image.src = asset.preview_url || asset.content_url;
      image.alt = label;
      const tag = document.createElement("span");
      tag.className = "tag";
      tag.textContent = label;
      const badge = document.createElement("span");
      badge.className = "tag asset-badge";
      badge.textContent = "素材库";
      slot.append(image, tag, badge);
      slot.classList.add("filled");
      state.files[key] = null;
      state.assetRefs[key] = {asset_id: asset.id, name: asset.name, category: asset.category};
    };
    inp.addEventListener("change", e => {
      const file = e.target.files[0];
      if (file) fillSlot(file);
    });
    slot.addEventListener("dragover", event => {
      const types = event.dataTransfer.types;
      if (!types.includes("application/x-h3-image") && !types.includes("application/x-h3-asset")) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = "copy";
      slot.classList.add("drag-over");
    });
    slot.addEventListener("dragleave", () => slot.classList.remove("drag-over"));
    slot.addEventListener("drop", async event => {
      // 优先按 asset_id 引用（素材库拖过来的），无 asset_id 时回退旧的 blob 路径
      const assetRaw = event.dataTransfer.getData("application/x-h3-asset");
      if (assetRaw) {
        event.preventDefault();
        slot.classList.remove("drag-over");
        try {
          const payload = JSON.parse(assetRaw);
          const resp = await fetch(`/api/assets/${encodeURIComponent(payload.asset_id)}`);
          if (!resp.ok) throw new Error("素材不存在或已删除");
          const asset = await resp.json();
          const version = asset.current_version || {};
          fillFromAsset({
            id: asset.id, name: asset.name, category: asset.category,
            preview_url: asset.preview_url,
            content_url: asset.content_url || `/api/assets/${asset.id}/content`,
          });
        } catch (error) {
          alert("无法引用这个素材：" + error.message);
        }
        return;
      }
      const raw = event.dataTransfer.getData("application/x-h3-image");
      if (!raw) return;
      event.preventDefault();
      slot.classList.remove("drag-over");
      slot.classList.add("loading");
      try {
        const source = JSON.parse(raw);
        const response = await fetch(source.url, {cache: "no-store"});
        if (!response.ok) throw new Error("图片读取失败");
        const blob = await response.blob();
        const file = new File([blob], source.name || "completed-image.png", {type: blob.type || "image/png"});
        fillSlot(file, source.url);
      } catch (error) {
        alert("无法引用这张图片：" + error.message);
      } finally {
        slot.classList.remove("loading");
      }
    });
    slot.addEventListener("click", event => {
      if (event.target.closest(".pick-asset")) return;
      inp.click();
    });
    // 「从素材库选」按钮
    const pickBtn = document.createElement("button");
    pickBtn.type = "button";
    pickBtn.className = "pick-asset";
    pickBtn.textContent = "素材库";
    pickBtn.title = "从素材库选择一个素材填入这个位置";
    pickBtn.addEventListener("click", event => {
      event.stopPropagation();
      if (!window.ASSETLIB || !window.ASSETLIB.openPicker) {
        alert("素材库模块未加载");
        return;
      }
      window.ASSETLIB.openPicker({
        categories: ["image"], multiple: false, limit: 1,
        onDone: (ids) => {
          if (!ids.length) return;
          const assetId = ids[0];
          fetch(`/api/assets/${encodeURIComponent(assetId)}`).then(r => r.json()).then(asset => {
            fillFromAsset({
              id: asset.id, name: asset.name, category: asset.category,
              preview_url: asset.preview_url,
              content_url: `/api/assets/${asset.id}/content`,
            });
          }).catch(err => alert("读取素材失败：" + err.message));
        },
      });
    });
    slot.appendChild(pickBtn);
    row.appendChild(slot);
  };
  if (mode === "flf") {
    $("#uploadTitle").textContent = "上传首帧与尾帧，或从已完成图片拖入（尾帧可省略）";
    zone.style.display = "block";
    mk("first", "首帧"); mk("last", "尾帧");
  } else if (mode === "r2v") {
    $("#uploadTitle").textContent = "上传参考图，或从已完成图片拖入（1~3 张）";
    zone.style.display = "block";
    mk("ref0", "参考 1"); mk("ref1", "参考 2"); mk("ref2", "参考 3");
  } else {
    zone.style.display = "none";
  }
  // 文生图隐藏时长 / 模型 / 步数
  const isVideo = info.kind === "video";
  $("#durField").style.display = isVideo ? "" : "none";
  // 图像页用自己的「分辨率档位 + 画面比例」联动控件，顶部快捷分辨率卡片不显示
  $("#resField").style.display = isVideo ? "" : "none";
  $("#modelField").style.display = isVideo ? "" : "none";
  $("#stepsField").style.display = isVideo ? "" : "none";
  // 图像模式用自己的一套比例/分辨率控件，隐藏视频模式的那套
  $("#aspectField").style.display = isVideo ? "" : "none";
  // 音频模式（kind === "audio"）也自带面板：若沿用 isVideo 判断，
  // 整个 Z-Image 参数块（分辨率档位 / 画面比例 / 输出尺寸 / 生成批次 /
  // Z-Image 模型 / LoRA …）会整块漏进音乐面板。
  // 所以这里按「确实是图像模式」判断，而不是「不是视频」。
  const isImageMode = info.kind === "image";
  $("#imagePanel").style.display = isImageMode ? "" : "none";
  if (isImageMode) renderImagePanel();

  // 文生视频（t2v）用自己的一套面板：宽高直填 + 模型 / 加速 LoRA 组合 /
  // 注意力机制 / 二采。flf / r2v / longfilm 仍走旧面板（本次不改造）。
  const isT2V = mode === "t2v";
  const videoPanel = $("#videoPanel");
  if (videoPanel) videoPanel.style.display = isT2V ? "" : "none";
  if (isT2V) {
    // 旧面板的控件全部让位（分辨率快捷档 / 比例 / 模型 / LoRA / 步数）
    $("#resField").style.display = "none";
    $("#aspectField").style.display = "none";
    $("#modelField").style.display = "none";
    $("#stepsField").style.display = "none";
    if (window.VIDEO_PANEL) window.VIDEO_PANEL.render();
  }

  // 音乐模式（YuE2）：用自己的面板，图像/视频那套全部让位
  const music = isMusicMode(mode);
  const musicPanel = $("#musicPanel");
  if (musicPanel) musicPanel.style.display = music ? "" : "none";
  if (music) {
    // 音乐面板自带模型/步数/采样器，通用那套全部隐藏
    $("#resField").style.display = "none";
    $("#durField").style.display = "none";
    $("#modelField").style.display = "none";
    $("#stepsField").style.display = "none";
    // 【2026-11-11】原来这里隐藏 #seedStrengthPair —— 该容器已删除
    // 提示词框搬进了音乐面板（#musicPrompt），通用「提示词」区让位。
    // 【2026-11-09】音乐模式下用户用音乐面板里那套「增强」分裂按钮
    // （#musicEnhanceSplit，在 #musicPromptField 内），所以通用 #promptField 整个隐藏。
    const promptField = $("#promptField");
    if (promptField) promptField.style.display = "none";
    if (window.MusicUI) window.MusicUI.render();
  } else {
    const promptField = $("#promptField");
    if (promptField) promptField.style.display = "";
  }
  // 模式变了 -> 增强模板下拉按新模式过滤，当前模板不适用则自动回落
  if (typeof refreshEnhanceSpecForMode === "function") refreshEnhanceSpecForMode();
  updateSeedStrengthLayout();
  updateActionButtons();
  // 时长标签
  $("#durVal").textContent = state.duration + "s";
}

// ---------- 图像模式（Z-Image 文生图） ----------
// 档位 + 比例 -> 像素宽高。规则与后端 z_image_dimensions 一致：
// 档位值给到**短边**，横图给高度、竖图给宽度，宽高对齐到 8 的倍数。
function imgAutoSize(tier, aspect) {
  // 与后端 z_image_dimensions / qwen_image_dimensions / ComfyUI
  // ResolutionSelector 同一算法：
  //   total = megapixels * 1024^2; scale = sqrt(total / (wr*hr))
  //   w = round(wr*scale/mult)*mult;  h = round(hr*scale/mult)*mult
  // multiple 用 16（Z-Image / Qwen-Image 2.1 都要求宽高是 16 的倍数：
  // Qwen-Image 2.1 的 VAE 是 16 倍下采样）。
  const ctx = imgCtx();
  const tierOpt = ctx.tiers.find(t => t.key === tier);
  const megapixels = (tierOpt && tierOpt.megapixels) || 1.0;
  const mult = ctx.multiple || 16;
  const [wr, hr] = IMG_ASPECT_NUM[aspect] || [16, 9];
  const total = megapixels * 1024 * 1024;
  const scale = Math.sqrt(total / (wr * hr));
  let w = Math.round(wr * scale / mult) * mult;
  let h = Math.round(hr * scale / mult) * mult;
  w = Math.max(mult, w);
  h = Math.max(mult, h);
  return [w, h];
}

// 当前图像模式的「参数上下文」：Z-Image 与 Qwen-Image 2.1 两套清单 + 两份状态。
// 面板所有控件都从这里取当前该用的那一套，避免两套参数互相污染。
function imgCtx() {
  if (isMossMode()) {
    return {
      opts: MOSS_OPTIONS,
      st: state.imgMoss,
      tiers: MOSS_OPTIONS.tiers,
      models: MOSS_OPTIONS.models,
      multiple: MOSS_OPTIONS.sizeMultiple,
      maxMp: MOSS_OPTIONS.maxDirectMp,
      minMp: MOSS_OPTIONS.minMp,
    };
  }
  if (isQwenMode()) {
    return {
      opts: QWEN_OPTIONS,
      st: state.imgQwen,
      tiers: QWEN_OPTIONS.tiers,
      models: QWEN_OPTIONS.models,
      multiple: QWEN_OPTIONS.sizeMultiple,
      maxMp: QWEN_OPTIONS.maxDirectMp,
    };
  }
  return {
    opts: IMG_OPTIONS,
    st: state.img,
    tiers: IMG_OPTIONS.tiers,
    models: IMG_OPTIONS.models,
    multiple: IMG_OPTIONS.sizeMultiple,
    maxMp: IMG_OPTIONS.maxDirectMp,
  };
}

function imgCurrentSize() {
  const s = imgCtx().st;
  if (s.width && s.height) return [s.width, s.height];
  return imgAutoSize(s.tier, s.aspect);
}

// 档位下拉：显示具体像素，例如 "1080P · 1920×1088 (2.0MP)"
function imgTierLabel(tier) {
  const ctx = imgCtx();
  const [w, h] = imgAutoSize(tier.key, ctx.st.aspect);
  const mp = (w * h) / (1024 * 1024);
  const up = mp > (ctx.maxMp || 3);
  return `${tier.label} · ${w}×${h} (${mp.toFixed(1)}MP)${up ? " · 放大" : ""}`;
}

// ---------- 生成批次（1-5） ----------
const IMG_BATCH_OPTIONS = [1, 2, 3, 4, 5];

function renderBatchChips() {
  const box = $("#imgBatchChips");
  if (!box) return;
  const s = imgCtx().st;
  box.innerHTML = IMG_BATCH_OPTIONS.map(n =>
    `<button type="button" data-n="${n}" class="${n === s.batchCount ? "active" : ""}">${n} 张</button>`
  ).join("");
  box.querySelectorAll("button").forEach(btn => {
    btn.addEventListener("click", () => {
      imgCtx().st.batchCount = Number(btn.dataset.n);
      renderBatchChips();
    });
  });
  const hint = $("#imgBatchHint");
  if (hint) {
    hint.textContent = s.batchCount > 1
      ? `一次出 ${s.batchCount} 张 · 独立种子`
      : "一次出几张";
  }
}

// ---------- LoRA 面板 ----------
// LoRA 面板上下文：Z-Image 与 Qwen-Image 2.1 是两套清单 + 两份已选列表。
// 底模结构不同，兼容性要各自比对，不能混用。
function imgLoraCtx() {
  if (isMossMode()) {
    return {store: MOSS_LORAS, list: state.imgMoss.loras, label: "MOSS-Image"};
  }
  if (isQwenMode()) {
    return {store: QWEN_LORAS, list: state.imgQwen.loras, label: "Qwen-Image 2.1"};
  }
  return {store: IMG_LORAS, list: state.img.loras, label: "Z-Image"};
}

function imgLoraMeta(name) {
  const ctx = imgLoraCtx();
  return ctx.store.available.find(l => l.name === name) || null;
}

function renderLoraSelect() {
  const sel = $("#imgLoraSelect");
  if (!sel) return;
  const ctx = imgLoraCtx();
  const used = new Set(ctx.list.map(l => l.name));
  const options = ctx.store.available.filter(l => !used.has(l.name));
  // 下拉里带上「功能 / 适用场景」的一句话说明 —— 用户在选之前
  // 就能看到这个 LoRA 是干什么的（原来只有名字，选完才看得到说明）。
  // 用 title 属性挂完整说明（原生下拉的 tooltip），文本只带简短版，
  // 免得把下拉撑得太长。
  //
  // **分组**：东亚人像目录一个族就有 85 条，不分组根本没法找。
  // 有 group 的按 group 用 <optgroup> 归拢，没有的（其它目录的条目）
  // 放进「其它效果 LoRA」组，保持下拉结构统一。
  const withGroup = options.filter(l => l.group);
  const withoutGroup = options.filter(l => !l.group);
  const order = [];
  const buckets = new Map();
  for (const l of withGroup) {
    if (!buckets.has(l.group)) { buckets.set(l.group, []); order.push(l.group); }
    buckets.get(l.group).push(l);
  }
  const optOf = (l) => {
    const scene = (l.scene || "").split(/[。；\n]/)[0].trim();
    const brief = scene ? ` — ${scene.slice(0, 28)}` : "";
    const title = l.scene ? ` title="${esc(l.scene)}"` : "";
    return `<option value="${esc(l.name)}"${title}>${esc(l.display)}${esc(brief)}</option>`;
  };
  const groupsHtml = order.map(g =>
    `<optgroup label="${esc(g)}">${buckets.get(g).map(optOf).join("")}</optgroup>`
  ).join("");
  const otherHtml = withoutGroup.length
    ? `<optgroup label="其它效果 LoRA">${withoutGroup.map(optOf).join("")}</optgroup>`
    : "";
  sel.innerHTML = options.length
    ? groupsHtml + otherHtml
    : `<option value="">${ctx.store.available.length ? "全部已添加" : `本机未找到 ${ctx.label} LoRA`}</option>`;
  sel.disabled = !options.length;
  $("#imgLoraAdd").disabled = !options.length;
}

// 被排除的 LoRA：显示名称 + 排除原因（让用户知道为什么少了它）
function renderExcludedLoras() {
  const box = $("#imgLoraExcluded");
  if (!box) return;
  const items = imgLoraCtx().store.incompatible || [];
  if (!items.length) {
    box.innerHTML = "";
    box.hidden = true;
    return;
  }
  box.hidden = false;
  // 标题里的底模族名随模式切换（原来写死「Z-Image 底模」，
  // 在 MOSS / Qwen 面板下会把「为什么被排除」解释错族）。
  const famLabel = imgLoraCtx().label;
  box.innerHTML = `<div class="img-lora-excluded-title">已排除（与 ${esc(famLabel)} 底模不兼容）</div>` +
    items.map(it => `<div class="img-lora-excluded-item" title="${esc(it.incompatible_reason || "")}">
        <span class="img-lora-excluded-name">${esc(it.display)}</span>
        <span class="img-lora-excluded-reason">${esc(it.incompatible_reason || "兼容性检测未通过")}</span>
      </div>`).join("");
}

function renderLoraList() {
  const box = $("#imgLoraList");
  if (!box) return;
  const ctx = imgLoraCtx();
  renderExcludedLoras();
  if (!ctx.list.length) {
    box.innerHTML = "";
    const accel = (ctx.store.acceleration || []).length;
    const bad = (ctx.store.incompatible || []).length;
    $("#imgLoraNote").textContent = ctx.store.available.length
      ? `未添加效果 LoRA。可选本机已安装的效果类 ${ctx.label} LoRA（${ctx.store.available.length} 个）` +
        (accel ? `；另有 ${accel} 个加速/蒸馏类 LoRA 由工作流固定使用，不在此列表。` : "。") +
        (bad ? ` ${bad} 个 LoRA 因与底模不兼容被排除（见下方说明）。` : "") +
        " 强度 0 表示关闭。"
      : `本机未检测到可用的效果类 ${ctx.label} LoRA 文件。`;
    return;
  }
  box.innerHTML = ctx.list.map((item, index) => {
    const meta = imgLoraMeta(item.name);
    const missing = !meta && !item.excluded;
    // 被排除（不兼容）—— 明确标出原因，避免用户以为能正常用
    const excluded = !!item.excluded;
    const rec = meta && Number(item.strength) === Number(meta.recommended_strength);
    const disabled = item.enabled === false || Number(item.strength) <= 0 || excluded;
    // 强度上限：权重过大的 LoRA 必须封顶（实测 1.0 出噪点、0.8 安全）
    const cap = Number(meta && meta.strength_cap !== undefined
                       ? meta.strength_cap
                       : (item.strength_cap !== undefined ? item.strength_cap : 1.5));
    const capVal = cap < 1.5 ? cap : 1.5;
    // context_refiner 阻尼：服务端会自动用阻尼后的派生 LoRA，
    // 强度可以保持 1.0（这是根因修复，比全局降强度更稳）
    const damp = meta && meta.ctx_damping !== undefined && meta.ctx_damping !== null
      ? Number(meta.ctx_damping) : 0;
    const ctxPert = meta && meta.ctx_perturbation !== undefined && meta.ctx_perturbation !== null
      ? Number(meta.ctx_perturbation) : 0;
    const dampNote = damp > 0
      ? `<span class="img-lora-tag damp" title="该 LoRA 对 context_refiner 的权重扰动较大（${(ctxPert * 100).toFixed(1)}%），该模块联合处理文本条件与潜变量，长提示词下会把采样推到发散。服务端已自动对 context_refiner 做 ${damp.toFixed(2)} 阻尼，强度可保持 1.0">已阻尼 ×${damp.toFixed(2)}</span>`
      : "";
    // 「近似适用」标注：清单里标的是「近似适用」（基座未指明 / 无 base_model tag）
    // 的条目，界面上必须能一眼看出来 —— 不能让它跟「原生」条目长得一样。
    const near = meta && meta.base_verdict === "近似适用";
    const nearNote = near
      ? `<span class="img-lora-tag near" title="${esc(meta.scene || "")}">近似适用</span>`
      : "";
    // 触发词：可编辑 —— 用户能改 / 删 / 手输；留空即不追加该 LoRA 的触发词
    const triggerText = item.trigger !== undefined && item.trigger !== null
      ? item.trigger : (meta && meta.trigger ? meta.trigger : "");
    const triggerBlock = `
      <div class="img-lora-trigger">
        <span class="lora-trigger-label" title="该 LoRA 贡献的触发词，会自动追加到提示词末尾">触发词</span>
        <input type="text" class="lora-trigger-input" value="${esc(triggerText)}"
               placeholder="${meta && meta.trigger ? esc(meta.trigger) : "（上游未标注触发词）"}"
               title="自动追加到最终提示词；可编辑或删除">
      </div>`;
    return `<div class="img-lora-item ${disabled ? "disabled" : ""} ${missing ? "missing" : ""} ${excluded ? "excluded" : ""}" data-idx="${index}">
      <div class="img-lora-head">
        <button type="button" class="img-lora-btn toggle" title="${excluded ? "该 LoRA 与底模不兼容，不能使用" : (disabled ? "启用" : "停用")}" ${excluded ? "disabled" : ""}>${disabled ? "○" : "●"}</button>
        <span class="img-lora-name" title="${esc(item.name)}">${esc(meta ? meta.display : (item.display || item.name))}</span>
        ${excluded ? `<span class="img-lora-tag excl">不兼容</span>` : ""}
        ${missing ? `<span class="img-lora-tag miss">文件缺失</span>` : ""}
        ${rec ? `<span class="img-lora-tag rec">推荐强度</span>` : ""}
        ${nearNote}
        ${dampNote}
        <button type="button" class="img-lora-btn up" title="上移" ${index === 0 ? "disabled" : ""}>↑</button>
        <button type="button" class="img-lora-btn down" title="下移" ${index === ctx.list.length - 1 ? "disabled" : ""}>↓</button>
        <button type="button" class="img-lora-btn del" title="删除">×</button>
      </div>
      <div class="img-lora-slider">
        <input type="range" class="strength" min="0" max="${capVal}" step="0.05" value="${Math.min(Number(item.strength), capVal)}" ${excluded ? "disabled" : ""} title="${cap < 1.0 ? `强度上限 ${cap}（该 LoRA 权重较大，强度过高会出彩色噪点）` : '强度 0-1.5，0 表示关闭'}">
        <span class="img-lora-strength">${Number(item.strength).toFixed(2)}</span>
        ${cap < 1.0 ? `<span class="img-lora-cap" title="该 LoRA 权重相对原权重过大，强度过高会破坏采样、实测出图为彩色噪点">上限 ${cap.toFixed(2)}</span>` : ""}
        ${damp > 0 ? `<span class="img-lora-cap damped" title="context_refiner 阻尼系数 ${damp.toFixed(2)}（服务端自动应用，强度不打折）">ctx 阻尼 ${damp.toFixed(2)}</span>` : ""}
      </div>
      ${excluded ? `<div class="img-lora-excl-reason">${esc(item.incompatible_reason || `与 ${ctx.label} 底模不兼容`)}</div>` : ""}
      ${triggerBlock}
      <div class="img-lora-meta">基础：${esc(meta ? meta.base : "—")}${meta && meta.strength_source ? " · " + esc(meta.strength_source) : ""}</div>
      ${damp > 0 ? `<div class="img-lora-meta">context_refiner 扰动 ${(ctxPert * 100).toFixed(1)}%，已自动阻尼至 ×${damp.toFixed(2)}（长提示词下避免彩色噪点，强度不打折）</div>` : ""}
      <div class="img-lora-meta"><button type="button" class="img-lora-info-btn" style="background:none;border:0;padding:0;color:var(--accent);cursor:pointer;font-size:10px;font-family:inherit">查看简易说明 ›</button></div>
    </div>`;
  }).join("");

  box.querySelectorAll(".img-lora-item").forEach(el => {
    const index = Number(el.dataset.idx);
    const list = imgLoraCtx().list;
    const item = list[index];
    el.querySelector(".toggle").addEventListener("click", () => {
      item.enabled = item.enabled === false;
      saveLoraConfig();
    });
    el.querySelector(".up").addEventListener("click", () => {
      if (index === 0) return;
      [list[index - 1], list[index]] = [list[index], list[index - 1]];
      saveLoraConfig();
    });
    el.querySelector(".down").addEventListener("click", () => {
      if (index === list.length - 1) return;
      [list[index + 1], list[index]] = [list[index], list[index + 1]];
      saveLoraConfig();
    });
    el.querySelector(".del").addEventListener("click", () => {
      list.splice(index, 1);
      saveLoraConfig();
    });
    const slider = el.querySelector("input.strength");
    slider.addEventListener("input", () => {
      item.strength = Number(slider.value);
      el.querySelector(".img-lora-strength").textContent = item.strength.toFixed(2);
      el.classList.toggle("disabled", item.enabled === false || item.strength <= 0);
    });
    slider.addEventListener("change", () => saveLoraConfig());
    // 触发词可编辑：改动即写回 state（保存时发给服务端 + 生成时进提示词）
    const trigInput = el.querySelector("input.lora-trigger-input");
    if (trigInput) {
      trigInput.addEventListener("input", () => {
        item.trigger = trigInput.value;
      });
      trigInput.addEventListener("change", () => saveLoraConfig());
    }
    el.querySelector(".img-lora-info-btn").addEventListener("click", () => openLoraInfo(item.name));
  });
  const accelNote = (ctx.store.acceleration || []).length
    ? ` 加速/蒸馏类 ${ctx.store.acceleration.length} 个由工作流固定使用（不列出）。` : "";
  const badNote = (ctx.store.incompatible || []).length
    ? ` ${ctx.store.incompatible.length} 个不兼容 LoRA 已排除。` : "";
  $("#imgLoraNote").textContent =
    `已添加 ${ctx.list.length} 个效果 LoRA（配置存在服务端，局域网共用）。${accelNote}${badNote}`;
}

// LoRA 配置持久化到服务端
let loraSaveTimer = null;
function saveLoraConfig() {
  renderLoraList();
  renderLoraSelect();
  clearTimeout(loraSaveTimer);
  loraSaveTimer = setTimeout(async () => {
    try {
      const ctx = imgLoraCtx();
      const resp = await fetch("/api/image-loras", {
        method: "PUT",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({loras: ctx.list, base: ctx.label}),
      });
      const data = await resp.json();
      if (!resp.ok) {
        // 不兼容的 LoRA 会被服务端拒绝 —— 必须让用户看到原因
        setEnhanceHint(`LoRA 配置保存失败：${data.error || resp.status}`, "error");
        alert(`LoRA 配置保存失败\n\n${data.error || resp.status}`);
        // 回滚到服务端实际保存的状态，避免界面与服务端不一致
        await loadImageLoras();
        return;
      }
      // 回写服务端实际保存的状态（按当前底模族回写，别写串了）
      const list = imgLoraCtx().list;
      list.splice(0, list.length, ...(data.loras || []).map(l => ({
        name: l.name, strength: Number(l.strength), enabled: l.enabled !== false,
        trigger: l.trigger !== undefined && l.trigger !== null ? l.trigger : "",
        excluded: !!l.excluded,
        incompatible_reason: l.incompatible_reason || "",
        display: l.display || l.name,
        strength_cap: l.strength_cap !== undefined ? l.strength_cap : 1.5,
        ctx_damping: l.ctx_damping !== undefined ? l.ctx_damping : null,
        ctx_perturbation: l.ctx_perturbation !== undefined ? l.ctx_perturbation : null,
      })));
      renderLoraList();
      renderLoraSelect();
    } catch (e) {
      setEnhanceHint(`LoRA 配置保存失败：${e.message}`, "error");
    }
  }, 250);
}

function openLoraInfo(name) {
  const meta = imgLoraMeta(name);
  const body = $("#loraInfoBody");
  if (!meta) {
    body.innerHTML = `<div class="lora-scene">本机未找到该 LoRA 文件：<b>${esc(name)}</b>。请检查文件是否仍在原路径。</div>`;
  } else {
    const trigger = meta.trigger
      ? `<span class="lora-trigger">${esc(meta.trigger)}</span>` : "上游未标注触发词";
    body.innerHTML = `<dl>
      <dt>显示名</dt><dd>${esc(meta.display)}</dd>
      <dt>文件名</dt><dd>${esc(meta.name)}</dd>
      <dt>文件路径</dt><dd>${esc(meta.path)}</dd>
      <dt>大小</dt><dd>${meta.size_mb} MB</dd>
      <dt>基础模型</dt><dd>${esc(meta.base)}</dd>
      <dt>分组</dt><dd>${esc(meta.group || "—")}</dd>
      <dt>基座适用性</dt><dd>${meta.base_verdict === "近似适用"
        ? "⚠️ 近似适用（非该基座原生训练，依据见下方说明）"
        : (meta.base_verdict || "原生")}</dd>
      <dt>触发词</dt><dd>${trigger}</dd>
      <dt>推荐强度</dt><dd>${Number(meta.recommended_strength).toFixed(2)}${meta.strength_source ? "（" + esc(meta.strength_source) + "）" : ""}</dd>
      <dt>类型</dt><dd>${meta.kind === "acceleration" ? "加速 / 蒸馏类（工作流固定使用，不可选）" : "效果类（可自行选用）"}</dd>
      <dt>说明来源</dt><dd>${esc(meta.source || "—")}</dd>
    </dl>
    <div class="lora-scene">${esc(meta.scene || "（上游未提供场景说明）")}</div>`;
  }
  $("#loraInfoModal").hidden = false;
  document.body.classList.add("modal-open");
}

function closeLoraInfo() {
  $("#loraInfoModal").hidden = true;
  document.body.classList.remove("modal-open");
}

// 载入本机 LoRA 清单 + 已保存配置
async function loadImageLoras() {
  try {
    // 两族底模的 LoRA 清单与配置是分开的（底模结构不同，兼容性各自比对，
    // 混在一起会让用户选到不匹配的权重）。
    const bases = [
      {name: "Z-Image", store: IMG_LORAS,
       set: v => { state.img.loras = v; }},
      {name: "Qwen-Image 2.1", store: QWEN_LORAS,
       set: v => { state.imgQwen.loras = v; }},
      {name: "MOSS-Image", store: MOSS_LORAS,
       set: v => { state.imgMoss.loras = v; }},
    ];
    for (const base of bases) {
      const resp = await fetch("/api/image-loras?base=" + encodeURIComponent(base.name),
                              {cache: "no-store"});
      if (!resp.ok) continue;
      const data = await resp.json();
      base.store.available = data.available || [];
      base.store.acceleration = data.acceleration || [];
      base.store.incompatible = data.incompatible || [];
      base.set((data.config || []).map(l => ({
        name: l.name, strength: Number(l.strength), enabled: l.enabled !== false,
        trigger: l.trigger !== undefined && l.trigger !== null ? l.trigger : "",
        // 被排除的（不兼容）也要显示出来并说明原因
        excluded: !!l.excluded,
        incompatible_reason: l.incompatible_reason || "",
        display: l.display || l.name,
        strength_cap: l.strength_cap !== undefined ? l.strength_cap : 1.5,
        ctx_damping: l.ctx_damping !== undefined ? l.ctx_damping : null,
        ctx_perturbation: l.ctx_perturbation !== undefined ? l.ctx_perturbation : null,
      })));
    }
    renderLoraSelect();
    renderLoraList();
  } catch (e) { /* 静默：不阻断出图 */ }
}

// 负面提示词说明：区分「模型不支持」和「当前 CFG 值下不生效」。
// 单独抽出来是因为它依赖 CFG 值，而 CFG 滑块在拖动时不能重渲染整个面板
// （那会把滑块重建掉，拖到一半就断了）。
function updateNegativeNote() {
  const ctx = imgCtx();
  const model = ctx.models[ctx.st.model];
  const negSupported = !!(model && model.supports_negative);
  const cfg = Number(ctx.st.cfg);
  const negIgnoredAtCfg1 = isQwenMode() && Math.abs(cfg - 1) < 1e-6;
  const note = $("#imgNegNote");
  if (!note) return;
  // 【cfg=1 时负面提示词在数学上无效】
  //   ComfyUI 的 samplers.py:609-627 在 cfg==1 时直接跳过 uncond 分支，
  //   negative_prompt 不会参与采样。diffusers 侧同理：
  //   do_true_cfg = true_cfg_scale > 1 and has_neg_prompt。
  //   官方 Qwen 路径默认就是 cfg=1（官方 note: "unused while cfg is 1"）。
  //   -> 界面把输入框置灰并说明，别让用户填了无效参数还以为生效了。
  const textarea = $("#imgNegative");
  if (textarea) {
    const disabled = !negSupported || negIgnoredAtCfg1;
    textarea.disabled = disabled;
    textarea.style.opacity = disabled ? "0.55" : "";
    textarea.placeholder = disabled
      ? "当前设置下负面提示词不生效（见下方说明）"
      : "extra fingers, poorly drawn hands, deformed, disfigured";
  }
  note.textContent = negSupported
    ? (negIgnoredAtCfg1
        ? "当前 CFG=1：ComfyUI 会跳过 uncond 分支，负面提示词数学上无效（已置灰）。"
          + "官方路径就是 cfg=1；要启用负面提示词请把 CFG 调到 1 以上。"
        : (isQwenMode()
            ? "Qwen-Image 2.1 非蒸馏，CFG>1 时负面提示词生效。"
            : (isMossMode()
                ? "MOSS-Image 未蒸馏，CFG>1 时负面提示词生效。"
                  + "官方 USERGUIDE 推荐 CFG 3.0，并给出一组实测有效的负面标签"
                  + "（nsfw, worst quality, bad anatomy, watermark, jpeg artifact）。"
                : "Base 非蒸馏，负面提示词生效（官方强烈建议）")))
    : "当前版本为蒸馏模型：CFG=1 时 ComfyUI 会跳过 uncond 分支，负面提示词数学上无效，已禁用。";
}

function renderImagePanel() {
  const ctx = imgCtx();
  const s = ctx.st;
  const model = ctx.models[s.model];

  // 分辨率档位
  const resSel = $("#imgResSelect");
  resSel.innerHTML = "";
  ctx.tiers.forEach(tier => {
    const opt = document.createElement("option");
    opt.value = tier.key;
    opt.textContent = imgTierLabel(tier);
    opt.selected = tier.key === s.tier;
    resSel.appendChild(opt);
  });

  // 比例
  const chips = $("#imgAspectChips");
  chips.innerHTML = "";
  ctx.opts.aspects.forEach(aspect => {
    const b = document.createElement("button");
    b.type = "button";
    b.dataset.v = aspect.key;
    b.textContent = aspect.key;
    b.title = aspect.label;
    if (aspect.key === s.aspect) b.classList.add("active");
    b.addEventListener("click", () => {
      s.aspect = aspect.key;
      // 换比例后旧的自动尺寸作废，回到档位自动值
      // （手填的自定义尺寸也一并清掉 —— 比例和手填尺寸不能同时成立）
      s.width = null; s.height = null;
      s.rawWidth = null; s.rawHeight = null;
      s.custom = false;
      imgSizeTyping = false;   // 退出输入态，保证预设尺寸写回输入框
      renderImagePanel();
    });
    chips.appendChild(b);
  });

  // 模型
  // 权重不在本机的版本（Turbo 权重可能没下载）—— 标「不可用」并禁选，
  // 避免用户选完才在跑图时炸掉。
  const modelSel = $("#imgModelSelect");
  modelSel.innerHTML = "";
  // 标题里的底模族名随模式切换（原来写死「Z-Image 模型」，
  // 在 MOSS / Qwen 面板下会误导用户以为选的是 Z-Image 权重）。
  const modelLabel = $("#imgModelLabel");
  if (modelLabel) {
    modelLabel.textContent = isMossMode() ? "MOSS-Image 模型"
      : (isQwenMode() ? "Qwen-Image 2.1 模型" : "Z-Image 模型");
  }
  Object.entries(ctx.models).forEach(([key, m]) => {
    const opt = document.createElement("option");
    opt.value = key;
    const unavailable = m.available === false;
    opt.textContent = unavailable ? m.label + "（权重缺失，不可用）" : m.label;
    opt.disabled = unavailable;
    opt.selected = key === s.model;
    modelSel.appendChild(opt);
  });
  // 当前选中的模型不可用时（权重被删了），回落到默认模型
  if (model && model.available === false) {
    const fallback = ctx.models[ctx.opts.defaultModel];
    if (fallback) {
      s.model = ctx.opts.defaultModel;
      applyModelDefaults(ctx.opts.defaultModel);
    }
  }

  // 采样器 / 调度器：列表按模型版本区分（Turbo 不给 karras/exponential 调度器）
  const fillSelect = (sel, values, current) => {
    sel.innerHTML = "";
    (values || []).forEach(v => {
      const opt = document.createElement("option");
      opt.value = v;
      opt.textContent = v;
      opt.selected = v === current;
      sel.appendChild(opt);
    });
    if (current && !(values || []).includes(current)) {
      // 当前值不在该版本的推荐列表里（比如换了模型），补上并保持选中
      const opt = document.createElement("option");
      opt.value = current;
      opt.textContent = current + "（自定义）";
      opt.selected = true;
      sel.appendChild(opt);
    }
  };
  fillSelect($("#imgSamplerSelect"), model && model.samplers, s.sampler);
  fillSelect($("#imgSchedulerSelect"), model && model.schedulers, s.scheduler);

  // 步数 / CFG / shift
  // 滑块量程按模式切换：Z-Image 的 SHIFT 是 3（min=1），
  // Qwen-Image 2.1 的 SHIFT 是 0.69 —— 用同一套量程会把 0.69 夹成 1，
  // 界面显示的值就和真正送出去的不一致。
  const stepsEl = $("#imgSteps"), cfgEl = $("#imgCfg"), shiftEl = $("#imgShift");
  if (isMossMode()) {
    // MOSS-Image 官方 USERGUIDE：steps >= 50 / CFG 3.0~6.0 / shift 5.0
    // 量程要覆盖官方推荐值 —— steps 上限给到 100（官方下限 50 起步），
    // CFG 给到 0.1 步进（官方区间 3.0~6.0，需要能调到 3.5/4.0/4.5…），
    // shift 给到 0.1 步进（官方推荐 5.0）。
    stepsEl.min = "1"; stepsEl.max = "100"; stepsEl.step = "1";
    cfgEl.min = "0"; cfgEl.max = "10"; cfgEl.step = "0.1";
    shiftEl.min = "0"; shiftEl.max = "10"; shiftEl.step = "0.1";
  } else if (isQwenMode()) {
    stepsEl.min = "1"; stepsEl.max = "60"; stepsEl.step = "1";
    cfgEl.min = "0"; cfgEl.max = "8"; cfgEl.step = "0.1";
    shiftEl.min = "0"; shiftEl.max = "3"; shiftEl.step = "0.01";
  } else {
    stepsEl.min = "1"; stepsEl.max = "60"; stepsEl.step = "1";
    cfgEl.min = "1"; cfgEl.max = "8"; cfgEl.step = "0.5";
    shiftEl.min = "1"; shiftEl.max = "7"; shiftEl.step = "0.5";
  }
  stepsEl.value = s.steps;
  $("#imgStepsVal").textContent = s.steps;
  cfgEl.value = s.cfg;
  $("#imgCfgVal").textContent = Number(s.cfg).toFixed(isQwenMode() ? 1 : 1);
  shiftEl.value = s.shift;
  // MOSS 的 shift 量程是 0~10 步进 0.1 -> 显示 1 位小数即可；
  // Qwen 是 0~3 步进 0.01 -> 要 2 位；Z-Image 是 1~7 步进 0.5 -> 1 位
  $("#imgShiftVal").textContent = Number(s.shift).toFixed(
    isQwenMode() ? 2 : (isMossMode() ? 1 : 1));

  // 负面提示词：Turbo 在 CFG=1 下负面被跳过（samplers.py:610）-> 禁用
  const negSupported = !!(model && model.supports_negative);
  $("#imgNegField").style.display = negSupported ? "" : "none";
  updateNegativeNote();
  if (negSupported) $("#imgNegative").value = s.negative || "";

  // Qwen-Image 2.1 专属：参考图区
  //
  // 【官方 PE 提示词扩写不再有独立勾选框】——
  // 它已并入「增强」按钮的下拉菜单（「qwen21图片官方增强PE」）。
  // 这里只保留参考图区（图片编辑模式）。
  const qwen = isQwenMode();
  const refField = $("#imgRefField");
  if (refField) refField.style.display = (qwen && state.mode === "i2i_qwen") ? "" : "none";
  if (qwen) {
    renderQwenRefs();
  }

  renderBatchChips();
  renderLoraSelect();
  renderLoraList();
  renderSlgChips();

  updateImageSizeFields();
  updateImageModelNote();
  renderStaleBackendBanner();
}

// ---------- 后端版本错配告警 ----------
// 【为什么需要】前端是静态文件（改了刷新页面就生效），后端是长跑进程
// （改了必须重启才生效）。两者错配时，用户看到的是：
//   - 分辨率档位 / 模型下拉全空
//   - LoRA 下拉串到别的底模族
//   - 点生成直接报「未知模式 t2i_moss」
// 这类症状极难自查（前端没错、后端也没错，只是版本对不上）。
// 所以这里把「后端没重启」直接写在面板顶上，并给出修复命令。
function renderStaleBackendBanner() {
  const box = $("#staleBackendBanner");
  if (!box) return;
  if (!isMossMode()) {
    box.hidden = true;
    box.innerHTML = "";
    return;
  }
  if (MOSS_BACKEND_READY) {
    box.hidden = true;
    box.innerHTML = "";
    return;
  }
  box.hidden = false;
  box.innerHTML =
    "<b>后端未重启，MOSS-Image 面板当前不可用。</b><br>"
    + "页面资源（前端）已经是新的，但后端进程还是旧代码 —— "
    + "所以分辨率档位/模型下拉是空的，点生成会报「未知模式 t2i_moss」。<br>"
    + "修复：<code>systemctl restart h3-studio.service</code> "
    + "（本机）或 <code>sudo systemctl restart h3-studio.service</code>，"
    + "然后刷新页面。";
}

// ---------- MOSS-Image：SLG（SkipLayerGuidance）skip layer ----------
// 官方 USERGUIDE 推荐 22（0-indexed），22/24/26 都有帮助；
// 空字符串 = 关闭 SLG（画面更写实，但官方提示部分细节会崩）。
const SLG_LAYER_OPTIONS = ["22", "24", "26", ""];
const SLG_LAYER_LABELS = {"22": "22（官方推荐）", "24": "24", "26": "26", "": "关闭"};

function renderSlgChips() {
  const box = $("#imgSlgChips");
  if (!box) return;
  const field = $("#imgSlgField");
  if (!isMossMode()) {
    if (field) field.style.display = "none";
    box.innerHTML = "";
    return;
  }
  if (field) field.style.display = "";
  box.innerHTML = "";
  SLG_LAYER_OPTIONS.forEach(value => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.dataset.v = value;
    btn.textContent = SLG_LAYER_LABELS[value] || value;
    if (value === (state.imgMoss.slgSingle || "")) btn.classList.add("active");
    btn.addEventListener("click", () => {
      state.imgMoss.slgSingle = value;
      renderSlgChips();
      updateImageModelNote();
    });
    box.appendChild(btn);
  });
}

// 宽高吸附：与后端 workflows.normalize_z_image_size() 同一套规则。
// 16 的倍数来自 EmptySD3LatentImage 的节点 schema（step=16），
// 上限 16384 来自 nodes.MAX_RESOLUTION。
function normalizeImgSize(w, h) {
  const ctx = imgCtx();
  const mult = ctx.multiple || 16;
  const min = (ctx.opts.sizeMin !== undefined ? ctx.opts.sizeMin : 16);
  const max = (ctx.opts.sizeMax !== undefined ? ctx.opts.sizeMax : 16384);
  const reasons = [];
  let nw = Math.max(mult, Math.round(w / mult) * mult);
  let nh = Math.max(mult, Math.round(h / mult) * mult);
  if (nw !== w) reasons.push(`宽度 ${w} 不是 ${mult} 的倍数 → ${nw}`);
  if (nh !== h) reasons.push(`高度 ${h} 不是 ${mult} 的倍数 → ${nh}`);
  if (nw > max) { reasons.push(`宽度超过上限 ${max} → 已钳到上限`); nw = max; }
  if (nh > max) { reasons.push(`高度超过上限 ${max} → 已钳到上限`); nh = max; }
  if (nw < min) { reasons.push(`宽度低于下限 ${min} → 已抬到下限`); nw = min; }
  if (nh < min) { reasons.push(`高度低于下限 ${min} → 已抬到下限`); nh = min; }
  // MOSS-Image 额外有「总像素」区间（官方推荐 0.25M ~ 4M）。
  // 规则与后端 normalize_moss_image_size() 一致：
  //   下限 = 官方推荐区间下沿（512×512）
  //   上限 = 官方直出上限（≈2K）；更大尺寸需挂 4MP+ hires LoRa
  const minMp = ctx.minMp;
  if (minMp) {
    const minPixels = minMp * 1024 * 1024;
    if (nw * nh < minPixels) {
      const scale = Math.sqrt(minPixels / (nw * nh));
      const sw = Math.max(mult, Math.round(nw * scale / mult) * mult);
      const sh = Math.max(mult, Math.round(nh * scale / mult) * mult);
      reasons.push(`总像素 ${(nw * nh / 1048576).toFixed(2)}MP 低于官方下限 ${minMp}MP → 按比例放大到 ${sw}×${sh}`);
      nw = sw; nh = sh;
    }
  }
  const maxMp = ctx.maxMp;
  if (maxMp) {
    const maxPixels = maxMp * 1024 * 1024;
    if (nw * nh > maxPixels) {
      const scale = Math.sqrt(maxPixels / (nw * nh));
      const sw = Math.max(mult, Math.round(nw * scale / mult) * mult);
      const sh = Math.max(mult, Math.round(nh * scale / mult) * mult);
      reasons.push(`总像素 ${(nw * nh / 1048576).toFixed(2)}MP 超过官方直出上限 ${maxMp}MP → 按比例缩到 ${sw}×${sh}（更大尺寸需挂 4MP+ hires LoRa）`);
      nw = sw; nh = sh;
    }
  }
  return [nw, nh, reasons];
}

// 「正在输入」标记 —— 用户敲键盘的过程中不能把吸附后的值写回输入框，
// 否则会打断输入（经典 controlled-input bug）：
//   字段显示 "136" -> 用户敲 "5" -> 字段变成 "1365" -> 吸附成 1360 ->
//   写回字段 -> 字段又变回 "1360" -> 用户继续敲 "5" -> "13605" -> 越敲越离谱。
// 所以：input 事件里只算不写；change 事件（失焦/回车）才吸附并写回。
let imgSizeTyping = false;

function updateImageSizeFields() {
  const ctx = imgCtx();
  const s = ctx.st;
  const manual = !!s.custom;
  let w, h, reasons = [];
  if (manual) {
    // 手填值 -> 每一边各自吸附到合法倍数，并把「原始值 / 生效值 / 原因」算出来。
    // 注意：**不能**用「两边都填了才算自定义」的判定 —— 用户先填宽再填高时，
    // 第一步就会因为 height 还是 null 而回落到预设，把刚填的宽冲掉。
    //
    // 用 rawWidth/rawHeight 存**用户原始输入**：吸附结果写回 width/height 后，
    // 再拿 raw 跟 width 比才能算出「原始 2559 -> 生效 2560」的差异说明；
    // 否则第二次重绘时两边已经相等，说明就消失了。
    const base = imgAutoSize(s.tier, s.aspect);
    const rawW = (s.rawWidth !== null && s.rawWidth !== undefined) ? Number(s.rawWidth) : base[0];
    const rawH = (s.rawHeight !== null && s.rawHeight !== undefined) ? Number(s.rawHeight) : base[1];
    const snapped = normalizeImgSize(rawW, rawH);
    w = snapped[0]; h = snapped[1]; reasons = snapped[2];
    // 吸附结果写回 state —— 提交给 ComfyUI 的必须是吸附后的值
    s.width = w; s.height = h;
  } else {
    [w, h] = imgAutoSize(s.tier, s.aspect);
  }
  // 用户正在输入时不要覆盖输入框里的原始文本（否则输入会被打断）
  if (!imgSizeTyping) {
    $("#imgWidth").value = w;
    $("#imgHeight").value = h;
  }
  $("#imgSizeHint").textContent = `${w}×${h}`;
  const mp = (w * h) / (1024 * 1024);
  const up = mp > (ctx.maxMp || 3);
  const parts = [`${mp.toFixed(2)} MP`];
  if (up) parts.push(`超过 ${ctx.maxMp}MP，改走「生成 + 官方放大」路径（输出像素为近似值）`);
  if (manual) parts.push("自定义尺寸，点「自动」可恢复档位计算值");
  $("#imgSizeManualNote").textContent = parts.join(" · ");

  // 「自定义」标记
  // 用 class 切换而不是 hidden 属性 —— .img-tag 是 inline-block，
  // hidden 会被 CSS 的 display 盖掉（与历史踩过的「hidden 被 display:grid
  // 盖掉」同类问题）。
  const modeTag = $("#imgSizeMode");
  if (modeTag) {
    modeTag.classList.toggle("hidden", !manual);
    modeTag.textContent = manual ? "自定义" : "";
  }
  // 吸附说明：原始值 / 生效值 / 调整原因
  const adj = $("#imgSizeAdjust");
  if (adj) {
    if (reasons.length) {
      adj.hidden = false;
      adj.textContent = "已自动调整：" + reasons.join("；");
    } else {
      adj.hidden = true;
      adj.textContent = "";
    }
  }
}

// 切模型时把该版本的官方推荐值同步到各控件（推荐值来自服务端下发的清单）
// 切模型版本时把该版本的官方推荐值同步到各控件（推荐值来自服务端下发的清单）
//
// 【步数随模型版本联动】
//   基座 = 40 步（官方 README 默认 num_inference_steps=40）
//   Turbo = 8 步（8 步蒸馏版，步数与固定 sigma 表绑定）
// 这里直接取该模型的 steps —— 切版本时步数自动跟着变，
// 不会出现「Turbo 跑 40 步」这种与蒸馏 schedule 不匹配的组合。
function applyModelDefaults(modelKey) {
  const ctx = imgCtx();
  const m = ctx.models[modelKey];
  if (!m) return;
  const s = ctx.st;
  s.model = modelKey;
  s.steps = m.steps;
  s.cfg = m.cfg;
  s.shift = m.shift;
  s.sampler = m.sampler;
  s.scheduler = m.scheduler;
}

function updateImageModelNote() {
  const ctx = imgCtx();
  const s = ctx.st;
  const m = ctx.models[s.model];
  if (!m) return;
  $("#imgModelNote").textContent = m.note;
  if (m.warn) $("#imgModelWarn").textContent = m.warn;
  // MOSS-Image 专属：SLG（SkipLayerGuidance）skip layer 的当前值
  // 官方 USERGUIDE 推荐 22（0-indexed）；22/24/26 都有帮助，空 = 关。
  // 关掉 SLG 会让画面「非常写实」，但部分细节会崩 —— 所以这里显式告知。
  const slgBox = $("#imgSlgNote");
  if (slgBox) {
    if (isMossMode()) {
      const layer = state.imgMoss.slgSingle;
      slgBox.hidden = false;
      slgBox.textContent = layer
        ? `SLG skip layer = ${layer}（官方推荐 22；22/24/26 都有帮助）。`
          + "关掉 SLG 画面会更写实，但部分细节会崩。"
        : "SLG 已关闭：画面更写实，但官方提示部分细节会崩。";
    } else {
      slgBox.hidden = true;
      slgBox.textContent = "";
    }
  }
  // 加速 / 蒸馏 LoRA 固定内置于工作流 —— 明确告诉用户它不在下拉列表里
  const accelBox = $("#imgAccelNote");
  if (accelBox) {
    if (m.has_accel_lora) {
      accelBox.hidden = false;
      accelBox.textContent =
        "本版本已内置加速/蒸馏 LoRA（固定接在工作流里，不在 LoRA 下拉列表中）。"
        + "步数/CFG/采样器已按蒸馏档锁定。";
    } else {
      accelBox.hidden = true;
      accelBox.textContent = "";
    }
  }
  // 推荐值高亮：当前值等于该模型推荐值时打「推荐」标记
  $("#imgSamplerHint").textContent = s.sampler === m.sampler ? "推荐" : "已改";
  $("#imgSchedulerHint").textContent = s.scheduler === m.scheduler ? "推荐" : "已改";
  $("#imgStepsHint").textContent = s.steps === m.steps ? "推荐" : "已改";
  $("#imgCfgHint").textContent = Number(s.cfg) === Number(m.cfg) ? "推荐" : "已改";
  // 加速 LoRA 绑定的模型版本：步数/CFG/采样器/调度器锁定
  // （蒸馏 LoRA 是在 8 步 schedule 上训的，改了会破坏 schedule 出噪点）
  const locked = !!m.params_locked_by_accel;
  ["#imgSteps", "#imgCfg", "#imgSamplerSelect", "#imgSchedulerSelect"].forEach(sel => {
    const el = $(sel);
    if (!el) return;
    el.disabled = locked;
    el.title = locked ? "已绑定加速 LoRA 的蒸馏档，不能修改" : "";
  });
  if (locked) {
    $("#imgStepsHint").textContent = "锁定";
    $("#imgCfgHint").textContent = "锁定";
    $("#imgSamplerHint").textContent = "锁定";
    $("#imgSchedulerHint").textContent = "锁定";
  }
  // SHIFT 的说明比别的参数多一层：官方是 dynamic shifting，由图像 token 数
  // 自动算 mu（base_shift 0.5 → max_shift 0.9，1024×1024 对应 mu≈0.69）。
  // ComfyUI 侧把它固化成了 1024×1024 拟合的常数 0.69 —— 非 1024 分辨率下
  // 是近似值。所以标「推荐/自动」并说明。
  const shiftHint = $("#imgShiftHint");
  if (shiftHint) {
    if (Number(s.shift) !== Number(m.shift)) {
      shiftHint.textContent = "已改";
      shiftHint.title = "";
    } else if (isQwenMode()) {
      shiftHint.textContent = "推荐/自动";
      shiftHint.title = "官方用 dynamic shifting 按图像 token 数自动算 mu"
        + "（base_shift 0.5 → max_shift 0.9，1024×1024 对应 mu≈0.69）。"
        + "ComfyUI 固化的是 1024×1024 拟合值，非 1024 分辨率下为近似值。";
    } else {
      shiftHint.textContent = "推荐";
      shiftHint.title = "";
    }
  }
}

$("#imgResSelect").addEventListener("change", e => {
  const s = imgCtx().st;
  s.tier = e.target.value;
  // 切档位 -> 回到预设尺寸（自定义值失效）
  s.width = null; s.height = null;
  s.rawWidth = null; s.rawHeight = null;
  s.custom = false;
  // 退出「正在输入」状态，否则重渲染时不会把预设尺寸写回输入框
  imgSizeTyping = false;
  renderImagePanel();
});
$("#imgModelSelect").addEventListener("change", e => {
  applyModelDefaults(e.target.value);
  renderImagePanel();
  // 模型换了 -> 增强模板按新模型的模型族重新过滤
  if (typeof refreshEnhanceSpecForMode === "function") refreshEnhanceSpecForMode();
});
$("#imgSamplerSelect").addEventListener("change", e => {
  imgCtx().st.sampler = e.target.value;
  updateImageModelNote();
});
$("#imgSchedulerSelect").addEventListener("change", e => {
  imgCtx().st.scheduler = e.target.value;
  updateImageModelNote();
});
$("#imgSteps").addEventListener("input", e => {
  const s = imgCtx().st;
  s.steps = parseInt(e.target.value);
  $("#imgStepsVal").textContent = s.steps;
  updateImageModelNote();
});
$("#imgCfg").addEventListener("input", e => {
  const s = imgCtx().st;
  s.cfg = parseFloat(e.target.value);
  $("#imgCfgVal").textContent = s.cfg.toFixed(1);
  updateImageModelNote();
  // 负面提示词的说明依赖 CFG 值（cfg=1 时它无效）——
  // 只刷那一句，不能重渲染整个面板（会把正在拖的滑块重建掉）
  updateNegativeNote();
});
$("#imgShift").addEventListener("input", e => {
  const s = imgCtx().st;
  s.shift = parseFloat(e.target.value);
  $("#imgShiftVal").textContent = Number(s.shift).toFixed(
    isQwenMode() ? 2 : (isMossMode() ? 1 : 1));
  updateImageModelNote();
});
$("#imgNegative").addEventListener("input", e => {
  imgCtx().st.negative = e.target.value;
});
// 手填宽高：先原样存进 state，吸附交给 updateImageSizeFields()
// （那里会把「原始值 / 生效值 / 调整原因」一起显示出来）
$("#imgWidth").addEventListener("input", e => {
  const s = imgCtx().st;
  const v = parseInt(e.target.value);
  s.width = Number.isFinite(v) && v > 0 ? v : null;
  s.rawWidth = s.width;          // 留原始输入，用于算调整原因
  // 用户一旦手填 -> 进入自定义模式（哪怕只填了一边）
  if (s.width !== null) s.custom = true;
  imgSizeTyping = true;
  updateImageSizeFields();
});
$("#imgHeight").addEventListener("input", e => {
  const s = imgCtx().st;
  const v = parseInt(e.target.value);
  s.height = Number.isFinite(v) && v > 0 ? v : null;
  s.rawHeight = s.height;
  if (s.height !== null) s.custom = true;
  imgSizeTyping = true;
  updateImageSizeFields();
});
// 失焦 / 回车：这时才把吸附后的值写回输入框
["#imgWidth", "#imgHeight"].forEach(sel => {
  const el = $(sel);
  if (!el) return;
  const commit = () => {
    imgSizeTyping = false;
    updateImageSizeFields();
  };
  el.addEventListener("change", commit);
  el.addEventListener("blur", commit);
});
$("#imgSizeReset").addEventListener("click", () => {
  const s = imgCtx().st;
  s.width = null; s.height = null;
  s.rawWidth = null; s.rawHeight = null;
  s.custom = false;          // 回到预设档位 -> 「自定义」标记也要清掉
  imgSizeTyping = false;     // 退出输入态，保证预设尺寸写回输入框
  renderImagePanel();
});

// ---------- Qwen-Image 2.1：参考图（素材库来源，最多 10 张） ----------
//
// 参考图的完整逻辑搬到 web/qwen-refs.js（window.QwenRefs）——
// 理由：app.js 正被并发任务修改（移动端适配 / LoRA 面板），
// 参考图这块改动集中在一个文件里，冲突面最小。
//
// 关键语义变化（相对上一版）：
//   * 参考图**只能**来自素材库 —— 本地上传通道已移除；
//   * 占位标记是官方的 <imageN>（不是中文「<参考图N>」）：
//     官方 PE-I2I system prompt 要求多图必须用 <image1>/<image2>，
//     单图不要用标签；ComfyUI 的 qwen_image21.py 也用 "<image{n}>" 前缀。
//   * 顺序即语义：第 N 张 <-> <imageN> <-> ComfyUI 的 image_N 插槽。
function qwenRefLimit() {
  return QWEN_OPTIONS.maxRefImages || 10;
}

function renderQwenRefs() {
  if (window.QwenRefs) window.QwenRefs.render();
}

$("#imgRefAdd")?.addEventListener("click", () => {
  if (window.QwenRefs) window.QwenRefs.openLibraryPicker();
});

$("#imgRefSort")?.addEventListener("click", () => {
  if (window.QwenRefs) window.QwenRefs.smartSort($("#prompt")?.value || "");
});

// 【官方 PE 扩写的档位说明】
// 官方 PE 有两个独立 checkpoint，**不可互换**：
//   PE-T2I（文生图）：presence_penalty=1.5, max_new_tokens=16256
//   PE-I2I（编辑）  ：presence_penalty=0,   max_new_tokens=24000
// 填错不报错，只会悄悄改采样分布 —— 所以档位随模式写死。
//
// 【为什么没有独立的 PE 勾选框】
// 官方 PE 扩写已并入「增强」按钮的下拉菜单（「qwen21图片官方增强PE」）。
// 面板上再放一个同名勾选框会出现两个入口、两种语义 —— 用户会问
// 「这两个哪个算数」。所以这里**只保留出图时的官方 PE 接线**，
// 手动扩写统一从「增强」下拉进。
//
// 【出图时到底扩不扩 —— 两态，与「普通增强」同语义】
//   默认（没做过手动官方 PE 扩写）：usePe=true -> 出图时链路内部再扩写一次
//     （官方最佳实践：Qwen-Image 2.1 官方工作流就是这么走的）
//   做过手动官方 PE 扩写后：usePe=false -> 提示词框里**已经是**官方 PE 结果，
//     出图时不再二次扩写（否则会把用户的编辑冲掉、白白多花一次 9B 推理）
//   两条路都能拿到官方 PE 效果，区别只在「谁来扩写」。
//
// 注：原先读取官方 PE 档位的辅助函数，随独立 PE 勾选框（及其档位徽标）
//     一起删除后已无调用点，故不再保留。

// 拉取服务端下发的档位 / 比例 / 模型清单
async function loadImageOptions() {
  try {
    const resp = await fetch("/api/image-options", {cache: "no-store"});
    if (!resp.ok) return;
    const data = await resp.json();
    // ---- 后端能力探测（必须在读 moss 字段之前）----
    // 后端没重启时 /api/image-options 里没有 moss 字段 -> MOSS 面板的
    // 档位/模型下拉会是空的、LoRA 会串到 Z-Image 族。
    // 与其让用户对着空白面板猜，不如直接告诉他「后端没重启」。
    SERVER_FEATURES = data.features || SERVER_FEATURES;
    IMG_OPTIONS.tiers = data.tiers || [];
    IMG_OPTIONS.aspects = data.aspects || [];
    IMG_OPTIONS.models = data.models || {};
    IMG_OPTIONS.sizeMultiple = data.size_multiple || 16;
    IMG_OPTIONS.maxDirectMp = data.max_direct_mp || 3;
    // 自定义宽高的合法范围（服务端从 ComfyUI 节点 schema 下发）
    IMG_OPTIONS.sizeMin = data.size_min || 16;
    IMG_OPTIONS.sizeMax = data.size_max || 16384;
    // Qwen-Image 2.1 的独立一套清单（服务端 qwen 字段）
    const q = data.qwen || {};
    QWEN_OPTIONS.tiers = q.tiers || [];
    QWEN_OPTIONS.aspects = q.aspects || [];
    QWEN_OPTIONS.models = q.models || {};
    QWEN_OPTIONS.sizeMultiple = q.size_multiple || 16;
    QWEN_OPTIONS.maxDirectMp = q.max_direct_mp || 4;
    QWEN_OPTIONS.sizeMin = 16;
    QWEN_OPTIONS.sizeMax = 16384;
    QWEN_OPTIONS.defaultTier = q.default_tier || "1mp";
    QWEN_OPTIONS.defaultModel = q.default_model || "qwen_image_2.1_bf16";
    QWEN_OPTIONS.maxRefImages = q.max_ref_images || 10;
    QWEN_OPTIONS.minRefImages = q.min_ref_images || 1;
    QWEN_OPTIONS.nodeRefSlots = q.node_ref_slots || 16;
    QWEN_OPTIONS.peParams = q.pe_params || {};
    QWEN_OPTIONS.peDefaultMaxLength = q.pe_default_max_length || 4096;
    // Qwen 默认值对齐服务端推荐（首次进入该模式时用）
    // Qwen-Image 2.1 的默认值：以服务端下发的推荐值为准（步数/CFG/SHIFT…）
    const qm = QWEN_OPTIONS.models[QWEN_OPTIONS.defaultModel];
    if (qm) {
      state.imgQwen.model = QWEN_OPTIONS.defaultModel;
      state.imgQwen.steps = qm.steps;
      state.imgQwen.cfg = qm.cfg;
      state.imgQwen.shift = qm.shift;
      state.imgQwen.sampler = qm.sampler;
      state.imgQwen.scheduler = qm.scheduler;
      state.imgQwen.tier = QWEN_OPTIONS.defaultTier;
      const qKnown = QWEN_OPTIONS.aspects.some(a => a.key === state.imgQwen.aspect);
      if (!qKnown && QWEN_OPTIONS.aspects.length) {
        state.imgQwen.aspect = QWEN_OPTIONS.aspects[0].key;
      }
    }
    // MOSS-Image 的独立一套清单（服务端 moss 字段）
    const m = data.moss || {};
    // 服务端没下发 moss 字段 = 后端还是旧代码（没重启）
    MOSS_BACKEND_READY = !!(m.tiers && m.tiers.length);
    MOSS_OPTIONS.tiers = m.tiers || [];
    MOSS_OPTIONS.aspects = m.aspects || [];
    MOSS_OPTIONS.models = m.models || {};
    MOSS_OPTIONS.sizeMultiple = m.size_multiple || 16;
    MOSS_OPTIONS.maxDirectMp = m.max_direct_mp || 4;
    MOSS_OPTIONS.minMp = m.min_mp || 0.25;
    MOSS_OPTIONS.sizeMin = 16;
    MOSS_OPTIONS.sizeMax = 16384;
    MOSS_OPTIONS.defaultTier = m.default_tier || "1mp";
    MOSS_OPTIONS.defaultModel = m.default_model || "moss_image_base";
    MOSS_OPTIONS.defaultNegative = m.default_negative || "";
    // MOSS 默认值对齐服务端推荐（官方 USERGUIDE：50 步 / cfg 3 / shift 5）
    const mm = MOSS_OPTIONS.models[MOSS_OPTIONS.defaultModel];
    if (mm) {
      state.imgMoss.model = MOSS_OPTIONS.defaultModel;
      state.imgMoss.steps = mm.steps;
      state.imgMoss.cfg = mm.cfg;
      state.imgMoss.shift = mm.shift;
      state.imgMoss.sampler = mm.sampler;
      state.imgMoss.scheduler = mm.scheduler;
      state.imgMoss.slgSingle = mm.slg_single_layers !== undefined
        ? mm.slg_single_layers : "22";
      state.imgMoss.tier = MOSS_OPTIONS.defaultTier;
      // 负面提示词：官方推荐值预填（用户改过就不覆盖）
      if (!state.imgMoss.negative && MOSS_OPTIONS.defaultNegative) {
        state.imgMoss.negative = MOSS_OPTIONS.defaultNegative;
      }
      const mKnown = MOSS_OPTIONS.aspects.some(a => a.key === state.imgMoss.aspect);
      if (!mKnown && MOSS_OPTIONS.aspects.length) {
        state.imgMoss.aspect = MOSS_OPTIONS.aspects[0].key;
      }
    }
    if (data.default_model && IMG_OPTIONS.models[data.default_model]) {
      state.img.model = data.default_model;
    }
    if (data.default_tier) state.img.tier = data.default_tier;
    const known = IMG_OPTIONS.aspects.some(a => a.key === state.img.aspect);
    if (!known && IMG_OPTIONS.aspects.length) state.img.aspect = IMG_OPTIONS.aspects[0].key;
    applyModelDefaults(state.img.model);
    if (IMAGE_MODES.includes(state.mode)) renderImagePanel();
  } catch (e) { /* 忽略：保持内置默认值 */ }
}

// ---------- 通用分段选择 ----------
function bindSeg(sel, key, cb) {
  $$(sel + " button").forEach(b => b.addEventListener("click", () => {
    $$(sel + " button").forEach(x => x.classList.remove("active"));
    b.classList.add("active");
    state[key] = b.dataset.v;
    if (cb) cb();
  }));
}
bindSeg("#resSeg", "res");
bindSeg("#aspectChips", "aspect");
bindSeg("#modelCards", "model", () => {
  $("#loraField").style.display = state.model === "lora" ? "" : "none";
  updateSeedStrengthLayout();
  setDefaultSteps();
  // 视频模型换了 -> 增强模板按新模型的模型族重新过滤
  if (typeof refreshEnhanceSpecForMode === "function") refreshEnhanceSpecForMode();
});
bindSeg("#loraCards", "lora");
// 【2026-11-11】LoRA 强度滑杆从「生成按钮下方」搬到了「LoRA 选择」旁边
// （index.html 的 #loraField 内）。控件没删，只是换了位置 ——
// 底部那个是重复控件（与文生视频面板「加速 LoRA」旁的强度控件同指一事）。
// flf / r2v 走旧工作流路径，读的就是 params.lora_strength。
$("#loraStrength").addEventListener("input", e => {
  state.loraStrength = parseFloat(e.target.value);
  $("#loraStrengthVal").textContent = state.loraStrength.toFixed(2);
});
$("#steps").addEventListener("input", e => {
  state.steps = parseInt(e.target.value);
  $("#stepsVal").textContent = state.steps;
});
function setDefaultSteps() {
  const def = state.model === "lora" ? 6 : 20;
  state.steps = def;
  $("#steps").value = def;
  $("#stepsVal").textContent = def;
}
// 【2026-11-11】原来这个函数控制「生成按钮下方」的 LoRA 强度滑杆显隐。
// 那个滑杆已搬到「LoRA 选择」旁边（#loraField 内），所以这里改成控制
// #loraStrengthField —— 它现在住在 #loraField 里，而 #loraField 只在
// 选了 lora 模型时显示，因此两者的显隐天然一致。
// （#seedStrengthPair 容器已不存在，不再操作。）
function updateSeedStrengthLayout() {
  const showStrength = MODE_INFO[state.mode].kind === "video" && state.model === "lora";
  const field = $("#loraStrengthField");
  if (field) field.style.display = showStrength ? "" : "none";
}
$("#duration").addEventListener("input", e => {
  state.duration = parseInt(e.target.value);
  $("#durVal").textContent = state.duration + "s";
});

// ---------- 提示词增强状态（图像页未增强拦截） ----------
// 记录「当前提示词已被增强」的时刻：用户手动编辑过就视为未增强。
const promptEnhance = { text: null, busy: false };

// 提示词版本谱系：原始 / 一次增强 / 二次增强
//   versions[] 保留每一级的文本与产生方式，可查看、可回退、可再增强
const promptVersions = {
  // [{level: 0|1|2, label, text, source, specId, at}]
  versions: [],
  // 二次增强的作用对象：0=原始 / 1=一次增强
  refineTarget: 0,
  busy: false,
};

const PROMPT_LEVEL_LABEL = {0: "原始", 1: "一次增强", 2: "二次增强"};

function promptIsEnhanced() {
  return promptEnhance.text !== null && $("#prompt").value === promptEnhance.text;
}

function markPromptEnhanced() {
  promptEnhance.text = $("#prompt").value;
  updatePromptBadge();
}

function clearPromptEnhanced() {
  promptEnhance.text = null;
  updatePromptBadge();
}

function updatePromptBadge() {
  const badge = $("#promptEnhancedBadge");
  if (!badge) return;
  const level = currentPromptLevel();
  badge.hidden = level === 0;
  if (level > 0) {
    badge.textContent = PROMPT_LEVEL_LABEL[level] || "已增强";
    badge.classList.toggle("lv2", level === 2);
  }
}

// 当前提示词处于第几级增强（0=原始）
function currentPromptLevel() {
  const current = $("#prompt").value;
  let level = 0;
  for (const v of promptVersions.versions) {
    if (v.text === current) level = Math.max(level, v.level);
  }
  // 手动编辑过 -> 视为回到原始级
  if (promptEnhance.text !== null && promptEnhance.text !== current) level = 0;
  if (promptVersions.busy) return level;
  return level;
}

/**
 * 当前提示词框内容的「原始版」（增强之前用户写的那段）。
 *
 * 【为什么需要】
 * 提示词框里放的是**增强后**的内容（用户点「替换提示词框」就换掉了）。
 * 但提示词历史要同时保留「原始」与「增强后」两份 ——
 * 所以提交时把原始版一起带上（params.original_prompt），
 * 服务端才能把两条都记进历史。
 *
 * 取法：版本谱系里的 level 0（就是「原始提示词」那条）。
 * 没有 level 0（比如用户自己手敲的、没走过增强）-> 返回空串，
 * 服务端会退化成「原始 == 最终」。
 */
function currentOriginalPrompt() {
  const original = promptVersions.versions.find(v => v.level === 0 && v.text);
  return original ? original.text : "";
}

// 记录一个版本
function pushPromptVersion(level, text, source, specId) {
  const trimmed = (text || "").trim();
  if (!trimmed) return;
  const existing = promptVersions.versions.findIndex(v => v.level === level && v.text === trimmed);
  const entry = {level, label: PROMPT_LEVEL_LABEL[level] || `第${level}级`,
                 text: trimmed, source: source || "", specId: specId || "",
                 at: Date.now()};
  if (existing >= 0) promptVersions.versions[existing] = entry;
  else promptVersions.versions.push(entry);
  renderPromptVersions();
}

// 恢复某个版本到输入框
function restorePromptVersion(index) {
  const v = promptVersions.versions[index];
  if (!v) return;
  $("#prompt").value = v.text;
  // 恢复到该版本 = 该版本已被「采用」，标记为已增强（level>0）
  if (v.level > 0) markPromptEnhanced();
  else clearPromptEnhanced();
  updatePromptBadge();
  closePromptVersionModal();
}

// 版本历史弹窗
function openPromptVersionModal() {
  const box = $("#promptVersionList");
  if (!box) return;
  if (!promptVersions.versions.length) {
    box.innerHTML = `<div class="version-empty">还没有增强记录。先做一次「增强」或「二次增强」。</div>`;
  } else {
    const current = $("#prompt").value;
    box.innerHTML = promptVersions.versions.map((v, i) => `
      <div class="version-item ${v.text === current ? "current" : ""}">
        <div class="version-head">
          <span class="version-badge lv${v.level}">${esc(v.label)}</span>
          ${v.source ? `<span class="version-source">${esc(v.source)}</span>` : ""}
          ${v.text === current ? `<span class="version-current">当前</span>` : ""}
        </div>
        <div class="version-text">${esc(v.text.slice(0, 400))}${v.text.length > 400 ? "…" : ""}</div>
        <div class="version-actions">
          <button type="button" data-restore="${i}">恢复到输入框</button>
        </div>
      </div>`).join("");
    box.querySelectorAll("[data-restore]").forEach(btn => {
      btn.addEventListener("click", () => restorePromptVersion(Number(btn.dataset.restore)));
    });
  }
  $("#promptVersionModal").hidden = false;
  document.body.classList.add("modal-open");
}

function closePromptVersionModal() {
  const modal = $("#promptVersionModal");
  if (!modal || modal.hidden) return;
  modal.hidden = true;
  document.body.classList.remove("modal-open");
}

// 二次增强作用对象（原始 / 一次增强）
function renderRefineTargets() {
  const box = $("#refineTargetChips");
  if (!box) return;
  const hasL1 = promptVersions.versions.some(v => v.level === 1);
  const options = [
    {level: 0, label: "原始提示词", enabled: true},
    {level: 1, label: "一次增强后", enabled: hasL1},
  ];
  // 目标级别被删掉后回落
  if (!options.some(o => o.level === promptVersions.refineTarget && o.enabled)) {
    promptVersions.refineTarget = 0;
  }
  box.innerHTML = options.map(o =>
    `<button type="button" data-level="${o.level}" class="${promptVersions.refineTarget === o.level ? "active" : ""}" ${o.enabled ? "" : 'disabled title="还没有一次增强的结果"'}>${esc(o.label)}</button>`
  ).join("");
  box.querySelectorAll("button").forEach(btn => {
    btn.addEventListener("click", () => {
      if (btn.disabled) return;
      promptVersions.refineTarget = Number(btn.dataset.level);
      renderRefineTargets();
    });
  });
}

function renderPromptVersions() {
  updatePromptBadge();
  renderRefineTargets();
}

// 手动编辑提示词 -> 增强标记立即失效
$("#prompt").addEventListener("input", () => {
  promptEnhance.text = null;
  updatePromptBadge();
});

// 增强中 / 生成中互斥：避免并发重复提交
// 注意：一次增强与二次增强期间，生成按钮都必须禁用。
function updateActionButtons() {
  const enhanceBusy = promptEnhance.busy || promptVersions.busy;
  const busy = enhanceBusy || state.genBusy;
  const genBtn = $("#genBtn");
  const enhBtn = $("#enhanceBtn");
  const enhText = $("#enhanceBtnText");
  const menuBtn = $("#enhanceMenuBtn");
  if (genBtn) {
    genBtn.disabled = busy;
    genBtn.classList.toggle("busy", state.genBusy);
    genBtn.textContent = state.genBusy ? "生成中…" : "生成";
  }
  if (enhBtn) {
    enhBtn.disabled = busy;
    enhBtn.classList.toggle("busy", promptEnhance.busy);
  }
  // 主按钮文字随「当前增强方式」变化 —— 方式名由 enhance-menu.js 管，
  // 这里只负责 busy 期间的临时文案。
  //
  // 【多选】按钮文字取决于**选中了几项**（一项=该项文字，多项=「增强（N 项）」），
  // 这个逻辑在 enhance-menu.js 里（updateButton），所以这里不要自己拼 ——
  // 两处各拼一遍必然走偏。enhance-menu.js 暴露了 refreshButton() 供这里调用。
  if (enhText) {
    if (promptEnhance.busy) {
      enhText.textContent = "正在增强…";
    } else if (promptVersions.busy) {
      enhText.textContent = "正在细化…";
    } else if (window.enhanceMethods
               && typeof window.enhanceMethods.refreshButton === "function") {
      window.enhanceMethods.refreshButton();
    }
  }
  // 下拉箭头同样要禁 —— 否则增强进行中还能切换方式
  if (menuBtn) menuBtn.disabled = busy;
}

// ---------- 未增强拦截：确认用原始提示词生成 / 取消中止 ----------
let gateResolve = null;

function askEnhanceGate() {
  const modal = $("#enhanceGateModal");
  if (!modal) return Promise.resolve(true);
  modal.hidden = false;
  document.body.classList.add("modal-open");
  return new Promise(resolve => { gateResolve = resolve; });
}

function closeEnhanceGate(confirmed) {
  const modal = $("#enhanceGateModal");
  if (!modal || modal.hidden) return;
  modal.hidden = true;
  document.body.classList.remove("modal-open");
  const fn = gateResolve;
  gateResolve = null;
  if (fn) fn(!!confirmed);
}

$("#enhanceGateConfirm").addEventListener("click", () => closeEnhanceGate(true));
$("#enhanceGateCancel").addEventListener("click", () => closeEnhanceGate(false));
$("#enhanceGateClose").addEventListener("click", () => closeEnhanceGate(false));
$("#enhanceGateModal").addEventListener("click", event => {
  if (event.target === $("#enhanceGateModal")) closeEnhanceGate(false);
});
document.addEventListener("keydown", event => {
  if (event.key === "Escape" && !$("#enhanceGateModal").hidden) closeEnhanceGate(false);
});

// ---------- 生成中跟踪：任务到达终态后恢复按钮 ----------
const pendingJobs = new Set();
const JOB_POLL_MAX_MS = 30 * 60 * 1000;
let jobPollStartedAt = 0;

function trackJob(jobId) {
  if (!jobId) return;
  pendingJobs.add(jobId);
  if (!jobPollStartedAt) jobPollStartedAt = Date.now();
  state.genBusy = true;
  updateActionButtons();
  pollPendingJobs();
}

async function pollPendingJobs() {
  if (!pendingJobs.size) {
    state.genBusy = false;
    jobPollStartedAt = 0;
    updateActionButtons();
    return;
  }
  // 安全上限：极端情况下（后端不回写状态）也不要永久锁死按钮
  if (Date.now() - jobPollStartedAt > JOB_POLL_MAX_MS) {
    pendingJobs.clear();
    state.genBusy = false;
    jobPollStartedAt = 0;
    updateActionButtons();
    return;
  }
  for (const id of [...pendingJobs]) {
    try {
      const resp = await fetch(`/api/jobs/${id}`, {cache: "no-store"});
      if (!resp.ok) continue;
      const job = await resp.json();
      if (!["queued", "running"].includes(job.status)) pendingJobs.delete(id);
    } catch (e) { /* 网络抖动则下一轮再试 */ }
  }
  state.genBusy = pendingJobs.size > 0;
  if (!state.genBusy) jobPollStartedAt = 0;
  updateActionButtons();
  if (pendingJobs.size) setTimeout(pollPendingJobs, 1500);
}

// ---------- 提交 ----------
$("#genBtn").addEventListener("click", async () => {
  if (state.genBusy || promptEnhance.busy) return;
  const mode = state.mode;

  // 音乐模式：描述来自音乐面板的「音乐描述」框（#musicPrompt），
  // 走 music.js 的收集逻辑
  if (isMusicMode(mode)) {
    if (!window.MusicUI) { alert("音乐面板模块未加载"); return; }
    const collected = window.MusicUI.collect();
    // 有结构化风格时，style 由结构化字段合成（后端用 style_* 字段），
    // 所以「描述」可以为空；没有结构化风格时描述必填。
    // 结构化字段在 params 里是 style_<field> 形式。
    const hasStruct = Object.keys(collected.params)
      .some(k => k.startsWith("style_") && collected.params[k] !== "");
    if (!collected.params.prompt && !hasStruct) {
      alert("请填写音乐描述，或先用「增强」生成结构化风格");
      return;
    }
    if (mode === "a2a" && !collected.assetRefs.length) {
      alert("参考生音乐需要从素材库选择一段参考音频。");
      return;
    }
    const musicFd = new FormData();
    musicFd.append("mode", mode);
    // 【提示词历史】音乐面板的原始版 = 增强前的音乐描述。
    // 音乐增强是**结构化**的（风格字段 + 歌词），提示词框里的描述通常没被改，
    // 所以这里取提示词框当前值即可；服务端会自己判定是否算「未增强」。
    if (collected.params.prompt && !collected.params.original_prompt) {
      collected.params.original_prompt = collected.params.prompt;
    }
    musicFd.append("params", JSON.stringify(collected.params));
    if (collected.assetRefs.length) {
      musicFd.append("asset_refs", JSON.stringify(collected.assetRefs));
    }
    try {
      const resp = await fetch("/api/jobs", { method: "POST", body: musicFd });
      const data = await resp.json();
      if (!resp.ok) { alert(data.error || "提交失败"); return; }
      trackJob(data.id);
      state.tab = "active";
      renderTabs();
      refreshJobs();
    } catch (e) {
      alert("提交失败: " + e.message);
    }
    return;
  }

  const prompt = $("#prompt").value.trim();
  if (!prompt) { alert("请填写提示词"); return; }
  // 图像模式：提示词未增强时先确认
  //   确认 -> 用用户原始提示词继续生成；取消/关闭 -> 中止本次生成（不自动开始增强）
  if (IMAGE_MODES.includes(mode) && currentPromptLevel() === 0) {
    const ok = await askEnhanceGate();
    if (!ok) return;
  }
  const fd = new FormData();
  fd.append("mode", mode);
  const params = {
    prompt,
    // 【提示词历史】原始版一起带上 —— 提示词框里现在是增强后的内容，
    // 没有这一项服务端就分不出「原始」与「增强后」。
    original_prompt: currentOriginalPrompt(),
    resolution: state.res,
    aspect: state.aspect,
    duration: state.duration,
    steps: state.steps,
    model: state.model,
    lora: state.model === "lora" ? state.lora : "none",
    lora_strength: state.loraStrength,
    seed: $("#seed").value.trim() || null,
  };
  if (IMAGE_MODES.includes(mode)) {
    // 图像模式：把档位 / 比例 / 实际宽高 / 采样参数一起带上去。
    // resolution 复用同一个字段，图像模式的值是分辨率档位（1mp / 480p…4k）。
    const ctx = imgCtx();
    const s = ctx.st;
    const [w, h] = imgCurrentSize();
    params.resolution = s.tier;
    params.aspect = s.aspect;
    params.width = w;
    params.height = h;
    params.model = s.model;
    params.sampler = s.sampler;
    params.scheduler = s.scheduler;
    params.steps = s.steps;
    params.cfg = s.cfg;
    params.shift = s.shift;
    params.negative_prompt = s.negative || "";
    // 生成批次：一次提交 N 张（服务端拆成 N 个独立批次）
    params.batch_count = s.batchCount;
    // LoRA：只提交启用且强度 > 0 的（服务端会再校验文件是否存在）
    // trigger 一起带上 —— 服务端据此把触发词追加进最终提示词
    params.loras = s.loras
      .filter(l => l.enabled !== false && Number(l.strength) > 0)
      .map(l => ({
        name: l.name,
        strength: Number(l.strength),
        trigger: l.trigger !== undefined && l.trigger !== null ? l.trigger : "",
      }));
    if (isQwenMode()) {
      // Qwen-Image 2.1：出图时是否再走一次官方 PE 扩写。
      // 默认 true（官方最佳实践，官方工作流就是这么走的）；
      // 用户在「增强」下拉里手动跑过「qwen21图片官方增强PE」后变 false ——
      // 此时提示词框里已经是官方 PE 结果，再扩一次会冲掉用户的编辑。
      params.use_pe = !!state.imgQwen.usePe;
      // 官方 PE 的档位参数（presence_penalty / max_new_tokens）：
      // 由「增强」多选列表里官方 PE 那一项自动套用（选中即套用，可再改）。
      // 多项共存时是**叠加**结果，后一项不会清掉前一项已套用的参数。
      if (state.imgQwen.peParams) {
        params.pe_params = {
          presence_penalty: state.imgQwen.peParams.presence_penalty,
          max_new_tokens: state.imgQwen.peParams.max_new_tokens,
        };
      }
      // 官方 PE 的 system prompt（用户在「增强」多选列表里编辑过的那份）：
      // 与手动扩写用的是同一份文本 -> 扩写与出图走同一套规则。
      const peSp = state.imgQwen.peSystemPrompt;
      if (peSp && String(peSp).trim()) {
        params.pe_system_prompt = String(peSp);
      }
    }
    if (isMossMode()) {
      // MOSS-Image：SLG（SkipLayerGuidance）skip layer —— 官方推荐 22，空 = 关
      params.slg_single_layers = state.imgMoss.slgSingle || "";
    }
  }
  if (mode === "t2v" && window.VIDEO_PANEL && window.VIDEO_PANEL.isReady()) {
    // 文生视频新面板：宽高 / 模型 / 加速 LoRA 组合 / 注意力 / 二采
    Object.assign(params, window.VIDEO_PANEL.submitParams());
  }
  // 图片编辑的参考图：**必须来自素材库**。
  // 这里只提交素材库条目 ID + 描述 + 标签 + 顺序，
  // 服务端据此把素材库文件上传到 ComfyUI，并按 <imageN> 顺序接线。
  if (mode === "i2i_qwen") {
    const max = QWEN_OPTIONS.maxRefImages || 10;
    const refList = window.QwenRefs ? window.QwenRefs.getRefs() : [];
    if (!refList.length) {
      alert("图片编辑的参考图必须来自素材库：请点「从素材库添加」选择至少 1 张。");
      return;
    }
    if (refList.length > max) {
      alert(`参考图最多 ${max} 张，当前 ${refList.length} 张。`);
      return;
    }
    // 提交前一致性校验：占位标记 <imageN> 与参考图数量必须匹配
    const problems = window.QwenRefs.validate($("#prompt")?.value || "");
    if (problems) {
      alert("参考图校验未通过：\n\n" + problems.map(x => "· " + x).join("\n"));
      return;
    }
    params.ref_images = window.QwenRefs.toPayload();
  }
  fd.append("params", JSON.stringify(params));
  try {
    // 素材库引用：槽位 -> asset_id，随任务一起提交（服务端按槽位上传到 ComfyUI）
    const assetRefs = [];
    const SLOT_FOR_KEY = {
      first: "first_frame", last: "last_frame",
      ref0: "ref_image", ref1: "ref_image", ref2: "ref_image",
    };
    Object.entries(state.assetRefs || {}).forEach(([key, item], index) => {
      if (!item || !item.asset_id) return;
      assetRefs.push({
        asset_id: item.asset_id,
        slot: SLOT_FOR_KEY[key] || "ref_image",
        ordinal: index,
      });
    });
    if (assetRefs.length) fd.append("asset_refs", JSON.stringify(assetRefs));

    if (mode === "flf") {
      const hasFirst = state.files.first || (state.assetRefs && state.assetRefs.first);
      if (!hasFirst) { alert("首尾帧模式需要上传首帧图片"); return; }
      if (state.files.first) fd.append("file_first", state.files.first);
      if (state.files.last) fd.append("file_last", state.files.last);
    } else if (mode === "r2v") {
      const refs = [state.files.ref0, state.files.ref1, state.files.ref2].filter(Boolean);
      const assetCount = Object.keys(state.assetRefs || {}).filter(k => k.startsWith("ref")).length;
      if (!refs.length && !assetCount) { alert("参考模式至少需要 1 张参考图"); return; }
      refs.forEach(f => fd.append("file_ref", f));
    }
    const resp = await fetch("/api/jobs", { method: "POST", body: fd });
    const data = await resp.json();
    if (!resp.ok) { alert(data.error || "提交失败"); return; }
    trackJob(data.id);
    // 清空文件选择（保留提示词便于连做）
    state.files = { first: null, last: null, refs: [] };
    state.assetRefs = {};
    renderMode();
    state.tab = "active";
    renderTabs();
    refreshJobs();
  } catch (e) {
    alert("提交失败: " + e.message);
  }
});

// ---------- 任务列表 ----------
// ---------- 生成类型标签（服务端下发，可与状态标签叠加） ----------
function renderGenTypeTabs() {
  const box = $("#genTypeTabs");
  if (!box) return;
  const label = box.querySelector(".tabs-label");
  const buttons = [
    `<button data-type="" class="${state.genType ? "" : "active"}">全部类型</button>`,
    ...GEN_TYPES.types.map(t =>
      `<button data-type="${esc(t.key)}" class="${state.genType === t.key ? "active" : ""}" title="${esc(t.label)}">${esc(t.label)}</button>`
    ),
  ];
  box.innerHTML = "";
  if (label) box.appendChild(label);
  buttons.forEach(html => {
    const wrap = document.createElement("span");
    wrap.innerHTML = html;
    box.appendChild(wrap.firstElementChild);
  });
  box.querySelectorAll("button").forEach(btn => {
    btn.addEventListener("click", () => {
      state.genType = btn.dataset.type || "";
      renderGenTypeTabs();
      refreshJobs();
    });
  });
}

async function loadGenerationTypes() {
  try {
    const resp = await fetch("/api/generation-types", {cache: "no-store"});
    if (!resp.ok) return;
    const data = await resp.json();
    GEN_TYPES.types = data.types || [];
    GEN_TYPES.registeredModes = data.registered_modes || [];
    // 把「Agent 有哪些素材通道」下发给媒体语义模块。
    //
    // 【为什么必须在这里接 —— 2026-11-10 审查修复】
    // AgentMedia 里原来**硬编码**「a2a / flf 是 Agent 建不了的」，
    // 而后端已改成按 mode 分派槽位（a2a→ref_audio、flf→first_frame）——
    // 合并后那条提示变成**反向阻断**：后端能建，前端说不能。
    // 现在能力位由服务端下发，前端只渲染服务端给的结论。
    const media = window.AgentMedia;
    if (media && typeof media.applyServerAgentSlots === "function") {
      media.applyServerAgentSlots(data);
    }
    renderGenTypeTabs();
  } catch (e) { /* 静默：不阻断任务列表 */ }
}

// 状态标签组：只取带 data-st 的按钮
// 注意：类型标签组也带 .jobs-tabs class，不能用 `.jobs-tabs button`，
// 否则 renderTabs 会把类型标签的 active 状态一起清掉。
function renderTabs() {
  $$(".jobs-tabs button[data-st]").forEach(b =>
    b.classList.toggle("active", b.dataset.st === state.tab));
  $("#doneFilters").hidden = !["done", "all"].includes(state.tab);
}
$$(".jobs-tabs button[data-st]").forEach(b => b.addEventListener("click", () => {
  state.tab = b.dataset.st;
  if (["done", "longfilm", "all"].includes(state.tab)) state.donePage = 1;
  showCreate();
  renderTabs();
  refreshJobs();
}));

function fmtTime(ts) {
  const d = new Date(ts * 1000);
  const p = n => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}/${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function fmtElapsed(sec) {
  if (sec == null) return "—";
  return sec < 90 ? `${Math.round(sec)}s` : `${Math.floor(sec / 60)}m${Math.round(sec % 60)}s`;
}

function modelLabel(p) {
  const steps = p.steps || (p.model === "standard" ? 20 : 4);
  if (p.model === "standard") return `标准 H3 · ${steps}步`;
  return `H3 + ${p.lora === "ckpt850" ? "ckpt850" : "v4-step600"} · ${steps}步`;
}

// 图像模式：模型版本的可读名（清单来自 /api/image-options）
function imgModelLabel(key) {
  // 三族模型各自的清单分开 —— key 在别的族里查不到就回落到原 key
  const m = IMG_OPTIONS.models[key] || MOSS_OPTIONS.models[key]
            || QWEN_OPTIONS.models[key];
  return m ? m.label : (key || "—");
}

// 音乐任务的模型显示名（YuE2 checkpoint key -> 人话）
// 注意：作品卡片可能在 /api/music-options 返回前就渲染出来，
// 所以这里必须带一份静态兜底表，不能只依赖 window.MusicUI。
const MUSIC_MODEL_FALLBACK = {
  yue2_3b_bf16: "YuE2 3B · BF16 完整版",
};
function musicModelLabel(key) {
  if (!key) return "YuE2";
  const opts = (window.MusicUI && window.MusicUI.getOptions) || null;
  const entry = opts && opts.checkpoints ? opts.checkpoints[key] : null;
  if (entry && entry.label) return entry.label;
  if (MUSIC_MODEL_FALLBACK[key]) return MUSIC_MODEL_FALLBACK[key];
  // 未知 key = 已下线的历史档位（量化版已删除，见 music_yue2.py）。
  // 不再回落到裸 key —— 那会把已删除的模型键原样印在作品卡片上。
  return "YuE2（历史档位）";
}

function outputUrl(job, output) {
  const relative = String(output.file || "").replace(/\\/g, "/");
  const path = relative.includes("/") ? relative : `${job.id}/${relative}`;
  return "/files/" + path.split("/").map(encodeURIComponent).join("/");
}

// ---------- 生成结果图片查看器（lightbox） ----------
// 纯前端 + 现有 /files 路由，局域网所有客户端一致生效，历史图片同样适用。
const LIGHTBOX_MIN_SCALE = 0.05;
const LIGHTBOX_MAX_SCALE = 16;

const lightbox = {
  open: false,
  src: "",
  name: "",
  naturalWidth: 0,
  naturalHeight: 0,
  scale: 1,
  fitScale: 1,
  dragging: false,
  dragX: 0,
  dragY: 0,
  scrollX: 0,
  scrollY: 0,
};

// 按原始尺寸显示，但不超出视口；即：先适应屏幕，超过原始尺寸时不再放大
function lightboxComputeFitScale() {
  const stage = $("#lightboxStage");
  const pad = 36;
  const availW = Math.max(1, stage.clientWidth - pad);
  const availH = Math.max(1, stage.clientHeight - pad);
  const w = lightbox.naturalWidth || 1;
  const h = lightbox.naturalHeight || 1;
  return Math.min(availW / w, availH / h, 1);
}

function lightboxApplyScale(animate) {
  const img = $("#lightboxImage");
  if (animate) {
    img.classList.add("zooming");
    requestAnimationFrame(() => requestAnimationFrame(() => img.classList.remove("zooming")));
  }
  // 直接改 width/height（而不是 transform）：布局盒与视觉盒一致，
  // 滚动区域正确，且缩放后图片空白处的点击不会误判为「点遮罩」
  const w = Math.max(1, Math.round(lightbox.naturalWidth * lightbox.scale));
  const h = Math.max(1, Math.round(lightbox.naturalHeight * lightbox.scale));
  img.style.width = `${w}px`;
  img.style.height = `${h}px`;
  $("#lightboxZoom").textContent = `${Math.round(lightbox.scale * 100)}%`;
  $("#lightboxZoomOut").disabled = lightbox.scale <= LIGHTBOX_MIN_SCALE + 1e-6;
  $("#lightboxZoomIn").disabled = lightbox.scale >= LIGHTBOX_MAX_SCALE - 1e-6;
}

function lightboxSetScale(next, anchorClientX, anchorClientY) {
  const stage = $("#lightboxStage");
  const prev = lightbox.scale;
  const clamped = Math.min(LIGHTBOX_MAX_SCALE, Math.max(LIGHTBOX_MIN_SCALE, next));
  if (clamped === prev) return;

  // 以鼠标/按键为锚点缩放：保持锚点下的图内容位置不变
  if (anchorClientX != null && anchorClientY != null) {
    const rect = stage.getBoundingClientRect();
    const img = $("#lightboxImage");
    // 锚点在图片坐标系里的位置（缩放前后保持不变 -> 鼠标下的内容不跳动）
    const px = anchorClientX - rect.left - img.offsetLeft;
    const py = anchorClientY - rect.top - img.offsetTop;
    const ratio = clamped / prev;
    lightbox.scale = clamped;
    lightboxApplyScale(false);
    stage.scrollLeft = px * ratio + img.offsetLeft - (anchorClientX - rect.left);
    stage.scrollTop = py * ratio + img.offsetTop - (anchorClientY - rect.top);
  } else {
    lightbox.scale = clamped;
    lightboxApplyScale(true);
  }
}

function lightboxFit() {
  lightbox.scale = lightboxComputeFitScale();
  lightboxApplyScale(true);
  const stage = $("#lightboxStage");
  stage.scrollLeft = 0;
  stage.scrollTop = 0;
}

function lightboxActual() {
  lightbox.scale = 1;
  lightboxApplyScale(true);
}

function lightboxClose() {
  if (!lightbox.open) return;
  lightbox.open = false;
  $("#lightbox").hidden = true;
  document.body.classList.remove("modal-open");
  $("#lightboxImage").removeAttribute("src");
}

function lightboxShow(src, name, naturalWidth, naturalHeight) {
  lightbox.open = true;
  lightbox.src = src;
  lightbox.name = name || "图片";
  lightbox.naturalWidth = naturalWidth || 0;
  lightbox.naturalHeight = naturalHeight || 0;
  const modal = $("#lightbox");
  modal.hidden = false;
  document.body.classList.add("modal-open");
  $("#lightboxTitle").textContent = lightbox.name;
  $("#lightboxDownload").href = src;
  $("#lightboxDownload").setAttribute("download", lightbox.name);
  $("#lightboxOpenNew").href = src;
  const img = $("#lightboxImage");
  img.src = src;
  // 图片尺寸就绪后再按原始尺寸铺开
  const onLoad = () => {
    if (!lightbox.open) return;
    lightbox.naturalWidth = img.naturalWidth || naturalWidth || 0;
    lightbox.naturalHeight = img.naturalHeight || naturalHeight || 0;
    $("#lightboxDim").textContent = lightbox.naturalWidth && lightbox.naturalHeight
      ? `${lightbox.naturalWidth}×${lightbox.naturalHeight}` : "";
    lightboxFit();
  };
  if (img.complete && img.naturalWidth) onLoad();
  else img.addEventListener("load", onLoad, {once: true});
  img.removeEventListener("error", onLoad, {once: true});
}

$("#lightboxClose").addEventListener("click", lightboxClose);
$("#lightboxZoomIn").addEventListener("click", () => lightboxSetScale(lightbox.scale * 1.25));
$("#lightboxZoomOut").addEventListener("click", () => lightboxSetScale(lightbox.scale / 1.25));
$("#lightboxFit").addEventListener("click", lightboxFit);
$("#lightboxActual").addEventListener("click", lightboxActual);

// 点击遮罩关闭：只认「点到 lightbox 根或 stage 空白处」，
// 点到图片本身（含缩放后的空白）不关
$("#lightbox").addEventListener("click", event => {
  if (event.target === $("#lightbox") || event.target === $("#lightboxStage")) lightboxClose();
});

// 滚轮缩放
$("#lightboxStage").addEventListener("wheel", event => {
  if (!lightbox.open) return;
  event.preventDefault();
  const factor = event.deltaY < 0 ? 1.15 : 1 / 1.15;
  lightboxSetScale(lightbox.scale * factor, event.clientX, event.clientY);
}, {passive: false});

// 拖拽平移（图片大于视口时）
(() => {
  const stage = $("#lightboxStage");
  stage.addEventListener("mousedown", event => {
    if (!lightbox.open) return;
    if (event.button !== 0) return;
    lightbox.dragging = true;
    lightbox.dragX = event.clientX;
    lightbox.dragY = event.clientY;
    lightbox.scrollX = stage.scrollLeft;
    lightbox.scrollY = stage.scrollTop;
    stage.classList.add("dragging");
    event.preventDefault();
  });
  window.addEventListener("mousemove", event => {
    if (!lightbox.dragging) return;
    stage.scrollLeft = lightbox.scrollX - (event.clientX - lightbox.dragX);
    stage.scrollTop = lightbox.scrollY - (event.clientY - lightbox.dragY);
  });
  window.addEventListener("mouseup", () => {
    lightbox.dragging = false;
    stage.classList.remove("dragging");
  });
})();

// 键盘：Esc 关闭，+/- 缩放，0 适应屏幕，1 原始尺寸，方向键平移
document.addEventListener("keydown", event => {
  if (!lightbox.open) return;
  const stage = $("#lightboxStage");
  switch (event.key) {
    case "Escape": lightboxClose(); event.preventDefault(); break;
    case "+": case "=": lightboxSetScale(lightbox.scale * 1.25); event.preventDefault(); break;
    case "-": case "_": lightboxSetScale(lightbox.scale / 1.25); event.preventDefault(); break;
    case "0": lightboxFit(); event.preventDefault(); break;
    case "1": lightboxActual(); event.preventDefault(); break;
    case "ArrowLeft": stage.scrollLeft -= 60; event.preventDefault(); break;
    case "ArrowRight": stage.scrollLeft += 60; event.preventDefault(); break;
    case "ArrowUp": stage.scrollTop -= 60; event.preventDefault(); break;
    case "ArrowDown": stage.scrollTop += 60; event.preventDefault(); break;
    default: break;
  }
});

window.addEventListener("resize", () => {
  if (lightbox.open) lightboxFit();
  // 窗口宽度变了，网格列数要跟着重算（每页作品数不变）
  applyGridColumns(state.pageSize);
  updateResponsiveChrome();
});

// 单条任务内的批次结果：可展开/折叠的缩略图网格 + 每批次状态
// 折叠状态按 job.id 记忆，卡片被重建（实时刷新 / 批次状态变化）后依然保持。
const expandedBatches = new Set();

// 折叠三角朝向：展开 ▼ / 收纳 ▶。用显式字形而不是 CSS 旋转，
// 这样朝向和状态永远一一对应，读屏软件也能读出正确方向。
function batchCaret(open) {
  return open ? "▼" : "▶";
}

// 把折叠状态落到 DOM：标题行 class/aria + 网格 hidden
function applyBatchesState(card, open) {
  const btn = card.querySelector('[data-act="toggle-batches"]');
  if (btn) {
    btn.classList.toggle("open", open);
    btn.setAttribute("aria-expanded", open ? "true" : "false");
    const caret = btn.querySelector(".caret");
    if (caret) caret.textContent = batchCaret(open);
  }
  const grid = card.querySelector("[data-batches]");
  if (grid) grid.hidden = !open;
}

function renderJobBatches(job) {
  const batches = job.batches || [];
  if (!batches.length) return "";
  const p = job.params || {};
  const doneCount = batches.filter(b => b.status === "done").length;
  const failedCount = batches.filter(b => b.status === "failed").length;
  const open = expandedBatches.has(job.id);
  const summary = failedCount
    ? `${doneCount}/${batches.length} 成功 · ${failedCount} 失败`
    : `${doneCount}/${batches.length} 完成`;
  const cells = batches.map(batch => {
    const out = (batch.outputs || []).find(o => o.kind === "image");
    const statusText = {done: "完成", failed: "失败", running: "生成中",
                        queued: "排队中", canceled: "已取消"}[batch.status] || batch.status;
    // 批次失败时即使残留了旧产物也不展示图 —— 状态优先，避免「失败却显示成图」
    const failed = batch.status === "failed";
    const thumb = out && !failed
      ? `<div class="job-batch-thumb"><img src="${outputUrl(job, out)}" alt="批次 ${batch.idx + 1}"><span class="zoom-hint">⤢</span></div>`
      : `<div class="job-batch-thumb ${failed ? "failed" : ""}">${
          failed ? (batch.error || "失败").slice(0, 60)
          : batch.status === "running" ? `${Math.round((batch.progress || 0) * 100)}%`
          : batch.status === "canceled" ? "已取消"
          : "排队中"}</div>`;
    return `<div class="job-batch" data-batch="${batch.id}">
      ${thumb}
      <div class="job-batch-foot">
        <span class="st ${batch.status}">#${batch.idx + 1} ${statusText}</span>
        <span class="seed" title="该批次种子">${batch.seed ?? "—"}</span>
      </div>
    </div>`;
  }).join("");
  // 标题行摘要：完成/失败数 + 出图尺寸 —— 折叠时也能一眼看出里面是什么
  const size = p.width && p.height ? ` · ${p.width}×${p.height}` : "";
  return `<button type="button" class="job-batches-toggle ${open ? "open" : ""}" data-act="toggle-batches" aria-expanded="${open ? "true" : "false"}">
      <span class="caret">${batchCaret(open)}</span> 批次结果（${summary}${size}）
    </button>
    <div class="job-batches" data-batches ${open ? "" : "hidden"}>${cells}</div>`;
}

function jobCard(job) {
  const p = job.params || {};
  const info = MODE_INFO[job.mode] || { name: job.mode };
  const card = document.createElement("div");
  card.className = "job-card";
  const runningOrQueued = ["queued", "running"].includes(job.status);
  const prog = job.status === "queued" ? 0 : (job.status === "running" ? Math.min(job.progress || 0.03, 0.98) : 1);
  const pctText = job.status === "queued" ? "排队中…"
    : `${(job.progress_text || "生成中")} · ${Math.round(prog * 100)}%`;

  let preview = "";
  const video = (job.outputs || []).find(o => o.kind === "video");
  const image = (job.outputs || []).find(o => o.kind === "image");
  const audio = (job.outputs || []).find(o => o.kind === "audio");
  if (audio) {
    preview = `<audio src="${outputUrl(job, audio)}" controls preload="metadata"></audio>`;
  } else if (video) {
    preview = `<video src="${outputUrl(job, video)}" controls preload="metadata"></video>`;
  } else if (image) {
    preview = `<img src="${outputUrl(job, image)}">
      <span class="zoom-hint" title="点击查看大图"><svg viewBox="0 0 16 16" aria-hidden="true"><path d="M7 2H2v5M9 2h5v5M9 14h5V9M7 14H2V9" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg></span>`;
  } else if (runningOrQueued) {
    preview = `<div class="progress-wrap">
      <div class="progress-bar"><div style="width:${Math.round(prog * 100)}%"></div></div>
      <div class="progress-text">${pctText}</div>
    </div>`;
  } else if (job.status === "failed") {
    preview = `<div class="progress-wrap"><div class="err-text">✕ ${job.error || "生成失败"}</div></div>`;
  } else if (job.status === "canceled") {
    preview = `<div class="progress-wrap"><div class="progress-text">已取消</div></div>`;
  }

  const topRight = runningOrQueued
    ? `<button class="cancel-btn" data-act="cancel">取消</button>`
    : `<span>${fmtTime(job.created_at)}</span>`;

  card.dataset.jid = job.id;
  card.innerHTML = `
    <div class="job-top"><span>${job.mode === "longfilm" ? "长片 · " + escapeHtml(p.title || "未命名") : info.name} · ${fmtTime(job.created_at)}</span>${topRight}</div>
    <div class="job-preview">${preview}</div>
    ${job.status === "failed" ? "" : `<div class="job-meta">
      ${job.mode === "longfilm" ? `
      <div class="row"><span>标题</span><b>${escapeHtml(p.title || "未命名")}</b></div>
      <div class="row"><span>片段</span><b>${(p.clips || []).length} 个</b></div>
      <div class="row longfilm-intro"><span>简介</span><b>${escapeHtml((p.intro || "—").slice(0, 80))}</b></div>` : `
      <div class="row"><span>模式</span><b>${info.name}</b></div>
      ${(job.mode === "t2i" || job.mode === "t2i_moss") ? `<div class="row"><span>分辨率</span><b>${p.width || "—"}×${p.height || "—"} · ${p.aspect || "16:9"}</b></div>
      <div class="row"><span>模型</span><b>${escapeHtml(imgModelLabel(p.model))}</b></div>
      <div class="row"><span>采样</span><b>${p.sampler || "—"} · ${p.scheduler || "—"}</b></div>
      <div class="row"><span>步数/CFG</span><b>${p.steps || "—"} · ${p.cfg ?? "—"}</b></div>` : info.kind === "audio" ? `
      <div class="row"><span>模型</span><b>${escapeHtml(musicModelLabel(p.checkpoint))}</b></div>
      <div class="row"><span>采样</span><b>${p.sampler || "—"} · ${p.scheduler || "—"}</b></div>
      <div class="row"><span>步数/CFG</span><b>${p.steps || "—"} · ${p.cfg ?? "—"}</b></div>
      <div class="row"><span>谱面</span><b>${(p.plan_mode || p.mode) === "melody" ? "仅旋律" : "完整谱面"}</b></div>
      <div class="row"><span>格式</span><b>${(p.format || "flac").toUpperCase()}</b></div>` : `
      <div class="row"><span>分辨率</span><b>${p.resolution || "768p"} · ${p.aspect || "16:9"}</b></div>
      ${info.kind === "video" ? `<div class="row"><span>时长</span><b>${p.duration || 5}s</b></div>
      <div class="row"><span>模型</span><b>${modelLabel(p)}</b></div>` : ""}`}
      <div class="row"><span>种子</span><b>${p.seed || "随机"}</b></div>`}
      <div class="row"><span>耗时</span><b>${job.status === "done" ? fmtElapsed(job.elapsed) : fmtElapsed(job.elapsed)}</b></div>
      ${job.mode !== "longfilm" ? (info.kind === "audio" ? `<button class="row prompt-row" data-act="prompt" title="点击查看并复制完整风格描述与歌词"><span>风格描述</span><b>${escapeHtml((p.prompt || "").slice(0, 80))}</b></button>` : `<button class="row prompt-row" data-act="prompt" title="点击查看并复制完整提示词"><span>提示词</span><b>${escapeHtml((p.prompt || "").slice(0, 80))}</b></button>`) : ""}
    </div>`}
    ${renderJobBatches(job)}
    <div class="job-actions">
      ${video || image || audio ? `<button data-act="download">⬇ 下载</button>` : ""}
      ${image ? `<a data-act="open-new" href="${outputUrl(job, image)}" target="_blank" rel="noopener" title="在新窗口打开原图">↗ 新窗口打开</a>` : ""}
      ${video && job.status === "done" && state.page === "stitch" ? `<button data-act="add-stitch">＋ 拼接</button>` : ""}
      ${job.mode !== "longfilm" ? `<button data-act="reuse">复用参数</button>` : ""}
      ${!runningOrQueued ? `<button data-act="delete" class="danger">删除</button>` : ""}
    </div>`;

  if (image) {
    const previewBox = card.querySelector(".job-preview");
    previewBox.classList.add("has-image");
    const dragImage = card.querySelector(".job-preview img");
    const source = {url: outputUrl(job, image), name: String(image.file || "completed-image.png").split(/[\\/]/).pop()};
    dragImage.draggable = true;
    dragImage.title = "点击查看大图；也可拖到左侧参考图、首帧或尾帧位置";
    // 可聚焦 + 键盘可达：Enter / Space 同样能打开大图
    dragImage.tabIndex = 0;
    dragImage.setAttribute("role", "button");
    dragImage.setAttribute("aria-label", `查看大图：${source.name}`);
    const openLightbox = () =>
      lightboxShow(source.url, source.name, dragImage.naturalWidth, dragImage.naturalHeight);
    // 点击缩略图 -> 打开大图查看器
    dragImage.addEventListener("click", event => {
      event.preventDefault();
      event.stopPropagation();
      openLightbox();
    });
    dragImage.addEventListener("keydown", event => {
      if (event.key !== "Enter" && event.key !== " ") return;
      event.preventDefault();
      openLightbox();
    });
    previewBox.addEventListener("click", event => {
      if (event.target !== previewBox) return;
      openLightbox();
    });
    dragImage.addEventListener("dragstart", event => {
      event.dataTransfer.effectAllowed = "copy";
      event.dataTransfer.setData("application/x-h3-image", JSON.stringify(source));
      event.dataTransfer.setData("text/uri-list", new URL(source.url, location.href).href);
      card.classList.add("dragging-image");
    });
    dragImage.addEventListener("dragend", () => card.classList.remove("dragging-image"));
  }
  if (video && job.status === "done") {
    const source = {jobId: job.id, file: video.file, name: String(video.file || "video.mp4").split(/[\\/]/).pop(),
      url: outputUrl(job, video), duration: Number(p.duration || 0), title: p.title || info.name};
    const dragVideo = card.querySelector(".job-preview video");
    dragVideo.draggable = false;
    card.draggable = true;
    card.title = "整张卡片可拖到左侧长片拼接区";
    card.addEventListener("dragstart", event => {
      if (event.target.closest("button")) { event.preventDefault(); return; }
      event.dataTransfer.effectAllowed = "copy";
      event.dataTransfer.setData("application/x-h3-video", JSON.stringify(source));
      card.classList.add("dragging-image");
    });
    card.addEventListener("dragend", () => card.classList.remove("dragging-image"));
    card.querySelector('[data-act="add-stitch"]')?.addEventListener("click", () => {
      state.stitchClips.push(source);
      renderStitchClips();
      $("#stitchPanel").scrollTo({top: $("#stitchPanel").scrollHeight, behavior: "smooth"});
    });
  }

  card.querySelector('[data-act="cancel"]')?.addEventListener("click", async event => {
    const button = event.currentTarget;
    button.disabled = true;
    button.textContent = "正在取消…";
    try {
      const resp = await fetch(`/api/jobs/${job.id}/cancel`, {method:"POST"});
      if (!resp.ok) throw new Error((await resp.json()).error || "取消失败");
      await refreshJobs();
    } catch (err) {
      button.disabled = false;
      button.textContent = "取消";
      alert(err.message);
    }
  });
  card.querySelector('[data-act="delete"]')?.addEventListener("click", async () => {
    if (!confirm("删除该记录及输出文件？")) return;
    await fetch(`/api/jobs/${job.id}`, { method: "DELETE" });
    refreshJobs();
  });
  card.querySelector('[data-act="download"]')?.addEventListener("click", () => {
    const f = video || image || audio;
    if (!f) return;
    const a = document.createElement("a");
    a.href = outputUrl(job, f);
    a.download = String(f.file || "").split(/[\\/]/).pop();
    a.click();
  });
  card.querySelector('[data-act="prompt"]')?.addEventListener("click", () => {
    if (info.kind === "audio") {
      const lyrics = (p.lyrics || "").trim();
      const body = "【风格描述】\n" + (p.prompt || "") + (lyrics ? "\n\n【歌词】\n" + lyrics : "");
      openPromptModal(body, "完整风格描述与歌词");
    } else {
      openPromptModal(p.prompt || "");
    }
  });
  // 批次结果展开/折叠（点标题行任意位置或三角都可以）
  card.querySelector('[data-act="toggle-batches"]')?.addEventListener("click", event => {
    event.stopPropagation();
    const open = !expandedBatches.has(job.id);
    if (open) expandedBatches.add(job.id); else expandedBatches.delete(job.id);
    applyBatchesState(card, open);
  });
  // 批次缩略图点击放大
  card.querySelectorAll(".job-batch-thumb img").forEach(el => {
    el.addEventListener("click", event => {
      event.preventDefault();
      event.stopPropagation();
      const url = el.getAttribute("src");
      const name = url.split("/").pop();
      lightboxShow(url, name, el.naturalWidth, el.naturalHeight);
    });
  });
  card.querySelector('[data-act="reuse"]')?.addEventListener("click", () => {
    // 找到对应模式侧栏项并切换
    $(`.nav-item[data-mode="${job.mode}"]`)?.click();
    $("#prompt").value = p.prompt || "";
    clearPromptEnhanced();
    if (job.mode === "t2v" && p.model_key && window.VIDEO_PANEL) {
      // 文生视频新面板：还原宽高 / 模型 / 加速 LoRA / 注意力 / 二采
      window.VIDEO_PANEL.applyConfig({
        model: p.model_key,
        aspect: p.aspect || (window.VIDEO_PANEL ? window.VIDEO_PANEL.state.aspect : "16:9"),
        width: p.width, height: p.height,
        rawWidth: p.width, rawHeight: p.height,
        custom: true,
        fixed_accel_lora: p.fixed_accel_lora,
        accel_loras: Array.isArray(p.accel_loras) ? p.accel_loras : [],
        attention: p.attention,
        twopass: p.twopass,
      });
      window.VIDEO_PANEL.render();
      if (p.duration) {
        state.duration = p.duration;
        $("#duration").value = p.duration;
        $("#durVal").textContent = p.duration + "s";
      }
      if (p.steps) {
        state.steps = p.steps;
        $("#steps").value = p.steps;
        $("#stepsVal").textContent = p.steps;
      }
      if (p.seed) $("#seed").value = p.seed;
      window.scrollTo({ top: 0, behavior: "smooth" });
      return;
    }
    if (job.mode === "t2i_moss") {
      // MOSS-Image：还原档位 / 比例 / 实际宽高 / 采样参数 / SLG
      const s = state.imgMoss;
      if (p.resolution) s.tier = p.resolution;
      if (p.aspect) s.aspect = p.aspect;
      if (p.width && p.height) {
        s.width = p.width; s.height = p.height;
        s.rawWidth = p.width; s.rawHeight = p.height;
        s.custom = true;      // 复用旧任务的尺寸 -> 也是自定义模式
      }
      if (p.model && MOSS_OPTIONS.models[p.model]) applyModelDefaults(p.model);
      if (p.sampler) s.sampler = p.sampler;
      if (p.scheduler) s.scheduler = p.scheduler;
      if (p.steps) s.steps = p.steps;
      if (p.cfg != null) s.cfg = p.cfg;
      if (p.shift != null) s.shift = p.shift;
      if (p.negative_prompt != null) s.negative = p.negative_prompt;
      if (p.batch_count) s.batchCount = Number(p.batch_count);
      if (p.slg_single_layers !== undefined && p.slg_single_layers !== null) {
        s.slgSingle = String(p.slg_single_layers);
      }
      if (Array.isArray(p.loras)) {
        s.loras = p.loras.map(l => ({
          name: l.name,
          strength: Number(l.strength),
          enabled: true,
          trigger: l.trigger !== undefined && l.trigger !== null ? l.trigger : "",
          display: l.name,
          strength_cap: 1.0,
        }));
      }
      renderImagePanel();
      return;
    }
    if (job.mode === "t2i") {
      // 图像模式：还原分辨率档位 / 比例 / 实际宽高 / Z-Image 采样参数
      const s = state.img;
      if (p.resolution) s.tier = p.resolution;
      if (p.aspect) s.aspect = p.aspect;
      if (p.width && p.height) {
        s.width = p.width; s.height = p.height;
        s.rawWidth = p.width; s.rawHeight = p.height;
        s.custom = true;      // 复用旧任务的尺寸 -> 也是自定义模式
      }
      if (p.model && IMG_OPTIONS.models[p.model]) applyModelDefaults(p.model);
      if (p.sampler) s.sampler = p.sampler;
      if (p.scheduler) s.scheduler = p.scheduler;
      if (p.steps) s.steps = p.steps;
      if (p.cfg != null) s.cfg = p.cfg;
      if (p.shift != null) s.shift = p.shift;
      if (p.negative_prompt != null) s.negative = p.negative_prompt;
      if (p.batch_count) s.batchCount = Number(p.batch_count);
      if (Array.isArray(p.loras)) {
        s.loras = p.loras.map(l => ({
          name: l.name,
          strength: Number(l.strength),
          enabled: true,
          // 触发词沿用任务记录里的版本（用户可能改过）
          trigger: l.trigger !== undefined ? l.trigger : "",
        }));
      }
      renderImagePanel();
    } else {
      if (p.resolution) setSeg("#resSeg", p.resolution), state.res = p.resolution;
      if (p.aspect) setSeg("#aspectChips", p.aspect), state.aspect = p.aspect;
    }
    if (p.duration) { state.duration = p.duration; $("#duration").value = p.duration; $("#durVal").textContent = p.duration + "s"; }
    if (p.steps && job.mode !== "t2i") { state.steps = p.steps; $("#steps").value = p.steps; $("#stepsVal").textContent = p.steps; }
    if (p.seed) $("#seed").value = p.seed;
    if (p.model && job.mode !== "t2i") { setSeg("#modelCards", p.model); state.model = p.model; $("#loraField").style.display = p.model === "lora" ? "" : "none"; updateSeedStrengthLayout(); }
    if (p.lora) { setSeg("#loraCards", p.lora); state.lora = p.lora; }
    window.scrollTo({ top: 0, behavior: "smooth" });
  });
  bindMediaContextMenu(card, job);
  return card;
}

// ---------- 媒体库右键菜单：添加到素材库 ----------
// 覆盖两种来源：
//   * 有批次：产物挂在 job_batches 上（批次缩略图）
//   * 无批次：产物直接挂在 job 上（卡片预览图 / 视频）
function bindMediaContextMenu(card, job) {
  const openMenu = (event, payload) => {
    event.preventDefault();
    event.stopPropagation();
    const menu = $("#mediaContext");
    if (!menu) return;
    const label = payload.batch_id
      ? `批次 #${(payload.batch_index ?? 0) + 1} 的结果`
      : "这个结果";
    menu.innerHTML = `
      <button data-act="add-asset">添加到素材库（${label}）</button>
      <div class="ctx-sep"></div>
      <button data-act="download">下载原文件</button>`;
    menu.classList.add("open");
    // 定位到鼠标处，并防止贴边溢出
    menu.style.left = "0px";
    menu.style.top = "0px";
    const rect = menu.getBoundingClientRect();
    menu.style.left = Math.max(4, Math.min(event.clientX, window.innerWidth - rect.width - 8)) + "px";
    menu.style.top = Math.max(4, Math.min(event.clientY, window.innerHeight - rect.height - 8)) + "px";

    menu.querySelectorAll("button[data-act]").forEach(btn => {
      btn.addEventListener("click", async () => {
        menu.classList.remove("open");
        if (btn.dataset.act === "add-asset") {
          if (window.ASSETLIB && window.ASSETLIB.addFromMedia) {
            await window.ASSETLIB.addFromMedia({
              job_id: job.id,
              batch_id: payload.batch_id || null,
              file: payload.file || null,
            });
          } else {
            alert("素材库模块未加载");
          }
        } else if (btn.dataset.act === "download" && payload.file) {
          const a = document.createElement("a");
          a.href = outputUrl(job, { file: payload.file, kind: payload.kind || "image" });
          a.download = String(payload.file).split(/[\\/]/).pop();
          a.click();
        }
      });
    });
  };

  // 无批次：卡片上的图片 / 视频 / 音频
  const image = (job.outputs || []).find(o => o.kind === "image");
  const video = (job.outputs || []).find(o => o.kind === "video");
  const audioOut = (job.outputs || []).find(o => o.kind === "audio");
  const previewImg = card.querySelector(".job-preview img");
  if (previewImg && image) {
    previewImg.addEventListener("contextmenu", event => openMenu(event, {
      file: image.file, kind: "image", batch_id: null,
    }));
  }
  const previewVideo = card.querySelector(".job-preview video");
  if (previewVideo && video) {
    previewVideo.addEventListener("contextmenu", event => openMenu(event, {
      file: video.file, kind: "video", batch_id: null,
    }));
  }
  const previewAudio = card.querySelector(".job-preview audio");
  if (previewAudio && audioOut) {
    previewAudio.addEventListener("contextmenu", event => openMenu(event, {
      file: audioOut.file, kind: "audio", batch_id: null,
    }));
  }

  // 有批次：批次缩略图
  card.querySelectorAll(".job-batch-thumb img").forEach(el => {
    const batchId = el.closest(".job-batch")?.dataset.batch;
    const batch = (job.batches || []).find(b => b.id === batchId);
    const out = (batch?.outputs || []).find(o => o.kind === "image")
      || (batch?.outputs || [])[0];
    if (!out) return;
    el.addEventListener("contextmenu", event => openMenu(event, {
      file: out.file, kind: out.kind || "image",
      batch_id: batchId, batch_index: batch?.idx ?? 0,
    }));
  });
}

function openPromptModal(prompt, title) {
  const t = $("#promptModalTitle");
  if (t) t.textContent = title || "完整提示词";
  $("#promptModalText").value = prompt;
  $("#promptModal").hidden = false;
  document.body.classList.add("modal-open");
}

function closePromptModal() {
  $("#promptModal").hidden = true;
  document.body.classList.remove("modal-open");
}

$("#promptModalClose").addEventListener("click", closePromptModal);
$("#promptModal").addEventListener("click", event => {
  if (event.target === $("#promptModal")) closePromptModal();
});
document.addEventListener("keydown", event => {
  if (event.key === "Escape" && !$("#promptModal").hidden) closePromptModal();
});
$("#promptCopy").addEventListener("click", async () => {
  const textArea = $("#promptModalText");
  const button = $("#promptCopy");
  try {
    await navigator.clipboard.writeText(textArea.value);
  } catch (_) {
    textArea.focus();
    textArea.select();
    document.execCommand("copy");
  }
  button.textContent = "已复制";
  setTimeout(() => { button.textContent = "复制提示词"; }, 1200);
});

function setSeg(sel, val) {
  $$(sel + " button").forEach(b => b.classList.toggle("active", b.dataset.v === val));
}

// esc() 的正式定义在 providers.js（脚本顺序在后）。但 app.js 在启动阶段
// (renderPromptVersions -> renderRefineTargets) 就会调用它，若此处没有定义就会抛
// ReferenceError 并中断整个 app.js 顶层脚本（含末尾的 setInterval 注册）。
// 这里给一个与 providers.js 同语义的兜底定义，providers.js 加载后会被它覆盖。
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function jobSig(j) {
  // 批次状态/产物变化时也要重建卡片，否则批次网格不会刷新
  const batchSig = (j.batches || [])
    .map(b => `${b.idx}:${b.status}:${Math.round((b.progress || 0) * 100)}:${(b.outputs || []).length}:${b.error || ""}`)
    .join(",");
  return [j.id, j.status, Math.round((j.progress || 0) * 100), j.progress_text || "",
    (j.outputs || []).map(o => o.file).join(","), batchSig].join("|");
}

let jobsRequest = 0;
let displayedTab = null;
let displayedFilterKey = null;
let filterDebounce = null;

// 网格列数由「每页作品数 + 容器宽度」共同决定，目标是一页排成整行：
//   1. 列数不超过每页作品数 —— 每页 8 张时排出 9 列，最后一行只有 1 张、
//      右侧空一大片（这正是 22:9 超宽屏上的问题）；
//   2. 列数尽量贴近宽度能放下的上限，但优先选「能整除每页作品数」的列数，
//      这样每一行都是满的，不会出现「最后一行只有 1 个」；
//   3. 找不到合适的因数时保留原列数（宁可最后一行少几张，也不要整行浪费宽度）。
// 列用 1fr —— 行始终铺满容器宽度，不会出现右侧空白。
// 卡片最小可读宽度与间距：**真源在 CSS**（responsive.css 的 --card-min-width /
// --grid-gap）。JS 从网格元素上读计算样式，这样窄屏换档时
// 「CSS 决定的卡片宽度」与「JS 决定的列数」永远是同一套数字 ——
// 两处各写一份常量必然走偏（窄屏卡片变小、列数没变 = 卡片被挤扁）。
const CARD_MIN_WIDTH_FALLBACK = 260;   // 读不到 CSS 变量时的兜底
const GRID_GAP_FALLBACK = 14;

function readGridMetrics() {
  const grid = $("#jobsGrid");
  if (!grid) return {cardMin: CARD_MIN_WIDTH_FALLBACK, gap: GRID_GAP_FALLBACK};
  const cs = getComputedStyle(grid);
  const min = parseFloat(cs.getPropertyValue("--card-min-width"));
  const gap = parseFloat(cs.getPropertyValue("--grid-gap"));
  return {
    cardMin: Number.isFinite(min) && min > 0 ? min : CARD_MIN_WIDTH_FALLBACK,
    gap: Number.isFinite(gap) && gap >= 0 ? gap : GRID_GAP_FALLBACK,
  };
}

function gridColumnCount(pageSize, containerWidth) {
  const width = containerWidth || 0;
  if (width <= 0) return 1;
  const {cardMin: CARD_MIN_WIDTH, gap: GRID_GAP} = readGridMetrics();
  const maxByWidth = Math.floor((width + GRID_GAP) / (CARD_MIN_WIDTH + GRID_GAP));
  const maxCols = Math.max(1, Math.min(maxByWidth, pageSize));
  // 优先「数量能整除」的列数，使每一行都是满的 —— 既不浪费宽度，又不留空行：
  //   数量能放下时，取最大的整除列数；反之规则的列数变得很少，
  //   例如 2560px 宽库宽可以放 7 列，但 7 不整除 8 和 16，
  //   取 6 列（数、完整、宽度也摊得到）。
  const divisors = [];
  for (let d = 1; d <= maxCols; d++) {
    if (pageSize % d === 0) divisors.push(d);
  }
  if (!divisors.length) return maxCols;
  // 从大到小取第一个整除列数
  divisors.sort((a, b) => b - a);
  return divisors[0];
}

// 把列数写到网格上：inline style 覆盖 CSS 里的 auto-fill
//
// 关键：容器不可见时（width === 0）**必须原样返回，不写 inline style**。
// 素材库页会把 #jobsArea 设成 display:none，此时容器宽度是 0，
// gridColumnCount(0) 会返回 1 —— 一旦把这个「1 列」写进 inline style，
// 切回生成页时网格就会先按 1 列铺满整行渲染一帧：
//   · 首张卡片被拉到整个容器宽（实测 1040px）；
//   · 卡片里的原图按自身像素尺寸渲染（实测 1024×1024）；
//   · 于是用户看到「先出一张原始尺寸巨图，再跳成网格」。
// 保持上一次的正确列数，等容器重新可见时由 ResizeObserver 同帧修正。
function applyGridColumns(pageSize) {
  const grid = $("#jobsGrid");
  if (!grid) return;
  const width = grid.getBoundingClientRect().width;
  if (width <= 0) return;                 // 容器不可见：不动列数
  const cols = gridColumnCount(pageSize, width);
  grid.style.gridTemplateColumns = `repeat(${cols}, minmax(0, 1fr))`;
  grid.dataset.cols = String(cols);
}

// 网格容器尺寸变化时重算列数。
//
// 为什么必须有它：素材库页把 #jobsArea 隐藏（display:none），
// 此时容器宽度 0；切回生成页时容器重新获得宽度，但 refreshJobs 里
// 的 applyGridColumns 要等数据返回（约 1.5s）才跑 —— 这中间的空窗期
// 正是「首图巨大闪现」的窗口。ResizeObserver 的回调在同帧的
// 「布局之后、绘制之前」触发，列数在第一帧绘制前就修正了。
let gridResizeObserver = null;
function initGridResizeObserver() {
  const grid = $("#jobsGrid");
  if (!grid || gridResizeObserver) return;
  if (typeof ResizeObserver === "undefined") return;
  gridResizeObserver = new ResizeObserver(() => applyGridColumns(state.pageSize));
  gridResizeObserver.observe(grid);
}

// ---------- 骨架占位 ----------
// 切换筛选（状态标签 / 生成类型 / 分页）时先铺一组骨架卡，
// 让网格的列宽与行高在数据到达前就稳定下来 —— 数据到达瞬间不再有
// 「图片从无到有」引起的布局抖动。
//
// 关键设计：**骨架卡直接复用真实卡片的 DOM 结构**（jobCard()），
// 只把预览图换成占位块。
// 为什么不能另写一套骨架：真实卡片的元信息区高度随「生成模式」变化
// （t2i 有 8 行、t2v 有 7 行、t2i_qwen 只有 5 行），而且行高受
// @container 断点（卡片窄于 340px 时 3 列改 2 列）影响。
// 另写的骨架只能猜一个高度 —— 数据到达时高度一变就是布局抖动。
// 复用 jobCard() 则保证「骨架 → 真实卡片」的盒模型逐像素一致。
const SKELETON_MAX = 12;

// 造一个「形状与真实卡片一致」的假任务：
// mode 用当前模式，params 用该模式会渲染的字段集 —— 于是元信息行数相同。
function skeletonJob(index) {
  const mode = state.mode || DEFAULT_MODE;
  const isImage = IMAGE_MODES.includes(mode);
  const params = {
    mode: mode,
    prompt: "骨架占位提示词，用于让元信息区高度与真实卡片保持一致，长度对齐真实内容。",
    aspect: "16:9",
    resolution: "768p",
    duration: 5,
    width: 1360,
    height: 768,
    model: "z_image_turbo_bf16",
    steps: 8,
    cfg: 1,
    sampler: "res_multistep",
    scheduler: "simple",
    seed: 0,
  };
  if (!isImage) {
    // 视频模式不显示分辨率/模型/采样这几行，删掉免得多出空行
    delete params.width;
    delete params.height;
    delete params.model;
    delete params.steps;
    delete params.cfg;
    delete params.sampler;
    delete params.scheduler;
  }
  return {
    id: "__skeleton__" + index,
    created_at: 0,
    mode: mode,
    params: params,
    status: "done",
    progress: 1,
    progress_text: "",
    outputs: [],
    batches: [],
    elapsed: 0,
  };
}

function renderGridSkeleton(grid, count) {
  const n = Math.max(1, Math.min(count || SKELETON_MAX, SKELETON_MAX));
  const frag = document.createDocumentFragment();
  for (let i = 0; i < n; i++) {
    const card = jobCard(skeletonJob(i));
    // 去掉 jid —— 骨架不是真实卡片，不能被增量更新逻辑当成已有卡片复用
    delete card.dataset.jid;
    delete card.dataset.sig;
    card.classList.add("job-skeleton");
    card.setAttribute("aria-hidden", "true");
    // 预览区换成占位块（真实卡片这里是 <img>/<video>）
    const preview = card.querySelector(".job-preview");
    if (preview) {
      preview.classList.remove("has-image");
      preview.innerHTML = '<span class="sk-spin"></span>';
    }
    frag.appendChild(card);
  }
  grid.replaceChildren(frag);
}

// 网格「筛选态」签名：任何一项变了都要重新铺骨架。
function jobsFilterKey(tab) {
  return [tab, state.genType, state.donePage, state.pageSize,
          JSON.stringify(state.doneFilters)].join("|");
}

// 每页作品数选择器：选项来自 PAGE_SIZE_OPTIONS，值从 localStorage 恢复。
function initPageSizeSelect() {
  const select = $("#pageSizeSelect");
  if (!select) return;
  select.innerHTML = PAGE_SIZE_OPTIONS
    .map(size => `<option value="${size}">${size} 个</option>`)
    .join("");
  select.value = String(state.pageSize);
  select.addEventListener("change", () => {
    const size = Number(select.value);
    if (!Number.isInteger(size) || !PAGE_SIZE_OPTIONS.includes(size)) return;
    state.pageSize = size;
    savePageSize(size);
    state.donePage = 1;          // 改每页数量后回到第 1 页
    applyGridColumns(size);
    refreshJobs();
  });
}

function applyDoneFilters() {
  state.doneFilters = {
    keyword: $("#doneSearch").value.trim(),
    media: $("#doneMedia").value,
    aspect: $("#doneAspect").value,
    duration: $("#doneDuration").value,
  };
  state.donePage = 1;
  refreshJobs();
}

$("#doneSearch").addEventListener("input", () => {
  clearTimeout(filterDebounce);
  filterDebounce = setTimeout(applyDoneFilters, 280);
});
["#doneMedia", "#doneAspect", "#doneDuration"].forEach(selector => {
  $(selector).addEventListener("change", applyDoneFilters);
});
$("#doneFilterReset").addEventListener("click", () => {
  $("#doneSearch").value = "";
  $("#doneMedia").value = "";
  $("#doneAspect").value = "";
  $("#doneDuration").value = "";
  applyDoneFilters();
});

function renderDonePagination(total, currentPage) {
  const pager = $("#jobsPagination");
  // 「全部」也要分页：否则「全部类型」名义下只显示最新 8 条（可能全是同一类），
  // 其余分类翻不到，与「全部类型」的语义不符。
  const pageSize = state.pageSize;
  if (!["done", "longfilm", "all"].includes(state.tab) || total <= pageSize) {
    pager.hidden = true;
    pager.innerHTML = "";
    return;
  }
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  const pages = [];
  for (let page = 1; page <= totalPages; page++) {
    if (page === 1 || page === totalPages || Math.abs(page - currentPage) <= 1) pages.push(page);
  }
  const controls = [`<button data-page="${currentPage - 1}" ${currentPage === 1 ? "disabled" : ""}>上一页</button>`];
  let previous = 0;
  pages.forEach(page => {
    if (previous && page - previous > 1) controls.push('<span class="page-summary">…</span>');
    controls.push(`<button data-page="${page}" class="${page === currentPage ? "active" : ""}">${page}</button>`);
    previous = page;
  });
  controls.push(`<button data-page="${currentPage + 1}" ${currentPage === totalPages ? "disabled" : ""}>下一页</button>`);
  controls.push(`<span class="page-summary">共 ${total} 个作品</span>`);
  // 分页条每 2 秒被刷新重建一次；内容没变就不动 DOM ——
  // 换了节点等于把「上一页/下一页」按钮换掉，用户点了没反应、焦点也丢了。
  const changed = window.H3UI
    ? window.H3UI.setHTMLIfChanged(pager, controls.join(""))
    : (pager.innerHTML = controls.join(""), true);
  pager.hidden = false;
  if (changed) {
    pager.querySelectorAll("button[data-page]").forEach(button => button.addEventListener("click", () => {
      if (button.disabled) return;
      state.donePage = Number(button.dataset.page);
      refreshJobs();
      $("#jobsArea").scrollTo({top: 0, behavior: "smooth"});
    }));
  }
}

async function refreshJobs() {
  if (state.page === "system") return;
  const tab = state.tab;
  const ticket = ++jobsRequest;
  const grid = $("#jobsGrid");
  const filterKey = jobsFilterKey(tab);
  // 筛选态变了（切状态标签 / 生成类型 / 翻页 / 改筛选条件 / 改每页数量）
  // —— 先铺骨架占位，再去取数据。
  //
  // 为什么必须先铺骨架：数据到达前后网格的列宽与行高必须一致。
  // 旧实现是「清空 -> 插入真实卡片」，插入瞬间图片还没解码，
  // 卡片高度按图片自然尺寸撑开 —— 于是首帧出现一张原尺寸巨图，
  // 随后图片解码完成才跳回网格（就是用户看到的「首图巨大闪现」）。
  // 骨架卡有固定的 aspect-ratio 与高度，数据到达时网格尺寸不变，无抖动。
  if (displayedFilterKey !== filterKey) {
    displayedFilterKey = filterKey;
    // 骨架数量按当前每页数量铺（但不超过 SKELETON_MAX），
    // 这样骨架行数与最终行数尽量一致，切换时不会「先 3 行后 4 行」。
    renderGridSkeleton(grid, state.pageSize);
    // 容器此刻可能不可见（素材库页把 #jobsArea 隐藏了）——
    // 此时宽度是 0，不能据此算列数，否则会写进一个错误的「1 列」。
    applyGridColumns(state.pageSize);
  }
  try {
    // 「全部」= 不按状态过滤；其余标签各自过滤
    const statuses = tab === "all" ? ["all"]
      : tab === "active" ? ["queued", "running"]
      : [tab === "longfilm" ? "done" : tab];
    const groups = await Promise.all(statuses.map(async status => {
      const query = new URLSearchParams({status});
      // 生成类型筛选（与状态叠加，服务端按 mode 过滤）
      if (state.genType) query.set("gen_type", state.genType);
      // 「已完成」「全部」都走服务端筛选 + 分页；「全部」不按状态过滤但保留筛选条件
      if (["done", "all"].includes(status)) {
        query.set("page", state.donePage);
        query.set("page_size", state.pageSize);
        if (tab === "longfilm") query.set("media", "longfilm");
        else Object.entries(state.doneFilters).forEach(([key, value]) => { if (value) query.set(key, value); });
      }
      const response = await fetch("/api/jobs?" + query.toString(), {cache: "no-store"});
      if (!response.ok) throw new Error("HTTP " + response.status);
      const data = await response.json();
      // status=all 时服务端不按状态过滤，前端也不做二次过滤
      const jobs = status === "all" ? data.jobs : data.jobs.filter(job => job.status === status);
      return {jobs, total: data.total ?? jobs.length, page: data.page ?? 1};
    }));
    // 【竞态防护】只有「筛选态」变了才丢弃本次请求的数据。
    //
    // 为什么不能只看 ticket !== jobsRequest：
    //   jobsRequest 是「请求发起次数」的计数器。轮询间隔 2 秒，但一次
    //   /api/jobs 在服务端被阻塞时可能要走好几秒（实测并发任务的
    //   music_style 会在 aiohttp 事件循环里同步拉 ComfyUI /object_info，
    //   把整个服务堵住约 6 秒）。这时下一次轮询先把 jobsRequest 递增了，
    //   旧请求回来时 ticket 必然对不上 —— 于是**每一次**都提前 return，
    //   骨架占位永远替换不掉（表现为「任务列表一直是骨架」）。
    //
    // 正确的判据是「**筛选态**有没有变」：
    //   - 筛选态没变（只是又轮询了一轮）-> 旧数据依然有效，照样渲染；
    //   - 筛选态变了（切标签/翻页/改筛选条件）-> 旧数据对应的是旧筛选态，
    //     必须丢弃，否则会把上一个筛选态的卡片画到新筛选态上。
    if (tab !== state.tab || filterKey !== jobsFilterKey(state.tab)) return;
    // 最后一道闸：**此刻**如果有原生下拉正开着，就不动 DOM。
    // 上面 fetch 是异步的 —— 判断通过之后、数据返回之前，
    // 用户可能刚好展开了下拉；这时写 DOM 会把下拉当场关闭。
    const writeAll = () => {
      if (["done", "longfilm", "all"].includes(tab)) {
        state.donePage = groups[0].page;
        renderDonePagination(groups[0].total, state.donePage);
      } else {
        renderDonePagination(0, 1);
      }
      const jobs = [...new Map(groups.flatMap(group => group.jobs).map(job => [job.id, job])).values()]
        .sort((a,b) => (b.status === "running") - (a.status === "running") || b.created_at - a.created_at);
      // 空态与卡片更新都在「保住焦点 / 滚动」的包裹里跑：
      // 刷新周期里用户可能正按着某张卡片上的按钮，或正滚到列表中部。
      const area = $("#jobsArea");
      const preserve = fn => {
        window.H3UI ? window.H3UI.withPreserved(area, fn) : fn();
      };
      if (!jobs.length) {
        preserve(() => {
          grid.innerHTML = '<div class="empty-hint">' + ({active:"当前没有排队或生成中的任务", done:"暂无已完成的任务", longfilm:"暂无已合成的长片", failed:"暂无失败的任务", canceled:"暂无已取消的任务"}[tab]) + '</div>';
        });
        return;
      }
      preserve(() => syncJobCards(grid, jobs));
    };
    if (window.H3UI && !window.H3UI.canWriteDOM("jobs", () => refreshJobs())) return;
    writeAll();
  } catch (e) { if (ticket === jobsRequest && tab === state.tab) grid.textContent = "任务列表读取失败，正在重试…"; }
}

// ---------- 「在生成库中定位」通道（2026-11-12） ----------
//
// 【为什么需要这一层 —— 2026-11-12 实测发现】
// Agent 的产物面板上有个「在生成库中定位」按钮，它原来只做两件事：
//   1. 切到生成页；
//   2. 把任务 id 写进 location.hash。
// 但**全仓没有任何地方读这个 hash**（grep 命中 0 处消费者）——
// 于是这个按钮的实际效果只是「切换了一下页面」，用户到了生成库还要
// 自己翻找那条任务。按钮写着「定位」却**根本没有定位**，这是假功能。
//
// 【现在的做法】
// 走生成库**既有的**关键词筛选通道（#doneSearch -> state.doneFilters.keyword
// -> /api/jobs?keyword=...，服务端按 id LIKE 匹配），
// 再等列表渲染完成后滚动到那张卡片并高亮。
//
// 【为什么不新造一套查询接口】
// 多一份查询 = 多一份状态 = 多一处会漂移的地方。
// 复用既有的 keyword 筛选，用户也能在搜索框里看到并修改这个条件 ——
// 「定位」不是黑魔法，它就是把筛选条件填好了。
//
// 【为什么要有 pendingLocate 而不是直接滚动】
// refreshJobs 是**异步**的（要等网络）。点完按钮那一刻卡片还没渲染出来，
// 直接 scrollIntoView 会滚到一个不存在的元素上（静默失败）。
// 所以先把目标 id 存起来，等 syncJobCards 跑完再消费它。
let pendingLocateJobId = "";
let pendingLocateTimer = null;

// 定位任务的等待上限（毫秒）。超过就不再等 ——
// 免得「定位」这个一次性动作留下一个永久挂着的定时器。
const LOCATE_TIMEOUT_MS = 8000;

// 高亮样式挂到 .job-card 上（见 style.css 的 .job-card.is-located）。
function flashLocatedCard(card) {
  if (!card) return;
  card.classList.add("is-located");
  // 2.5 秒后撤掉高亮 —— 高亮是「刚才定位到这」的提示，不该一直挂着。
  setTimeout(() => card.classList.remove("is-located"), 2500);
  try {
    card.scrollIntoView({block: "center", behavior: "smooth"});
  } catch (e) {
    // 老浏览器不支持 scrollIntoView 选项 -> 退化成默认行为
    try { card.scrollIntoView(); } catch (e2) { /* 忽略 */ }
  }
}

/**
 * 把生成库定位到指定任务。
 *
 * @param {string} jobId 任务 id
 * @returns {boolean} 是否成功发起定位
 */
function locateJobInLibrary(jobId) {
  const id = String(jobId || "").trim();
  if (!id) return false;
  pendingLocateJobId = id;
  if (pendingLocateTimer) clearTimeout(pendingLocateTimer);
  pendingLocateTimer = setTimeout(() => {
    // 超时未命中 -> 放弃（不报错，用户自己看列表就行）
    pendingLocateJobId = "";
    pendingLocateTimer = null;
  }, LOCATE_TIMEOUT_MS);

  // 「全部」标签 = 不按状态过滤，能看到任何状态的任务。
  state.tab = "all";
  state.genType = "";
  state.donePage = 1;
  // 清掉其它筛选条件，否则命中不了目标任务。
  ["#doneMedia", "#doneAspect", "#doneDuration"].forEach(sel => {
    const el = $(sel);
    if (el) el.value = "";
  });
  // 关键词筛选：服务端 keyword 会匹配 params / id / outputs。
  //
  // 【为什么用 applyDoneFilters() 而不是自己写 state.doneFilters —— 2026-11-12】
  // applyDoneFilters() 是**既有的**筛选真源（它同时读 DOM 控件 + 写 state + 触发刷新）。
  // 自己再写一遍 state.doneFilters = {...} 就是第二份状态 —— 将来改了筛选逻辑
  // （比如加一个筛选项）这里就会漏掉。两份状态必然漂移。
  const search = $("#doneSearch");
  if (search) search.value = id;

  // 切到生成页 + 同步标签高亮。
  showCreate();
  renderTabs();
  applyDoneFilters();
  return true;
}

// syncJobCards 渲染完卡片后消费「待定位」的目标。
function consumePendingLocate() {
  if (!pendingLocateJobId) return;
  const grid = $("#jobsGrid");
  if (!grid) return;
  const target = pendingLocateJobId;
  // 用 dataset 遍历而不是 CSS 选择器 —— 任务 id 里的字符不一定是
  // 「选择器安全」的（CSS.escape 在极老浏览器上不存在），
  // 而 dataset 比较没有转义问题。
  let card = null;
  for (const node of grid.children) {
    if (node.dataset && node.dataset.jid === target) { card = node; break; }
  }
  if (!card) return;   // 还没渲染出来 -> 下一轮再试
  pendingLocateJobId = "";
  if (pendingLocateTimer) { clearTimeout(pendingLocateTimer); pendingLocateTimer = null; }
  flashLocatedCard(card);
}

// 把任务列表按「稳定 key + 最小改动」同步到网格里。
//
// 关键约束：**顺序没变的节点一律不碰 DOM**。
// 旧实现无条件 grid.appendChild(el) —— 对已在容器里的节点，appendChild
// 是「移动」：节点被摘下来再挂回去，焦点随之丢失（实测：焦点在卡片
// 「提示词」按钮上，2 秒后焦点掉到 body），视频也会从头重播。
function syncJobCards(grid, jobs) {
  // 骨架不是卡片节点（没有 data-jid），直接清掉；
  // 真正的卡片会被下面的增量逻辑复用，不会被重建。
  // 非卡片节点（空态提示）也一并清掉 —— 否则下标会算错。
  for (const node of [...grid.childNodes]) {
    if (node.nodeType !== 1) { node.remove(); continue; }
    if (!node.dataset || !node.dataset.jid) node.remove();
  }

  // 增量更新：数据未变化的卡片不重建，避免打断视频播放
  const existing = new Map();
  [...grid.children].forEach(el => { if (el.dataset?.jid) existing.set(el.dataset.jid, el); });
  const seen = new Set();
  jobs.forEach(j => {
    seen.add(j.id);
    const sig = jobSig(j);
    let el = existing.get(j.id);
    if (el && el.dataset.sig !== sig) {
      const fresh = jobCard(j);
      el.replaceWith(fresh);
      el = fresh;
    } else if (!el) {
      el = jobCard(j);
    }
    el.dataset.sig = sig;
    // 只有「位置真的错了」才搬动节点：位置正确 = 原样不动 = 焦点/播放不中断。
    const atIndex = Array.prototype.indexOf.call(grid.children, el);
    if (atIndex !== seen.size - 1) {
      grid.insertBefore(el, grid.children[seen.size - 1] || null);
    }
  });
  existing.forEach((el, id) => { if (!seen.has(id)) el.remove(); });
  // 列数跟着「每页作品数」走；实时刮新也走这里，所以每页数量不会被刷掉
  applyGridColumns(state.pageSize);
  // 「在生成库中定位」：卡片渲染完成后消费待定位目标（滚动 + 高亮）。
  consumePendingLocate();
}

// ---------- 长片拼接 ----------
function renderStitchClips() {
  const list = $("#stitchList");
  list.innerHTML = "";
  state.stitchClips.forEach((clip, index) => {
    const item = document.createElement("div");
    item.className = "stitch-clip";
    item.draggable = true;
    item.dataset.index = index;
    item.innerHTML = `<span class="stitch-index">${index + 1}</span>
      <video class="stitch-thumb" src="${clip.url}" muted preload="metadata"></video>
      <span class="stitch-clip-info"><b>${escapeHtml(clip.title || clip.name)}</b><span>${clip.duration ? clip.duration + " 秒" : "视频片段"}</span></span>
      <button class="stitch-remove" title="移除">×</button>`;
    item.querySelector("video").draggable = false;
    item.querySelector(".stitch-remove").addEventListener("click", () => {
      state.stitchClips.splice(index, 1);
      renderStitchClips();
    });
    item.addEventListener("dragstart", event => {
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("application/x-h3-clip-index", String(index));
      item.classList.add("dragging");
    });
    item.addEventListener("dragend", () => item.classList.remove("dragging"));
    item.addEventListener("dragover", event => {
      if (!event.dataTransfer.types.includes("application/x-h3-clip-index")) return;
      event.preventDefault();
      item.classList.add("drag-target");
    });
    item.addEventListener("dragleave", () => item.classList.remove("drag-target"));
    item.addEventListener("drop", event => {
      const from = Number(event.dataTransfer.getData("application/x-h3-clip-index"));
      if (!Number.isInteger(from)) return;
      event.preventDefault();
      item.classList.remove("drag-target");
      const [moved] = state.stitchClips.splice(from, 1);
      state.stitchClips.splice(index, 0, moved);
      renderStitchClips();
    });
    list.appendChild(item);
  });
  const total = state.stitchClips.reduce((sum, clip) => sum + Number(clip.duration || 0), 0);
  $("#stitchSummary").textContent = state.stitchClips.length
    ? `已选择 ${state.stitchClips.length} 个片段${total ? ` · 预计 ${total} 秒` : ""}`
    : "尚未选择片段";
  $("#stitchBtn").disabled = state.stitchClips.length < 2;
}

const stitchDrop = $("#stitchDrop");
stitchDrop.addEventListener("dragover", event => {
  if (!event.dataTransfer.types.includes("application/x-h3-video")) return;
  event.preventDefault();
  event.dataTransfer.dropEffect = "copy";
  stitchDrop.classList.add("drag-over");
});
stitchDrop.addEventListener("dragleave", () => stitchDrop.classList.remove("drag-over"));
stitchDrop.addEventListener("drop", event => {
  const raw = event.dataTransfer.getData("application/x-h3-video");
  if (!raw) return;
  event.preventDefault();
  stitchDrop.classList.remove("drag-over");
  try {
    state.stitchClips.push(JSON.parse(raw));
    renderStitchClips();
  } catch (_) {
    alert("无法读取这个视频片段");
  }
});

$("#stitchBtn").addEventListener("click", async () => {
  const title = $("#longfilmTitle").value.trim();
  const intro = $("#longfilmIntro").value.trim();
  if (!title) { alert("请填写长片标题"); return; }
  if (state.stitchClips.length < 2) { alert("至少拖入 2 个视频片段"); return; }
  const button = $("#stitchBtn");
  button.disabled = true;
  button.textContent = "正在提交…";
  try {
    const response = await fetch("/api/longfilms", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({title, intro,
        duration: state.stitchClips.reduce((sum, clip) => sum + Number(clip.duration || 0), 0),
        clips: state.stitchClips.map(clip => ({job_id: clip.jobId, file: clip.file, name: clip.name}))})
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "提交失败");
    state.stitchClips = [];
    $("#longfilmTitle").value = "";
    $("#longfilmIntro").value = "";
    renderStitchClips();
    state.tab = "longfilm";
    renderTabs();
    refreshJobs();
  } catch (error) {
    alert("长片合成提交失败：" + error.message);
  } finally {
    button.textContent = "合成长片";
    button.disabled = state.stitchClips.length < 2;
  }
});

// ---------- 状态栏 ----------
// 状态文案全部中文，并与 ComfyUI 真实队列一致：
//   ComfyUI 在跑（不管是 Studio 提交的还是用户在 ComfyUI 里直接提交的）
//   -> 「生成中」；队列里还有待执行的 -> 「排队中」；
//   全空 -> 「空闲」。ComfyUI 连不上 -> 「ComfyUI 未连接」。
const STATUS_TEXT = {
  offline: "ComfyUI 未连接",
  busy: "生成中",
  queued: "排队中",
  idle: "空闲",
};

// 把状态数据映射成 {text, dot, title}（纯函数，便于单独断言）
function statusLabel(data) {
  const comfyRunning = data.comfy_running;
  const comfyPending = data.comfy_pending;
  const studioRunning = !!data.running;
  const studioQueued = Number(data.queued || 0) > 0;

  // comfy_running 为 null = 读不到 ComfyUI 队列（连接异常）
  const comfyUnknown = comfyRunning === null || comfyRunning === undefined;
  const busy = comfyUnknown ? studioRunning : (comfyRunning > 0 || studioRunning);
  const pending = comfyUnknown ? studioQueued
    : (comfyPending > 0 || studioQueued);

  if (data.comfyui_ok === false) {
    return {text: STATUS_TEXT.offline, dot: "dot dot-off", title: "连不上 ComfyUI"};
  }
  if (busy) {
    const n = Math.max(Number(comfyRunning || 0), 1);
    return {text: STATUS_TEXT.busy, dot: "dot dot-busy",
            title: `ComfyUI 正在生成（${n} 个在执行）`};
  }
  if (pending) {
    const n = Number(data.queued || 0) || Number(comfyPending || 0);
    return {text: n > 0 ? `${STATUS_TEXT.queued}（${n}）` : STATUS_TEXT.queued,
            dot: "dot dot-busy", title: "ComfyUI 队列中有待执行的任务"};
  }
  return {text: STATUS_TEXT.idle, dot: "dot dot-ok", title: "ComfyUI 空闲"};
}

function renderStatus(data) {
  const el = $("#statusText");
  const dot = $("#topStatus .dot");
  if (!el || !dot) return;
  const label = statusLabel(data);
  el.textContent = label.text;
  dot.className = label.dot;
  el.title = label.title;
}

async function refreshSystem() {
  try {
    const r = await (await fetch("/api/system", {cache: "no-store"})).json();
    renderStatus(r);
  } catch (e) { /* 忽略：保留上一次状态 */ }
}

// SSE：状态一变就推过来，不必等轮询
function startStatusStream() {
  if (typeof EventSource === "undefined") return;
  let source;
  try {
    source = new EventSource("/api/status/stream");
  } catch (e) {
    return;
  }
  source.onmessage = event => {
    try {
      const data = JSON.parse(event.data);
      // SSE 帧里没有 comfyui_ok：能收到帧就说明服务端连得上 ComfyUI
      renderStatus(Object.assign({comfyui_ok: true}, data));
    } catch (e) { /* 忽略坏帧 */ }
  };
  source.onerror = () => {
    // 断线由浏览器自动重连；这里不主动 close，避免连不上时刷日志
  };
}

$("#unloadBtn").addEventListener("click", async () => {
  if (!confirm("让 ComfyUI 释放模型显存？")) return;
  await fetch("/api/unload", { method: "POST" });
});

// ---------- 硬件参数页 ----------
function showCreate() {
  state.page = "create";
  showPage($("#jobsArea"), true);
  showPage($("#panel"), true);
  showPage($("#stitchPanel"), false);
  showPage($("#systemPage"), false);
  showPage($("#providersPage"), false);
  showPage($("#assetsPage"), false);
  showPage($("#agentPage"), false);
  closePanelDrawerOnPageChange();
  updateResponsiveChrome();
}

function showStitch() {
  state.page = "stitch";
  $$(".nav-item").forEach(x => x.classList.remove("active"));
  $("#navStitch").classList.add("active");
  showPage($("#jobsArea"), true);
  showPage($("#panel"), false);
  showPage($("#stitchPanel"), true);
  showPage($("#systemPage"), false);
  showPage($("#providersPage"), false);
  showPage($("#assetsPage"), false);
  showPage($("#agentPage"), false);
  state.tab = "done";
  state.doneFilters.media = "video";
  $("#doneMedia").value = "video";
  renderTabs();
  renderStitchClips();
  refreshJobs();
  closePanelDrawerOnPageChange();
  updateResponsiveChrome();
}

function showSystem() {
  state.page = "system";
  $$(".nav-item").forEach(x => x.classList.remove("active"));
  $("#navSystem").classList.add("active");
  showPage($("#jobsArea"), false);
  showPage($("#panel"), false);
  showPage($("#stitchPanel"), false);
  showPage($("#systemPage"), true);
  showPage($("#providersPage"), false);
  showPage($("#assetsPage"), false);
  showPage($("#agentPage"), false);
  refreshHardware();
  closePanelDrawerOnPageChange();
  updateResponsiveChrome();
}

// 本地模型配置页（实现见 providers.js）
function showProviders() {
  state.page = "providers";
  $$(".nav-item").forEach(x => x.classList.remove("active"));
  $("#navProviders").classList.add("active");
  showPage($("#jobsArea"), false);
  showPage($("#panel"), false);
  showPage($("#stitchPanel"), false);
  showPage($("#systemPage"), false);
  showPage($("#providersPage"), true);
  showPage($("#assetsPage"), false);
  showPage($("#agentPage"), false);
  loadProviders();
  loadAgentConfig();
  closePanelDrawerOnPageChange();
  updateResponsiveChrome();
}

// 内置多模态 Agent 页（实现见 agent.js）
function showAgent() {
  state.page = "agent";
  $$(".nav-item").forEach(x => x.classList.remove("active"));
  const nav = $("#navAgent");
  if (nav) nav.classList.add("active");
  showPage($("#jobsArea"), false);
  showPage($("#panel"), false);
  showPage($("#stitchPanel"), false);
  showPage($("#systemPage"), false);
  showPage($("#providersPage"), false);
  showPage($("#assetsPage"), false);
  showPage($("#agentPage"), true);
  closePanelDrawerOnPageChange();
  updateResponsiveChrome();
  if (window.AgentUI && typeof window.AgentUI.renderMessages === "function") {
    window.AgentUI.renderMessages();
  }
}

$("#navSystem").addEventListener("click", showSystem);
$("#navStitch").addEventListener("click", showStitch);
$("#sysRefresh").addEventListener("click", () => { refreshHardware(true); });

function fmtBytes(b) {
  if (b == null) return "—";
  const gb = b / 1073741824;
  if (gb >= 1) return gb.toFixed(1) + " GB";
  return (b / 1048576).toFixed(0) + " MB";
}

function fmtUptime(sec) {
  if (!sec) return "—";
  const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60);
  return (d ? d + " 天 " : "") + (h ? h + " 小时 " : "") + m + " 分";
}

function barClass(pct) {
  return pct >= 90 ? "bar danger" : pct >= 70 ? "bar warn" : "bar";
}

function card(title, badge, body) {
  return `<div class="sys-card">
    <div class="sys-card-title"><span>${title}</span>${badge ? `<span class="badge">${badge}</span>` : ""}</div>
    ${body}
  </div>`;
}

// ---------- 显卡功耗上限（Power Limit）----------
// 硬件面板每 3 秒自动刷新会整块重绘，用户在下拉框里选好的目标值必须保留，
// 否则永远点不到「应用」。
const PL_PENDING = {};
// 结果提示同样要跨重绘存活：硬件面板每 3 秒重绘一次，提示写在 DOM 上会被抹掉。
const PL_MSG = {};
// 每张卡一个控件：读出当前值、显示可调范围、下拉选择目标值 + 应用。
// 安全约束：默认只允许下调；提高功耗需要二次确认。
function powerLimitOptions(g) {
  const info = g.power_limit_info || {};
  if (!info.supported) return [];
  const lo = Number(info.min), hi = Number(info.max);
  if (!isFinite(lo) || !isFinite(hi) || hi <= 0) return [];
  const cur = Number(info.current);
  // 步进 5W；小于 100W 的卡用 10W 步进，避免下拉项过长
  const step = hi - lo > 200 ? 5 : 10;
  const opts = [];
  for (let v = hi; v >= lo - 0.001; v -= step) {
    const val = Math.round(v);
    if (opts.length && Math.abs(opts[opts.length - 1].val - val) < 0.001) continue;
    opts.push({ val: val, cur: Math.abs(val - cur) < 0.001 });
  }
  if (!opts.length) opts.push({ val: Math.round(cur), cur: true });
  return opts;
}

function renderPowerLimitControl(g, cap) {
  const info = g.power_limit_info || {};
  const idx = g.index;
  if (!cap || !cap.available) {
    const reason = (cap && cap.reason) || "未检测到可调功耗上限能力";
    return `<div class="sys-pl">
      <div class="sys-pl-head"><span>功耗上限</span><b class="sys-pl-na">不可用</b></div>
      <div class="sys-pl-note">${escapeHtml(reason)}</div>
    </div>`;
  }
  if (!info.supported) {
    const reason = info.reason || "该显卡或当前驱动不支持修改功耗上限";
    return `<div class="sys-pl">
      <div class="sys-pl-head"><span>功耗上限</span><b class="sys-pl-na">不可用</b></div>
      <div class="sys-pl-note">${escapeHtml(reason)}</div>
    </div>`;
  }
  const opts = powerLimitOptions(g);
  const cur = Number(info.current);
  const sel = opts.find(o => o.cur);
  const curVal = sel ? sel.val : Math.round(cur);
  const pending = PL_PENDING[idx];
  const pendingVal = (pending != null && opts.some(o => o.val === pending)) ? pending : null;
  return `<div class="sys-pl" data-gpu-index="${idx}">
    <div class="sys-pl-head"><span>功耗上限</span><b class="sys-pl-cur" data-pl-cur="${idx}">${curVal} W</b></div>
    <div class="sys-pl-range">可调范围 ${Math.round(info.min)} – ${Math.round(info.max)} W（默认 ${Math.round(info.default)} W）</div>
    <div class="sys-pl-ctl">
      <select class="sys-pl-select" data-pl-select="${idx}" aria-label="GPU ${idx} 功耗上限">
        ${opts.map(o => `<option value="${o.val}"${(pendingVal != null ? o.val === pendingVal : o.cur) ? " selected" : ""}>${o.val} W</option>`).join("")}
      </select>
      <button type="button" class="sys-pl-btn" data-pl-apply="${idx}">应用</button>
    </div>
    <div class="sys-pl-msg${PL_MSG[idx] ? " " + PL_MSG[idx].kind : ""}" data-pl-msg="${idx}">${PL_MSG[idx] ? escapeHtml(PL_MSG[idx].text) : ""}</div>
  </div>`;
}

async function applyPowerLimit(index, watts, allowRaise) {
  const curEl = document.querySelector(`[data-pl-cur="${index}"]`);
  // 提示写进状态，随后重绘时由 renderPowerLimitControl 还原
  const setMsg = (text, kind) => {
    PL_MSG[index] = { text: text, kind: kind || "" };
    const el = document.querySelector(`[data-pl-msg="${index}"]`);
    if (el) {
      el.textContent = text;
      el.className = "sys-pl-msg" + (kind ? " " + kind : "");
    }
  };
  try {
    const resp = await fetch("/api/gpu/power-limit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ index: index, watts: watts, allow_raise: !!allowRaise }),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok || !data.ok) {
      setMsg(data.message || data.error || `设置失败（HTTP ${resp.status}）`, "err");
      return;
    }
    delete PL_PENDING[index];
    if (curEl && data.current != null) curEl.textContent = `${Math.round(data.current)} W`;
    // 回读校验已由后端完成；刷新面板让范围/当前值同步。
    // 提示存在 PL_MSG 里，重绘后仍可见。
    setMsg(data.message || "已生效", "ok");
    await refreshHardware(true);
  } catch (e) {
    setMsg("请求失败：" + (e && e.message ? e.message : e), "err");
  }
}

// 事件委托：下拉框选择变化 → 记住目标值（防止自动刷新重置）
document.addEventListener("change", event => {
  const sel = event.target.closest("[data-pl-select]");
  if (!sel) return;
  const index = Number(sel.getAttribute("data-pl-select"));
  const val = Number(sel.value);
  if (isFinite(val)) {
    PL_PENDING[index] = val;
    delete PL_MSG[index];
  }
});

// 事件委托：应用按钮
document.addEventListener("click", event => {
  const btn = event.target.closest("[data-pl-apply]");
  if (!btn) return;
  const index = Number(btn.getAttribute("data-pl-apply"));
  const sel = document.querySelector(`[data-pl-select="${index}"]`);
  if (!sel) return;
  const watts = Number(sel.value);
  if (!isFinite(watts)) return;
  const curEl = document.querySelector(`[data-pl-cur="${index}"]`);
  const cur = curEl ? parseFloat(String(curEl.textContent)) : NaN;
  if (isFinite(cur) && watts > cur) {
    // 提高功耗上限：必须二次确认（安全约束）
    if (!confirm(`确认提高 GPU ${index} 的功耗上限？\n\n${cur} W → ${watts} W\n\n提高功耗会提升整机发热与供电压力，确认继续？`)) return;
    applyPowerLimit(index, watts, true);
  } else {
    applyPowerLimit(index, watts, false);
  }
});

function renderHardware(d) {
  const grid = $("#sysGrid");
  const cards = [];
  // 功耗上限整体能力（nvidia-smi 缺失 / 驱动不支持时 available=false）
  const plCap = d.power_limit || { available: false, reason: "未返回功耗上限能力信息" };

  // GPU
  (d.gpus || []).forEach(g => {
    const pct = g.vram_total ? Math.round(g.vram_used / g.vram_total * 100) : 0;
    cards.push(card("GPU 显卡", g.temp ? `${Math.round(g.temp)}°C` : "", `
      <div class="sys-main">${g.name}</div>
      <div class="bar ${pct >= 90 ? "danger" : pct >= 70 ? "warn" : ""}"><div style="width:${pct}%"></div></div>
      <div class="bar-label"><span>显存 ${fmtBytes(g.vram_used)} / ${fmtBytes(g.vram_total)}</span><span>${pct}%</span></div>
      <div class="sys-row"><span>可用显存</span><b>${fmtBytes(g.vram_free)}</b></div>
      <div class="sys-row"><span>GPU 利用率</span><b>${Math.round(g.util)}%</b></div>
      <div class="sys-row"><span>温度</span><b>${Math.round(g.temp)} °C</b></div>
      <div class="sys-row"><span>功耗</span><b>${Math.round(g.power)} W / ${Math.round(g.power_limit)} W</b></div>
      <div class="sys-row"><span>核心频率</span><b>${g.clock || "—"} MHz</b></div>
      <div class="sys-row"><span>驱动版本</span><b>${g.driver}</b></div>
      ${renderPowerLimitControl(g, plCap)}`));
  });
  if (!(d.gpus || []).length) {
    cards.push(card("GPU 显卡", "未检测", `<div class="sys-main">nvidia-smi 不可用</div>
      <div class="sys-row"><span>提示</span><b>请确认 NVIDIA 驱动已安装</b></div>`));
  }

  // CPU
  const c = d.cpu || {};
  const cpuName = c.name || c.model || "—";
  const hetero = c.heterogeneous && (c.models || []).length > 1;
  cards.push(card("CPU 处理器", c.percent != null ? Math.round(c.percent) + "%" : "", `
    <div class="sys-main">${escapeHtml(cpuName)}</div>
    <div class="${barClass(c.percent || 0)}"><div style="width:${c.percent || 0}%"></div></div>
    <div class="bar-label"><span>当前占用</span><span>${Math.round(c.percent || 0)}%</span></div>
    <div class="sys-row"><span>核心 / 线程</span><b>${c.cores} 核 / ${c.threads} 线程</b></div>
    <div class="sys-row"><span>当前频率</span><b>${c.freq ? c.freq + " MHz" : "—"}</b></div>
    ${c.architecture ? `<div class="sys-row"><span>架构</span><b>${escapeHtml(c.architecture)}</b></div>` : ""}
    ${c.vendor ? `<div class="sys-row"><span>厂商</span><b>${escapeHtml(c.vendor)}</b></div>` : ""}
    ${hetero ? (c.models || []).map(m => `<div class="sys-row"><span>${escapeHtml(m.name)}</span><b>${m.logical} 线程</b></div>`).join("") : ""}
    ${c.source ? `<div class="sys-row"><span>型号来源</span><b>${escapeHtml(c.source)}</b></div>` : ""}`));

  // 内存
  const r = d.ram || {};
  cards.push(card("内存", r.percent != null ? Math.round(r.percent) + "%" : "", `
    <div class="sys-main">${fmtBytes(r.used)} / ${fmtBytes(r.total)}</div>
    <div class="${barClass(r.percent || 0)}"><div style="width:${r.percent || 0}%"></div></div>
    <div class="bar-label"><span>已用</span><span>${Math.round(r.percent || 0)}%</span></div>
    <div class="sys-row"><span>可用</span><b>${fmtBytes(r.available)}</b></div>
    <div class="sys-row"><span>虚拟内存</span><b>${fmtBytes(r.swap_used)} / ${fmtBytes(r.swap_total)}</b></div>`));

  // 磁盘
  const disks = (d.disks || []).map(dk => `
    <div class="sys-main" style="font-size:13px;margin-bottom:6px">${dk.device} · ${fmtBytes(dk.total)}</div>
    <div class="${barClass(dk.percent)}"><div style="width:${dk.percent}%"></div></div>
    <div class="bar-label"><span>已用 ${Math.round(dk.percent)}%</span><span>剩余 ${fmtBytes(dk.free)}</span></div>`).join('<div style="height:12px"></div>');
  cards.push(card("磁盘", `${(d.disks || []).length} 个分区`, disks || "—"));

  // 软件环境
  const sf = d.software || {};
  const os = d.os || {};
  cards.push(card("软件环境", "", `
    <div class="sys-row"><span>ComfyUI</span><b>v${sf.comfyui || "—"}</b></div>
    <div class="sys-row"><span>PyTorch</span><b>${sf.pytorch || "—"}</b></div>
    <div class="sys-row"><span>ComfyUI Python</span><b>${(sf.python || "").split(" ")[0] || "—"}</b></div>
    <div class="sys-row"><span>Studio Python</span><b>${sf.studio_python}</b></div>
    <div class="sys-row"><span>操作系统</span><b>${os.platform || "—"}</b></div>
    <div class="sys-row"><span>计算机名</span><b>${os.hostname || "—"}</b></div>
    <div class="sys-row"><span>已运行</span><b>${fmtUptime(Date.now() / 1000 - (os.boot || 0))}</b></div>`));

  // 模型库
  const models = d.models || [];
  const totalSize = models.reduce((s, m) => s + m.size, 0);
  cards.push(card("模型库", fmtBytes(totalSize), models.length
    ? models.map(m => `<div class="model-line"><span>${m.name}</span><b>${m.count} 个 · ${fmtBytes(m.size)}</b></div>`).join("")
      + `<div class="sys-row" style="margin-top:8px"><span>目录</span><b style="font-size:11px">${d.models_dir || "—"}</b></div>`
    : `<div class="sys-row"><span>目录</span><b style="font-size:11px">${d.models_dir || "—"}</b></div>
       <div class="sys-row"><span>状态</span><b>未扫描到模型</b></div>`));

  // 硬件面板每 3 秒刷新一次。原来这里是整块 innerHTML 重建 —— 后果是：
  //   1. 功耗上限 <select> 被换成一个新节点 -> 原生下拉关闭、展开位置丢失；
  //   2. 用户刚选好的目标值（PL_PENDING）虽然被还原了，但**交互过程**被打断；
  //   3. 焦点掉回 body，键盘操作直接断掉。
  // 现在改成三条：
  //   a) 有原生下拉正开着 -> 本轮整个跳过（guardDOMWrite）；
  //   b) 内容没变就不动 DOM（setHTMLIfChanged）—— 绝大多数刷新属于这一类；
  //   c) 真要重建时，先记焦点快照再重建、重建后还原（withPreserved）。
  const html = cards.join("");
  const write = () => {
    if (window.H3UI) {
      window.H3UI.withPreserved($("#systemPage"), () => {
        window.H3UI.setHTMLIfChanged(grid, html);
      });
    } else {
      grid.innerHTML = html;
    }
    $("#sysSub").textContent = `刷新于 ${new Date().toLocaleTimeString("zh-CN")} · 每 3 秒自动刷新`;
  };
  if (window.H3UI && !window.H3UI.canWriteDOM("hardware", () => refreshHardware())) return;
  write();
}

async function refreshHardware(force) {
  if (state.page !== "system" && !force) return;
  try {
    const d = await (await fetch("/api/hardware")).json();
    renderHardware(d);
    state.hwTimer = Date.now();
  } catch (e) { /* ignore */ }
}
// 硬件页的 3 秒轮询注册在文件末尾（scheduleRefresh 里）——
// 交互期间会推迟刷新，所以注册点必须能拿到 scheduleRefresh。

// ---------- LoRA 面板交互 ----------
$("#imgLoraAdd")?.addEventListener("click", () => {
  const name = $("#imgLoraSelect").value;
  if (!name) return;
  const meta = imgLoraMeta(name);
  const list = imgLoraCtx().list;
  if (list.length >= 8) {
    setEnhanceHint("最多 8 个 LoRA", "error");
    return;
  }
  list.push({
    name,
    // 推荐强度自动应用（用户之后可手动改，上限由服务端按权重封顶）
    strength: meta ? Number(meta.recommended_strength) : 1.0,
    strength_cap: meta ? Number(meta.strength_cap !== undefined ? meta.strength_cap : 1.5) : 1.5,
    ctx_damping: meta ? (meta.ctx_damping !== undefined ? meta.ctx_damping : null) : null,
    ctx_perturbation: meta ? (meta.ctx_perturbation !== undefined ? meta.ctx_perturbation : null) : null,
    enabled: true,
    // 触发词自动带上上游标注的那个（用户可编辑 / 删除）
    trigger: meta && meta.trigger ? meta.trigger : "",
  });
  saveLoraConfig();
});

$("#loraInfoClose")?.addEventListener("click", closeLoraInfo);
$("#loraInfoDone")?.addEventListener("click", closeLoraInfo);
$("#loraInfoModal")?.addEventListener("click", event => {
  if (event.target === $("#loraInfoModal")) closeLoraInfo();
});
document.addEventListener("keydown", event => {
  if (event.key === "Escape" && !$("#loraInfoModal").hidden) closeLoraInfo();
});

// ---------- 窄屏适配：抽屉 / 底部面板 ----------
//
// 断点真源在 web/responsive.css（--bp-phone / --bp-tablet），
// 这里只读不写 —— 改断点只改 CSS 一处。
const BREAKPOINTS = {phone: 700, tablet: 1100};
// 挂到 window —— 响应式回归测试要读它，和 CSS 的 --bp-phone 做一致性比对
window.BREAKPOINTS = BREAKPOINTS;

// ---------- 跨模块导出（2026-11-12） ----------
//
// Agent 的产物面板要「在生成库中定位」某条任务，
// 但生成库是 app.js 管的（agent_sessions.js 碰不到它的 DOM 与 state）。
// 通过 window 暴露一个**窄接口**，而不是让 agent_sessions.js 直接
// 读 app.js 的内部 state —— 后者会把两个模块焊死。
//
// 同时导出 lightboxShow：产物缩略图点「查看大图」要复用生成库
// 已经调好的大图查看器（缩放/拖拽/下载/新窗口），
// 不再新造一个功能更少的模态框。
window.H3Studio = Object.assign(window.H3Studio || {}, {
  locateJobInLibrary,
  lightboxShow,
});

function isPhoneViewport() {
  return window.innerWidth <= BREAKPOINTS.phone;
}

// 侧栏抽屉（窄屏）
function setSidebarOpen(open) {
  const sidebar = $("#sidebar");
  const mask = $("#sidebarMask");
  const toggle = $("#navToggle");
  if (!sidebar) return;
  sidebar.classList.toggle("open", open);
  if (mask) mask.classList.toggle("open", open);
  if (toggle) toggle.setAttribute("aria-expanded", open ? "true" : "false");
}

// 当前「可折叠参数面板」—— 创作页是 #panel，长片拼接页是 #stitchPanel。
// 两者在 HTML 里都带 .panel 类（窄屏下都是底部抽屉）。
function activePanel() {
  const create = $("#panel");
  const stitch = $("#stitchPanel");
  if (pageIsVisible(stitch)) return stitch;
  if (pageIsVisible(create)) return create;
  return null;
}

// 参数面板底部抽屉（窄屏）
function setPanelOpen(open) {
  const panel = activePanel();
  const mask = $("#panelMask");
  if (!panel) return;
  panel.classList.toggle("open", open);
  if (mask) mask.classList.toggle("open", open);
}

// 浮动「参数」按钮：只在**参数面板处于抽屉模式**时出现。
// 抽屉模式的断点是 tablet（≤1100px）—— 平板上参数区也收进了底部抽屉，
// 所以按钮在平板同样要显示；桌面端参数区常驻，不需要按钮。
function updateResponsiveChrome() {
  const fab = $("#panelFab");
  if (!fab) return;
  const panel = activePanel();
  const drawerMode = window.innerWidth <= BREAKPOINTS.tablet;
  if (!drawerMode || !panel) {
    fab.hidden = true;
    return;
  }
  fab.hidden = false;
  const stitch = panel.id === "stitchPanel";
  fab.querySelector("span").textContent = stitch ? "片段" : "参数";
}

function closeDrawers() {
  setSidebarOpen(false);
  setPanelOpen(false);
}

// 切到别的页面时收掉抽屉 —— 参数抽屉的内容属于上一页，
// 留着会造成「点开参数看到的是上一个模式的参数」。
function closePanelDrawerOnPageChange() {
  if (window.innerWidth > BREAKPOINTS.tablet) return;
  const panel = activePanel();
  if (panel) panel.classList.remove("open");
  const mask = $("#panelMask");
  if (mask) mask.classList.remove("open");
}

function initResponsive() {
  const toggle = $("#navToggle");
  if (toggle) {
    toggle.addEventListener("click", () => {
      const open = !$("#sidebar").classList.contains("open");
      setSidebarOpen(open);
      // 打开侧栏时关掉参数抽屉：两个抽屉不叠在一起
      if (open) setPanelOpen(false);
    });
  }
  const sidebarMask = $("#sidebarMask");
  if (sidebarMask) sidebarMask.addEventListener("click", () => setSidebarOpen(false));

  const panelMask = $("#panelMask");
  if (panelMask) panelMask.addEventListener("click", () => setPanelOpen(false));

  const fab = $("#panelFab");
  if (fab) {
    fab.addEventListener("click", () => {
      const panel = activePanel();
      if (!panel) return;
      const open = !panel.classList.contains("open");
      panel.classList.toggle("open", open);
      const mask = $("#panelMask");
      if (mask) mask.classList.toggle("open", open);
      if (open) setSidebarOpen(false);
    });
  }

  // 点导航项后自动收起侧栏（窄屏）：选完模式就该看到内容，而不是被抽屉挡住
  $$(".nav-item").forEach(item => item.addEventListener("click", () => {
    if (isPhoneViewport()) setSidebarOpen(false);
  }));

  // Esc 关闭抽屉
  document.addEventListener("keydown", event => {
    if (event.key !== "Escape") return;
    closeDrawers();
  });

  // 视口跨过断点时清掉抽屉状态：
  // 桌面端 .sidebar / .panel 是常驻的，残留的 .open 类会让
  // 缩小窗口时侧栏被 translateX(-100%) 藏起来。
  window.addEventListener("resize", () => {
    // 视口跨过断点时清掉抽屉状态：
    // 桌面端 .sidebar / .panel 是常驻的，残留的 .open 类会让
    // 缩小窗口时侧栏被 translateX(-100%) 藏起来。
    if (window.innerWidth > BREAKPOINTS.tablet) {
      setSidebarOpen(false);
      setPanelOpen(false);
    }
  });
}

// ---------- 启动 ----------
// 启动脚本必须「逐项隔离」：任何一项抛异常都不能中断后面的项。
// 之前 esc() 未定义抛 ReferenceError 会中断整个顶层脚本，连带
// refreshJobs() / setInterval(refreshJobs, 2000) 都没注册 —— 表现是
// 标签显示「全部 / 全部类型」但任务列表空白，点一次才恢复。
// 这里每个步骤单独 try/catch，失败只记日志不阻断其余步骤。
function boot(step, fn) {
  try {
    fn();
  } catch (e) {
    console.error(`[启动] ${step} 失败:`, e);
  }
}

// 默认进入「Z-IMAGE 文生图」：导航高亮 + 参数面板 + 首屏任务列表
boot("默认模式", () => {
  const nav = $(`.nav-item[data-mode="${DEFAULT_MODE}"]`);
  if (nav) activateMode(DEFAULT_MODE, nav);
  else { state.mode = DEFAULT_MODE; showCreate(); renderMode(); }
});
boot("图像参数清单", loadImageOptions);
boot("视频参数清单", async () => {
  if (!window.VIDEO_PANEL) return;
  await window.VIDEO_PANEL.load();
  window.VIDEO_PANEL.bind();
  window.VIDEO_PANEL.render();
});
boot("音乐参数清单", async () => {
  if (!window.MusicUI) return;
  await window.MusicUI.loadOptions();
  window.MusicUI.bind();
  window.MusicUI.render();
});
boot("图像 LoRA 清单", loadImageLoras);
boot("生成类型清单", loadGenerationTypes);
boot("模式面板", renderMode);
boot("状态标签", renderTabs);
boot("每页数量", initPageSizeSelect);
boot("网格尺寸监听", initGridResizeObserver);
boot("窄屏抽屉", initResponsive);
// assets.js 是独立作用域（IIFE），要通过 window 才能调到这里的函数
window.updateResponsiveChrome = updateResponsiveChrome;
boot("提示词版本", renderPromptVersions);   // 初始化二次增强「作用对象」切换（原始 / 一次增强后）
boot("长片片段", renderStitchClips);
boot("任务列表", refreshJobs);
boot("系统状态", refreshSystem);
boot("状态实时推送", startStatusStream);

// ---------- 定时刷新调度 ----------
//
// 原来的写法是 setInterval(refreshJobs, 2000) —— 不管用户在干什么，
// 每 2 秒就把列表重画一遍。用户正展开着下拉、正打着字、正滚到列表中部，
// 都会被这次重画打断（实测：焦点掉到 body、原生下拉关闭、滚动位置跳）。
//
// 现在改成「用户正在交互时把这次刷新推迟，交互结束再补跑」：
//   1. 定时器照常触发（所以新内容仍会实时出现，不是「干脆不刷新」）；
//   2. 但如果此刻用户正在交互（原生下拉展开 / 焦点在输入框 / 弹出层展开），
//      这次刷新被登记为「待补跑」，等交互结束后由守卫模块统一执行；
//   3. 同一类刷新只保留最后一次，避免堆积成风暴。
function scheduleRefresh(key, fn) {
  if (!window.H3UI) { fn(); return; }
  if (window.H3UI.isUserInteracting()) {
    window.H3UI.deferRefresh(key, fn);
    return;
  }
  fn();
}

setInterval(() => scheduleRefresh("jobs", refreshJobs), 2000);
// 状态栏**不**推迟：renderStatus() 只写 #statusText 和那个小圆点，
// 既不碰输入框也不碰下拉框 —— 推迟它反而会让右上角状态变陈旧
// （用户停在一个输入框里不动时，状态就一直不更新）。
setInterval(refreshSystem, 3000);
// 硬件页：用户正在硬件页时才刷；交互期间同样推迟
setInterval(() => {
  if (state.page !== "system") return;
  scheduleRefresh("hardware", () => refreshHardware());
}, 3000);
