/* H3 Studio —— 视频面板「参考素材槽位」的编号与占位标记同步
 *
 * 【要解决的问题】
 * 首尾帧（flf）/ 参考生视频（r2v）面板的素材槽位填满后，用户想换掉
 * 其中一条**没有任何入口** —— 只能去素材库删素材，或者切模式清空整块。
 * 本模块提供「移除某条槽位素材」所需要的全部纯逻辑：
 *   1. 同组素材**前移**（保持编号连续，不留空洞）；
 *   2. 提示词里的占位标记同步重写（被移除那条的标记删掉，
 *      其余按「旧序号 -> 新序号」单遍替换，不留悬空引用）。
 *
 * 【为什么占位标记的序号语义必须知道 T8 节点的接法】
 * H3 视频的参考素材在 ComfyUI 侧由 T8 条件节点
 * （comfyui-minimax-h3-audio-T8/h3_t8/conditioning.py）接进文本编码器，
 * 编号规则是**按类型分别计数**：
 *   * 图片 -> <Picture 1> <Picture 2> ...
 *     （flf 的首/尾帧是 keyframe，排在参考图**前面**；r2v 只有参考图）
 *   * 视频 -> <Video 1> <Video 2> ...
 *   * 音频 -> <Audio 1> <Audio 2> ...
 *     （音轨来源选「参考视频背景音」时，**每个参考视频的背景音**
 *       排在独立参考音乐**前面**，且与视频一一对应）
 * 并且 T8 的 prepare_prompt() 在 strict 模式下**硬失败**：
 *   "<Picture 2> is not connected; available picture count is 1"
 * —— 也就是说，移除素材后如果提示词里的序号没同步，任务会直接报错。
 *
 * 【身份怎么认 —— 这是本模块最容易错的地方】
 * 移除一条槽位后，同组后面的素材会**前移**（ref2 的素材搬到 ref1）。
 * 如果用「当前槽位 key」当身份，前移就会让身份跟着变 ——
 * 旧 ref2 的素材在新列表里住进了 ref1，于是「谁是谁」全乱了。
 * 如果用 asset_id 当身份，同一张图同时放在首帧和尾帧时又分不清。
 *
 * 正确做法：**身份 = 变更前那一轮的条目对象本身**。
 * 本模块先拍一份「变更前」快照（每个条目带唯一 uid），
 * 变更后**沿用同一批条目对象**（只重新编号），uid 不变。
 * 于是「旧序号 -> 新序号」的映射是可靠的。
 *
 * 【边界：只动 H3 视频的标记语法】
 * Qwen-Image 的 <imageN>（图片编辑面板的语法）与 H3 视频的 <Picture N>
 * 是两套东西，但**在视频模式里** T8 会把 <Image N> 规范化成 <Picture N>
 * （见 h3_t8/prompt_tags.py 的 canonicalize_media_tags）—— 两者在视频
 * 提示词里指的是同一个东西，所以视频模式的重写必须把 <Image N> 一并算进去。
 * 反过来，**本模块只在视频模式被调用**，绝不会碰到 Qwen 图片编辑面板
 * 自己管理的 <imageN> 列表（那是 web/qwen-refs.js 的职责）。
 */
