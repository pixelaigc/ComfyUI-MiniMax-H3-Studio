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
  // MiniMax-H3-Image：一个 checkpoint 同时做文生图与图片编辑。
  // 6 步蒸馏 / guidance 固定 1（无 CFG pass -> 无负面提示词）。
  t2i_h3image: { name: "H3-Image 文生图", kind: "image", engine: "h3image" },
  i2i_h3image: { name: "H3-Image 图片编辑", kind: "image", engine: "h3image" },
  longfilm: { name: "长片", kind: "video" },
  // 音乐（YuE2）：文生音乐 / 参考生音乐
  t2a: { name: "文生音乐", kind: "audio", engine: "music" },
  a2a: { name: "参考生音乐", kind: "audio", engine: "music" },
};

// 【2026-11-13 需求 C3】把 MODE_INFO 暴露到 window ——
// 详情模态框（web/job-detail.js，独立作用域）要用它显示模式名。
window.MODE_INFO = MODE_INFO;
// 【2026-11-13】推荐参数的来源类别标签（服务端 /api/video-options 下发）——
// web/job-meta.js 用它给**逐字段**标注来源（作者提供 / ComfyUI 硬性值 …）。
window.JOB_META_SOURCE_KIND_LABEL = {};
// 图像类模式：共用「图像」参数面板
const IMAGE_MODES = ["t2i", "t2i_qwen", "i2i_qwen", "t2i_moss",
                    "t2i_h3image", "i2i_h3image"];
// Qwen-Image 2.1 专用模式（参数清单与 Z-Image 不同）
const QWEN_MODES = ["t2i_qwen", "i2i_qwen"];
// MOSS-Image 专用模式（参数清单与 Z-Image / Qwen 都不同）
const MOSS_MODES = ["t2i_moss"];
// MiniMax-H3-Image 专用模式（文生图 + 图片编辑共用一套参数面板）
// 规格差异：32 倍数宽高 / 6 步 PDD 蒸馏 / 无负面提示词 / 最多 16 张参考图
const H3IMAGE_MODES = ["t2i_h3image", "i2i_h3image"];

function isQwenMode(mode) {
  return QWEN_MODES.includes(mode || state.mode);
}

function isMossMode(mode) {
  return MOSS_MODES.includes(mode || state.mode);
}

function isH3ImageMode(mode) {
  return H3IMAGE_MODES.includes(mode || state.mode);
}

// Z-Image 专用模式（目前只有 t2i；它是唯一有「官方 2K 放大路径」的族 ——
// 见 workflows.build_t2i 里超过 Z_IMAGE_MAX_DIRECT_MP 时的分支）。
function isZImageMode(mode) {
  const m = mode || state.mode;
  return m === "t2i";
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
  // 【2026-11-12 修】初值必须与工作流档位一致。
  // 工作流（workflows._h3_video_workflow）：params.lora != "none" -> steps 4。
  // 之前这里写 6、HTML 滑杆写 6，但工作流实际跑 4 步；
  // 后来 HTML 改成 4 之后，state.steps 还是 6
  // -> 滑杆显示 4、说明行写「步数 6」= 界面自相矛盾。
  steps: 4,
  // 【2026-11-11】首尾帧 / 参考生视频的采样参数（走旧工作流路径，
  // 但采样链路与文生视频相同，所以这些参数一样是真参数）。
  // 候选项来自 /api/video-options 的 sampling 清单（服务端从本机
  // ComfyUI 的 /object_info 取，前端不硬编码名单）。
  vidSampler: "",
  vidScheduler: "",
  vidShiftVideo: 12.0,
  vidShiftAudio: 3.0,
  donePage: 1,
  // 每页作品数（可配置，持久化在 localStorage）
  pageSize: loadPageSize(),
  doneFilters: { keyword: "", media: "", aspect: "", duration: "" },
  stitchClips: [],
  files: { first: null, last: null, refs: [] },
  // 素材库引用：{ 槽位: {asset_id, name, category} } —— 只存 id，不复制文件
  assetRefs: {},
  // 【2026-11-14】音轨来源（B2 音轨三选一）：
  //   "" = 使用生成的音乐
  //   "reference" = 使用参考音乐作为成片音轨
  //   "video_bg" = 使用参考视频的背景音乐作为成片音轨
  // 合法值与可见性判据见 h3studio/h3_audio_track.py（服务端与前端同一套）。
  audioTrackSource: "",
  timer: null,
  // 【2026-11-13 队列化改造】genBusy 不再表示「有任务在跑、不能提交」。
  // 它现在只表示「本会话有任务还没到终态」，**不参与按钮的禁用判断**
  // （按钮态见 updateActionButtons）。保留这个字段是为了不破坏
  // 其它可能读它的代码路径。
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
  // MiniMax-H3-Image 专属状态（文生图与图片编辑共用一份）
  imgH3Image: {
    tier: "4mp",
    aspect: "16:9",
    // 自定义宽高（与其它族同一套逻辑，但倍数是 32 而不是 16）
    custom: false,
    width: null,
    height: null,
    rawWidth: null,
    rawHeight: null,
    model: "h3_image_pruned_int8",
    // 官方模板：6 步 / cfg 1 / euler / simple
    steps: 6,
    cfg: 1,
    shift: 1,
    sampler: "euler",
    scheduler: "simple",
    // H3-Image guidance 固定 1 -> 无 CFG pass -> 负面提示词无效
    negative: "",
    batchCount: 1,
    // LoRA：官方暂未发布 H3-Image 的 LoRA —— 入口留位但暂无可选权重
    loras: [],
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
// MiniMax-H3-Image 的独立一套清单（服务端 /api/image-options 的 h3image 字段）
const H3IMAGE_OPTIONS = {
  tiers: [], aspects: [], models: {}, sizeMultiple: 32, maxDirectMp: 16,
  minMp: 1, sizeMin: 32, sizeMax: 16384,
  defaultTier: "4mp", defaultModel: "h3_image_pruned_int8",
  // 参考图上限：官方能力 16 张（与 Qwen 的 10 张不同）
  maxRefImages: 16, minRefImages: 1,
  // 官方占位标记语法（H3-Image 用 <Picture N>，不是 Qwen 的 <imageN>）
  placeholderExample: "<Picture 1>",
  // 本机能力探测结果（后端没重启时为 false -> 面板显示明确提示）
  probeComplete: true,
  // LoRA：官方暂未发布 -> 界面留位 + 明确说明
  loraSupported: false,
  loraNote: "",
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
// MiniMax-H3-Image 的 LoRA 清单。
// 【官方暂未发布 H3-Image 的 LoRA】—— 所以这份清单恒为空，
// 但结构保留：以后官方出了 LoRA，只要后端 /api/image-loras 下发即可，
// 前端不用改（见 renderLoraSelect 的「暂无可选 LoRA」分支）。
const H3IMAGE_LORAS = { available: [], acceleration: [], incompatible: [], config: [] };
// 服务端下发的功能位（后端重启后才有新位）
let SERVER_FEATURES = {};
// 后端是否已支持 MOSS-Image（/api/image-options 有 moss 字段才算）
let MOSS_BACKEND_READY = false;
// 后端是否已支持 MiniMax-H3-Image（/api/image-options 有 h3image 字段才算）
let H3IMAGE_BACKEND_READY = false;
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
  // 【2026-11-11 两级导航】视频分组的高亮要跟着当前 mode 走 ——
  // 切到图像模式时视频分组必须取消高亮，否则两个高亮项并存。
  syncVideoNavActive();
  showCreate();
  renderMode();
}

$$(".nav-item[data-mode]").forEach(el => el.addEventListener("click", () => {
  activateMode(el.dataset.mode, el);
}));

// ---------- 左侧栏折叠（2026-11-13）----------
//
// 【为什么在这里挂钩，而不是让 sidebar-fold.js 自己绑】
// 折叠模块只管「展开/收起」这一个动作；
// 「点了某个入口项 -> 先把它所在的分组/类别展开，再切模式」是**跨模块**的行为，
// 必须由掌握 activateMode() 的这一层来串（sidebar-fold.js 不认识 activateMode）。
//
// 【只在这条路径展开】
// 启动恢复路径（VideoNav.applyDefault / activateVideoMode）不展开 ——
// 否则「刷新后保持折叠状态」会被打破。
function expandNavForElement(el) {
  if (!window.SidebarFold) return;
  window.SidebarFold.ensureExpandedForElement(el);
}

// 【2026-11-13 折叠】点入口项前先把它所在的分组/类别展开 ——
// 否则选中态落在看不见的地方，用户会以为没点中。
// 用**事件委托**绑在 #sidebar 上：视频模型项是异步渲染的，
// 逐个 addEventListener 绑不到后渲染出来的节点。
(function bindNavFoldAwareClick() {
  const sidebar = $("#sidebar");
  if (!sidebar) return;
  sidebar.addEventListener("click", event => {
    const item = event.target.closest(".nav-item");
    if (!item) return;
    expandNavForElement(item);
  }, true);   // 捕获阶段：早于 activateMode 的切换，先展开再切
})();

// ---------- 视频两级导航（类别 -> 模型，2026-11-11）----------
//
// 【为什么单独一个模块】
// 视频导航是「类别 -> 模型」两级，而其它模式（图像 / 音乐）仍是平铺的
// 单级入口。两套结构混在一起会让 activateMode() 的高亮逻辑分叉，
// 所以视频这一支独立走 web/video-nav.js：
//   * 类别 -> 模型的映射只有服务端一份（/api/video-categories），
//     前端不再维护第二张映射表；
//   * 每个模型项仍带 data-mode（老 mode），老任务卡片的「复用」
//     按钮按 .nav-item[data-mode=...] 点选照样命中 —— 向后兼容。
function videoNavContainer() {
  return $("#navVideoCategories");
}

function syncVideoNavActive() {
  const box = videoNavContainer();
  if (!box || !window.VideoNav) return;
  // 传入当前 mode —— 视频分组只在「当前 mode 确实是视频模式」时高亮，
  // 否则首屏会同时高亮「Z-IMAGE 文生图」和视频项。
  window.VideoNav.syncActive(box, state.mode);
}

/** 选中某个视频 mode（来自两级导航或老任务卡片），并高亮对应层级。 */
function activateVideoMode(mode) {
  const box = videoNavContainer();
  let navEl = null;
  if (box && window.VideoNav) {
    window.VideoNav.selectByMode(mode);
    navEl = box.querySelector(`.nav-model-item[data-mode="${mode}"]`);
  }
  if (!navEl) {
    navEl = document.querySelector(`.nav-item[data-mode="${mode}"]`);
  }
  // 【2026-11-13 折叠】老任务卡片的「复用」按钮会走到这里。
  // 如果该视频类别正折着，选中态就落在看不见的地方 —— 先展开。
  // （启动恢复路径 applyDefault **不**展开，否则刷新后折叠状态保不住。）
  if (window.SidebarFold && navEl) {
    window.SidebarFold.ensureExpandedForElement(navEl);
  }
  activateMode(mode, navEl);
  syncVideoNavActive();
}

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
    // 【2026-11-14】首帧与尾帧**都可以单独给**（原来只认首帧）。
    $("#uploadTitle").textContent =
      "上传首帧与尾帧，或从素材库选（首帧 / 尾帧给其中一张即可）";
    zone.style.display = "block";
    mk("first", "首帧"); mk("last", "尾帧");
  } else if (mode === "r2v") {
    // 【2026-11-14】参考图上限从 3 张提升到 **9 张**
    // —— H3 参考生成节点的 ref_images 槽位上限就是 9（实测 object_info）。
    // 原来卡 3 张是历史遗留，把能力人为砍掉了。
    $("#uploadTitle").textContent =
      "上传参考图，或从素材库选（最多 9 张）";
    zone.style.display = "block";
    for (let i = 0; i < 9; i++) mk("ref" + i, "参考 " + (i + 1));
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
  // 注意力机制 / 二采 / 采样参数。flf / r2v 走旧面板，但**采样参数**
  // （采样器 / 调度器 / shift / CFG 说明）同样补齐 —— 它们的采样链路
  // 与 t2v 完全相同。longfilm 只做拼接，不采样，不需要这些参数。
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

  // 音轨来源（首尾帧 / 参考生视频）
  renderAudioTrackSelect();

  // 首尾帧 / 参考生视频：显示「采样参数」区块（步数沿用上面的通用滑杆）
  const legacyVideoSampling = $("#videoSamplingField");
  if (legacyVideoSampling) {
    const show = mode === "flf" || mode === "r2v";
    legacyVideoSampling.style.display = show ? "" : "none";
    if (show) {
      // 【2026-11-14】旧面板的加速 LoRA 卡片也用统一清单渲染 ——
      // 否则旧面板只能选写死的两枚，与文生视频面板行为不一致。
      renderLegacyAccelLoraCards();
      renderLegacyVideoSampling();
    }
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

// ---------- 音轨来源（首尾帧 / 参考生视频） ----------
//
// 【2026-11-14 新增 · B2 需求「音轨三选一」】
//   (a) 使用生成的音乐
//   (b) 使用参考音乐作为成片音轨
//   (c) 使用参考视频的背景音乐作为成片音轨
//
// 【为什么「没有素材就不出现」而不是置灰】
// 置灰等于「能选但服务端会把它改掉」—— 那是假选项。
// 所以这里**按实际有没有对应素材**来生成选项。
//
// 【判据必须与服务端一致】
// 服务端用 h3studio/h3_audio_track.py 归一（同一个 key 集合），
// 前端这里用同一套 key 字符串 —— 两边一致才不会
// 「界面能选、提交后被改掉」。
const AUDIO_TRACK_OPTIONS = [
  { key: "", label: "使用生成的音乐",
    note: "音轨由 H3 与画面一同采样生成，音画天然同步。" },
  { key: "reference", label: "使用参考音乐作为成片音轨",
    note: "参考音乐被贴到成片上（不参与采样），"
        + "长度不足会补静音、超出会裁到画面时长。" },
  { key: "video_bg", label: "使用参考视频的背景音乐作为成片音轨",
    note: "参考视频的背景音乐被贴到成片上（不参与采样），"
        + "长度不足会补静音、超出会裁到画面时长。" },
];

/** 当前有没有参考音乐 / 参考视频背景音（决定哪些选项出现）。 */
function audioTrackAvailability() {
  const slots = state.assetRefs || {};
  let hasRefAudio = false;
  let hasVideoAudio = false;
  for (const [key, item] of Object.entries(slots)) {
    if (!item || !item.asset_id) continue;
    const cat = item.category || "";
    if (cat === "audio") {
      if (key.startsWith("refvid")) hasVideoAudio = true;
      else hasRefAudio = true;
    }
  }
  return { hasRefAudio, hasVideoAudio };
}

function renderAudioTrackSelect() {
  const field = $("#audioTrackField");
  const sel = $("#audioTrackSelect");
  const note = $("#audioTrackNote");
  if (!field || !sel) return;
  const isF2V = state.mode === "flf" || state.mode === "r2v";
  field.style.display = isF2V ? "" : "none";
  if (!isF2V) return;

  const { hasRefAudio, hasVideoAudio } = audioTrackAvailability();
  const available = AUDIO_TRACK_OPTIONS.filter(o => {
    if (o.key === "reference") return hasRefAudio;
    if (o.key === "video_bg") return hasVideoAudio;
    return true;
  });
  sel.innerHTML = available.map(o =>
    `<option value="${esc(o.key)}"${o.key === state.audioTrackSource ? " selected" : ""}>`
    + `${esc(o.label)}</option>`).join("");
  // 当前选中的不再可用（素材被删了）-> 回落成「使用生成的音乐」
  if (!available.some(o => o.key === state.audioTrackSource)) {
    state.audioTrackSource = "";
    sel.value = "";
  }
  const cur = available.find(o => o.key === state.audioTrackSource);
  if (note) {
    note.textContent = cur ? cur.note
      : "成片音轨由 H3 与画面一同采样生成。";
  }
}

// 音轨来源下拉的事件绑定
document.addEventListener("change", event => {
  if (event.target && event.target.id === "audioTrackSelect") {
    state.audioTrackSource = event.target.value || "";
    renderAudioTrackSelect();
  }
});

// ---------- 图像模式（Z-Image 文生图） ----------
// 档位 + 比例 -> 像素宽高。**原始值**（不做像素区间钳制）。
// 规则与后端 z_image_dimensions / qwen_image_dimensions / ComfyUI
// ResolutionSelector 同一算法：
//   total = megapixels * 1024^2; scale = sqrt(total / (wr*hr))
//   w = round(wr*scale/mult)*mult;  h = round(hr*scale/mult)*mult
// multiple 用 16（Z-Image / Qwen-Image 2.1 都要求宽高是 16 的倍数：
// Qwen-Image 2.1 的 VAE 是 16 倍下采样）。
function imgTierSizeRaw(tier, aspect) {
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

// 档位 -> **最终生效**宽高 + 调整原因。
//
// ⚠️ 为什么 Z-Image 要区别对待：
//   - Z-Image：超过 3MP 是**故意的** —— 后端 build_t2i 会切到官方 2K 放大路径
//     （低分辨率生成 + RealESRGAN 放大 + 二次细化），最终输出尺寸 = 档位尺寸。
//     所以这里**绝不能**钳制，钳了就永远走不到放大路径。
//   - MOSS / Qwen：后端**没有**放大路径，超限尺寸会被后端钳到官方直出上限
//     （MOSS 4MP / Qwen 4.2MP）。前端必须**用同一套规则**钳制，
//     否则界面显示 2736×1536、真正提交给 ComfyUI 的却是 2720×1520
//     —— 用户看到的尺寸是假的（实测证据：evidence/e2e_size_consistency.json）。
function imgTierSize(tier, aspect) {
  const [rw, rh] = imgTierSizeRaw(tier, aspect);
  if (isZImageMode()) return [rw, rh, []];
  return normalizeImgSize(rw, rh);
}

// 只取宽高（兼容旧调用点）
function imgAutoSize(tier, aspect) {
  const s = imgTierSize(tier, aspect);
  return [s[0], s[1]];
}

// 当前图像模式的「参数上下文」：Z-Image 与 Qwen-Image 2.1 两套清单 + 两份状态。
// 面板所有控件都从这里取当前该用的那一套，避免两套参数互相污染。
function imgCtx() {
  if (isH3ImageMode()) {
    return {
      opts: H3IMAGE_OPTIONS,
      st: state.imgH3Image,
      tiers: H3IMAGE_OPTIONS.tiers,
      models: H3IMAGE_OPTIONS.models,
      multiple: H3IMAGE_OPTIONS.sizeMultiple,
      maxMp: H3IMAGE_OPTIONS.maxDirectMp,
      minMp: H3IMAGE_OPTIONS.minMp,
    };
  }
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
  // 用 imgTierSize（生效值）而不是原始档位值 —— 否则 MOSS/Qwen 超限档位
  // 会显示一个**永远不会被真正提交**的尺寸（如 2736×1536，实际是 2720×1520）。
  const size = imgTierSize(tier.key, ctx.st.aspect);
  const [w, h] = size;
  const mp = (w * h) / (1024 * 1024);
  const up = mp > (ctx.maxMp || 3);
  // 「放大」标记只对 Z-Image 成立（它真的有官方 2K 放大路径）；
  // MOSS / Qwen 超限是「直接按此尺寸出图」，标成「放大」会误导用户。
  const upTag = up ? (isZImageMode() ? " · 放大" : " · 超限直出") : "";
  return `${tier.label} · ${w}×${h} (${mp.toFixed(1)}MP)${upTag}`;
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
  if (isH3ImageMode()) {
    // H3-Image 官方暂未发布 LoRA -> 清单恒为空
    // （界面仍显示 LoRA 区域 + 说明文字，见 renderLoraSelect 的留位分支）
    return {store: H3IMAGE_LORAS, list: state.imgH3Image.loras, label: "H3-Image"};
  }
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
  // 【H3-Image 的 LoRA 留位】
  // 官方暂未发布 H3-Image 的 LoRA —— 需求是「界面需留位或明确说明」。
  // 这里两者都做：下拉框**保留在面板上**（不隐藏），但显示
  // 「官方暂未发布该模型的 LoRA」的说明，并把下拉置灰。
  // 以后官方出了 LoRA，只要后端下发清单，这个分支自动失效。
  if (isH3ImageMode() && !ctx.store.available.length) {
    sel.innerHTML = `<option value="">官方暂未发布 H3-Image 的 LoRA</option>`;
    sel.disabled = true;
    const addBtn = $("#imgLoraAdd");
    if (addBtn) addBtn.disabled = true;
    return;
  }
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
      : (isH3ImageMode()
          ? (H3IMAGE_OPTIONS.loraNote ||
             "MiniMax-H3-Image 官方暂未发布 LoRA —— 面板已预留 LoRA 入口，暂无可选权重。")
          : `本机未检测到可用的效果类 ${ctx.label} LoRA 文件。`);
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
    // ⚠️ **2026-11-15 修复（形态不实标注）**：MOSS 底模的注意力投影是
    // **分离 q/k/v**，而上游有一批 LoRA 用的是**融合 qkv** 形态。
    // 直接挂上去 ComfyUI 会静默丢弃（只打 "lora key not loaded"，
    // 出图照跑但 LoRA 没生效）—— 所以服务端在提交工作流前会**自动转换**。
    //
    // 界面必须如实说出来：不能让用户以为它「原生」就能直接用，
    // 也不能让它跟真·分离形态的条目长得一模一样。
    //
    // 【2026-11-15 缺陷 A 收口】标签按 **conversion_status** 分三档，
    // 不再只看「是不是融合形态」：
    //   derived    转换**已验证**（派生文件已生成并实测完整生效）
    //   unverified 转换**未验证** —— 这类条目服务端已判不可用，不下发
    //   （兼容旧字段：没有 conversion_status 时退回 qkv_convertible 判定）
    const qkvShape = meta && meta.qkv_shape ? String(meta.qkv_shape) : "";
    const convStatus = meta && meta.conversion_status
      ? String(meta.conversion_status) : "";
    const qkvConv = meta && meta.qkv_convertible === true;
    const fusedNote = qkvShape === "fused"
      ? (convStatus === "derived"
        ? `<span class="img-lora-tag qkv" title="该 LoRA 的注意力权重是「融合 qkv」形态（q/k/v 合并在一个张量里），本机 MOSS 底模用的是分离 q/k/v。服务端会在提交时自动使用已生成的归一版（转换已实测验证：patched=${Number(meta.conversion_patched) || 263} / missing=0），转换后权重完整生效 —— 直接挂原文件会被 ComfyUI 静默丢弃。">融合 qkv · 已验证可转换</span>`
        : (convStatus === "unverified"
          ? `<span class="img-lora-tag qkv warn" title="该 LoRA 的注意力权重是「融合 qkv」形态，与本机底模的分离 q/k/v 结构不一致，且转换尚未在本机验证 —— 服务端已将它排除在可选列表之外（避免选中后 LoRA 不生效）。">融合 qkv · 转换未验证</span>`
          : (qkvConv
            ? `<span class="img-lora-tag qkv" title="该 LoRA 的注意力权重是「融合 qkv」形态（q/k/v 合并在一个张量里），本机 MOSS 底模用的是分离 q/k/v。服务端会在提交时自动做无损转换，转换后权重完整生效 —— 直接挂原文件会被 ComfyUI 静默丢弃。">融合 qkv · 自动转换</span>`
            : `<span class="img-lora-tag qkv warn" title="该 LoRA 的注意力权重是「融合 qkv」形态，且无法无损拆分 —— 在本机 MOSS-Image 底模上不能完整生效。">融合 qkv · 不可用</span>`)))
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
        ${fusedNote}
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
        : (meta.base_verdict
          ? esc(meta.base_verdict)
          : "未标注（本机实测可用，但上游目录未登记基座判定结论）")}</dd>
      <dt>权重形态</dt><dd>${meta.qkv_shape === "fused"
        ? (meta.conversion_status === "derived"
          ? "融合 qkv（q/k/v 合并在一个张量里）—— 本机底模用分离 q/k/v；服务端提交时自动使用已生成的归一版，转换已实测验证（missing=0），完整生效"
          : (meta.conversion_status === "unverified"
            ? "融合 qkv（q/k/v 合并在一个张量里）—— 与本机底模结构不一致，且转换尚未在本机验证，已排除在可选列表之外"
            : (meta.qkv_convertible
              ? "融合 qkv（q/k/v 合并在一个张量里）—— 本机底模用分离 q/k/v，服务端提交时自动做无损转换后完整生效"
              : "融合 qkv，且无法无损拆分 —— 在本机底模上不能完整生效")))
        : (meta.qkv_shape === "split" ? "分离 q/k/v（与本机底模结构一致，可直接挂载）" : "—")}</dd>
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
      // H3-Image：官方暂未发布 LoRA -> 后端会返回空清单（不会报错）
      {name: "H3-Image", store: H3IMAGE_LORAS,
       set: v => { state.imgH3Image.loras = v; }},
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
    : (isH3ImageMode()
        ? "H3-Image 的 guidance 固定为 1（官方 6 步蒸馏，无 CFG pass）——"
          + "负面提示词**数学上无效**，已禁用。"
          + "要把「不要什么」写进正面提示词里（例如「不要出现文字、不要模糊」）。"
        : "当前版本为蒸馏模型：CFG=1 时 ComfyUI 会跳过 uncond 分支，负面提示词数学上无效，已禁用。");
}

function renderImagePanel() {
  const ctx = imgCtx();
  const s = ctx.st;
  const model = ctx.models[s.model];
  // 【参考图占位语法随模型族切换】—— 必须在这里**无条件**设置，
  // 不能放进「显示参考图」的分支里：profile 是参考图模块的**模块级全局**，
  // 漏切一次就会用另一族的占位语法去校验当前模式的提示词
  // （比如切回 Qwen 文生图后仍用 H3-Image 的 <Picture N> 规则）。
  if (window.QwenRefs && window.QwenRefs.setProfile) {
    window.QwenRefs.setProfile(isH3ImageMode() ? "h3image" : "qwen");
  }

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
      : (isQwenMode() ? "Qwen-Image 2.1 模型"
        : (isH3ImageMode() ? "H3-Image 模型" : "Z-Image 模型"));
  }
  Object.entries(ctx.models).forEach(([key, m]) => {
    const opt = document.createElement("option");
    opt.value = key;
    const unavailable = m.available === false;
    // 【不可用原因要说准】原来是写死的「（权重缺失，不可用）」——
    // 但 H3-Image 缺的是**核心节点**（ComfyUI 版本过低），权重齐全。
    // 写死「权重缺失」会让用户去下载权重（那不是问题所在），
    // 或者反过来以为权重没问题。按服务端给的真实原因分类显示。
    let unavailableReason = "";
    if (unavailable) {
      // 【nodes_available 是三态】
      //   true  = 节点在
      //   false = 确认缺节点（可以断言「ComfyUI 版本过低」）
      //   null  = 探测没问全（ComfyUI 忙 / 超时）—— 不能断言缺什么
      if (m.nodes_available === false && m.weights_available === true) {
        unavailableReason = "（本机 ComfyUI 缺 H3-Image 核心节点，不可用）";
      } else if (m.weights_available === false) {
        unavailableReason = "（权重缺失，不可用）";
      } else if (m.nodes_available === null || m.nodes_available === undefined) {
        unavailableReason = "（暂时无法确认本机 ComfyUI 能力，不可用）";
      } else {
        unavailableReason = "（不可用）";
      }
    }
    opt.textContent = unavailable ? m.label + unavailableReason : m.label;
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
  if (isH3ImageMode()) {
    // H3-Image 官方模板：steps=6 / cfg=1 / euler / simple
    // 步数与 PDD sigma 表绑定（6 步蒸馏）-> 步数上限给到 12 便于对照，
    // 但默认与推荐值都是 6；CFG 固定 1（guidance 固定，无 CFG pass）。
    stepsEl.min = "1"; stepsEl.max = "12"; stepsEl.step = "1";
    cfgEl.min = "1"; cfgEl.max = "8"; cfgEl.step = "0.5";
    shiftEl.min = "0"; shiftEl.max = "3"; shiftEl.step = "0.1";
  } else if (isMossMode()) {
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
    isQwenMode() ? 2 : (isMossMode() ? 1 : (isH3ImageMode() ? 1 : 1)));

  // 负面提示词：Turbo 在 CFG=1 下负面被跳过（samplers.py:610）-> 禁用
  const negSupported = !!(model && model.supports_negative);
  $("#imgNegField").style.display = negSupported ? "" : "none";
  updateNegativeNote();
  if (negSupported) $("#imgNegative").value = s.negative || "";

  // 参考图区：Qwen 图片编辑 + H3-Image 图片编辑共用同一块 DOM。
  //
  // 【官方 PE 提示词扩写不再有独立勾选框】——
  // 它已并入「增强」按钮的下拉菜单（「qwen21图片官方增强PE」）。
  // 这里只保留参考图区（图片编辑模式）。
  //
  // 【占位语法随模型切换】—— Qwen 用 <imageN>，H3-Image 用 <Picture N>。
  // 参考图模块（web/qwen-refs.js）是一份实现两套语义，靠 setProfile 切换。
  // 不切换的后果：用户写 <Picture 1> 会被 Qwen 的正则扫不到 ->
  // 提交时报「缺少 <image1> 占位标记」（误拒合法提交）。
  const qwen = isQwenMode();
  const h3img = isH3ImageMode();
  const refField = $("#imgRefField");
  const showRefs = (qwen && state.mode === "i2i_qwen")
    || (h3img && state.mode === "i2i_h3image");
  if (refField) refField.style.display = showRefs ? "" : "none";
  if (showRefs) {
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
  // 【H3-Image 同理】后端没重启时 /api/image-options 里没有 h3image 字段
  // -> H3-Image 面板的档位/模型下拉是空的，点生成会报
  // 「未知模式 t2i_h3image」—— 症状与 MOSS 完全一样，且更难自查。
  const mossStale = isMossMode() && !MOSS_BACKEND_READY;
  const h3Stale = isH3ImageMode() && !H3IMAGE_BACKEND_READY;
  if (!mossStale && !h3Stale) {
    box.hidden = true;
    box.innerHTML = "";
    return;
  }
  box.hidden = false;
  const which = mossStale ? "MOSS-Image" : "H3-Image";
  const modeName = mossStale ? "t2i_moss" : "t2i_h3image";
  box.innerHTML =
    `<b>后端未重启，${which} 面板当前不可用。</b><br>`
    + "页面资源（前端）已经是新的，但后端进程还是旧代码 —— "
    + `所以分辨率档位/模型下拉是空的，点生成会报「未知模式 ${modeName}」。<br>`
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
  // ⚠️ Z-Image **不做**像素上限钳制。
  //   原因：后端 build_t2i 对 >3MP 的目标尺寸会切到**官方 2K 放大路径**
  //   （workflows._z_image_upscale_nodes：低分辨率生成 -> RealESRGAN ->
  //   ImageScale(width=目标宽, height=目标高) -> 低 denoise 二次细化），
  //   最终输出就是用户要的**原始尺寸**。
  //   如果这里先钳掉，用户要 2048x2048 却被改成 1760x1760，
  //   界面显示的尺寸和实际出图就不一致了（实测：后端 ImageScale 给的是 2048x2048）。
  //   MOSS / Qwen 没有放大路径，超限必须钳 —— 那才是官方直出上限。
  const maxMp = isZImageMode() ? null : ctx.maxMp;
  if (maxMp) {
    const maxPixels = maxMp * 1024 * 1024;
    if (nw * nh > maxPixels) {
      const scale = Math.sqrt(maxPixels / (nw * nh));
      let sw = Math.max(mult, Math.round(nw * scale / mult) * mult);
      let sh = Math.max(mult, Math.round(nh * scale / mult) * mult);
      // ⚠️ 16 的倍数是离散步长：按比例缩到目标像素后再对齐到 16 的倍数，
      //    可能又略微超出（例如 4.0MP -> 2736x1536 = 4.008MP）。
      //    缩放后仍超 -> 再退一格直到不超（后端 normalize_moss_image_size /
      //    normalize_qwen_image_size 同逻辑）。
      //    不超 -> 已是能表示的最接近值，保持不变（只提示）。
      //    规则必须与后端逐格一致，否则界面显示的尺寸 != 实际提交的尺寸。
      // ⚠️ 「退一格」逻辑必须**按族**决定，不能一刀切：
      //   MOSS / Qwen：后端 normalize_moss_image_size /
      //     normalize_qwen_image_size 里都有这个 while 循环 —— 前端必须逐格一致，
      //     否则界面显示 2736×1536、真正提交给 ComfyUI 的是 2720×1520。
      //   H3-Image：后端 h3image.normalize_h3_image_size **没有**这个循环
      //     （只做一次比例缩放）—— 前端若多退一格，反而比后端更小，
      //     界面显示 5440×3040、实际提交 5472×3072。
      //   实测依据：evidence_moss_live/parity_probe.py
      //     （MOSS/Qwen 带循环 0 处不一致；H3-Image 带循环 3 处不一致）。
      if (sw * sh > maxPixels && !isH3ImageMode()) {
        while (sw * sh > maxPixels && sw > mult && sh > mult) {
          sw -= mult; sh -= mult;
        }
      }
      if (sw * sh > maxPixels) {
        // 【按模式区分上限文案】
        // MOSS 有 4MP+ hires LoRa 路径（可继续放大）；
        // H3-Image **没有**官方放大链（官方模板只有单次直出），
        // 写「更大尺寸需挂 hires LoRa」会指向一条不存在的路。
        const overCapNote = isH3ImageMode()
          ? "（H3-Image 无官方放大链，超出即按比例缩小）"
          : "（更大尺寸需挂 4MP+ hires LoRa）";
        reasons.push(`总像素 ${(nw * nh / 1048576).toFixed(2)}MP 超过官方直出上限 ${maxMp}MP → 按比例缩到 ${sw}×${sh}${overCapNote}`);
      } else {
        reasons.push(`总像素 ${(nw * nh / 1048576).toFixed(2)}MP 略超官方直出上限 ${maxMp}MP（对齐到 ${mult} 的倍数后已是最接近值 ${sw}×${sh} = ${(sw * sh / 1048576).toFixed(2)}MP）→ 保持不变`);
      }
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
    // 档位路径：Z-Image 不钳制（要走官方 2K 放大路径），MOSS/Qwen 按官方直出上限钳制
    const tierSize = imgTierSize(s.tier, s.aspect);
    w = tierSize[0]; h = tierSize[1]; reasons = tierSize[2];
    // 生效值写回 state —— 提交给 ComfyUI 的必须是这个值
    s.width = w; s.height = h;
  }
  // 【尺寸输入框的 step 随模式切换】
  // H3-Image 的宽高必须是 **32** 的倍数（官方文档），其它族是 16。
  // step 不跟着改的话，浏览器会允许输入 2721 这种非法值，
  // 用户按回车才发现被吸附 —— 体验上像「输入框没生效」。
  const mult = ctx.multiple || 16;
  ["#imgWidth", "#imgHeight"].forEach(sel => {
    const el = $(sel);
    if (el) el.step = mult;
  });
  // 用户正在输入时不要覆盖输入框里的原始文本（否则输入会被打断）
  if (!imgSizeTyping) {
    $("#imgWidth").value = w;
    $("#imgHeight").value = h;
  }
  $("#imgSizeHint").textContent = `${w}×${h}`;
  const mp = (w * h) / (1024 * 1024);
  const up = mp > (ctx.maxMp || 3);
  const parts = [`${mp.toFixed(2)} MP`];
  // ⚠️ 「超过上限」的处置方式**因族而异**，不能一句话套用：
  //   - Z-Image：后端 build_t2i 确实会切到官方 2K 放大路径
  //     （workflows._z_image_upscale_nodes），所以「改走生成+放大」是真的。
  //   - MOSS / Qwen：后端**没有**放大路径，超限尺寸是原样发给 ComfyUI 的
  //     （build_moss_t2i / build_qwen_t2i 里都没有 upscale 分支）。
  //     之前这里统一显示「改走「生成 + 官方放大」路径」= 对用户撒了谎，
  //     用户以为会走放大，实际拿到的是未放大的原尺寸图。
  //     现在改成如实说明「超限直出」。
  if (up) {
    parts.push(isZImageMode()
      ? `超过 ${ctx.maxMp}MP，改走「生成 + 官方放大」路径（输出像素为近似值）`
      : `超过 ${ctx.maxMp}MP 官方直出区间，本模式无放大路径，将按此尺寸直接出图`);
  }
  // 档位路径被钳到官方直出上限时，要**如实告诉用户**被调了多少
  // （档位原始值 -> 生效值），否则用户选了 2K 却拿到 2720×1520 却不知情。
  if (!manual && reasons.length) {
    parts.push(`档位原始值已按官方直出上限调整为 ${w}×${h}`);
  }
  if (manual) parts.push("自定义尺寸，点「自动」可恢复档位计算值");
  $("#imgSizeManualNote").textContent = parts.join(" · ");

  // 【2026-11-14 可实现性核查修复】放大路径不仅换尺寸，还会**写死采样参数**
  // （workflows.py:_z_image_upscale_nodes -> steps=8/cfg=1.0/res_multistep/
  // simple + 二次细化 5 步 dpmpp_2m_sde）。用户在这里调的步数/CFG/采样器/
  // 调度器在放大路径下**不生效** —— 尺寸栏只说了「改走放大路径」，
  // 没说不生效，这里补一条明确说明。
  const upWarn = $("#imgUpscaleParamWarn");
  if (upWarn) {
    const upActive = up && !isQwenMode() && !isMossMode() && !isH3ImageMode();
    upWarn.hidden = !upActive;
    upWarn.textContent = upActive
      ? `当前尺寸超过 ${ctx.maxMp}MP：后端改走「低分辨率生成 + 官方放大」路径，`
        + "该路径使用官方 2K 模板的固定采样参数（8 步 / CFG 1.0 / res_multistep / simple），"
        + "本页的步数、CFG、采样器、调度器不生效。需要自定义采样参数请把尺寸降到 "
        + `${ctx.maxMp}MP 以内。`
      : "";
  }

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
  // 【2026-11-11】同视频面板：没有内容就别渲染橙色空条
  // （.img-warn 自带背景 + padding + 边框，空内容也会显示成一条橙条）。
  const imgWarn = $("#imgModelWarn");
  if (imgWarn) {
    imgWarn.textContent = m.warn || "";
    imgWarn.hidden = !m.warn;
  }
  // 【H3-Image 不可用时的明确说明】
  // 需求原文：「须如实说明缺失项，并保证选择该模型时前端不崩溃、
  // 有明确提示，而不是静默失败」。
  //
  // 缺节点 / 缺权重两种情况都要说清楚具体缺什么 ——
  // 「不可用」三个字对用户毫无用处。
  const h3Note = $("#imgH3ImageNote");
  if (h3Note) {
    if (isH3ImageMode() && m.available === false) {
      const missing = Array.isArray(m.missing) ? m.missing : [];
      h3Note.hidden = false;
      // 【三态判定】
      //   nodes_available === false -> 确认缺节点，列出具体缺什么
      //   nodes_available == null  -> 探测没问全，不能断言缺什么
      //   权重缺失                  -> 无论节点探测结果如何都要说
      const nodeUnknown = (m.nodes_available === null
                           || m.nodes_available === undefined);
      const weightsMissing = m.weights_available === false;
      if (missing.length) {
        // 服务端已把「确认缺的东西」列全（缺节点 + 缺权重）
        h3Note.textContent =
          "本机尚不具备 H3-Image 运行条件，缺以下项：\n"
          + missing.map(x => "· " + x).join("\n")
          + "\n补齐后即可正常出图（模型下拉里会自动变为可选）。";
      } else if (nodeUnknown) {
        h3Note.textContent =
          "暂时无法探测本机 ComfyUI 是否具备 H3-Image 能力"
          + "（ComfyUI 可能正忙或不可达）—— 请稍后刷新面板重试。\n"
          + "已确认的部分："
          + (weightsMissing ? "权重缺失。" : "权重齐全。");
      } else if (weightsMissing) {
        h3Note.textContent =
          "本机缺少 H3-Image 所需的权重文件（见上方说明）。";
      } else {
        h3Note.textContent = "本机尚不具备 H3-Image 运行条件。";
      }
    } else {
      h3Note.hidden = true;
      h3Note.textContent = "";
    }
  }
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
  // 【2026-11-14 可实现性核查修复】Qwen-Image 2.1 Turbo 走**固定 sigma 表**
  // （workflows.py 的 ManualSigmas 分支）—— 图里根本没有 `steps` 键，
  // 用户拖到 40 步实际还是 8 步。之前 steps 滑杆**可拖但不生效**（假控件）。
  // 服务端早就下发了 `manual_sigmas_steps`（server.py:4444 注释明写
  // 「前端据此提示『步数与 schedule 绑定』」），但前端一直没消费它。
  // 修法：与 params_locked_by_accel 同一套约定 —— 锁定控件 + 说明原因。
  const manualSigmasSteps = m.manual_sigmas_steps;
  const stepsLockedBySigma = manualSigmasSteps != null && manualSigmasSteps > 0;
  // 【#imgShift 一并锁定】
  // H3-Image 的 guidance 固定 1 -> shift 不参与计算；
  // Z-Image Base-Distill 之类也把 shift 与蒸馏档绑定。
  // 加进来是为了让「disabled」的真相只有一个地方 —— 之前 H3-Image
  // 单独写了一段 shiftEl.disabled = true，两处会互相覆盖。
  ["#imgSteps", "#imgCfg", "#imgSamplerSelect", "#imgSchedulerSelect",
   "#imgShift"].forEach(sel => {
    const el = $(sel);
    if (!el) return;
    el.disabled = locked;
    el.title = locked
      ? (isH3ImageMode()
          ? "H3-Image 的 guidance 固定为 1、步数与 PDD sigma 表绑定，不能修改"
          : "已绑定加速 LoRA 的蒸馏档，不能修改")
      : "";
  });
  // 固定 sigma 表的模型：只锁「步数」（cfg/sampler/scheduler 仍可调 ——
  // Turbo 的 CFGGuider 读 cfg、KSamplerSelect 读 sampler_name，
  // 只有 scheduler 被 ManualSigmas 顶掉）。
  if (stepsLockedBySigma) {
    const stepsEl = $("#imgSteps");
    if (stepsEl) {
      stepsEl.disabled = true;
      stepsEl.title = `该模型使用固定 ${manualSigmasSteps} 步 sigma 表（ManualSigmas），`
        + "步数与蒸馏 schedule 绑定，不能修改";
      // 把值拉回固定步数，避免界面上留着「已改 40」这种不存在的状态
      if (Number(s.steps) !== Number(manualSigmasSteps)) {
        s.steps = Number(manualSigmasSteps);
        stepsEl.value = String(manualSigmasSteps);
        $("#imgStepsVal").textContent = s.steps;
      }
    }
    const sh = $("#imgStepsHint");
    if (sh) {
      sh.textContent = "锁定";
      sh.title = `固定 ${manualSigmasSteps} 步 sigma 表`;
    }
  }
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
    isQwenMode() ? 2 : (isMossMode() ? 1 : (isH3ImageMode() ? 1 : 1)));
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
    // MiniMax-H3-Image 的独立一套清单（服务端 h3image 字段）
    const h = data.h3image || {};
    // 服务端没下发 h3image 字段 = 后端还是旧代码（没重启）
    H3IMAGE_BACKEND_READY = !!(h.tiers && h.tiers.length);
    H3IMAGE_OPTIONS.tiers = h.tiers || [];
    H3IMAGE_OPTIONS.aspects = h.aspects || [];
    H3IMAGE_OPTIONS.models = h.models || {};
    H3IMAGE_OPTIONS.sizeMultiple = h.size_multiple || 32;
    H3IMAGE_OPTIONS.maxDirectMp = h.max_direct_mp || 16;
    H3IMAGE_OPTIONS.minMp = h.min_mp || 1;
    H3IMAGE_OPTIONS.sizeMin = 32;
    H3IMAGE_OPTIONS.sizeMax = 16384;
    H3IMAGE_OPTIONS.defaultTier = h.default_tier || "4mp";
    H3IMAGE_OPTIONS.defaultModel = h.default_model || "h3_image_pruned_int8";
    H3IMAGE_OPTIONS.maxRefImages = h.max_ref_images || 16;
    H3IMAGE_OPTIONS.minRefImages = h.min_ref_images || 1;
    H3IMAGE_OPTIONS.placeholderExample = h.placeholder_example || "<Picture 1>";
    // 【probe_complete 的语义】服务端只在**问全了**的情况下才敢断言
    // 「缺节点」。问不全（ComfyUI 忙/超时）时返回 false，
    // 此时界面应该说「暂时无法探测」而不是断言缺什么。
    H3IMAGE_OPTIONS.probeComplete = h.probe_complete === true;
    H3IMAGE_OPTIONS.loraSupported = !!h.lora_supported;
    H3IMAGE_OPTIONS.loraNote = h.lora_note || "";
    // H3-Image 默认值对齐服务端推荐（官方模板：6 步 / cfg 1 / euler / simple）
    const hm = H3IMAGE_OPTIONS.models[H3IMAGE_OPTIONS.defaultModel];
    if (hm) {
      state.imgH3Image.model = H3IMAGE_OPTIONS.defaultModel;
      state.imgH3Image.steps = hm.steps;
      state.imgH3Image.cfg = hm.cfg;
      state.imgH3Image.shift = hm.shift;
      state.imgH3Image.sampler = hm.sampler;
      state.imgH3Image.scheduler = hm.scheduler;
      state.imgH3Image.tier = H3IMAGE_OPTIONS.defaultTier;
      const hKnown = H3IMAGE_OPTIONS.aspects.some(a => a.key === state.imgH3Image.aspect);
      if (!hKnown && H3IMAGE_OPTIONS.aspects.length) {
        state.imgH3Image.aspect = H3IMAGE_OPTIONS.aspects[0].key;
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
  // 【2026-11-12】模型卡片换了 -> legacy 采样参数的「推荐」基准也变了
  // （Turbo 档 4 步 / euler / beta，标准档 20 步 / res_multistep / simple），
  // 必须重渲染，否则提示停留在旧基准。
  if (state.mode === "flf" || state.mode === "r2v") renderLegacyVideoSampling();
  // 视频模型换了 -> 增强模板按新模型的模型族重新过滤
  if (typeof refreshEnhanceSpecForMode === "function") refreshEnhanceSpecForMode();
});
bindSeg("#loraCards", "lora", () => {
  // 【2026-11-12】LoRA 卡片换了 -> 同样要重算 legacy 采样参数的推荐基准
  if (state.mode === "flf" || state.mode === "r2v") renderLegacyVideoSampling();
});

/**
 * 渲染首尾帧 / 参考生视频（旧面板）的加速 LoRA 卡片。
 *
 * 【2026-11-14 重构】旧实现是在 index.html 里**写死**两张卡片
 * （ckpt850 / v4step600），所以旧面板根本选不到 LightX2V / DMAD ——
 * 而文生视频面板能选。两个面板对同一件事给出不同选项，
 * 用户会以为「选了不生效」。
 *
 * 现在旧面板也读**同一份**清单（window.VIDEO_PANEL.accelOptions()），
 * 语义统一为「一个槽位，选哪枚工作流就只挂哪枚」。
 */
function renderLegacyAccelLoraCards() {
  const host = $("#loraCards");
  if (!host) return;
  const options = (window.VIDEO_PANEL && window.VIDEO_PANEL.accelOptions)
    ? window.VIDEO_PANEL.accelOptions() : [];
  if (!options.length) return;   // 清单还没到 -> 保留静态占位
  host.innerHTML = options.map(o => `
    <button data-v="${esc(o.key)}"${o.key === state.lora ? " class=\"active\"" : ""}>
      <b>${esc(o.display)}</b>
      <small>${esc(o.loader === "LoraLoaderModelOnly" ? "核心节点加载" : "Turbo 节点")}</small>
    </button>`).join("");
  $$("#loraCards button").forEach(b => b.addEventListener("click", () => {
    $$("#loraCards button").forEach(x => x.classList.remove("active"));
    b.classList.add("active");
    state.lora = b.dataset.v;
    if (state.mode === "flf" || state.mode === "r2v") renderLegacyVideoSampling();
  }));
  // 当前选中项若已不在清单里 -> 回落默认
  if (state.lora && !options.some(o => o.key === state.lora)) {
    state.lora = options[0].key;
    const btn = $(`#loraCards button[data-v="${state.lora}"]`);
    if (btn) btn.classList.add("active");
  }
}
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
  renderLegacyVideoSampling();
});
// ---- 首尾帧 / 参考生视频的采样参数事件 ----
(function bindLegacyVideoSampling() {
  const sampler = $("#vidLegacySamplerSelect");
  if (sampler) {
    sampler.addEventListener("change", e => {
      state.vidSampler = e.target.value;
      renderLegacyVideoSampling();
    });
  }
  const scheduler = $("#vidLegacySchedulerSelect");
  if (scheduler) {
    scheduler.addEventListener("change", e => {
      state.vidScheduler = e.target.value;
      renderLegacyVideoSampling();
    });
  }
  const shiftVideo = $("#vidLegacyShiftVideo");
  if (shiftVideo) {
    shiftVideo.addEventListener("input", e => {
      state.vidShiftVideo = parseFloat(e.target.value);
      renderLegacyVideoSampling();
    });
  }
  const shiftAudio = $("#vidLegacyShiftAudio");
  if (shiftAudio) {
    shiftAudio.addEventListener("input", e => {
      state.vidShiftAudio = parseFloat(e.target.value);
      renderLegacyVideoSampling();
    });
  }
})();
/**
 * 通用「步数」滑杆的默认值。
 *
 * 【2026-11-12 修】必须与 workflows._h3_video_workflow 的硬编码档位一致：
 *   params.lora != "none" -> steps 4
 *   params.lora == "none" -> steps 20
 * 而 params.lora = state.model === "lora" ? state.lora : "none"。
 *
 * 之前 LoRA 档写的是 6 —— 面板显示 6 步，工作流实际跑 4 步。
 * 卡片上的文案「Turbo 加速 · 默认 6 步」也一并改成 4 步（index.html）。
 */
function setDefaultSteps() {
  const def = legacySamplingDefaults().steps;
  state.steps = def;
  $("#steps").value = def;
  $("#stepsVal").textContent = def;
}

// ---------- 【2026-11-11】首尾帧 / 参考生视频的采样参数 ----------
// 这两个模式走旧工作流路径，但采样链路与文生视频完全相同
// （BasicScheduler + KSamplerSelect + BasicGuider），
// 所以采样器 / 调度器 / shift 一样是真参数。
//
// 候选项来自 /api/video-options 的 sampling 清单 —— 服务端从本机
// ComfyUI 的 /object_info 取（KSamplerSelect 68 项 / BasicScheduler 14 项），
// 前端不硬编码名单。清单拉不到时降级为「不显示这两项」，
// 不阻塞其它功能。
let VIDEO_SAMPLING_OPTIONS = null;

function videoSamplingDefaults() {
  const cfg = (VIDEO_SAMPLING_OPTIONS && VIDEO_SAMPLING_OPTIONS.sampling) || {};
  return {
    samplers: cfg.samplers || [],
    schedulers: cfg.schedulers || [],
    cfg_fixed: cfg.cfg_fixed != null ? cfg.cfg_fixed : 1.0,
    cfg_reason: cfg.cfg_fixed_reason || "",
    shift_default_video: cfg.shift_default_video != null ? cfg.shift_default_video : 12.0,
    shift_default_audio: cfg.shift_default_audio != null ? cfg.shift_default_audio : 3.0,
    // 【2026-11-12】shift 节点本机可用性 —— 不可用时禁用滑杆（不做假控件）
    sigma_shift_available: cfg.sigma_shift_available !== false,
  };
}

/**
 * 旧路径视频模式提交时**真正用的** params.lora 值。
 *
 * 必须与提交路径（app.js 的 params.lora）完全一致：
 *   lora: state.model === "lora" ? state.lora : "none"
 * 「标准 MiniMax-H3」卡片也会把 params.lora 置成 "none"（= 不用 LoRA），
 * 所以判据不能只看 state.lora。
 */
function effectiveLegacyLora() {
  // 【2026-11-14】旧路径的加速 LoRA 现在也走**统一槽位**：
  //   state.model === "lora" -> 用 state.lora 那一枚
  //   state.model === "standard" -> 一枚都不挂（空串）
  // 旧的 "none" 字面量保留成空串之外不再使用。
  return state.model === "lora" ? (state.lora || "") : "";
}

/**
 * 旧路径视频模式的采样参数默认值。
 *
 * 【2026-11-12 修】必须与 workflows._h3_video_workflow 的硬编码档位
 * **逐项一致**，否则「推荐」标记是假的：
 *   lora != "none" -> steps 4  / sampler euler          / scheduler beta
 *   lora == "none" -> steps 20 / sampler res_multistep  / scheduler simple
 *
 * 之前 steps 默认写的是 6，而工作流实际用 4（Turbo 档）——
 * 面板上写「默认 6 步」，工作流跑的是 4 步 = 假提示。
 */
function legacySamplingDefaults() {
  const key = effectiveLegacyLora();
  const withLora = !!key;
  if (!withLora) {
    return {
      steps: 20,
      sampler: "res_multistep",
      scheduler: "simple",
    };
  }
  // 【2026-11-14】步数按**所选那枚**的推荐值 —— 不同加速 LoRA 的
  // 推荐步数不同（4 步蒸馏 / 8 步蒸馏），写死 4 就是假提示。
  const opts = (window.VIDEO_PANEL && window.VIDEO_PANEL.accelOptions)
    ? window.VIDEO_PANEL.accelOptions() : [];
  const meta = opts.find(o => o.key === key);
  return {
    steps: (meta && meta.recommended_steps) ? Number(meta.recommended_steps) : 4,
    sampler: "euler",
    scheduler: "beta",
  };
}

function fillVideoSelect(sel, values, current) {
  if (!sel) return;
  sel.innerHTML = "";
  values.forEach(name => {
    const opt = document.createElement("option");
    opt.value = name;
    opt.textContent = name;
    opt.selected = name === current;
    sel.appendChild(opt);
  });
  if (current && !values.includes(current)) {
    const opt = document.createElement("option");
    opt.value = current;
    opt.textContent = current;
    sel.appendChild(opt);
  }
  sel.value = current;
}

function renderLegacyVideoSampling() {
  const field = $("#videoSamplingField");
  if (!field) return;
  const cfg = videoSamplingDefaults();
  const defaults = legacySamplingDefaults();

  if (!state.vidSampler) state.vidSampler = defaults.sampler;
  if (!state.vidScheduler) state.vidScheduler = defaults.scheduler;

  fillVideoSelect($("#vidLegacySamplerSelect"), cfg.samplers, state.vidSampler);
  fillVideoSelect($("#vidLegacySchedulerSelect"), cfg.schedulers, state.vidScheduler);

  const sHint = $("#vidLegacySamplerHint");
  if (sHint) sHint.textContent = state.vidSampler === defaults.sampler ? "推荐" : "已改";
  const scHint = $("#vidLegacySchedulerHint");
  if (scHint) scHint.textContent = state.vidScheduler === defaults.scheduler ? "推荐" : "已改";

  const svEl = $("#vidLegacyShiftVideo");
  const saEl = $("#vidLegacyShiftAudio");
  if (svEl) svEl.value = state.vidShiftVideo;
  if (saEl) saEl.value = state.vidShiftAudio;
  const svVal = $("#vidLegacyShiftVideoVal");
  if (svVal) svVal.textContent = Number(state.vidShiftVideo).toFixed(2);
  const saVal = $("#vidLegacyShiftAudioVal");
  if (saVal) saVal.textContent = Number(state.vidShiftAudio).toFixed(2);
  const svHint = $("#vidLegacyShiftVideoHint");
  if (svHint) {
    svHint.textContent = Number(state.vidShiftVideo) === Number(cfg.shift_default_video)
      ? "推荐" : "已改";
  }
  const saHint = $("#vidLegacyShiftAudioHint");
  if (saHint) {
    saHint.textContent = Number(state.vidShiftAudio) === Number(cfg.shift_default_audio)
      ? "推荐" : "已改";
  }

  // 【2026-11-12】shift 节点本机不可用时禁用滑杆（不制造假控件）
  const shiftAvailable = cfg.sigma_shift_available !== false;
  [svEl, saEl].forEach(el => { if (el) el.disabled = !shiftAvailable; });
  if (svHint) {
    svHint.textContent = shiftAvailable ? svHint.textContent : "节点不可用";
    svHint.title = shiftAvailable ? "" : "本机 ComfyUI 没有 MiniMaxH3SigmaShift 节点";
  }
  if (saHint) {
    saHint.textContent = shiftAvailable ? saHint.textContent : "节点不可用";
    saHint.title = shiftAvailable ? "" : "本机 ComfyUI 没有 MiniMaxH3SigmaShift 节点";
  }

  // CFG：H3 标准路径固定 1.0 —— 只读说明，不做假控件
  const cfgEl = $("#vidLegacyCfg");
  const cfgVal = $("#vidLegacyCfgVal");
  const cfgHint = $("#vidLegacyCfgHint");
  const cfgNote = $("#vidLegacyCfgFixedNote");
  if (cfgEl) cfgEl.value = Number(cfg.cfg_fixed).toFixed(1);
  if (cfgVal) cfgVal.textContent = Number(cfg.cfg_fixed).toFixed(1);
  if (cfgHint) cfgHint.textContent = "H3 固定";
  if (cfgNote) cfgNote.textContent = cfg.cfg_reason;

  // 步数的「推荐 / 已改」提示 —— 文生视频面板有 vidStepsHint，
  // 这里必须也有（否则用户看不到步数是不是被改过）。
  const stepsHint = $("#vidLegacyStepsHint");
  if (stepsHint) {
    stepsHint.textContent = Number(state.steps) === Number(defaults.steps)
      ? "推荐" : "已改";
  }

  const note = $("#vidLegacySamplingNote");
  if (note) {
    note.textContent = [
      `步数 ${state.steps} → BasicScheduler.steps`,
      `采样器 ${state.vidSampler} → KSamplerSelect.sampler_name`,
      `调度器 ${state.vidScheduler} → BasicScheduler.scheduler`,
      `shift ${Number(state.vidShiftVideo).toFixed(2)} / `
        + `${Number(state.vidShiftAudio).toFixed(2)} → MiniMaxH3SigmaShift`,
    ].join(" · ");
  }
}

/**
 * 拉一次 /api/video-options，只为拿 sampling 清单（首尾帧/参考模式用）。
 *
 * 【2026-11-12 修】这个 fetch 是异步的，而 boot() 不 await 它。
 * 用户在 fetch 返回前切到 flf/r2v 时：
 *   renderLegacyVideoSampling() 拿到的 VIDEO_SAMPLING_OPTIONS 还是 null
 *   -> 候选清单是空数组 -> 下拉只剩 1 个当前值（不是 68 / 14 个候选）
 * 而且 fetch 完成后没人重渲染 -> 残缺的下拉一直留着。
 * 修法：拿到清单后主动重渲染一次 legacy 面板。
 */
async function loadVideoSamplingOptions() {
  try {
    const resp = await fetch("/api/video-options", { cache: "no-store" });
    if (!resp.ok) return;
    VIDEO_SAMPLING_OPTIONS = await resp.json();
    const cfg = videoSamplingDefaults();
    if (!state.vidShiftVideo) state.vidShiftVideo = cfg.shift_default_video;
    if (!state.vidShiftAudio) state.vidShiftAudio = cfg.shift_default_audio;
    // 清单到位后重渲染（补上「fetch 未返回就先切模式」造成的残缺下拉）
    if (state.mode === "flf" || state.mode === "r2v") renderLegacyVideoSampling();
  } catch (e) {
    console.warn("[视频采样参数] 清单拉取失败:", e.message);
  }
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
//
// 【2026-11-13 队列化改造】
// 旧实现：生成任务一提交，state.genBusy=true -> 生成按钮 disabled + 文案
// 「生成中…」-> 用户**无法继续添加**新任务（界面看起来像卡死）。
//
// 新实现：生成走**队列**，提交后按钮不再禁用，只把文案换成
// 「已入队（队列 N）」，用户可以连续提交多个任务，它们依次排队执行。
// 真正需要禁用按钮的只剩「提示词增强中」—— 增强会改写提示词，
// 必须等它结束再提交，否则提交的是半成品提示词。
//
// 队列状态由 web/generation-queue.js 提供（它轮询 /api/queue），
// 队列变化时它回调 onChange -> 这里重算按钮态。
function updateActionButtons() {
  const enhanceBusy = promptEnhance.busy || promptVersions.busy;
  const genBtn = $("#genBtn");
  const enhBtn = $("#enhanceBtn");
  const enhText = $("#enhanceBtnText");
  const menuBtn = $("#enhanceMenuBtn");
  // 队列里待处理的任务数（含正在跑的那条）
  const queueCount = window.GenerationQueue
    ? window.GenerationQueue.pendingCount : 0;
  if (genBtn) {
    // 【关键】队列非空时**不禁用**按钮 —— 用户要能继续提交。
    genBtn.disabled = enhanceBusy;
    genBtn.classList.toggle("busy", false);
    genBtn.classList.toggle("queued", queueCount > 0);
    genBtn.textContent = queueCount > 0
      ? `已入队（队列 ${queueCount}）` : "生成";
    genBtn.title = queueCount > 0
      ? `当前队列 ${queueCount} 个任务（队首正在跑）。点击可继续加入队列。`
      : "提交到生成队列（可连续提交多个任务）";
  }
  if (enhBtn) {
    enhBtn.disabled = enhanceBusy;
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
  //
  // 【2026-11-13 队列化改造】原来这里用的是 `busy`（增强中 **或** 生成中）。
  // 生成改成队列后不再有「生成中」这个阻塞态，所以只按「增强中」禁用 ——
  // 否则任务在队列里跑着的时候，增强方式的下拉会被莫名其妙地锁住。
  if (menuBtn) menuBtn.disabled = enhanceBusy;
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

// ---------- 生成任务跟踪（2026-11-13 队列化改造） ----------
//
// 【旧行为】提交后 state.genBusy=true -> 生成按钮 disabled + 「生成中…」，
// 任务到达终态才恢复。用户在这期间**无法继续添加**新任务。
//
// 【新行为】后端本来就有真正的队列（建任务即 status='queued'，Worker 串行
// 取任务执行），所以前端**不需要**再自己维护一套「阻塞中」状态：
//   * 提交成功后只登记一下，刷新队列视图；
//   * 按钮的禁用与否交给 updateActionButtons()（只有「提示词增强中」才禁）；
//   * 队列的进度 / 顺序 / 状态由 web/generation-queue.js 轮询 /api/queue 呈现。
//
// 保留 pendingJobs 只是为了在「任务结束」时刷新一次任务列表 ——
// 队列面板每 1 秒自己会刷，但任务列表是 2 秒一轮，节奏不同。
const pendingJobs = new Set();
const JOB_POLL_MAX_MS = 30 * 60 * 1000;
let jobPollStartedAt = 0;

function trackJob(jobId) {
  if (!jobId) return;
  pendingJobs.add(jobId);
  if (!jobPollStartedAt) jobPollStartedAt = Date.now();
  // genBusy 只表示「本会话还有任务没到终态」——
  // **不参与按钮的禁用判断**（见 updateActionButtons 的说明）。
  state.genBusy = true;
  // 队列面板刷新（顺带把按钮文案更新成「已入队（队列 N）」）
  if (window.GenerationQueue) window.GenerationQueue.refresh();
  pollPendingJobs();
}

async function pollPendingJobs() {
  if (!pendingJobs.size) {
    jobPollStartedAt = 0;
    state.genBusy = false;
    return;
  }
  // 安全上限：极端情况下（后端不回写状态）也不要永久挂着轮询
  if (Date.now() - jobPollStartedAt > JOB_POLL_MAX_MS) {
    pendingJobs.clear();
    jobPollStartedAt = 0;
    state.genBusy = false;
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
  if (pendingJobs.size) setTimeout(pollPendingJobs, 1500);
}

// ---------- 提交 ----------
$("#genBtn").addEventListener("click", async () => {
  // 【2026-11-13 队列化改造】不再因为「有任务在跑」而拒绝提交 ——
  // 队列的意义就是让用户能连续添加。只有「提示词增强中」才拦
  // （增强会改写提示词，提交半成品提示词没有意义）。
  if (promptEnhance.busy || promptVersions.busy) return;
  // 防连点：极短时间内的重复点击直接忽略（否则队列里会多出重复任务）
  if (window.GenerationQueue && !window.GenerationQueue.shouldAcceptSubmit()) return;
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
    seed: $("#seed").value.trim() || null,
  };
  // 【2026-11-12 修 —— 根因之一】旧实现对**所有模式**无条件写
  //   lora: state.model === "lora" ? state.lora : "none"
  //   lora_strength: state.loraStrength
  // 而 state.lora / state.loraStrength 是**旧面板的遗留全局值**
  // （恒为 "ckpt850" / 1.0）。
  //
  // 文生视频新面板有自己的模型与加速 LoRA（model_key / accel_loras），
  // 但这段遗留赋值照样把 `lora = "ckpt850"` 一起提交、一起落库 ——
  // 于是落库记录里同时躺着「正确的 accel_loras」与「错误的 lora=ckpt850」，
  // 任何读 p.lora 的地方（卡片、Agent、导出）都会显示成 ckpt850。
  //
  // 修法：**只让真正走旧路径的模式**（flf / r2v）带这两个字段；
  // 其它模式（t2v / 图像 / 音乐）不写 —— 让落库记录里只剩真实生效的字段。
  if (mode === "flf" || mode === "r2v") {
    // 【2026-11-14】旧路径（首尾帧 / 参考生视频）也走**统一槽位**：
    // 提交 accel_lora（key）+ accel_lora_strength，
    // 服务端按同一套换算逻辑决定挂哪枚、用哪个节点加载。
    //
    // 仍然保留 params.lora —— 老任务 / 老读侧（job_meta）还要看它，
    // 但它是**从槽位派生的**，不再是独立的输入源。
    const accelKey = effectiveLegacyLora();
    params.accel_lora = accelKey;
    params.accel_lora_strength = state.loraStrength;
    params.lora = accelKey || "none";
    params.lora_strength = state.loraStrength;
  }
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
    if (isH3ImageMode()) {
      // MiniMax-H3-Image
      //
      // 【负面提示词】guidance 固定 1 -> 无 CFG pass -> 数学上无效。
      // 前端直接**不提交**（而不是提交一个无效值）——
      // 服务端也会再丢一次并回一条说明，双保险。
      delete params.negative_prompt;
      // 【LoRA】官方暂未发布 H3-Image 的 LoRA -> 不提交 loras。
      // （提交空数组也行，但显式删掉更清楚：落库记录里不会留一个
      //   「看起来支持 LoRA 其实不支持」的空字段。）
      delete params.loras;
    }
  }
  if (mode === "t2v" && window.VIDEO_PANEL && window.VIDEO_PANEL.isReady()) {
    // 文生视频新面板：宽高 / 模型 / 加速 LoRA 组合 / 注意力 / 二采 / 采样参数
    Object.assign(params, window.VIDEO_PANEL.submitParams());
    // 【接续】把接续上下文一起提交（服务端据此校验并算段号）。
    //
    // 【为什么这里原样透传而不自己算段号】前端回填的 continuation
    // 描述的是**上一段**；本段是新的一段，段号必须 +1 —— 那只能由
    // 服务端按任务记录算（前端改个数字就能让接缝错位）。
    if (window.Continuation && window.Continuation.state) {
      const ctx = window.Continuation.state().current;
      if (ctx && ctx.continuation) {
        // 【2026-11-12 修】提交前的**最后一道闸**：接续上下文与当前
        // 面板尺寸必须一致。潜空间桥逐 token 拷贝上一段尾部，latent
        // 分辨率固定 —— 跨分辨率接续会被 Relay-Kit 直接拒绝
        // （「拷贝桥禁止跨分辨率：上段 WxH ≠ 本段 WxH」）。
        //
        // 用户**自己改尺寸**（手填宽高）也能造成同样的不一致，
        // 所以「复用参数时清 context」之外还要在这里拦一次。
        // 不一致就当场提示并中止提交 —— 不浪费一次生成。
        if (typeof window.Continuation.checkSubmitConsistency === "function") {
          const size = window.VIDEO_PANEL._debug
            ? window.VIDEO_PANEL._debug.currentSize()
            : null;
          if (!window.Continuation.checkSubmitConsistency(size)) return;
        }
        params.continuation = ctx.continuation;
      }
    }
  } else if (mode === "flf" || mode === "r2v") {
    // 【2026-11-11】首尾帧 / 参考生视频：采样参数（采样器 / 调度器 / shift）
    // 与文生视频同链路，同样是真参数。CFG 在 H3 标准路径固定 1.0
    // （BasicGuider 无 cfg 槽位），面板只做只读说明，不提交。
    params.sampler = state.vidSampler;
    params.scheduler = state.vidScheduler;
    params.shift_video = Number(state.vidShiftVideo);
    params.shift_audio = Number(state.vidShiftAudio);
    // 【2026-11-14】音轨三选一（B2）
    params.audio_track_source = state.audioTrackSource || "";
  }
  // 图片编辑的参考图：**必须来自素材库**。
  // 这里只提交素材库条目 ID + 描述 + 标签 + 顺序，
  // 服务端据此把素材库文件上传到 ComfyUI，并按 <imageN> 顺序接线。
  if (mode === "i2i_qwen" || mode === "i2i_h3image") {
    // 上限按模型族取：Qwen 10 张 / H3-Image 16 张（官方能力不同）
    const max = mode === "i2i_h3image"
      ? (H3IMAGE_OPTIONS.maxRefImages || 16)
      : (QWEN_OPTIONS.maxRefImages || 10);
    const refList = window.QwenRefs ? window.QwenRefs.getRefs() : [];
    if (!refList.length) {
      alert("图片编辑的参考图必须来自素材库：请点「从素材库添加」选择至少 1 张。");
      return;
    }
    if (refList.length > max) {
      alert(`参考图最多 ${max} 张，当前 ${refList.length} 张。`);
      return;
    }
    // 提交前一致性校验：占位标记与参考图数量必须匹配。
    // 【占位语法随模型族切换】Qwen 是 <imageN>，H3-Image 是 <Picture N>
    // —— 参考图模块已通过 setProfile 切到对应语法，这里直接调 validate()。
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
    // 【2026-11-13 队列化改造】**不再清空**参考图/参考帧选择。
    //
    // 【为什么】旧行为在提交后清空文件槽位，是配合「提交一次就锁死按钮」
    // 的阻塞式交互 —— 用户反正只能提交一次，清掉也就无所谓。
    // 改成队列后，用户要能**连续提交多个任务**；如果第二次提交时
    // 参考帧槽位已经被清空，flf / r2v 模式会直接被前置校验拦下
    // （「首尾帧模式需要上传首帧图片」），连续提交根本走不通。
    //
    // 提示词本来就一直保留（便于连做），参考帧同理。
    // 素材库引用（assetRefs）同样保留 —— 它只是 id，不会失效。
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

// ---------- 模型 / LoRA 显示（服务端摘要 = 唯一真源） ----------
//
// 【2026-11-12 修 —— 用户报出的核心缺陷】
// 旧实现 modelLabel(p) 读的是**遗留字段** p.model / p.lora：
//   return `H3 + ${p.lora === "ckpt850" ? "ckpt850" : "v4-step600"} · ${steps}步`;
// 而新文生视频面板提交的是 model_key / accel_loras / fixed_accel_lora，
// 于是用户选了「混合版 + DMAD 4 步（critic 版）」，卡片却恒显
// 「H3 + ckpt850 · 4步」、LoRA 那一行永远是「空」。
//
// 现在：**只读服务端算好的 job.model_info**（h3studio/job_meta.py）。
// 服务端按任务真实 params 算摘要 -> 面板所选 == 落库记录 == 卡片显示，
// 三者同源，不会再各说各话。
//
// 服务端没给 model_info 的老数据（旧服务端 / 离线快照）走本地兜底，
// 兜底也**优先读新字段**（model_key / accel_loras），不再只认遗留字段。
function jobModelInfo(job) {
  const p = (job && job.params) || {};
  const info = (job && job.model_info) || null;
  if (info && (info.model_label || info.lora_label || info.model_key)) return info;
  return localModelInfo(job.mode || "", p);
}

// 本地兜底：与服务端 job_meta.summarize 同语义，但只用前端已有的清单。
// 只在服务端没下发 model_info 时才会走到。
//
// 【坑】清单在 web/video-panel.js 里是 **IIFE 内部的 const VIDEO_OPTIONS**，
// 不是全局 —— 直接写 VIDEO_OPTIONS 会 ReferenceError，把整个 app.js 带崩
// （实测表现为「骨架卡片不出现」，因为渲染在 boot() 里被静默吞掉）。
// 正确入口是 window.VIDEO_PANEL.options（video-panel.js 导出的同一份对象）。
function videoOptions() {
  const panel = window.VIDEO_PANEL;
  return (panel && panel.options) || { models: {}, accel_lora_combos: [],
                                      fixed_accel_loras: [] };
}

function localModelInfo(mode, p) {
  const out = {
    model_key: "", model_label: "", model_unet: "",
    lora_label: "", lora_strength: null, lora_name: "",
    fixed_lora_label: "", fixed_lora_name: "", fixed_lora_strength: null,
    source: "local_fallback",
  };
  if (mode === "t2v" && p.model_key) {
    const opts = videoOptions();
    const entry = (opts.models || {})[p.model_key];
    out.model_key = p.model_key;
    out.model_label = entry ? (entry.label || p.model_key) : p.model_key;
    const combos = opts.accel_lora_combos || [];
    for (const item of (p.accel_loras || [])) {
      if (!item || item.enabled === false) continue;
      const combo = combos.find(c => c.key === item.key);
      if (!combo) continue;
      out.lora_label = combo.display || item.key;
      out.lora_strength = Number(item.strength);
      out.lora_name = combo.name || "";
      break;
    }
    const fixed = (opts.fixed_accel_loras || [])
      .find(f => f.key === p.fixed_accel_lora);
    if (fixed) {
      out.fixed_lora_label = fixed.display || "";
      out.fixed_lora_name = fixed.name || "";
    }
    // 效果 LoRA（可多选）—— 服务端没给 model_info 时（本地兜底路径）
    // 从 params 直接读，标签用文件名（服务端权威清单已算好的 label 优先）。
    out.effect_lora_labels = (p.effect_loras || [])
      .filter(it => it && it.name && it.enabled !== false
                    && Number(it.strength) > 0)
      .map(it => ({
        name: it.name,
        label: it.display || it.name,
        strength: Number(it.strength),
        trigger: it.trigger || "",
      }));
    return out;
  }
  // 旧路径视频：model = lora / standard，lora = ckpt850 / v4step600 / none
  if (mode === "t2v" || mode === "flf" || mode === "r2v" || mode === "longfilm") {
    const steps = p.steps || (p.model === "standard" ? 20 : 4);
    if (p.model === "standard") {
      out.model_label = `标准 H3 · ${steps}步`;
    } else if (p.model === "lora") {
      out.model_label = `H3 + ${p.lora || "ckpt850"} · ${steps}步`;
    } else if (p.model) {
      out.model_label = p.model;
    } else {
      // 没记录 model -> 工作流走默认档（workflows._h3_video_workflow）。
      // 底模按 mode 区分：t2v/flf 用 fl2va，r2v 用 ref2va
      // （workflows.build_r2v:477 `unet_name: UNET["ref2va"][precision]`）。
      const precision = p.precision || "int8";
      const ref2va = mode === "r2v";
      out.model_label = ref2va
        ? `MiniMax-H3 Ref2VA（${precision}，默认档）`
        : `MiniMax-H3 FL2VA（${precision}，默认档）`;
      out.model_key = ref2va ? "ref2va" : "fl2va";
    }
    if (p.lora && p.lora !== "none") {
      out.lora_label = p.lora;
      out.lora_strength = Number(p.lora_strength ?? 1);
    } else if (!p.lora) {
      // 没记录 lora -> 工作流默认 ckpt850（且确实挂上了）
      out.lora_label = "ckpt850";
      out.lora_strength = 1;
    }
    return out;
  }
  if (mode === "t2i" || mode === "t2i_qwen" || mode === "i2i_qwen"
      || mode === "t2i_moss" || mode === "t2i_h3image" || mode === "i2i_h3image") {
    out.model_label = imgModelLabel(p.model);
    out.model_key = p.model || "";
    const loras = (p.loras || []).filter(l => l && l.enabled !== false
      && Number(l.strength) > 0);
    if (loras.length) {
      out.lora_label = loras.map(l => `${l.display || l.name} ×${Number(l.strength).toFixed(2)}`).join("、");
      out.lora_strength = Number(loras[0].strength);
      out.lora_name = loras[0].name || "";
    }
    return out;
  }
  if (mode === "t2a" || mode === "a2a") {
    out.model_label = musicModelLabel(p.checkpoint);
    out.model_key = p.checkpoint || "";
    return out;
  }
  return out;
}

// 「LoRA」一行的 HTML。
//
// 【2026-11-13 改 —— 需求 C1】
// 用户截图证据：媒体库「文生视频」卡片的 LoRA 行显示
//   integrated_multimodal_description
// 那是 H3 三段式提示词的**字段名**，根本不是 LoRA；该任务实际也没用它。
//
// 根因：取值时**按位置/模糊匹配**，而不是按明确 key 读。
//   旧实现把 `params.lora` 原样当 LoRA 名，而 params.lora 这条链路
//   **没有白名单校验**（落库不校验，Agent 校验只守 Agent 工具入口）。
//
// 现在：LoRA 只能来自 `job.model_info`（服务端按白名单算好的权威摘要），
// 取值口径统一在 web/job-meta.js（纯函数，可单测）。
//
// 【2026-11-15 文案统一】没有 LoRA 时显示「未使用 LoRA」（用户明确要求：
// 「缺失时应显示『未使用 LoRA』而不是错名」）。
// 服务端 gen_params.lora 已经给的是这个文案；这里的兜底同步，
// 避免任何一条路径漏出去又变回「无」。
function loraRowHtml(loraText) {
  const value = (loraText === undefined || loraText === null || loraText === "")
    ? "未使用 LoRA" : String(loraText);
  return `<div class="row"><span>LoRA</span>`
    + `<b title="${escapeHtml(value)}">${escapeHtml(value)}</b></div>`;
}

// ------------------------------------------------------------------
// C2：卡片补齐关键生成参数（2026-11-13）
// ------------------------------------------------------------------
//
// 【为什么要有】用户 2026-11-13 报出 BUG 2：卡片不显示步数等参数。
//
// 【取值纪律 —— 唯一真源在服务端】
// 所有值都读服务端下发的 job.gen_params（h3studio/job_meta.gen_params）。
// 前端**不自己拼** —— 因为：
//   * 步数在 params.steps，但视频旧路径工作流会按 lora 档位给默认值；
//   * shift 视频是 shift_video / 音频是 shift_audio，图像是 shift；
//   * 时长音乐是 max_duration，视频图像是 duration；
//   * 尺寸优先用「产物实际像素」，不是请求值。
// 这些差异全在服务端算好了。
//
// 【缺失 = 「—」】
// gen_params 里缺失的字段是空串 —— 这里显示「—」，
// **不拿别的字段顶替**（这是本次需求的硬要求）。
function paramOrDash(value) {
  const text = String(value == null ? "" : value).trim();
  return text || "—";
}

// 卡片的「生成参数」分组行。
// 返回值是**整组 HTML**（一个 <div class="row-group"> 里多行）——
// 这样卡片上多出来的行能整体折叠/统一排版，不打乱既有的 .row 样式。
//
// 【为什么用分组而不是一堆散行】
// 用户报的是「显示区域有限」—— 卡片上要显示的东西变多了。
// 分成「基础（模式/分辨率/时长/种子/耗时）」+「参数（模型/LoRA/采样/步数）」
// 两组，视觉上更紧凑，也给「详情」按钮留出位置。
function genParamRowsHtml(job) {
  const gp = job.gen_params || null;
  const p = job.params || {};
  const info = jobModelInfo(job);
  const rows = [];
  // 模型：gen_params 里没有（老服务端）时退回既有的 modelLabel()
  const modelText = gp ? gp.model : modelLabel(job);
  rows.push(["模型", paramOrDash(modelText)]);
  // LoRA：BUG 1 的修复落点 —— 只显示真实用到的 LoRA。
  // 【关键】服务端 gen_params.lora 已经是白名单判定过的结果
  // （job_meta.resolve_job_lora），没有就是字符串「无」。
  // 前端**不再自己猜** lora 字段，也**不拿 prompt 顶替**。
  const loraText = gp ? gp.lora : loraLabel(job);
  rows.push(["LoRA", paramOrDash(loraText)]);
  // 采样器 / 调度器
  rows.push(["采样器", paramOrDash(gp ? gp.sampler : p.sampler)]);
  rows.push(["调度器", paramOrDash(gp ? gp.scheduler : p.scheduler)]);
  // 步数/CFG —— **标签沿用基线的「步数/CFG」**。
  //
  // 【为什么不改成「步数」】基线卡片上这一行的标签就叫「步数/CFG」，
  // 而 tests/test_status_realtime.py::test_library_renders_status_and_params_of_new_entry
  // 断言卡片 meta 里含「步数/CFG」。改成「步数」会把这条既有契约打破，
  // 属于本次需求（补参数）之外的**回归**。
  // 值里同时给出 CFG —— 基线那一行本来就是「步数 · CFG」两值，
  // 这里保留这个语义，缺失的 CFG 显式渲染成「—」（不拿别的字段顶替）。
  const stepsVal = gp ? gp.steps : p.steps;
  const cfgVal = gp ? gp.cfg : p.cfg;
  rows.push(["步数/CFG", `${paramOrDash(stepsVal)} · ${paramOrDash(cfgVal)}`]);
  // 尺寸：卡片已有「分辨率」一行（带比例），这里只补纯像素尺寸
  rows.push(["尺寸", paramOrDash(gp ? gp.size : "")]);
  // 时长
  rows.push(["时长", paramOrDash(gp ? gp.duration : p.duration)]);
  // seed —— 沿用基线标签「种子」
  rows.push(["种子", paramOrDash(gp ? gp.seed : p.seed)]);
  return `<div class="row-group gen-params">`
    + rows.map(([label, value]) =>
        `<div class="row"><span>${escapeHtml(label)}</span>`
        + `<b title="${escapeHtml(value)}">${escapeHtml(value)}</b></div>`).join("")
    + `</div>`;
}

// 「模型」一行的文案。
// 服务端摘要里的 model_label 已经是**面板下拉里的原文**（如
// 「MiniMax-H3 混合版 · FL2VA+Ref2VA」），直接用，不自己拼。
function modelLabel(job) {
  const info = jobModelInfo(job);
  const p = (job && job.params) || {};
  const steps = p.steps;
  const label = info.model_label || "—";
  if (!label || label === "—") return "—";
  // 旧路径摘要里已经带了步数（服务端已拼好），新路径不带 -> 这里补上。
  if (/\d+\s*步/.test(label)) return label;
  return steps ? `${label} · ${steps}步` : label;
}

// 「注意力机制」一行的文案（2026-11-12 新增）。
//
// 【为什么要有】用户要求「媒体库/生成库标注所用注意力机制」——
// 每个视频条目要显示生成时**实际使用的**注意力机制。
// 文案取**服务端摘要**（job.model_info.attention_label）——
// 它与面板下拉里的 label 逐字同源（h3studio/job_meta.py 的
// _attention_label -> video_models.VIDEO_ATTENTION_MODES），
// 所以不会出现「面板写 Sol-Attn、卡片写 sol」这种对不上的情况。
//
// 老任务没有记录注意力机制 -> label 为空 -> **整行不渲染**
// （不显示「空」，也不编一个）。
function attentionLabel(job) {
  // 【2026-11-13】取值口径统一到 web/job-meta.js（与详情模态框一致）。
  const meta = window.JOB_META;
  if (meta && typeof meta.attentionText === "function") return meta.attentionText(job);
  const info = jobModelInfo(job);
  const label = (info && info.attention_label) || "";
  if (!label) return "";
  const speed = info.attention_speed_label || "";
  return speed ? `${label} · ${speed}` : label;
}

function attentionRowHtml(job) {
  const text = attentionLabel(job);
  if (!text) return "";
  const p = (job && job.params) || {};
  const note = p.attention === "sol_scheduled"
    ? "（该机制已下线：实测画面有叠影）" : "";
  return `<div class="row"><span>注意力</span>`
    + `<b title="${escapeHtml(text + note)}">${escapeHtml(text + note)}</b></div>`;
}

// 「LoRA」一行的文案。
//
// 【语义 —— 2026-11-12】文生视频面板上有**两个** LoRA 概念：
//   * 「加速 LoRA」下拉 —— **用户的选择**（单选，带强度）
//   * 工作流固定的那枚 Turbo LoRA —— 常驻，不在可勾选列表里
// 所以这一行要同时说清两件事：
//   1. 用户选的那枚（带强度）；**没选时显示「空」**
//      —— 需求原文「未选 LoRA 时才显示「空」」。
//   2. 工作流固定的那枚（标明是「工作流固定」，免得被误读成用户选的）。
//
// 这样卡片显示与 ComfyUI prompt 里的 LoRA 节点**严格对应**：
// prompt 里挂了几枚，这里就列几枚。
//
// 【为什么「空」只给视频模式】图像面板的「效果 LoRA」本来就是
// 「有就加、没有就不加」的可选列表，没加是常态 —— 给每张图卡都加一行
// 「LoRA：空」是噪声。视频面板的加速 LoRA 是一个**明确的档位选择**
//（面板上有那个下拉），显示「空」才是在回答「用户选了哪档」。
function loraLabel(job) {
  // 【2026-11-13 改 —— 需求 C1】取值口径统一到 web/job-meta.js：
  // LoRA **只能**来自服务端算好的 model_info（服务端已按白名单挡掉脏值），
  // 前端不再从 params 里猜。没有 LoRA 时如实显示「无」。
  const meta = window.JOB_META;
  if (meta && typeof meta.loraText === "function") return meta.loraText(job);
  // 兜底（job-meta.js 未加载时的降级）——同样只读 model_info。
  const info = jobModelInfo(job);
  const parts = [];
  // 【2026-11-13 合并】前端**兜底**白名单（media-card-detail 引入）。
  //
  // 【为什么服务端已经挡住了、这里还要再挡一层】
  // 主路径走 window.JOB_META.loraText(job) —— 服务端已按白名单算好，
  // 脏值不可能出现。但这段兜底代码在 job-meta.js **未加载**时会被用到，
  // 而 job-meta.js 缺失意味着前端拿到的 model_info 可能来自**旧服务端**
  // （没有白名单闸门）—— 那时 lora_label 仍可能是提示词原文。
  // 所以这里再按「来源」判一次：结构化字段（accel_loras / loras）天然可信；
  // 遗留 params.lora 路径必须过「key 形态」白名单。
  //
  // 【为什么不能一刀切地看字符串形态】真实的 LoRA 显示名**带空格和全角括号**
  // （"DMAD 4 步（critic 版）"、"Turbo 4 步 · ckpt850（工作流固定）"）——
  // 按「不含空格」去判会把真 LoRA 也误杀。
  const mode = (job && job.mode) || "";
  const isVideo = mode === "t2v" || mode === "flf" || mode === "r2v"
                  || mode === "longfilm";
  const p = (job && job.params) || {};
  const structured = Array.isArray(p.accel_loras)
    && p.accel_loras.some(item => item && item.enabled !== false && item.key);
  if (info.lora_label) {
    if (!isVideo || structured || isLoraKeyLike(info.lora_label)) {
      parts.push(info.lora_strength != null
        ? `${info.lora_label} ×${Number(info.lora_strength).toFixed(2)}`
        : info.lora_label);
    }
  }
  if (info.fixed_lora_label) {
    parts.push(`工作流固定：${info.fixed_lora_label}`);
  }
  return parts.length ? parts.join(" · ") : "无";
}

// 「这段文本长得像一个 LoRA **key** 吗？」（前端兜底白名单）
//
// 【只对「遗留 params.lora」这条路径生效】
// 遗留字段的值是 workflows.LORAS 的键（"ckpt850" / "v4step600"），
// 或者权重文件名（如 "minimax_h3_turbo_4step_ema_ckpt850.safetensors"）——
// 形态是**纯 ASCII 的标识符 / 文件名**。
//
// 而提示词形态完全相反：含空格、含中文、含标点、长。
// 所以判据是「只允许 ASCII 标识符字符」。
//
// 【为什么不查清单】前端拿不到「真实 LoRA 清单」的全量（不同族的清单
// 在 IMG_LORAS / QWEN_LORAS / MOSS_LORAS / videoOptions 里）。
// 清单判据留给服务端；前端只做**形态**判据 ——
// 足够挡住「提示词冒充 LoRA」这个具体缺陷，且不会误杀正常 LoRA 名。
function isLoraKeyLike(text) {
  const value = String(text == null ? "" : text).trim();
  if (!value) return false;
  if (value.length > 120) return false;
  // LoRA key / 权重文件名：ASCII 字母数字 + 下划线 + 点 + 斜杠 + 连字符
  return /^[A-Za-z0-9_.\/-]+$/.test(value);
}

// 图像模式：模型版本的可读名（清单来自 /api/image-options）
function imgModelLabel(key) {
  // 三族模型各自的清单分开 —— key 在别的族里查不到就回落到原 key
  const m = IMG_OPTIONS.models[key] || MOSS_OPTIONS.models[key]
            || QWEN_OPTIONS.models[key] || H3IMAGE_OPTIONS.models[key];
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

// ---------- 分辨率 / 比例标签（前端兜底） ----------
//
// 【与后端 h3studio/reso_label.py 的关系】
// 后端已经在 /api/jobs 的每条任务里下发 `resolution_info`（唯一真源）。
// 这个函数只在**服务端是旧版本**（没有该字段）时兜底 ——
// 此时还没有产物实际像素可用，就按请求的 width/height 算。
//
// 【为什么不能继续读 params.resolution】
// 它是个**档位字符串**（"544p" / "1mp"），与像素是两条独立的语义：
// 同一个 "1mp" 在 16:9 下是 1360×768、1:1 下是 1024×1024、21:9 下是 1568×672。
// 文生视频新面板压根不写这个字段，它永远停在遗留默认值 "544p" ——
// 于是实际跑 1344×768 的任务，卡片显示成「544p · 16:9」。
const RESO_KNOWN_ASPECTS = ["1:1", "2:3", "3:2", "3:4", "4:3", "9:16", "16:9", "21:9"];
// 比例匹配容差 = max(固定容差, 量化步长 / 短边)。
//
// 量化误差来自「宽高各自吸附到 16/32 的倍数」：短边越小，
// 同一个名义比例算出来的真实比例偏差越大。
// 例：16:9 的 0.2MP 预设 608×352，真实比例 1.727，
// 相对名义 1.778 差 2.8% —— 超过固定 2% 就会被误判成 19:11。
const RESO_ASPECT_TOLERANCE = 0.02;
const RESO_ASPECT_QUANTUM = 32;

function resoTierLabel(w, h) {
  // 档位名 = 短边像素，**原样用**不四舍五入。
  // 官方预设的短边是 352/416/480/544/608/640/672/736/768 ——
  // 其中 416/544/608/736/768 都不是 10 的倍数，
  // 四舍五入会让 768 变成 770，与面板自己的预设标签对不上。
  return `${Math.min(w, h)}p`;
}

function resoAspectLabel(w, h, preferred) {
  const short = Math.min(w, h);
  const tolerance = Math.max(RESO_ASPECT_TOLERANCE, RESO_ASPECT_QUANTUM / short);
  const actual = w / h;
  const candidates = [];
  for (const name of RESO_KNOWN_ASPECTS) {
    const [wr, hr] = name.split(":").map(Number);
    const err = Math.abs(actual - wr / hr) / (wr / hr);
    if (err <= tolerance) candidates.push({ err, name });
  }
  if (candidates.length) {
    // 请求的比例优先（只要也在容差内）—— 量化误差不该改写用户的意图
    if (preferred) {
      const hit = candidates.find(c => c.name === preferred);
      if (hit) return hit.name;
    }
    candidates.sort((a, b) => a.err - b.err);
    return candidates[0].name;
  }
  const gcd = (a, b) => b ? gcd(b, a % b) : a;
  const d = gcd(w, h) || 1;
  return `${w / d}:${h / d}`;
}

// 按请求参数算标签（没有产物实际像素时的兜底）。
function localResolutionInfo(p) {
  p = p || {};
  const w = Number(p.width) || 0;
  const h = Number(p.height) || 0;
  if (!w || !h) {
    // 两个尺寸都没有 -> 只把请求的档位/比例原样带出，不编造尺寸
    const tier = String(p.resolution || "");
    const aspect = String(p.aspect || "");
    return {
      label: [tier, aspect].filter(Boolean).join(" · "),
      note: "", hasSize: false, width: null, height: null,
    };
  }
  const tier = resoTierLabel(w, h);
  const aspect = resoAspectLabel(w, h, String(p.aspect || ""));
  return {
    label: `${tier} · ${w}×${h} · ${aspect}`,
    note: "", hasSize: true, width: w, height: h,
    tier, aspect, pixels: `${w}×${h}`,
  };
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
          failed ? friendlyError(batch.error).slice(0, 60)
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
  //
  // 【2026-11-11】尺寸取**批次产物里的实际像素**（服务端已把 width/height
  // 写进 outputs 条目），没有就退回请求的 width/height。
  // 原来只看请求值 —— 走放大链时请求 2560×1400、实际出 2560×1408，
  // 摘要会写错。
  let size = "";
  for (const batch of batches) {
    const hit = (batch.outputs || []).find(o => o && o.width && o.height);
    if (hit) { size = ` · ${hit.width}×${hit.height}`; break; }
  }
  if (!size && p.width && p.height) size = ` · ${p.width}×${p.height}`;
  return `<button type="button" class="job-batches-toggle ${open ? "open" : ""}" data-act="toggle-batches" aria-expanded="${open ? "true" : "false"}">
      <span class="caret">${batchCaret(open)}</span> 批次结果（${summary}${size}）
    </button>
    <div class="job-batches" data-batches ${open ? "" : "hidden"}>${cells}</div>`;
}

/**
 * 「复用参数」的配置解析 —— **纯函数**（无 DOM 依赖），供单测直接调用。
 *
 * 【为什么要抽出来】之前这段逻辑埋在 jobCard 的点击回调里，
 * 测试只能覆盖 applyConfig 层，**处理器本身零覆盖** ——
 * 于是「把宽高删掉」这种回归 60 条测试仍然全绿（独立验收实测）。
 * 抽成纯函数后，单测可以喂真实形状的 job/params，直接断言回填结果。
 *
 * @param {object} job    任务对象（含 resolution_info —— 服务端按产物实际像素算好）
 * @param {object} p      任务的 params（老任务可能没有 width/height / model_key）
 * @param {object} panel  window.VIDEO_PANEL（拿 state 与 size_presets 清单）
 * @returns {object} 给 VIDEO_PANEL.applyConfig() 的配置对象
 */
function reuseConfigFromJob(job, p, panel) {
  job = job || {};
  p = p || {};
  panel = panel || {};
  const panelState = panel.state || {};
  //
  // 【修复 2026-11-13 —— 宽高兜底，多种来源】
  // params.width/height 是「真正的实配来源」，但**并非所有任务都有** ——
  // 内置 Agent / 旧版变体创建的老任务里没有 width/height，model_key 也为空。
  // 旧实现把 p.model_key 当门槛，导致这批任务**整个回填分支被跳过**，
  // 宽高完全不回填（实测停在 640×352，档位停在「自定义」）。
  // 现在按优先级取：
  //   1. params.width/height —— 最新任务，真正的实配来源；
  //   2. job.resolution_info.width/height —— 服务端按**产物实际像素**算好的，
  //      老任务的真实尺寸在这里（source="actual"）；
  //   3. 都没有 -> 留空，applyConfig 会保持面板现状（不编造尺寸）。
  const resoInfo = job.resolution_info || {};
  // 【2026-11-13 改 —— 需求 C4】参数来源统一到**权威摘要**。
  //
  // 【为什么】C4 要求「复用参数」与详情模态框展示的是**同一套参数**。
  // 旧实现从 `params` 里逐个取，而 `params` 里同时躺着两套字段名
  // （遗留的 p.model/p.lora/p.sampler/p.steps 与新面板的
  //  model_key/accel_loras/sampler/scheduler），前者常常是空的 ——
  // 于是「复用参数」丢参数、而模态框显示的是另一套值。
  //
  // 现在：**先**取服务端算好的 model_info（服务端已按白名单算好，
  // 脏值已被挡掉），**再**用 params / resolution_info 兜底。
  //
  // LoRA 强度也一并复用（旧实现只复用了 key，没带强度）。
  const info = (job && job.model_info) || {};
  const reusedWidth = Number(info.width) || Number(p.width)
                      || Number(resoInfo.width) || 0;
  const reusedHeight = Number(info.height) || Number(p.height)
                       || Number(resoInfo.height) || 0;
  const reusedAspect = info.aspect || p.aspect || resoInfo.aspect
                       || panelState.aspect || "16:9";
  const presets = (panel.options && panel.options.size_presets) || [];
  const hit = presets.find(pp => pp.aspect === reusedAspect
    && pp.width === reusedWidth && pp.height === reusedHeight);
  return {
    model: p.model_key,
    aspect: reusedAspect,
    // 【修复 2026-11-13】用解析出的宽高（params → resolution_info），
    // 不要再直接取 p.width/p.height —— 那两个参数对老任务是 undefined。
    width: reusedWidth || undefined,
    height: reusedHeight || undefined,
    rawWidth: reusedWidth || undefined,
    rawHeight: reusedHeight || undefined,
    custom: true,
    // 【2026-11-12】档位 key：由宽高在服务端下发的清单里反查，
    // 查不到就留空（applyConfig 会自己按宽高反查）。唯一真相源仍是服务端清单。
    size_preset_key: hit ? hit.key : "",
    fixed_accel_lora: info.fixed_lora_key || p.fixed_accel_lora,
    // 【2026-11-13 改 —— C4】LoRA 列表**连强度**一起复用。
    // 旧实现只带 key（accel_loras 数组原样传），但面板是**单选**模型
    // （accel_lora + accel_lora_strength）—— 数组形式会走兼容分支，
    // 强度取的是 item.strength，跟面板当前档位不一定一致。
    // 这里显式给单选字段，保证「复用参数」后的档位 = 这条任务真实用的档位。
    accel_lora: info.lora_key || "",
    accel_lora_strength: (info.lora_strength != null)
      ? Number(info.lora_strength) : undefined,
    accel_loras: Array.isArray(p.accel_loras) ? p.accel_loras : [],
    // 【2026-11-13 效果 LoRA】复用参数时把效果 LoRA 一起带回
    // （连强度 + 触发词），否则「复用参数」后效果 LoRA 会丢空。
    // 字段与服务端落库的 params["effect_loras"] 同名同结构。
    effect_loras: Array.isArray(p.effect_loras) ? p.effect_loras : [],
    attention: info.attention_key || p.attention,
    twopass: p.twopass,
    // 【2026-11-12】采样参数也要一起复用（原来没带，
    // 于是「复用参数」后步数/采样器/调度器/shift 都丢回默认值）。
    // 【2026-11-13 改 —— C4】先取权威摘要，再退回 params。
    //
    // 【为什么不是直接读 p.*】media-card-detail 的版本直接读 p.steps /
    // p.sampler …，但 params 里同时躺着两套字段名（遗留的 p.model/p.lora
    // 与新面板的 model_key/accel_loras），前者常常是空的 —— 于是「复用参数」
    // 丢参数、而详情模态框显示的是另一套值。
    // 权威摘要（model_info）由服务端按白名单算好，两侧口径一致。
    steps: info.steps != null ? info.steps : p.steps,
    sampler: info.sampler || p.sampler,
    scheduler: info.scheduler || p.scheduler,
    shift_video: info.shift_video != null ? info.shift_video : p.shift_video,
    shift_audio: info.shift_audio != null ? info.shift_audio : p.shift_audio,
    // 模态框展示的其余参数也一并带过去（cfg / seed），
    // 保证「复用参数」与「详情」看到的是同一套。
    cfg: info.cfg != null ? info.cfg : p.cfg,
    seed: info.seed != null ? info.seed : p.seed,
    // 【media-card-detail】sigma 精修开关也要一起复用（h3-model-meta 没有）。
    use_sigma_shift: p.use_sigma_shift,
  };
}

function jobCard(job) {
  const p = job.params || {};
  const info = MODE_INFO[job.mode] || { name: job.mode };
  const card = document.createElement("div");
  card.className = "job-card";
  // ---- 分辨率 / 比例标签（服务端算好的唯一真源）----
  //
  // 【2026-11-11 修复】原来这里直接渲染 `p.resolution`（档位字符串），
  // 而文生视频新面板**根本不写这个字段** —— 它写的是 width/height，
  // 于是 params.resolution 永远停在遗留默认值 "544p"：
  //   实际跑 1344×768，卡片却显示「544p · 16:9」（544p 不是 1344×768
  //   的任何合法档位）。
  // 现在：优先用服务端下发的 resolution_info（它以**产物实际像素**为准）；
  // 服务端是旧版本（没这个字段）时，本地按同样规则兜底算一次。
  const reso = job.resolution_info || localResolutionInfo(p);
  const resoRow = escapeHtml(reso.label || "—");
  const resoNote = reso.note
    ? `<div class="row reso-note"><span>尺寸差异</span><b>${escapeHtml(reso.note)}</b></div>`
    : "";
  const runningOrQueued = ["queued", "running"].includes(job.status);
  // ---- 「LoRA」行 ----
  //
  // 【2026-11-12 修】这一行以前**根本不存在** —— 用户在面板上选的
  // 加速 LoRA 在生成库里完全看不到，只能从「模型」一行里猜。
  // 现在按服务端摘要渲染；**没有 LoRA 时整行不渲染**（不显示「空」）。
  //
  // 【2026-11-13 修 —— BUG 1】LoRA 行的取值改成「服务端白名单结果」：
  // gen_params.lora 已经是 job_meta.resolve_job_lora 判定过的 ——
  // 只可能是「真实用到的 LoRA（含强度）」或字符串「无」。
  // 老服务端没有 gen_params 时才走本地兜底 loraLabel()。
  const loraRow = loraRowHtml(job.gen_params
    ? job.gen_params.lora
    : loraLabel(job));
  // 【2026-11-13 新增 —— 需求 C2】卡片补齐生成参数。
  //
  // 【为什么不再直接读 p.sampler / p.steps】
  // 旧实现读的是 `params` 里的**遗留字段**（p.sampler / p.steps / p.cfg），
  // 而新文生视频面板提交的是 model_key / accel_loras / sampler / scheduler …
  // —— 两套字段名，前者常常是空的（实测卡片完全不显示步数/调度器）。
  // 现在统一走 `job.model_info`（服务端按白名单算好的权威摘要），
  // 取值口径集中在 web/job-meta.js。
  //
  // 取不到的值显示「—」，**不用别的字段顶替**（用户明确要求）。
  const jm = window.JOB_META;
  const samplingRow = jm ? escapeHtml(jm.samplingText(job)) : "—";
  const stepsRow = jm ? escapeHtml(jm.stepsText(job)) : "—";
  const durationRow = jm ? escapeHtml(jm.durationText(job)) : "—";
  const seedRow = jm ? escapeHtml(jm.seedText(job)) : "随机";
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
    preview = `<div class="progress-wrap"><div class="err-text">✕ ${friendlyError(job.error)}</div></div>`;
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
      ${IMAGE_MODES.includes(job.mode) ? `<div class="row"><span>分辨率</span><b>${resoRow}</b></div>
      ${resoNote}` : info.kind === "audio" ? `
      <div class="row"><span>谱面</span><b>${(p.plan_mode || p.mode) === "melody" ? "仅旋律" : "完整谱面"}</b></div>
      <div class="row"><span>格式</span><b>${(p.format || "flac").toUpperCase()}</b></div>` : `
      <div class="row"><span>分辨率</span><b>${resoRow}</b></div>
      ${resoNote}
      ${info.kind === "video" ? attentionRowHtml(job) : ""}`}`}
      ${genParamRowsHtml(job)}
      <div class="row"><span>耗时</span><b>${job.status === "done" ? fmtElapsed(job.elapsed) : fmtElapsed(job.elapsed)}</b></div>
      ${job.mode !== "longfilm" ? (info.kind === "audio" ? `<button class="row prompt-row" data-act="prompt" title="点击查看并复制完整风格描述与歌词"><span>风格描述</span><b>${escapeHtml((p.prompt || "").slice(0, 80))}</b></button>` : `<button class="row prompt-row" data-act="prompt" title="点击查看并复制完整提示词"><span>提示词</span><b>${escapeHtml((p.prompt || "").slice(0, 80))}</b></button>`) : ""}
    </div>`}
    ${renderJobBatches(job)}
    <div class="job-actions">
      ${video || image || audio ? `<button data-act="download">⬇ 下载</button>` : ""}
      ${image ? `<a data-act="open-new" href="${outputUrl(job, image)}" target="_blank" rel="noopener" title="在新窗口打开原图">↗ 新窗口打开</a>` : ""}
      <!-- 【2026-11-13】骨架卡：补上与真实卡片同形的「下载 / 新窗口打开」
           两个占位按钮（真实卡片有产物所以有，骨架没有产物所以缺）。
           缺这两个 -> 动作行少两个 -> 从 2 行缩到 1 行 ->
           骨架比真实卡片矮 17px -> 网格高度跳 68px（test_grid_flash 抓的）。 -->
      ${job.is_skeleton && !(video || image || audio) ? `<button data-act="download">⬇ 下载</button>` : ""}
      ${job.is_skeleton && !image ? `<a data-act="open-new" href="#" onclick="return false" title="在新窗口打开原图">↗ 新窗口打开</a>` : ""}
      ${video && job.status === "done" && state.page === "stitch" ? `<button data-act="add-stitch">＋ 拼接</button>` : ""}
      ${window.Continuation && window.Continuation.canShowButton(job) ? `<button data-act="continue" title="${escapeHtml(window.Continuation.buttonTitle(job))}">⏭ 接续</button>` : ""}
      ${job.mode !== "longfilm" ? `<button data-act="reuse">复用参数</button>` : ""}
      <!-- 【2026-11-13 需求 C3】详情按钮：卡片区域有限，
           完整生成参数（模型/步数/调度器/采样器/shift/LoRA 列表+强度/
           尺寸/时长/seed/完整提示词/注意力机制/任务来源与工作流名/创建时间）
           放到模态框里。 -->
      ${!runningOrQueued ? `<button data-act="detail" title="查看完整生成参数">ℹ 详情</button>` : ""}
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
  // 「详情」按钮：打开生成参数详情模态框（C3）。
  //
  // 【为什么要 stopPropagation】卡片本身也会绑「点击开详情」
  // （需求：点击卡片弹出模态框）。如果按钮不阻止冒泡，
  // 点「详情」会触发两次 —— 开了又关，或者开了又开。
  // **这正是「与下载 / 复用参数 / 删除按钮互不冲突」的关键**：
  // 所有 .job-actions 里的按钮都要 stopPropagation，
  // 否则点「删除」会同时弹出详情框（删除还要再点一次确认框，
  // 用户体验是「弹了个看不懂的框」）。
  //
  // 【2026-11-14 需求 3】改为**不自己开** #detailModal ——
  // job-detail.js 的 document 级委托已经处理了 [data-act="detail"]
  // （它 open() 的是字段更全的 #jobDetailModal）。
  // 两边都开 = 两个模态框叠在一起。
  // 这里保留 stopPropagation（防止冒泡到卡片监听器再被当成「点卡片」）。
  card.querySelector('[data-act="detail"]')?.addEventListener("click", event => {
    event.stopPropagation();
    // 【2026-11-14 需求 3】
    // #detailModal（浅色、字段完整）是本工作台**唯一**的详情弹层。
    //
    // 【曾经的双开缺陷】web/job-detail.js 里还有一套 document 级委托，
    // 也会响应卡片点击并打开它自己的 #jobDetailModal（深色）。
    // app.js 的卡片监听器没有 stopPropagation -> 事件冒泡到 document
    // -> 两套都开。而 #jobDetailModal 的 z-index（10050）更高，
    // 于是它盖在正确的 #detailModal（z-index 1000）上面 ——
    // 而它自己的文字色是坏的（深色面板 + 近黑文字 = 看不见），
    // 用户看到的就是一个「大部分空白」的弹层。
    //
    // 【现在】详情按钮只开 #detailModal；job-detail.js 那套的
    // 卡片委托已停用（见该文件的改动说明）。
    openDetailModal(job);
  });
  // 点击卡片其它位置 -> 也打开详情（需求原文）。
  //
  // 【2026-11-14 需求 3 · 修复「两个模态框同时弹出 + 弹层空白」】
  //
  // 【原缺陷链】
  //   1. app.js 这段监听器在点 .job-meta 时打开 #detailModal（浅色、可读）；
  //   2. 但它**没有** stopPropagation -> 事件冒泡到 document；
  //   3. web/job-detail.js 的 document 级委托又响应一次，
  //      打开它自己的 #jobDetailModal（**深色**，z-index 10050 > 1000）；
  //   4. 于是深色弹层盖在浅色弹层上面，而它的文字色继承全局
  //      --text（近黑）画在 #171b21（近黑）面板上 = **看不见**；
  //   5. 用户看到的就是「详情弹层大部分空白」。
  //
  // 【修法】stopPropagation —— 事件不再到达 document，
  // job-detail.js 的委托收不到，两套不会同时开。
  //
  // 【为什么保留 #detailModal 而不是改用 job-detail.js 那套】
  // #detailModal 是浅色主题、字段与卡片一致、并且仓库既有的
  // tests/test_c6_detail_recommend.py 明确断言「点详情只能弹
  // detailModal，jobDetailModal 必须保持 hidden」。
  // => 保留它作为唯一详情弹层，同时把 job-detail.js 那套的
  //    卡片委托停用（见 job-detail.js 的改动说明）。
  //
  // 【排除项】不能挡住这些交互：
  //   * 预览区的图片/视频（点它要放大/播放）
  //   * 提示词行（点它要开提示词弹窗）
  //   * 批次缩略图（点它要放大）
  //   * .job-actions 里的所有按钮（下载 / 复用 / 接续 / 拼接 / 删除）
  //   * 取消按钮
  card.addEventListener("click", event => {
    if (isDetailModalOpen()) return;
    // 点在已有的交互元素上 -> 不开详情
    if (event.target.closest("button, a, .job-preview, .prompt-row, "
      + ".job-batches, .job-batch, .cancel-btn, input, select, textarea")) {
      return;
    }
    openDetailModal(job);
    // 【2026-11-14 需求 3】阻止事件冒泡到 document ——
    // 否则 job-detail.js 的委托会再开一套 #jobDetailModal 叠上来。
    event.stopPropagation();
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
  // 「接续」：把这一段的全部配置回填到左侧配置区（提示词除外），
  // 用户写下一段提示词即可继续生成。
  //
  // 【与「复用参数」的区别】复用参数把提示词也带过去（独立重生成一段）；
  // 接续**不带**提示词 —— 上一段被潜空间桥真正接上，拼出完整视频。
  card.querySelector('[data-act="continue"]')?.addEventListener("click", () => {
    if (window.Continuation && typeof window.Continuation.start === "function") {
      window.Continuation.start(job);
    }
  });
  card.querySelector('[data-act="reuse"]')?.addEventListener("click", () => {
    // 【2026-11-12 修】点「复用参数」必须**清掉接续上下文**。
    //
    // 【为什么】两个动作语义互斥：
    //   * 复用参数 = 照这张卡片的全部参数（含提示词）独立重生成一段；
    //   * 接续     = 拿某一段的潜空间当接力棒续下一段。
    // 原来复用参数只改左侧控件、不清 context -> 用户点过「接续」之后
    // 再点别的卡片的「复用参数」，提交时变成「B 的参数 + A 的接力棒」
    // 的混合态。实测这会让 Relay-Kit 的拷贝桥直接 raise
    // 「拷贝桥禁止跨分辨率：上段 768x448 ≠ 本段 1024x576」。
    //
    // 复用参数 = 用户表达「我要照这张重新生成」-> 接力棒不该再挂着。
    if (window.Continuation && typeof window.Continuation.clearForReuse === "function") {
      window.Continuation.clearForReuse();
    }
    // 找到对应模式侧栏项并切换
    activateVideoMode(job.mode);
    $("#prompt").value = p.prompt || "";
    clearPromptEnhanced();
    if (job.mode === "t2v" && window.VIDEO_PANEL) {
      // 文生视频新面板：还原宽高 / 模型 / 加速 LoRA / 注意力 / 二采
      //
      // 【2026-11-12 修 —— 「复用参数」不填分辨率】
      // 旧实现只回填了 width/height，**没带 size_preset_key**，
      // 于是 applyConfig() 里的「按宽高反查档位」拿不到基准 ——
      // 「尺寸预设」下拉停在上一条任务的档位上（实测：宽高显示 1344×768，
      // 下拉却显示「480p · 864×480」），用户以为分辨率没填上。
      // 现在：**由宽高反查**出档位 key 一起传下去（唯一真相源仍是
      // 服务端下发的 size_presets 清单，前端不自己编档位）。
      // 【修复 2026-11-13】复用配置的**解析**抽成独立函数（reuseConfigFromJob），
      // 这样它能被单测直接调用 —— 上一轮 P1 漏网正是因为回填逻辑埋在
      // DOM 事件回调里，测试只能测 applyConfig 层，处理器本身零覆盖。
      const reuseCfg = reuseConfigFromJob(job, p, window.VIDEO_PANEL);
      window.VIDEO_PANEL.applyConfig(reuseCfg);
      window.VIDEO_PANEL.render();
      // 【2026-11-13 改 —— C4】时长 / 步数 / seed 也从**权威摘要**取，
      // 与详情模态框展示的完全一致（params 里的是遗留字段，常是空的）。
      const rInfo = (job && job.model_info) || {};
      const rDuration = rInfo.duration != null ? rInfo.duration : p.duration;
      const rSteps = rInfo.steps != null ? rInfo.steps : p.steps;
      const rSeed = rInfo.seed != null ? rInfo.seed : p.seed;
      if (rDuration) {
        state.duration = rDuration;
        $("#duration").value = rDuration;
        $("#durVal").textContent = rDuration + "s";
      }
      if (rSteps) {
        state.steps = rSteps;
        $("#steps").value = rSteps;
        $("#stepsVal").textContent = rSteps;
      }
      // 【2026-11-13 · C4】种子也一起复用（原任务用了固定种子，
      // 复用后回到「随机」就复现不出同一个结果）。
      // 优先用**权威摘要**里的 seed（与卡片/详情显示同一套），
      // 摘要没有才退回 params.seed。
      if (rSeed) $("#seed").value = rSeed;
      else if (p.seed) $("#seed").value = p.seed;
      // 【2026-11-13 · C4】复用后配置区显示的分辨率/尺寸必须与卡片一致。
      //
      // 【为什么需要这一段】卡片上的尺寸是**产物实际像素**
      // （resolution_info.source == "actual"，比如 1344×768），
      // 而 params.width/height 可能压根没有（老任务）或者
      // 是请求值（1344×760，实际出 1344×768）。
      // 只按 params 回填 -> 用户看到「1344×760」，与卡片「1344×768」
      // 不一致 —— 同一张卡片的两个数字对不上。
      //
      // 【规则】**产物实际像素优先**（与卡片的显示口径一致），
      // 没有实际像素才用请求值。
      const cardReso = job.resolution_info || {};
      const reuseWidth = Number(cardReso.width) || Number(p.width) || 0;
      const reuseHeight = Number(cardReso.height) || Number(p.height) || 0;
      if (reuseWidth && reuseHeight) {
        const cfg = window.VIDEO_PANEL.state || null;
        if (cfg) {
          cfg.width = reuseWidth;
          cfg.height = reuseHeight;
          cfg.rawWidth = reuseWidth;
          cfg.rawHeight = reuseHeight;
        }
        // 档位 key 由宽高反查（唯一真源是服务端下发的 size_presets 清单）
        const presets = (window.VIDEO_PANEL.options
          && window.VIDEO_PANEL.options.size_presets) || [];
        const hit = presets.find(pp => pp.width === reuseWidth
          && pp.height === reuseHeight);
        if (cfg) cfg.sizePresetKey = hit ? hit.key : "";
        window.VIDEO_PANEL.render();
      }
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
    if (job.mode === "t2i_h3image" || job.mode === "i2i_h3image") {
      // MiniMax-H3-Image：还原档位 / 比例 / 实际宽高 / 采样参数
      //
      // 【必须带 return】—— 否则会掉进后面的 else 分支（视频回填），
      // 那一支会把 p.resolution 写进 state.res（视频分辨率档），
      // 污染视频面板的状态。
      const s = state.imgH3Image;
      if (p.resolution) s.tier = p.resolution;
      if (p.aspect) s.aspect = p.aspect;
      if (p.width && p.height) {
        s.width = p.width; s.height = p.height;
        s.rawWidth = p.width; s.rawHeight = p.height;
        s.custom = true;      // 复用旧任务的尺寸 -> 也是自定义模式
      }
      if (p.model && H3IMAGE_OPTIONS.models[p.model]) applyModelDefaults(p.model);
      if (p.sampler) s.sampler = p.sampler;
      if (p.scheduler) s.scheduler = p.scheduler;
      if (p.steps) s.steps = p.steps;
      if (p.cfg != null) s.cfg = p.cfg;
      if (p.shift != null) s.shift = p.shift;
      if (p.batch_count) s.batchCount = Number(p.batch_count);
      // 【负面提示词不还原】H3-Image 的 guidance 固定 1 -> 负面无效，
      // 落库的记录里本来也没有 negative_prompt（服务端已丢弃）。
      // 【LoRA 不还原】官方暂未发布 H3-Image 的 LoRA。
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

// ------------------------------------------------------------------
// C3：生成参数详情模态框（2026-11-13）
// ------------------------------------------------------------------
//
// 【需求原文】点击卡片（或卡片上的「详情」按钮）弹出模态框，展示完整生成参数
//   —— 步数、调度器、采样器、shift、LoRA 名称+强度、尺寸、时长、seed、
//   完整提示词、注意力机制、工作流名、创建时间。
//   要求：支持关闭按钮 / Esc / 点击遮罩关闭；与卡片上既有的下载、复用参数、
//   删除按钮互不冲突；定时刷新不得打断已打开的模态框。
//
// 【取值纪律】全部读服务端下发的 job.gen_params（h3studio/job_meta.gen_params）。
// 缺失的字段显示「—」，**不拿别的字段顶替**。
//
// 【定时刷新不打断】模态框内容在**打开时快照**一次 ——
// 之后 2 秒一次的 refreshJobs 轮询不会改写它（用户正在看的内容不该被
// 悄悄换掉）。想看最新的可以关掉再打开。
const DETAIL_FIELDS = [
  ["model", "模型"],
  ["steps", "步数"],
  ["scheduler", "调度器"],
  ["sampler", "采样器"],
  ["shift", "shift"],
  ["cfg", "CFG"],
  ["lora", "LoRA"],
  ["size", "尺寸"],
  ["duration", "时长"],
  ["seed", "种子"],
  ["attention", "注意力机制"],
  ["workflow", "工作流名"],
];

// 详情模态框当前展示的任务 id（用于「刷新时不要改写」的判断）
let detailOpenJobId = null;

function detailParamValue(job, key) {
  const gp = job.gen_params || {};
  const value = gp[key];
  // 【缺失 = 「—」】服务端没给就是没有 —— 不拿别的字段顶替。
  const text = String(value == null ? "" : value).trim();
  return text || "—";
}

// ------------------------------------------------------------------
// 【2026-11-15 新增】产物实际元数据的渲染（详情模态框数据源）
// ------------------------------------------------------------------
//
// 【为什么数据源换成接口】`job.gen_params` 是**卡片行**用的摘要（值都
// 压成一行字符串）。用户要的是「点开看详细内容」—— 需要：
//   * 逐枚列出 LoRA（含强度、加载节点），而不是「A · B」拼一起；
//   * 区分「确实没有」与「没记录到」两种语义；
//   * 字段缺失时明确写「未知」，不拿别的字段顶替。
// 所以详情弹层改读 `GET /api/jobs/{id}/artifact-meta`（服务端算好的产物实际值）。
//
// 【降级】接口拿不到（老服务端 / 网络失败）-> 退回 job.gen_params，
// 保证详情框**永远能打开**（这是既有契约：点详情必须有反应）。

//: 详情模态框的字段定义（顺序即展示顺序）。
//: value 取自 artifact-meta 接口的字段名。
const ARTIFACT_META_FIELDS = [
  ["model", "模型"],
  ["model_file", "权重文件"],
  ["steps", "步数"],
  ["scheduler", "调度器"],
  ["sampler", "采样器"],
  ["cfg", "CFG"],
  ["seed", "种子"],
  ["width", "宽度"],
  ["height", "高度"],
  ["resolution", "分辨率档位"],
  ["aspect", "画面比例"],
  ["duration", "时长"],
  ["attention", "注意力机制"],
  ["workflow", "工作流名"],
];

//: 「未知」的统一文案。
const ARTIFACT_UNKNOWN = "未知";

/**
 * 把 artifact-meta 接口的返回值渲染成「生成参数」区的 HTML。
 *
 * @param {object} meta 接口返回值（artifact-meta）
 * @returns {string} HTML
 */
function artifactMetaRowsHtml(meta) {
  const data = meta || {};
  const rows = ARTIFACT_META_FIELDS.map(([key, label]) => {
    let value = data[key];
    if (value === null || value === undefined || value === "") {
      value = ARTIFACT_UNKNOWN;
    }
    const missing = value === ARTIFACT_UNKNOWN;
    const cls = missing ? ' class="is-missing"' : "";
    return `<div class="detail-kv-row"><span>${escapeHtml(label)}</span>`
      + `<b${cls} title="${escapeHtml(String(value))}">${escapeHtml(String(value))}</b></div>`;
  });
  return rows.join("");
}

/**
 * LoRA 区：逐枚列出产物实际挂载的 LoRA。
 *
 * 三种状态（服务端 lora_status 决定）：
 *   recorded     -> 逐枚列出（名字 + 强度 + 加载节点）
 *   none_used    -> 「未使用 LoRA」（工作流里确实没有 LoRA 节点）
 *   not_recorded -> 「未知（该任务生成时未记录 LoRA）」
 *
 * 【为什么不复用卡片行的「无」】卡片行要紧凑（一个字），
 * 详情框要明确 —— 「未记录」和「确实没用」是两回事，混成一个字
 * 就是在撒谎。
 *
 * 【DOM 结构 —— 必须保持 .detail-kv 的两列 grid】
 * `#detailParams` 的 class 是 `.detail-kv`（display:grid，两列）。
 * 每一行必须是 `<span>标签</span><b>值</b>` —— 值那一格只能放**行内**内容。
 * 早前一版把 chips 装进 `<div>` 里塞进第二格 -> 破坏了 grid，
 * 而且 `<b>` 缺失导致值读不出来（浏览器验收实测抓到）。
 * 所以这里多枚 LoRA 用 ` · ` 连接放在 `<b>` 里（与卡片行同款），
 * 加载节点放进 title（悬停可见，不占版面）。
 */
function artifactLoraHtml(meta) {
  const data = meta || {};
  const loras = Array.isArray(data.loras) ? data.loras : [];
  const status = String(data.lora_status || "");
  if (status === "none_used" || (status !== "not_recorded" && !loras.length)) {
    return `<div class="detail-kv-row"><span>LoRA</span>`
      + `<b>未使用 LoRA</b></div>`;
  }
  if (status === "not_recorded" && !loras.length) {
    return `<div class="detail-kv-row"><span>LoRA</span>`
      + `<b class="is-missing" title="该任务生成时未把 LoRA 记录到产物元数据">`
      + `未知（该任务生成时未记录 LoRA）</b></div>`;
  }
  if (!loras.length) {
    return `<div class="detail-kv-row"><span>LoRA</span>`
      + `<b class="is-missing">${ARTIFACT_UNKNOWN}</b></div>`;
  }
  const text = loras.map((item) => {
    const name = String(item.name || "");
    const strength = (item.strength === null || item.strength === undefined)
      ? "" : ` ×${Number(item.strength).toFixed(2)}`;
    return name + strength;
  }).join(" · ");
  const loaders = loras.map((item) => String(item.loader || "")).filter(Boolean);
  const titleAttr = loaders.length
    ? ` title="加载节点：${escapeHtml(loaders.join(" / "))}"` : "";
  return `<div class="detail-kv-row"><span>LoRA</span>`
    + `<b${titleAttr}>${escapeHtml(text)}</b></div>`;
}

/**
 * 【2026-11-14 · c6】详情模态框里的「作者推荐参数」分组。
 *
 * 与 web/job-detail.js 的 recBlock() 同源同口径，但只依赖公开的
 * window.JOB_META.recItems / recSource —— 不碰 job-detail.js 的 IIFE 私有实现。
 *
 * - recItems() 已把「作者未提供」的项标成 provided=false、value="作者未提供"
 *   （绝不把原始哨兵值 not_provided 显示给用户）；
 * - recSource() 给出「来源类型 / 出处 / note」，保证来源可追溯；
 * - 任一推荐对象为空 -> 不渲染该分组；三个都空 -> 返回 ""（不渲染空区块）。
 */
function detailRecommendHtml(job) {
  const meta = window.JOB_META;
  if (!meta || typeof meta.recItems !== "function") return "";
  const info = (job && job.model_info && typeof job.model_info === "object")
    ? job.model_info : {};
  const groups = [
    ["主模型推荐参数（作者 / ComfyUI 硬性值）", info.model_recommendation],
    ["加速 LoRA 推荐参数（作者）", info.lora_recommendation],
    ["工作流固定 LoRA 推荐参数（作者）", info.fixed_lora_recommendation],
  ];
  const blocks = [];
  for (const [title, rec] of groups) {
    if (!rec || typeof rec !== "object") continue;
    let items = [];
    try {
      items = meta.recItems(rec) || [];
    } catch (e) {
      items = [];
    }
    if (!items.length) continue;
    const chips = items.map((it) => {
      const cls = it.provided ? "job-detail-recchip" : "job-detail-recchip is-missing";
      const src = it.source ? ` ${it.source}` : "";
      const titleAttr = it.source ? ` title="${escapeHtml(it.source)}"` : "";
      return `<span class="${cls}"${titleAttr}>${escapeHtml(it.label)}：${escapeHtml(it.value)}${escapeHtml(src)}</span>`;
    }).join("");
    let sourceHtml = "";
    if (typeof meta.recSource === "function") {
      // 【为什么还要再洗一遍】recSource() 的来源类型在
      // window.JOB_META_SOURCE_KIND_LABEL 没就位时（/api/video-options 还没返回，
      // 或用户没打开过视频面板）会**回退成原始哨兵值** not_provided。
      // c6 要求「作者未提供」显式标注 —— 所以这里把漏出来的哨兵值洗成中文，
      // 不依赖标签表是否已加载。
      const source = String(meta.recSource(rec) || "")
        .replace(/\bnot_provided\b/g, "作者未提供");
      if (source) sourceHtml = `<div class="job-detail-source">${escapeHtml(source)}</div>`;
    }
    blocks.push(`<div class="detail-section-title">${escapeHtml(title)}</div>`
      + `<div class="job-detail-reclist">${chips}</div>${sourceHtml}`);
  }
  if (!blocks.length) return "";
  // 用 detail-section —— 复用 style.css 里现成的
  // `.detail-section + .detail-section { margin-top: 16px }` 与
  // `.detail-section-title` 样式，**零新增 CSS**。
  return `<div class="detail-section detail-rec-section" data-detail-rec>${blocks.join("")}</div>`;
}

function openDetailModal(job) {
  if (!job) return;
  const modal = $("#detailModal");
  if (!modal) return;
  // 【快照】把要展示的内容一次性渲染好 —— 后续轮询不会改写它。
  const title = $("#detailModalTitle");
  if (title) {
    const info = MODE_INFO[job.mode] || { name: job.mode };
    title.textContent = `${info.name} · 生成参数详情`;
  }
  const paramsBox = $("#detailParams");
  if (paramsBox) {
    const rows = DETAIL_FIELDS.map(([key, label]) => {
      const value = detailParamValue(job, key);
      return `<div class="detail-kv-row"><span>${escapeHtml(label)}</span>`
        + `<b title="${escapeHtml(value)}">${escapeHtml(value)}</b></div>`;
    });
    // 创建时间：不在 gen_params 里（那是任务属性，不是生成参数）
    rows.push(`<div class="detail-kv-row"><span>创建时间</span>`
      + `<b>${escapeHtml(fmtTime(job.created_at))}</b></div>`);
    // 【2026-11-14 · c6】作者推荐参数分组（需求 1 / 2 / 5）。
    //
    // 【为什么加在这里】h3-model-meta 把「作者推荐参数」做在 web/job-detail.js
    // （recBlock -> #jobDetailModal），但线上真正弹出的是 app.js 内联的
    // #detailModal（media-card-detail 版），而 job-detail.js 那套**没有调用方**
    // —— app.js 的 [data-act="detail"] 监听器先 stopPropagation()，
    // job-detail.js 的 document 级委托收不到该点击。
    // => 推荐参数在「详情区」里看不到。本段把同一份数据接进实际弹的那套。
    //
    // 【数据来源】服务端已在 model_info 里下发（h3studio/job_meta.py:
    //   model_recommendation / lora_recommendation / fixed_lora_recommendation），
    //   **服务端不用改**。取值口径统一走 window.JOB_META.recItems / recSource
    //   （web/job-meta.js，与卡片 / 面板下拉 tooltip 完全同源）。
    //
    // 【为什么不用 job-detail.js 的 recBlock】它是 IIFE 私有、没导出，
    //   依赖它就得改它的导出面（改动面更大）。recBlock 本身也只是
    //   recItems + recSource 的薄封装，这里直接用公开 API 重新拼，
    //   复用 style.css 里已有的 .job-detail-reclist / .job-detail-recchip
    //   / .is-missing / .job-detail-source 样式（零新增 CSS）。
    paramsBox.innerHTML = rows.join("");
    // 【2026-11-14 · c6】推荐参数分组作为**兄弟 section** 插在「生成参数」之后
    // —— 不能塞进 #detailParams（那是 .detail-kv，两列 grid，塞进去会被当网格单元）。
    //
    // 先无条件清掉上一次的（换一条任务重开时不能叠加；新任务没有推荐参数时也不能留旧的）。
    const stale = document.querySelector("[data-detail-rec]");
    if (stale) stale.remove();
    const recHtml = detailRecommendHtml(job);
    if (recHtml) {
      const section = paramsBox.closest(".detail-section");
      if (section) {
        const holder = document.createElement("div");
        holder.innerHTML = recHtml;
        const node = holder.firstElementChild;
        if (node) section.insertAdjacentElement("afterend", node);
      }
    }
  }
  const promptBox = $("#detailPrompt");
  if (promptBox) {
    // 完整提示词（不是截断的 80 字）
    promptBox.textContent = String((job.params && job.params.prompt) || "").trim()
      || "（这条任务没有提示词）";
  }
  // 【2026-11-14 需求 3】缩略图 + 下载。
  //
  // 【为什么原来没有】弹层只有参数表和提示词，没有任何**产物本身**的信息 ——
  // 用户点开详情是为了「这条到底是长什么样的」，却只能看到一堆参数。
  //
  // 【数据来源】job.outputs（服务端在任务完成时写回的产物清单），
  // 与卡片上的预览用的是**同一份**（outputUrl(job, output)）。
  // 视频给 <video controls>，音频给 <audio controls>，图片给 <img>。
  const previewBox = $("#detailPreview");
  if (previewBox) {
    const outputs = Array.isArray(job.outputs) ? job.outputs : [];
    // 先取第一个能显示的产物（按图片 -> 视频 -> 音频）
    const pick = (kinds) => outputs.find(o => o && kinds.includes(o.kind));
    const img = pick(["image"]);
    const vid = pick(["video"]);
    const aud = pick(["audio"]);
    const primary = img || vid || aud;
    if (!primary) {
      previewBox.hidden = true;
      previewBox.innerHTML = "";
    } else {
      previewBox.hidden = false;
      const url = outputUrl(job, primary);
      const name = String(primary.name || primary.file || "").split(/[\\/]/).pop();
      let media;
      if (primary.kind === "image") {
        media = `<img class="detail-preview-img" src="${escapeHtml(url)}"
                       alt="${escapeHtml(name)}" loading="lazy">`;
      } else if (primary.kind === "video") {
        media = `<video class="detail-preview-media" src="${escapeHtml(url)}"
                        controls preload="metadata"></video>`;
      } else {
        media = `<audio class="detail-preview-audio" src="${escapeHtml(url)}"
                        controls preload="metadata"></audio>`;
      }
      previewBox.innerHTML = media
        + `<div class="detail-preview-actions">
             <a class="detail-preview-dl" href="${escapeHtml(url)}" download
                title="下载原文件">⬇ 下载原文件</a>
             <span class="detail-preview-name" title="${escapeHtml(name)}">${escapeHtml(name)}</span>
           </div>`;
    }
  }
  detailOpenJobId = job.id;
  modal.hidden = false;
  document.body.classList.add("modal-open");
  // 滚回顶部（上次打开可能停在下面）
  const body = $("#detailModalBody");
  if (body) body.scrollTop = 0;

  // 【2026-11-15 新增】异步拉「产物实际元数据」并刷新参数区。
  //
  // 【为什么先开框再拉数据】详情框必须**立刻**有反应（既有契约：
  // 点详情就有东西出现）。先渲染卡片摘要（job.gen_params），
  // 再用接口的真实值替换 —— 用户看到的是「先有骨架，再变准」。
  //
  // 【为什么用 job.id 做竞态保护】用户可能连续点不同卡片。
  // 只有当前打开的那条才允许写 DOM。
  loadArtifactMeta(job.id);
}

/**
 * 拉取产物实际元数据并渲染到详情模态框。
 *
 * @param {string} jobId
 */
async function loadArtifactMeta(jobId) {
  let meta = null;
  try {
    const resp = await fetch(`/api/jobs/${encodeURIComponent(jobId)}/artifact-meta`);
    if (resp.ok) meta = await resp.json();
  } catch (e) {
    meta = null;
  }
  // 竞态保护：用户已经切到别的卡片了 -> 丢弃这份数据。
  if (!meta || detailOpenJobId !== jobId) return;
  const paramsBox = $("#detailParams");
  if (!paramsBox) return;
  // 【不整块替换】推荐参数分组是**另一个** section（[data-detail-rec]），
  // 整块替换 #detailParams 不会碰到它 —— 但创建时间行是 #detailParams
  // 里的，所以这里把「生成参数」行与「创建时间」行分开重建。
  const createdRow = paramsBox.querySelector(".detail-kv-row:last-child");
  const createdHtml = createdRow && createdRow.textContent.includes("创建时间")
    ? createdRow.outerHTML : "";
  const rows = artifactMetaRowsHtml(meta);
  const loraRow = artifactLoraHtml(meta);
  paramsBox.innerHTML = rows + loraRow + createdHtml;
}

function closeDetailModal() {
  const modal = $("#detailModal");
  if (!modal) return;
  modal.hidden = true;
  detailOpenJobId = null;
  syncModalOpenClass();
}

// body.modal-open 的统一维护。
//
// 【为什么需要】body.modal-open { overflow: hidden }（style.css:429）
// 是「有模态框打开时锁住页面滚动」。但页面上有**多个**模态框
// （提示词 / LoRA 说明 / 增强确认 / 详情），每个都各自 add/remove
// 这个 class —— 关掉其中一个会把另一个的锁也解掉。
// 所以关框时必须重新判断「还有没有别的框开着」。
//
// 【2026-11-15 D12】新增 fdModal（交互流程图面板）——
// 它由 web/flow-docs.js 打开/关闭，但**滚动锁必须走这一份**：
// 流程图面板与其它模态框互斥时，关掉其中一个不能把另一个的锁解掉。
const MODAL_IDS = ["promptModal", "loraInfoModal", "enhanceGateModal",
                   "detailModal", "fdModal"];
function syncModalOpenClass() {
  const anyOpen = MODAL_IDS.some(id => {
    const el = $("#" + id);
    return !!(el && !el.hidden);
  });
  document.body.classList.toggle("modal-open", anyOpen);
}

function isDetailModalOpen() {
  const modal = $("#detailModal");
  return !!(modal && !modal.hidden);
}

// 关闭方式 1/2：× 按钮 + 底部「关闭」按钮（底部按钮走 onclick 调上面的 close）
$("#detailModalClose").addEventListener("click", closeDetailModal);
// 关闭方式 3：点击遮罩（点到 .prompt-modal 本身才算，点对话框内部不算）
$("#detailModal").addEventListener("click", event => {
  if (event.target === $("#detailModal")) closeDetailModal();
});
// 关闭方式 4：Esc
document.addEventListener("keydown", event => {
  if (event.key === "Escape" && isDetailModalOpen()) closeDetailModal();
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
    // 【2026-11-13 修 —— 骨架与真实卡片行数对齐】
    //
    // 骨架走的是**同一套** jobCard() 渲染逻辑，而卡片现在从
    // `job.model_info` 取生成参数（LoRA / 采样 / 步数 / 尺寸 …）。
    // 骨架没有 model_info -> 那些行**不渲染** ->
    // 骨架比真实卡片少几行 -> 数据到达瞬间网格高度变化 ->
    // 「首图巨大闪现 / 布局抖动」（test_grid_flash 抓的就是这个）。
    //
    // 修法：给骨架一份 model_info，字段取值**跟真实卡片同形**。
    // 关键点：**LoRA 行按「无」渲染**（真实卡片在没有任何 LoRA 时
    // 就是显示「无」），采样/步数取占位值 —— 这样两边行数一致。
    //
    // 【为什么不用「骨架占位 LoRA」】那会让骨架比真实卡片**多**一行内容，
    // 而且真实任务里 LoRA 大概率是「无」—— 形状必须一致，不是内容一致。
    model_info: {
      model_key: "",
      model_label: "骨架占位模型",
      lora_label: "",
      lora_strength: null,
      fixed_lora_label: "",
      fixed_lora_strength: null,
      attention_label: "",
      attention_speed_label: "",
      steps: 8,
      cfg: 1,
      sampler: "res_multistep",
      scheduler: "simple",
      shift_video: 12,
      shift_audio: 3,
      width: 1360,
      height: 768,
      aspect: "16:9",
      resolution: "768p",
      duration: 5,
      seed: 0,
    },
    status: "done",
    progress: 1,
    progress_text: "",
    // 【2026-11-13 修 —— 骨架的动作按钮要与真实卡片同形】
    //
    // 真实卡片有 5 个动作按钮（下载 / 新窗口打开 / 复用参数 / 详情 / 删除），
    // 骨架 outputs 为空 -> 只有 3 个（复用参数 / 详情 / 删除）->
    // 动作行从 1 行变成 2 行 -> 每张卡矮 17px -> 4 行网格差 68px
    // （实测：真实 53px / 骨架 36px）。
    //
    // 修法：给骨架一个**标志位**，让 jobCard 知道「这是骨架」，
    // 按骨架形态渲染**同样的 5 个按钮**（下载 / 新窗口打开渲染成不可点的占位）。
    //
    // 【为什么不给假 outputs】给了 outputs，jobCard 会真的去渲染 <img>，
    // 骨架就变成「真实卡片」—— 测试断言「切换过程没出现骨架占位」直接挂。
    outputs: [],
    is_skeleton: true,
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
  // 【2026-11-13 修 —— 骨架至少显示一帧】
  // 骨架与真实卡片的写入在**同一帧**内完成时，骨架就等于没出现过 ——
  // 用户看到的是「列表直接跳到新内容」（没有加载态），
  // 而依赖骨架帧的测试（test_grid_flash）会判「整个切换过程没出现骨架占位」。
  // 实测：服务端响应快时（约 10ms）骨架在两帧之间就被换掉了。
  //
  // 修法：本轮**刚铺过**骨架时，真实卡片的写入推迟到下一帧 ——
  // 骨架必然完整显示一帧，既给用户加载反馈，也保证帧序列里有骨架。
  let skeletonJustShown = false;
  if (displayedFilterKey !== filterKey) {
    displayedFilterKey = filterKey;
    // 骨架数量按当前每页数量铺（但不超过 SKELETON_MAX），
    // 这样骨架行数与最终行数尽量一致，切换时不会「先 3 行后 4 行」。
    renderGridSkeleton(grid, state.pageSize);
    // 容器此刻可能不可见（素材库页把 #jobsArea 隐藏了）——
    // 此时宽度是 0，不能据此算列数，否则会写进一个错误的「1 列」。
    applyGridColumns(state.pageSize);
    skeletonJustShown = true;
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
    // 【2026-11-13 需求 C3】把本轮取到的任务缓存起来 ——
    // 详情模态框按 job id 回查完整条目（卡片 DOM 只有渲染后的片段，
    // 模态框要展示完整生成参数 + 推荐参数 + 完整提示词）。
    try {
      window.JOB_CACHE = groups.flatMap(g => (g.jobs || []));
    } catch (e) { /* 缓存失败不影响渲染 */ }
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
          // 【修既有 bug：tab="all" 时映射表没有 "all" 键 -> 文案变成字符串
          //   "undefined"】。之前只在 active/done 等子页签上出现过，
          //   切到「全部」且一条任务都没有时，任务区中央就显示字面量
          //   "undefined"（截图证据：evidence/h3image-t2i-panel.png）。
          //   兜底成「暂无任务」而不是 undefined。
          const EMPTY_TEXT = {
            active: "当前没有排队或生成中的任务",
            done: "暂无已完成的任务",
            longfilm: "暂无已合成的长片",
            failed: "暂无失败的任务",
            canceled: "暂无已取消的任务",
            all: "暂无任务",
          };
          grid.innerHTML = '<div class="empty-hint">'
            + (EMPTY_TEXT[tab] || EMPTY_TEXT.all) + '</div>';
        });
        return;
      }
      preserve(() => syncJobCards(grid, jobs));
    };
    if (window.H3UI && !window.H3UI.canWriteDOM("jobs", () => refreshJobs())) return;
    if (skeletonJustShown) {
      // 推迟一帧再写真实卡片 —— 让骨架完整显示一帧（见上面的说明）。
      requestAnimationFrame(() => requestAnimationFrame(writeAll));
    } else {
      writeAll();
    }
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

// 定位命中后的高亮持续时长（毫秒）。
//
// 【为什么是 4 秒而不是 2.5 秒 —— 2026-11-12 实测】
// 用户点「在生成库中定位」之后，页面要跳页 + 拉数据 + 渲染卡片，
// 等他真正看清那张卡片时可能已经过去 2-3 秒了。
// 高亮只闪 2.5 秒的话，用户第一眼看到的往往是「已经不高亮了」——
// 等于白做。
const LOCATE_FLASH_MS = 4000;

// 高亮样式挂到 .job-card 上（见 style.css 的 .job-card.is-located）。
function flashLocatedCard(card) {
  if (!card) return;
  card.classList.add("is-located");
  // 高亮是「刚才定位到这」的提示，不该一直挂着；
  // 但也不能太短（见 LOCATE_FLASH_MS 的说明）。
  setTimeout(() => card.classList.remove("is-located"), LOCATE_FLASH_MS);
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
  // 【2026-11-11】长片拼接不再是独立侧栏项，改成「长片拼接」类别下的
  // 一个模型项 —— 高亮走两级导航的 syncActive()。
  if (window.VideoNav) {
    window.VideoNav.selectByMode("longfilm");
    syncVideoNavActive();
  }
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

$("#navSystem").addEventListener("click", showSystem);
// 【2026-11-11】长片拼接入口搬进两级导航（web/video-nav.js），
// 这里不再直接绑定 #navStitch —— 那个 id 只存在于 <noscript> 兜底里，
// 直接 addEventListener 会在 null 上抛错，把整个 app.js 带崩。
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
  // 【2026-11-15 D12】滚动锁的统一维护入口 ——
  // web/flow-docs.js 打开/关闭自己的模态框时要走这一份，
  // 否则关掉流程图面板会把其它模态框的锁一起解掉。
  syncModalOpenClass,
  // 【2026-11-15 D11 收口】LoRA 面板的**测试钩子**。
  //
  // 用途：真实浏览器测试要能在不依赖本机真实 LoRA 文件的前提下，
  // 驱动前端的 LoRA 清单渲染（喂假条目 -> 看 DOM 里有没有如实标注）。
  //
  // 为什么必须暴露：前一轮用「源码里有没有某个字符串」断言形态标注，
  // 变异测试证明那是**空洞的** —— 把渲染插值改成空串，断言照样全绿。
  // 只有真的把页面渲染出来、在 DOM 里找标签，才能抓住这种回归。
  //
  // 只读暴露（不改变任何业务行为）：外部只能取到 store 与渲染函数。
  __loraTestHooks: {
    stores: { Z: IMG_LORAS, Qwen: QWEN_LORAS, MOSS: MOSS_LORAS, H3: H3IMAGE_LORAS },
    renderLoraList,
    renderLoraSelect,
    openLoraInfo,
    imgLoraMeta,
  },
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
  // 抽屉打开时把悬浮「参数」按钮藏掉：
  //  .panel-fab 的 z-index(960) 必须高于抽屉本体(950) 才能保证**可点**
  //  （实测 390px 下按钮被抽屉里溢出的滑块挡住 -> 完全点不动），
  //  但这样一来按钮就会浮在打开的抽屉上面，盖住面板内容。
  //  所以：关闭时显示（唯一作用是打开抽屉），打开时隐藏。
  const fab = $("#panelFab");
  if (fab && window.innerWidth <= BREAKPOINTS.tablet) {
    fab.hidden = !!open;
  }
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
      // 打开抽屉时把 FAB 藏掉（它会浮在抽屉上方挡住面板内容，
      // 原因见 setPanelOpen 里的注释）。
      fab.hidden = open;
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
boot("视频类别导航", async () => {
  const box = videoNavContainer();
  if (!box || !window.VideoNav) return;
  window.VideoNav.onSelect = (category, modelKey, mode) => {
    // 长片拼接是独立页面（#stitchPanel），不是创作页的普通模式。
    if (mode === "longfilm") {
      showStitch();
      syncVideoNavActive();
      return;
    }
    activateMode(mode, box.querySelector(`.nav-model-item[data-model-key="${modelKey}"]`));
    syncVideoNavActive();
  };
  window.VideoNav.onUnavailable = (category) => {
    const label = (window.VideoNav.categoryByKey(category) || {}).label || category;
    alert(`「${label}」类别下没有可用的模型，无法切换。`);
  };
  await window.VideoNav.load();
  window.VideoNav.render(box);
  window.VideoNav.bind(box);
  // 恢复持久化选择（从来没选过就不动 —— 让 DEFAULT_MODE 独家高亮）。
  // applyDefault 内部会通过 onSelect 把应用切到那个 mode，
  // 所以它必须在「默认模式」boot 之后跑。
  window.VideoNav.applyDefault();
  syncVideoNavActive();
});
boot("视频参数清单", async () => {
  if (!window.VIDEO_PANEL) return;
  await window.VIDEO_PANEL.load();
  window.VIDEO_PANEL.bind();
  window.VIDEO_PANEL.render();
});
boot("视频效果 LoRA 清单", async () => {
  if (!window.VIDEO_EFFECT_LORA) return;
  window.VIDEO_EFFECT_LORA.bind();
  // 【2026-11-13 选中回显 —— 顺序很关键】
  // VIDEO_PANEL.load() 内部会 applyConfig() 从 /api/video-config 恢复
  // 上次选中的效果 LoRA。若这里不等它跑完就 load()，
  // 「恢复」与「按当前底模剔除不兼容项」会并发 -> 恢复出来的选中项
  // 可能还没被剔除就被渲染（或反之），表现为**选中项随机丢失**。
  // 所以：**先等面板清单（含配置恢复）完成，再拉效果 LoRA 清单**。
  if (window.VIDEO_PANEL && window.VIDEO_PANEL.isReady
      && !window.VIDEO_PANEL.isReady()) {
    // 面板还没就绪 -> 等它（最多 10 秒，避免卡死启动）
    const t0 = Date.now();
    while (Date.now() - t0 < 10000
           && (!window.VIDEO_PANEL.isReady || !window.VIDEO_PANEL.isReady())) {
      await new Promise(r => setTimeout(r, 100));
    }
  }
  await window.VIDEO_EFFECT_LORA.load(
    window.VIDEO_PANEL && window.VIDEO_PANEL.state
      ? window.VIDEO_PANEL.state.model : "");
});
boot("视频采样参数清单", loadVideoSamplingOptions);
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
// 【2026-11-13 队列化】生成队列：面板渲染 + 快照轮询。
// 队列变化时回调 updateActionButtons()，让「生成」按钮的
// 「已入队（队列 N）」文案跟着更新。
boot("生成队列", () => {
  if (!window.GenerationQueue) return;
  window.GenerationQueue.onChange = () => {
    try { updateActionButtons(); } catch (e) { /* 忽略 */ }
  };
  window.GenerationQueue.init();
  updateActionButtons();
});

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

// ---------- 错误文案（不展示原始 Python 异常文本）----------
//
// 【为什么前端也要做一层 —— 2026-11-13 修】
// 服务端已经不再把原始异常文本写进 jobs.error（见 server.py 的
// `_user_facing_error`）。但前端这一层是**兜底**：
//   * 老任务（修复前建的）库里存的**就是**原始 Python 文本；
//   * 任何新引入的写入路径都可能漏掉服务端那一层。
//
// 【判据】Python 异常文本的特征：
//   * 形如 `ValueError: xxx` / `AttributeError: xxx`（异常类名 + 冒号）
//   * 或含 `'str' object has no attribute 'get'` 这类**双引号包裹的
//     类型名 + 属性名**。
// 命中任一条 -> 不展示原文，换成可读中文。
//
// 【原则】宁可**信息少一点**，也不要给用户一段他看不懂也修不了的
// Python 报错 —— 原始文本照样在服务端日志里。
const PY_EXCEPTION_PATTERNS = [
  /^[A-Z][A-Za-z_]*(Error|Exception|Interrupt|Warning)\b\s*[:：]/,  // ValueError: ...
  /'[^']+' object has no attribute/,                                     // 'str' object has no attribute 'get'
  /"[^"]+" object has no attribute/,
  /\bTraceback \(most recent call last\)/,
  /File "[^"]+", line \d+/,                                               // traceback 行
];

function looksLikePythonTraceback(text) {
  if (!text) return false;
  const t = String(text);
  return PY_EXCEPTION_PATTERNS.some((re) => re.test(t));
}

function friendlyError(text) {
  const raw = text == null ? "" : String(text).trim();
  if (!raw) return "生成失败";
  if (!looksLikePythonTraceback(raw)) return raw;
  // 是原始 Python 异常文本 -> 不展示原文。
  // 用户能做的动作：重试 / 改参数 / 看日志。
  return "生成失败（服务端未返回可读原因）。请稍后重试；若反复失败，请改用「复用参数」重新生成，或查看服务端日志。";
}

// 【测试可见性】把这两个函数挂到 window 上 —— 让前端测试能**执行本文件里
// 真实的实现**，而不是在测试里抄一份（抄一份的话，测试就测不到本文件的
// 改动，变异「改 app.js」不会被抓住）。
// 不影响生产行为（app.js 里没有同名全局变量）。
window.__h3FriendlyError = friendlyError;
window.__h3LooksLikePythonTraceback = looksLikePythonTraceback;

// ---------- 接续生成（continuation）----------
//
// web/continuation.js 需要访问三样东西：
//   * state            —— 时长 / 步数在全局 state 里（不在 VIDEO_PANEL 里）
//   * activateVideoMode —— 切换到文生视频面板
//   * refreshJobs      —— 回填后刷新卡片（「接续」按钮的显隐依赖卡片数据）
//
// 这里集中暴露，避免 continuation.js 去猜内部结构。
// 【为什么不直接把 continuation.js 合并进 app.js】app.js 已 3800+ 行；
// 接续是一条独立链路（按钮 -> 取上下文 -> 回填 -> 展示 -> 清除），
// 独立成模块才能单独测，也才不至于和「复用参数」逻辑搅在一起。
window.__h3State = state;
window.activateVideoMode = activateVideoMode;
window.refreshJobs = refreshJobs;

// 接续能力探测：Relay-Kit 节点在不在。
//
// 【为什么不据此决定按钮显隐】按钮显隐完全听服务端的逐任务判定
// （continuation_info.can_continue）—— 那是权威，且没有异步竞态。
// 能力探测只用于「本机到底缺不缺节点」的诊断信息。
if (window.Continuation && typeof window.Continuation.loadCapability === "function") {
  window.Continuation.loadCapability();
}