(function () {
  "use strict";

  // ---------- 槽位分组 ----------

  /**
   * 槽位 key -> 组。
   *   frame     首帧 / 尾帧（flf）
   *   ref_image 参考图（r2v）
   *   ref_video 参考视频（flf / r2v）
   *   ref_audio 参考音乐（flf / r2v）
   */
  function groupOfKey(key) {
    if (key === "first" || key === "last") return "frame";
    if (/^ref\d+$/.test(key)) return "ref_image";
    if (/^refvid\d+$/.test(key)) return "ref_video";
    if (/^refaud\d+$/.test(key)) return "ref_audio";
    return null;
  }

  /**
   * 组 -> 槽位 key 列表（按序号升序）。
   *
   * 【为什么槽位 key 与「参考编号」必须对齐】
   * 面板上的标签是「参考 1」「参考 2」…，对应 ref0 / ref1 / …。
   * 组内素材**前移**后，「参考 2」里装的就是原来「参考 3」的素材 ——
   * 界面编号与送进模型的序号始终一致。
   */
  function groupKeys(group) {
    if (group === "frame") return ["first", "last"];
    if (group === "ref_image") {
      const out = [];
      for (let i = 0; i < 9; i++) out.push("ref" + i);
      return out;
    }
    const prefix = group === "ref_video" ? "refvid" : "refaud";
    const out = [];
    for (let i = 0; i < 3; i++) out.push(prefix + i);
    return out;
  }

  function filledKeys(group, refs) {
    return groupKeys(group).filter(k => refs[k] && refs[k].asset_id);
  }

  // ---------- 素材在模型里的顺序 ----------

  /**
   * 当前会被送进模型的素材，按 T8 的编号规则排好。
   *
   * @param {string} mode              当前模式（flf / r2v）
   * @param {object} assetRefs         state.assetRefs
   * @param {string} audioTrackSource  音轨来源（"" / "reference" / "video_bg"）
   * @returns {{pictures: Array, videos: Array, audios: Array}}
   *   每个 entry = {uid, key, n, assetId}
   *     uid     —— **身份**。类型内唯一（类型 + 槽位 key [+ 派生标记]）。
   *                变更前后沿用同一批条目对象 -> uid 不变。
   *     key     —— 面板槽位 key
   *     n       —— T8 的 1 基序号
   *     assetId —— 素材库 ID
   */
  function mediaOrder(mode, assetRefs, audioTrackSource) {
    const refs = assetRefs || {};
    const pictures = [];
    const videos = [];
    const audios = [];

    // 图片：flf 是首/尾帧（keyframe 顺序 [first, last]），r2v 是参考图
    const picKeys = mode === "flf" ? filledKeys("frame", refs) : filledKeys("ref_image", refs);
    picKeys.forEach((key, i) => {
      pictures.push({
        uid: "pictures:" + key,
        key, n: i + 1,
        assetId: refs[key].asset_id,
      });
    });

    // 视频
    filledKeys("ref_video", refs).forEach((key, i) => {
      videos.push({
        uid: "videos:" + key,
        key, n: i + 1,
        assetId: refs[key].asset_id,
      });
    });

    // 音频
    // 【音轨来源选「参考视频背景音」时的额外一路】
    // 每个参考视频的背景音也会作为 <Audio N> 进条件节点，且排在
    // 独立参考音乐**前面**、与视频一一对应（服务端把参考视频的音轨
    // 抽出来放进 ref_video_audios，节点按序号配对）。
    //
    // 【口径说明】服务端抽音轨需要 ffmpeg 且视频真有音轨；前端拿不到
    // 探测结果，所以这里按**用户的意图**（每个参考视频都有背景音）建模。
    // 若某个参考视频实际没有音轨，服务端会把音轨回落成「生成的音乐」，
    // 那时 T8 自己会报出序号不匹配 —— 与改动前的行为同源。
    if (audioTrackSource === "video_bg") {
      videos.forEach(v => {
        audios.push({
          uid: "audios:soundtrack:" + v.key,
          key: v.key, n: audios.length + 1,
          assetId: v.assetId,
          derived: true,
        });
      });
    }
    filledKeys("ref_audio", refs).forEach(key => {
      audios.push({
        uid: "audios:" + key,
        key, n: audios.length + 1,
        assetId: refs[key].asset_id,
      });
    });

    return {pictures, videos, audios};
  }

  // ---------- 移除一条槽位素材 ----------

  /**
   * 移除某个槽位里的素材，并把**同组**后面的素材前移（编号保持连续）。
   *
   * @returns {object|null} 被移除的素材（{asset_id, name, category}）
   *
   * 【首/尾帧不前移】first / last 各自独立（首帧就是首帧，不存在
   * 「第二张首帧」），删掉就是删掉。但**提示词里的 <Picture N> 仍要
   * 跟着重排**（首帧没了，尾帧就从 <Picture 2> 变成 <Picture 1>）——
   * 那部分由 rewriteTags() 按 mediaOrder() 的前后快照负责。
   */
  function applyRemoval(assetRefs, key) {
    const group = groupOfKey(key);
    if (!group) return null;
    const removed = assetRefs[key] || null;
    if (!removed || !removed.asset_id) return null;
    delete assetRefs[key];
    if (group === "frame") return removed;

    const keys = groupKeys(group);
    const rest = keys.filter(k => k !== key && assetRefs[k] && assetRefs[k].asset_id);
    const values = rest.map(k => assetRefs[k]);
    // 【为什么整组删掉再重建，而不是就地赋值】
    // 提交侧（app.js 的 asset_refs 构建）是按 **Object.entries 的插入
    // 顺序**给同组素材编 ordinal 的。如果就地赋值（ref0 = ref1、ref1 = ref2），
    // 被删掉的 ref0 会被**重新插入到末尾** -> 顺序变成 ref1、ref2、ref0
    // -> ordinal 0 给了「参考 2」、ordinal 1 给了「参考 1」
    // -> 送进模型的图序与界面上的编号**对不上**（而且很难察觉）。
    // 整组删掉再按规范顺序重建 -> 插入顺序恒等于槽位号顺序。
    keys.forEach(k => { delete assetRefs[k]; });
    values.forEach((value, i) => { assetRefs[keys[i]] = value; });
    return removed;
  }

  /**
   * 规划一次移除：**先算清楚**再改状态。
   *
   * 返回的 before / after 是**同一批条目对象**（uid 不变），
   * rewriteTags() 靠 uid 把「旧序号」对上「新序号」。
   *
   * @returns {object|null}
   *   {removed, before:{pictures,videos,audios}, after:{...},
   *    mapping:{pictures:{uid->n|null}, videos:{...}, audios:{...}}}
   */
  function planRemoval(mode, assetRefs, audioTrackSource, key) {
    const before = mediaOrder(mode, assetRefs, audioTrackSource);
    const current = (assetRefs || {})[key];
    if (!current || !current.asset_id) return null;

    // 找出「这次移除会让哪些条目消失」。
    //   * 参考图 / 首帧 / 尾帧 / 参考视频 / 参考音乐 -> 自己的那一条；
    //   * 参考视频 + 音轨来源为「参考视频背景音」-> 视频**和它的背景音**
    //     一起消失（背景音是从视频里抽出来的，视频没了就没了）。
    //
    // 【为什么靠 key 匹配就够了】每条条目的 key 就是它所在的槽位。
    // 派生的背景音条目 key 也指向那个视频的槽位（refvidN），
    // 所以「移除 refvid0」会同时命中视频本身和它的背景音 ——
    // 这正是我们要的语义。
    const removedUids = new Set();
    ["pictures", "videos", "audios"].forEach(type => {
      (before[type] || []).forEach(entry => {
        if (entry.key !== key) return;
        removedUids.add(entry.uid);
      });
    });

    // after：沿用同一批条目对象，只把序号重新编成 1..k。
    //
    // 【为什么必须用「新对象」而不是原地改 entry.n】
    // before 与 after 共享同一批条目对象。如果原地把 entry.n 改成新序号，
    // before 里的**旧序号**也被改了 —— rewriteTags() 再按「before 里
    // 第 n 个是谁」去找，就找错了人（实测：<Picture 2> 会被当成
    // 原来第 2 条而不是新第 2 条，序号整体错位一位）。
    // 所以这里另建一批对象：uid 相同（身份一致）、n 不同（序号变了）。
    const after = {};
    const mapping = {};
    ["pictures", "videos", "audios"].forEach(type => {
      const oldList = before[type] || [];
      const kept = oldList.filter(e => !removedUids.has(e.uid));
      // after 的条目按**新顺序**排 —— 序号就是 kept 里的下标 + 1。
      after[type] = kept.map((entry, i) => ({
        uid: entry.uid,
        key: entry.key,
        assetId: entry.assetId,
        derived: entry.derived,
        n: i + 1,
      }));
      // mapping：uid -> 新序号（被移除的 -> null）。
      // 【关键】新序号必须取 **after** 里的 n，不能取 oldList 里的 n ——
      // oldList 里的 n 是**旧**序号（3 张图删掉第 1 张后，
      // 原来第 2 张的新序号是 1，而不是 2）。
      const newNByUid = new Map();
      after[type].forEach(entry => newNByUid.set(entry.uid, entry.n));
      const m = {};
      oldList.forEach(entry => {
        m[entry.uid] = removedUids.has(entry.uid) ? null : newNByUid.get(entry.uid);
      });
      mapping[type] = m;
    });

    return {
      removed: assetRefs[key] || null,
      before, after, mapping,
    };
  }

  // ---------- 提示词占位标记重写 ----------

  // H3 视频的官方标记语法：<Picture N> / <Video N> / <Audio N>
  //
  // 【为什么把 <Image N> 也算进来】T8 的 canonicalize_media_tags()
  // 会把 <Image N> 规范化成 <Picture N>（image 与 picture 同义）——
  // 在**视频**提示词里两者指的是同一个东西。所以视频模式的重写
  // 必须把 <Image N> 一并算进去，否则用户写的 <image 2> 在移除素材后
  // 就会变成悬空引用。
  //
  // 【边界】本模块只在视频模式被调用 —— Qwen 图片编辑面板的 <imageN>
  // 由 web/qwen-refs.js 单独管理，两边不会互相干扰。
  //
  // 【为什么要保留分隔符（下面第 2 个捕获组）】
  // Qwen 的占位正则是 `/<image(\d+)>/` —— **小写、无空格**。
  // 如果重写时统一输出 "<Picture 1>"，用户在 Qwen 面板里写的
  // "<image1>" 就会被改成 "<Picture 1>" —— 切回 Qwen 模式后
  // 那个正则扫不到，整条提示词被判成「缺少 <image1> 占位标记」。
  // 所以这里**只换数字**，类型文字与分隔符原样保留。
  const MEDIA_TAG_RE = /<\s*(Picture|Image|Video|Audio)(\s*)(\d+)(\s*)>/gi;

  function canonicalType(typeRaw) {
    const t = typeRaw.toLowerCase();
    // image 与 picture 是同义（官方两套叫法），统一算 pictures。
    if (t === "picture" || t === "image") return "Picture";
    if (t === "video") return "Video";
    if (t === "audio") return "Audio";
    return typeRaw;
  }

  function typeGroup(canonical) {
    if (canonical === "Picture") return "pictures";
    if (canonical === "Video") return "videos";
    if (canonical === "Audio") return "audios";
    return null;
  }

  /**
   * 按「变更前 / 变更后」的素材顺序，重写提示词里的占位标记。
   *
   * 规则（与 qwen-refs.js 的 reindexPrompt 同一套）：
   *   * 旧序号对应的素材还在 -> 换成新序号；
   *   * 旧序号对应的素材被删了 -> **整个标记删掉**（不留悬空引用）；
   *   * 提示词里的序号不在本次变更范围内（用户手写超出的）-> 原样留着，
   *     由 T8 自己的校验去报，不由前端偷偷改写。
   *
   * 【只换数字，不改写法】类型文字（Picture / image / Video / Audio）与
   * 分隔符（空格 / 无空格）全部原样保留 —— 用户的写法不该被前端改掉。
   *
   * @param {string} text
   * @param {object} plan planRemoval() 的返回值
   */
  function rewriteTags(text, plan) {
    const source = text || "";
    if (!source || !plan || !plan.mapping) return source;

    let removedAny = false;
    const next = source.replace(
      MEDIA_TAG_RE,
      (whole, typeRaw, sep, numRaw, tail) => {
        const canonical = canonicalType(typeRaw);
        const group = typeGroup(canonical);
        if (!group) return whole;
        const m = plan.mapping[group];
        if (!m) return whole;
        const n = Number(numRaw);
        // 找到「变更前第 n 个」是哪条素材（uid）。
        const entry = (plan.before[group] || []).find(e => e.n === n);
        if (!entry) return whole;         // 不在本次变更范围 -> 原样保留
        const nextN = m[entry.uid];
        if (nextN === null || nextN === undefined) {
          removedAny = true;
          return "";                      // 被移除的那条 -> 标记删掉
        }
        // 原样保留类型文字与分隔符，只换数字。
        return `<${typeRaw}${sep}${nextN}${tail}>`;
      });

    if (!removedAny) return next;
    // 清理删标记后留下的多余空格 / 连续空行（与 qwen-refs.js 同口径）
    return next.replace(/[ \t]{2,}/g, " ").replace(/\n{3,}/g, "\n\n");
  }

  window.RefSlots = {
    groupOfKey,
    groupKeys,
    mediaOrder,
    applyRemoval,
    planRemoval,
    rewriteTags,
  };
})();
