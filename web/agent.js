// 内置多模态 Agent —— 前端面板
//
// 职责边界：
//   * 只做「发消息 + 渲染结构化结果 + 把产物回填到创作面板」
//   * 不在前端做任何智能判断 —— 模式白名单、参数收敛、参考图约束
//     全部在服务端（h3studio/agent.py + server.prepare_job_params）
//
// 为什么单独一个文件而不是塞进 app.js：
//   app.js 已经 3900+ 行，且是「创作页」的逻辑。Agent 面板是独立页面，
//   独立成文件后可以单独加载/单独排错，也避免 app.js 的启动异常
//   （历史上出现过一次 esc() 未定义导致整页脚本中断）波及 Agent。
(function () {
  const $ = s => document.querySelector(s);
  const $$ = s => [...document.querySelectorAll(s)];

  const state = {
    sessionId: "",
    busy: false,
    messages: [],       // {role, content, jobs?, assets?, prompt?, steps?, tools?}
    lastResult: null,
    // 正在流式渲染的那条消息（增量渲染的锚点）
    streaming: null,
    // 【本轮待发送的附件 —— 2026-11-11】
    // 上传完成、**还没随消息发出**的媒体。
    // 每项：{asset_id, name, preview_url, description, tags, width, height,
    //       kind, duration_sec, analysis_status, available, analysis_job_id}
    // kind = image | audio | video（素材库里的真实类别）。
    // analysis_status = done（就绪）| pending（分析中）| failed（失败）。
    // 发出后由 send() 清空（附件只属于一条消息）。
    attachments: [],
    // 附件区的临时状态（上传中 / 出错）
    attachError: "",
    attachBusy: false,
    // 附件处理阶段的文案（「正在入库素材库…」「正在生成描述与标签…」）
    attachStage: "",
    // 音视频**异步分析**的进度：{asset_id: {status, progress, stage}}
    attachJobs: {},
  };

  // ---------- 附件（图片 / 音频 / 视频上传） ----------

  // 一次最多几张图片（与服务端 agent_media.MAX_IMAGES_PER_UPLOAD 对齐）。
  //
  // 【为什么要前端也限】
  // 前端限是为了**即时反馈**（用户选到第 10 张时立刻提示），
  // 不是安全边界 —— 服务端还会再限一次。
  const MAX_ATTACHMENTS = 9;

  // 一次最多几个音频/视频（与服务端 agent_media.MAX_MEDIA_FILES_PER_UPLOAD 对齐）。
  //
  // 【为什么单独计数、且比图片少】
  // H3 参考生成节点的槽位上限是 ref_videos 0~3 / ref_audios 0~3；
  // 而每个参考视频/音频都整段参与推理（不像参考图只进图像编码器），
  // 一次拖十几个几百 MB 的视频会把队列压垮。
  const MAX_MEDIA_ATTACHMENTS = 4;

  // 允许的文件类型（与 agent_media.ALLOWED_*_SUFFIXES 对齐）。
  const ATTACH_ACCEPT = [
    "image/png", "image/jpeg", "image/webp", "image/bmp", "image/gif",
    "audio/wav", "audio/mpeg", "audio/mp3", "audio/flac", "audio/x-flac",
    "audio/mp4", "audio/m4a", "audio/aac", "audio/ogg", "audio/opus",
    "video/mp4", "video/webm", "video/quicktime", "video/x-matroska",
    "video/x-msvideo",
  ].join(",");

  // 单文件大小上限（与服务端 agent_media.MAX_*_BYTES 对齐）。
  const MAX_ATTACH_BYTES = 20 * 1024 * 1024;
  const MAX_AUDIO_ATTACH_BYTES = 100 * 1024 * 1024;
  const MAX_VIDEO_ATTACH_BYTES = 500 * 1024 * 1024;

  // 媒体类型 -> 中文标签（卡片与提示共用）。
  const KIND_LABEL = { image: "图片", audio: "音频", video: "视频" };

  // 媒体类型 -> 该类型的单文件上限。
  function maxBytesForKind(kind) {
    if (kind === "audio") return MAX_AUDIO_ATTACH_BYTES;
    if (kind === "video") return MAX_VIDEO_ATTACH_BYTES;
    return MAX_ATTACH_BYTES;
  }

  // 文件名后缀 -> 媒体类型（与 agent_media._media_kind_for_suffix 同语义）。
  function kindForFile(file) {
    const name = ((file && file.name) || "").toLowerCase();
    const type = ((file && file.type) || "").toLowerCase();
    if (type.startsWith("image/")) return "image";
    if (type.startsWith("audio/")) return "audio";
    if (type.startsWith("video/")) return "video";
    if (/\.(png|jpe?g|webp|bmp|gif)$/.test(name)) return "image";
    if (/\.(wav|mp3|flac|m4a|aac|ogg|opus|aiff?|wma)$/.test(name)) return "audio";
    if (/\.(mp4|webm|mov|mkv|m4v|avi)$/.test(name)) return "video";
    return "";
  }

  // 时长格式化（秒 -> 0:12 / 1:05）。
  function fmtDuration(seconds) {
    const value = Number(seconds);
    if (!isFinite(value) || value <= 0) return "";
    const total = Math.round(value);
    const m = Math.floor(total / 60);
    const s = total % 60;
    return m + ":" + String(s).padStart(2, "0");
  }

  // 一个附件的「就绪」状态。
  //
  // 【为什么要分三种状态而不是布尔】
  //   ready    —— 分析完成，可以引用
  //   pending  —— 分析在跑，**还不能引用**（前端要拦住发送）
  //   failed   —— 分析失败，不可用
  // 图片上传是同步预处理的，上传完就是 ready；
  // 音视频走异步 job，要轮询到 done 才 ready。
  function attachStatus(item) {
    if (!item) return "ready";
    if (item.analysis_status === "failed") return "failed";
    if (item.analysis_pending || item.analysis_status === "pending"
        || item.analysis_status === "queued"
        || item.analysis_status === "running") return "pending";
    if (item.available === false && item.kind !== "image") return "pending";
    return "ready";
  }

  function renderAttachments() {
    const box = $("#agAttachments");
    const hint = $("#agAttachHint");
    const btn = $("#agAttachBtn");
    if (!box) return;
    const items = state.attachments;
    const pending = items.filter(it => attachStatus(it) === "pending");
    const failed = items.filter(it => attachStatus(it) === "failed");
    if (!items.length && !state.attachError && !state.attachBusy) {
      box.hidden = true;
      box.innerHTML = "";
      if (hint) hint.textContent = "上传的图片 / 音频 / 视频会入库素材库后交给 Agent（也可以直接 Ctrl+V 粘贴）";
      if (btn) btn.disabled = false;
      return;
    }
    box.hidden = false;
    const cards = items.map((item, index) => {
      const kind = item.kind || "image";
      const status = attachStatus(item);
      const tagHint = (item.tags && item.tags.length)
        ? ` title="${esc(item.tags.join("、"))}"` : "";
      const duration = fmtDuration(item.duration_sec);
      const meta = [];
      if (kind !== "image" && duration) meta.push(duration);
      if (item.width && item.height) meta.push(item.width + "×" + item.height);
      const metaText = meta.length ? esc(meta.join(" · ")) : "";
      // 【类型徽标 —— 卡片左上角】
      // 「分析中 / 不可用」是**状态**，优先级高于类型 ——
      // 用户要先知道「这个东西能不能用」，再关心它是什么类型。
      // 所以分析中/失败时只显示状态徽标，不显示类型徽标
      // （否则两个徽标会挤在 62px 的卡片里互相盖住）。
      const badge = status === "pending"
        ? `<span class="ag-attach-badge ag-attach-badge-pending">分析中</span>`
        : (status === "failed"
          ? `<span class="ag-attach-badge ag-attach-badge-failed">不可用</span>`
          : `<span class="ag-attach-badge ag-attach-badge-${esc(kind)}">${esc(KIND_LABEL[kind] || kind)}</span>`);
      // 【类型标签 —— 卡片右上角（× 按钮左侧）】
      // 类型徽标挪到**右上角**后，底部的时长/尺寸才有整行宽度可用 ——
      // 之前两者都在底部，「0:01 · 160×120」被压成「0:01 - 160…」（截断）。
      // 右上角这里留给类型标签（× 按钮在更右上，absolute 定位互不重叠）。
      const kindTag = (status === "ready" && kind !== "image")
        ? `<span class="ag-attach-kind">${esc(KIND_LABEL[kind] || kind)}</span>`
        : "";
      const progress = status === "pending"
        ? (() => {
            const job = state.attachJobs[item.asset_id] || {};
            const pct = Math.round((Number(job.progress) || 0) * 100);
            return `<div class="ag-attach-progress"><span style="width:${pct}%"></span></div>`;
          })()
        : "";
      return `
      <div class="ag-attach-thumb" data-asset-id="${esc(item.asset_id || "")}"
           data-kind="${esc(kind)}" data-status="${esc(status)}"${tagHint}>
        <img src="${esc(item.preview_url || "")}" alt="${esc(item.name || "附件")}"
             loading="lazy">
        ${badge}
        ${kindTag}
        ${metaText ? `<span class="ag-attach-meta">${metaText}</span>` : ""}
        ${progress}
        <button type="button" class="ag-attach-remove" data-act="remove-attachment"
                data-index="${index}" title="移除"
                aria-label="移除附件">×</button>
      </div>`;
    }).join("");
    // 处理中：明确告知在做什么、大概要多久
    const busy = state.attachBusy
      ? `<div class="ag-attach-busy">
           <span class="ag-attach-spinner"></span>
           <span>${esc(state.attachStage || "正在处理…")}素材库需要生成描述与标签，
             本地模型约 1~3 分钟/张，请耐心等待</span>
         </div>` : "";
    const notes = [];
    if (pending.length) {
      notes.push(`<div class="ag-attach-note">${pending.length} 个素材正在分析，
        分析完成前不能作为参考素材引用</div>`);
    }
    if (failed.length) {
      notes.push(`<div class="ag-attach-err">${failed.length} 个素材分析失败，
        不可用</div>`);
    }
    box.innerHTML = cards + busy + notes.join("")
      + (state.attachError
        ? `<div class="ag-attach-err">${esc(state.attachError)}</div>` : "");
    if (hint) {
      if (state.attachBusy) {
        hint.textContent = (state.attachStage || "正在处理…");
      } else if (pending.length) {
        hint.textContent = `已附 ${items.length} 个素材，${pending.length} 个仍在分析`;
      } else {
        const kinds = {};
        items.forEach(it => { kinds[it.kind || "image"] = true; });
        const summary = Object.keys(kinds).map(k => KIND_LABEL[k] || k).join("/");
        hint.textContent = items.length
          ? `已附 ${items.length} 个${summary ? "（" + summary + "）" : ""}，Agent 会引用它们`
          : "上传的图片 / 音频 / 视频会入库素材库后交给 Agent";
      }
    }
    if (btn) {
      btn.disabled = state.attachBusy;
      btn.textContent = state.attachBusy ? "处理中…" : "📎 媒体";
    }
  }

  // 校验一个待上传的文件。返回错误字符串，null = 通过。
  function validateAttachFile(file) {
    const name = (file && file.name) || "";
    const kind = kindForFile(file);
    if (!kind) {
      return `「${name}」不是支持的格式（图片 png/jpg/webp/bmp/gif；`
        + `音频 wav/mp3/flac/m4a/ogg；视频 mp4/webm/mov/mkv）`;
    }
    const limit = maxBytesForKind(kind);
    if (file.size > limit) {
      return `「${name}」超过 ${limit / (1024 * 1024)} MB 上限`
        + `（${KIND_LABEL[kind]}上限 ${limit / (1024 * 1024)} MB）`;
    }
    if (file.size <= 0) {
      return `「${name}」是空文件`;
    }
    return null;
  }

  async function uploadAttachments(files) {
    const list = [...(files || [])];
    if (!list.length) return;
    state.attachError = "";
    // 图片与音视频**分开**算额度
    const images = state.attachments.filter(a => (a.kind || "image") === "image");
    const medias = state.attachments.filter(a => (a.kind || "image") !== "image");
    const imageRoom = MAX_ATTACHMENTS - images.length;
    const mediaRoom = MAX_MEDIA_ATTACHMENTS - medias.length;
    if (imageRoom <= 0 && mediaRoom <= 0) {
      state.attachError = `一次最多附 ${MAX_ATTACHMENTS} 张图、`
        + `${MAX_MEDIA_ATTACHMENTS} 个音频/视频，请先移除一些`;
      renderAttachments();
      return;
    }
    const errors = [];
    const form = new FormData();
    let usedImages = 0;
    let usedMedias = 0;
    let dropped = 0;
    for (const file of list) {
      const kind = kindForFile(file);
      const err = validateAttachFile(file);
      if (err) { errors.push(err); continue; }
      if (kind === "image") {
        if (usedImages >= imageRoom) { dropped++; continue; }
        usedImages++;
      } else {
        if (usedMedias >= mediaRoom) { dropped++; continue; }
        usedMedias++;
      }
      form.append("files[]", file, file.name);
    }
    if (dropped) {
      errors.push(`超出数量上限（最多 ${MAX_ATTACHMENTS} 张图、`
        + `${MAX_MEDIA_ATTACHMENTS} 个音频/视频），${dropped} 个未上传`);
    }
    if (!form.has("files[]")) {
      state.attachError = errors.join("；") || "没有可上传的文件";
      renderAttachments();
      return;
    }
    state.attachBusy = true;
    // 【素材库预处理要时间 —— 2026-11-11】
    // 图片：服务端**同步**跑素材库的预处理管线（多模态分析 + 抽标签），
    // 实测一张 512x512 的图约需 **120~180 秒**（本地推理）。
    // 音视频：**异步** job（深度音频分析 / 视频抽帧多模态），
    // 上传立刻返回，之后由前端轮询 /api/agent/media/{id}/status。
    // 所以提示必须说清楚「在做什么、大概要多久」，否则用户以为卡死。
    state.attachStage = "正在入库素材库…";
    renderAttachments();
    try {
      const resp = await fetch("/api/agent/upload", { method: "POST", body: form });
      const raw = await resp.text();
      let data;
      try { data = raw ? JSON.parse(raw) : {}; }
      catch (e) { data = { error: raw || "服务端返回了非 JSON 响应" }; }
      if (!resp.ok) throw new Error(data.error || ("上传失败（HTTP " + resp.status + "）"));
      const results = data.results || [];
      for (const item of results) {
        const entry = {
          asset_id: item.asset_id,
          name: item.name || "",
          preview_url: item.preview_url || "",
          description: item.description || "",
          tags: item.tags || [],
          width: item.width || null,
          height: item.height || null,
          kind: item.kind || "image",
          duration_sec: item.duration_sec || null,
          analysis_status: item.analysis_status || "done",
          available: item.available !== false,
          analysis_job_id: item.analysis_job_id || null,
          analysis_pending: !!item.analysis_pending,
        };
        state.attachments.push(entry);
        if (entry.analysis_pending) {
          state.attachJobs[entry.asset_id] = { status: "queued", progress: 0 };
        }
      }
      const serverErrors = (data.errors || []).map(e => e.error).filter(Boolean);
      state.attachError = errors.concat(serverErrors).join("；");
      // 有音视频在分析 -> 开始轮询
      if (state.attachments.some(it => attachStatus(it) === "pending")) {
        pollMediaStatus();
      }
    } catch (err) {
      state.attachError = err.message || "上传失败";
    } finally {
      state.attachBusy = false;
      state.attachStage = "";
      renderAttachments();
    }
  }

  // 轮询音视频的分析就绪状态。
  //
  // 【为什么要轮询而不是让服务端阻塞】
  // 深度音频分析实测 116 秒素材要 ~600 秒；视频要抽 24 帧再调多模态。
  // 服务端阻塞 = HTTP 请求挂十几分钟，用户早就走了。
  //
  // 【为什么单独一个端点而不是复用 /api/assets/{id}/analysis-status】
  // 素材库那个端点只返回 job 状态，**不补标签**；
  // 而 Agent 侧要求「分析完成」与「标签已落库」是同一次返回。
  // /api/agent/media/{id}/status 两者都做。
  let mediaPollTimer = null;
  function pollMediaStatus() {
    if (mediaPollTimer) return;
    mediaPollTimer = setInterval(async () => {
      const pending = state.attachments.filter(it => attachStatus(it) === "pending");
      if (!pending.length) {
        clearInterval(mediaPollTimer);
        mediaPollTimer = null;
        renderAttachments();
        return;
      }
      for (const item of pending) {
        try {
          const resp = await fetch(
            "/api/agent/media/" + encodeURIComponent(item.asset_id) + "/status");
          if (!resp.ok) continue;
          const data = await resp.json();
          state.attachJobs[item.asset_id] = {
            status: data.status || "",
            progress: data.progress || 0,
            stage: data.stage || "",
          };
          if (data.status === "done") {
            item.analysis_status = "done";
            item.analysis_pending = false;
            item.available = data.available !== false;
            item.description = data.description || item.description;
            item.tags = data.tags || item.tags;
            item.duration_sec = data.duration_sec || item.duration_sec;
          } else if (data.status === "failed") {
            item.analysis_status = "failed";
            item.analysis_pending = false;
            item.available = false;
            item.analysis_error = data.error || "分析失败";
            state.attachError = `「${item.name || item.asset_id}」分析失败：`
              + (item.analysis_error || "未知原因");
          }
        } catch (e) {
          // 网络抖动 —— 下个周期再试，不打断整体
        }
      }
      renderAttachments();
    }, 3000);
  }

  function removeAttachment(index) {
    const idx = Number(index);
    if (!Number.isInteger(idx) || idx < 0 || idx >= state.attachments.length) return;
    state.attachments.splice(idx, 1);
    state.attachError = "";
    renderAttachments();
  }

  // ------------------------------------------------------------------
  // 剪贴板粘贴媒体（2026-11-13 审查目标 3）
  // ------------------------------------------------------------------
  //
  // 【为什么不能只支持文本粘贴】
  // 用户的真实工作流是「在别的应用里复制一段视频/一张图/一段音频，
  // 回到工作台直接 Ctrl+V」—— 原来只有文件选择器与拖拽，
  // 粘贴这一条最顺手的路是断的。
  //
  // 【怎么拿媒体】
  //
  // ⚠️ 【注释里的坑 —— 2026-11-14 实测踩过】
  // JS 注释里**绝对不能**出现块注释的开闭定界符（斜杠 + 星号）。
  // 本文件原来有一行注释写了 MIME 通配符（image 斜杠 星号），
  // 那个定界符被引擎当成**块注释开始**，把后面 400 多行代码全吞了 ——
  // 语法完全合法、浏览器不报错，但所有函数定义都消失了（页面能加载，
  // 逻辑全哑）。是 tests/test_agent_legacy_fixes.py 抓到的。
  // 所以下面写 MIME 时用「image 类型」而不是通配符写法。
  //  1) 首选 clipboardData.items —— 里面有 image/png 之类的**文件条目**，
  //     getAsFile() 拿到的就是真文件（不是 dataURL 字符串）；
  //  2) 退路是 clipboardData.files（部分浏览器只给这个）；
  //  3) 退路是 items 里的 string（image 类型 MIME 的 base64 dataURL）——
  //     手动转成 File 再上传。
  //
  // 【只处理媒体，纯文本照常粘贴】
  // 文本走浏览器默认行为（不 preventDefault），
  // 否则用户粘贴一段话会没反应。

  const PASTE_EXT_BY_TYPE = {
    "image/png": "png", "image/jpeg": "jpg", "image/webp": "webp",
    "image/bmp": "bmp", "image/gif": "gif",
  };

  function filesFromClipboard(event) {
    const dt = event.clipboardData;
    if (!dt) return [];
    const out = [];
    // 1) items 里的文件条目
    const items = dt.items || [];
    for (let i = 0; i < items.length; i++) {
      const item = items[i];
      if (!item || !item.kind || item.kind !== "file") continue;
      const file = item.getAsFile && item.getAsFile();
      if (file && file.size > 0) out.push(file);
    }
    if (out.length) return out;
    // 2) files 列表
    if (dt.files && dt.files.length) {
      for (let i = 0; i < dt.files.length; i++) {
        const file = dt.files[i];
        if (file && file.size > 0) out.push(file);
      }
      if (out.length) return out;
    }
    // 3) items 里的 image 类型 MIME 字符串（dataURL）
    for (let i = 0; i < items.length; i++) {
      const item = items[i];
      if (!item || item.kind !== "string") continue;
      const type = String(item.type || "");
      if (!type.startsWith("image/")) continue;
      const text = item.getAsString && item.getAsString();
      if (!text || !text.startsWith("data:")) continue;
      const comma = text.indexOf(",");
      if (comma < 0) continue;
      const meta = text.slice(5, comma);
      const mime = meta.split(";")[0] || "image/png";
      const isBase64 = meta.indexOf("base64") >= 0;
      const payload = text.slice(comma + 1);
      try {
        const binary = isBase64 ? atob(payload) : decodeURIComponent(payload);
        const bytes = new Uint8Array(binary.length);
        for (let k = 0; k < binary.length; k++) bytes[k] = binary.charCodeAt(k);
        const ext = PASTE_EXT_BY_TYPE[mime] || "png";
        out.push(new File([bytes], `pasted_${Date.now()}_${k || i}.${ext}`,
                          { type: mime }));
      } catch (e) {
        // dataURL 解不开就跳过这一项，不影响其它项
      }
    }
    return out;
  }

  function handlePaste(event) {
    if (!event.clipboardData) return;
    const files = filesFromClipboard(event);
    if (!files.length) return;   // 纯文本 -> 走浏览器默认粘贴
    event.preventDefault();
    const media = files.filter(f => kindForFile(f));
    if (!media.length) {
      state.attachError = "剪贴板里没有可用的图片 / 音频 / 视频";
      renderAttachments();
      return;
    }
    uploadAttachments(media);
  }

  // ---------- 工具 ----------

  function esc(text) {
    return String(text == null ? "" : text)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  function apiJSON(method, path, body) {
    const opts = { method, headers: { "Content-Type": "application/json" } };
    if (body !== undefined) opts.body = JSON.stringify(body);
    return fetch(path, opts).then(async resp => {
      const raw = await resp.text();
      let data;
      try { data = raw ? JSON.parse(raw) : {}; }
      catch (e) { data = { error: raw || "服务端返回了非 JSON 响应" }; }
      if (!resp.ok) {
        const err = new Error(data.error || ("请求失败（HTTP " + resp.status + "）"));
        err.code = data.code || "";
        err.payload = data;
        throw err;
      }
      return data;
    });
  }

  // ---------- 渲染 ----------

  function statusBadge(result) {
    const map = {
      completed: ["完成", "ok"],
      needs_clarification: ["需要澄清", "warn"],
      truncated: ["已截断", "warn"],
      failed: ["失败", "bad"],
    };
    const [label, cls] = map[result.status] || ["未知", ""];
    return `<span class="ag-badge ag-badge-${cls}">${esc(label)}</span>`;
  }

  // 任务状态 → 中文标签 + 徽标类。状态真源是服务端（jobs.status）。
  const JOB_STATUS_TEXT = {
    queued: ["排队中", "warn"],
    running: ["生成中", "warn"],
    done: ["已完成", "ok"],
    failed: ["失败", "bad"],
    canceled: ["已取消", ""],
  };

  function jobStatusBadge(status) {
    const [label, cls] = JOB_STATUS_TEXT[status] || [status || "未知", ""];
    return `<span class="ag-job-badge ag-badge-${cls || "warn"}">${esc(label)}</span>`;
  }

  // 产物链接：与 app.js 的 outputUrl 同一套规则（job_id/相对路径 -> /files/...）
  function outputUrl(jobId, output) {
    const relative = String((output && output.file) || "").replace(/\\/g, "/");
    if (!relative) return "";
    const path = relative.includes("/") ? relative : `${jobId}/${relative}`;
    return "/files/" + path.split("/").map(encodeURIComponent).join("/");
  }

  // 任务卡：job id / 模式 / 状态 / 关键参数 / 产物缩略图。
  //
  // 关键设计：
  //   1) **状态由前端轮询 GET /api/jobs/{id} 实时更新**，
  //      不依赖任何「一次性返回」—— 这是修掉 .model-run-token 失效导致
  //      任务卡不更新的核心（详见 docs/内置多模态Agent-前端增强.md）。
  //   2) **参数展示的是「实际生效值」**，不是 Agent 请求的值。
  //      后端对视频分辨率 / 音乐时长会静默回落，照抄请求值就是误导。
  //      回落由 AgentMedia.effectiveParams 判定，这里负责渲染与标注。
  function renderJobs(jobs) {
    if (!jobs || !jobs.length) return "";
    const media = window.AgentMedia || null;
    const rows = jobs.map(job => {
      const status = job.status || "queued";
      const params = job.params || {};
      const mode = job.mode || "";
      // 实际生效参数（媒体语义判定）
      // 【2026-11-11】把 job 一起传进去 —— 它带着服务端算好的
      // resolution_info（按产物实际像素算的分辨率/比例标签）。
      const effective = media
        ? media.effectiveParams(mode, params, job)
        : [];
      // 能力缺口提示（例如 Agent 建不了 a2a / flf 时给替代建议）
      const limitation = media ? media.limitationFor(mode, params) : null;
      const outputs = job.outputs || [];
      const thumb = outputs.length
        ? outputs.slice(0, 3).map(out => {
            const url = outputUrl(job.job_id, out);
            if (!url) return "";
            if (out.kind === "image") {
              return `<a class="ag-job-thumb" href="${esc(url)}" target="_blank" rel="noopener"
                        title="打开大图"><img src="${esc(url)}" alt="${esc(out.name || "产物")}" loading="lazy"></a>`;
            }
            if (out.kind === "video") {
              return `<a class="ag-job-thumb" href="${esc(url)}" target="_blank" rel="noopener"
                        title="打开视频"><video src="${esc(url)}" muted preload="metadata"></video></a>`;
            }
            if (out.kind === "audio") {
              return `<a class="ag-job-thumb ag-job-thumb-audio" href="${esc(url)}" target="_blank"
                        rel="noopener" title="打开音频">♪</a>`;
            }
            return "";
          }).join("")
        : "";
      const error = job.error ? `<div class="ag-job-error">${esc(job.error)}</div>` : "";
      const progress = (status === "running" || status === "queued") && job.progress_text
        ? `<span class="ag-job-progress">${esc(job.progress_text)}</span>` : "";
      const modeLabel = media ? media.modeLabel(mode) : (job.label || mode || "");
      // 「刷新」按钮：状态靠轮询更新，但用户也可以立刻手动拉一次。
      const refreshBtn = `<button type="button" class="ag-job-refresh" data-act="refresh-job"
                            data-job="${esc(job.job_id)}" title="立即刷新状态">刷新</button>`;
      // 静默回落提示：请求值 ≠ 实际值时必须显式说明
      const notes = effective.filter(p => p.fallback && p.note);
      const noteHtml = notes.length
        ? `<div class="ag-job-note ag-job-note-warn">${notes.map(p =>
            `<div>· ${esc(p.note)}</div>`).join("")}</div>`
        : "";
      const limitHtml = limitation
        ? `<div class="ag-job-note ag-job-note-warn"><div>· ${esc(limitation.reason)}</div>
             <div>  建议：${esc(limitation.advice)}</div></div>`
        : "";
      return `
      <div class="ag-job" data-job-id="${esc(job.job_id)}" data-mode="${esc(mode)}">
        <div class="ag-job-head">
          <b>${esc(modeLabel)}</b>
          <code>${esc(job.job_id)}</code>
          ${jobStatusBadge(status)}
          ${refreshBtn}
        </div>
        ${job.prompt ? `<div class="ag-job-prompt">${esc(job.prompt)}</div>` : ""}
        ${effective.length ? `<div class="ag-job-params">${effective.map(p =>
            `<span class="ag-param${p.fallback ? " ag-param-fallback" : ""}">${esc(p.label)} ${esc(p.value)}</span>`
          ).join("")}</div>` : ""}
        ${progress}
        ${error}
        ${noteHtml}
        ${limitHtml}
        ${thumb ? `<div class="ag-job-thumbs">${thumb}</div>` : ""}
        ${job.summary ? `<div class="ag-job-note">${esc(job.summary)}</div>` : ""}
      </div>`;
    }).join("");
    return `<div class="ag-block ag-jobs">
      <div class="ag-block-title">已下发的生成任务（${jobs.length}）</div>
      ${rows}
    </div>`;
  }

  function renderAssets(assets) {
    if (!assets || !assets.length) return "";
    const rows = assets.map(a => `
      <li>
        <b>${esc(a.name || a.asset_id)}</b>
        <span class="ag-tag">${esc(a.category || "")}</span>
        ${a.tags && a.tags.length ? `<span class="ag-tag">${esc(a.tags.join("、"))}</span>` : ""}
        ${a.description ? `<div class="ag-asset-desc">${esc(a.description)}</div>` : ""}
      </li>`).join("");
    return `<div class="ag-block">
      <div class="ag-block-title">命中的素材（${assets.length}）</div>
      <ul class="ag-assets">${rows}</ul>
    </div>`;
  }

  function renderPrompt(prompt) {
    if (!prompt || !prompt.enhanced) return "";
    return `<div class="ag-block">
      <div class="ag-block-title">提示词增强产物</div>
      <div class="ag-prompt-orig">${esc(prompt.original || "")}</div>
      <div class="ag-prompt-new">${esc(prompt.enhanced || "")}</div>
    </div>`;
  }

  // 工具调用轨迹（服务端一次性返回 steps 时的降级渲染）。
  function renderSteps(steps, msgIndex) {
    if (!steps || !steps.length) return "";
    const mi = msgIndex == null ? 0 : msgIndex;
    const rows = steps.map(s => {
      const name = s.tool || "（结论）";
      const cls = s.ok ? "ok" : "bad";
      const err = s.error ? ` — ${esc(s.error)}` : "";
      return `<li class="ag-step ag-step-${cls}">
        <span class="ag-step-n">${esc(s.step)}</span>
        <span class="ag-step-name">${esc(name)}</span>
        <span class="ag-step-ms">${esc(s.ms)}ms</span>
        ${err ? `<span class="ag-step-err">${err}</span>` : ""}
      </li>`;
    }).join("");
    return `<details class="ag-block ag-steps" data-detail-key="${detailScope()}m${mi}-steps">
      <summary class="ag-block-title">工具调用轨迹（${steps.length}）</summary>
      <ul class="ag-step-list">${rows}</ul>
    </details>`;
  }

  // 工具调用可视化：工具名 + 关键参数 + 结果摘要 + 成功/失败态，**可折叠**。
  //
  // 数据来源优先级：
  //   1) 流式事件 tool_call / tool_result（增量，实时）
  //   2) 一次性结果里的 steps（降级）
  //
  // 折叠状态：每个工具块独立折叠（key = "m<消息序号>-tool-<序号>"），
  // 整体容器另有自己的折叠态（key = "m<消息序号>-tools"）。
  // key 里必须带消息序号 —— 否则不同消息里的 tool-0 会撞成同一个 key，
  // 展开 A 消息的工具会把 B 消息的也一起展开。
  function toolArgsPreview(args) {
    if (!args || typeof args !== "object") return "";
    const parts = [];
    Object.keys(args).forEach(k => {
      let v = args[k];
      if (v == null) return;
      if (typeof v === "string") v = v.length > 60 ? v.slice(0, 60) + "…" : v;
      else if (Array.isArray(v)) v = `[${v.length} 项]`;
      else if (typeof v === "object") v = "{…}";
      parts.push(`${k}=${v}`);
    });
    return parts.join(" · ");
  }

  function toolResultSummary(res) {
    if (res == null) return "";
    if (typeof res === "string") return res.length > 240 ? res.slice(0, 240) + "…" : res;
    try {
      const text = JSON.stringify(res);
      return text.length > 240 ? text.slice(0, 240) + "…" : text;
    } catch (e) {
      return "";
    }
  }

  // 构造「可回答的提问气泡」数据。
  //
  // 【字段名兼容 —— 2026-11-10 契约修复】
  // 后端原来只发 `clarification`（一个字符串），
  // 前端却读 `clarify_options` / `clarify_hint` ——
  // 契约对不上，快捷选项 **100% 落空**（用户被迫手打「9:16」）。
  // 现在后端**同时**产出 clarify_options / clarify_hint，
  // 这里也做**多字段兼容**（万一对到老服务端）：
  //   问题文本 <- clarification || answer
  //   快捷选项 <- clarify_options || options
  //   提示     <- clarify_hint || hint
  //
  // 两条路径（流式 done 事件 / 一次性 JSON 结果）共用这一段。
  function buildClarify(data) {
    data = data || {};
    return {
      question: data.clarification || data.answer
        || "Agent 需要你补充一些信息才能继续。",
      options: Array.isArray(data.clarify_options)
        ? data.clarify_options
        : (Array.isArray(data.options) ? data.options : []),
      hint: data.clarify_hint || data.hint || "",
    };
  }

  // 工具结果里的「模式清单」要按 Agent 真实能力过滤。
  //
  // 【事实】workflows.BUILDERS 只有 9 个 mode；longfilm 走 /api/longfilms
  //   自有链路，Agent 的 create_job 建不了（基线 Agent 曾误报 longfilm）。
  //   所以 list_modes 的结果在展示时必须剔除 longfilm —— 否则用户看到
  //   一个 Agent 根本下不出来的模式。
  function filterToolResult(name, res) {
    const media = window.AgentMedia;
    if (!media || name !== "list_modes" || !res) return res;
    // 只在结果里认得出模式数组时过滤；认不出就原样显示（不臆造结构）
    let list = null;
    if (Array.isArray(res)) list = res;
    else if (Array.isArray(res.modes)) list = res.modes;
    else if (Array.isArray(res.data)) list = res.data;
    if (!list) return res;
    const kept = list.filter(m => {
      const key = typeof m === "string" ? m : (m && (m.mode || m.key || m.id));
      return key && !media.MANUAL_ONLY_MODES[key];
    });
    if (kept.length === list.length) return res;
    if (Array.isArray(res)) return kept;
    if (Array.isArray(res.modes)) return Object.assign({}, res, { modes: kept });
    if (Array.isArray(res.data)) return Object.assign({}, res, { data: kept });
    return res;
  }

  function renderTools(tools, msgIndex) {
    if (!tools || !tools.length) return "";
    const mi = msgIndex == null ? 0 : msgIndex;
    // 【会话作用域前缀 —— 2026-11-13 工具密度/折叠态修复】
    // data-detail-key 原来只有消息序号（m0-tool-0）。切换会话后，
    // 新会话的第一条消息又叫 m0-tool-0 —— **同一个 key**。
    // 于是 captureDetailsState/restoreDetailsState 会把**上一个会话**
    // 的折叠态贴到**新会话**的同名折叠块上：
    // 用户在会话 A 展开的工具行，切到会话 B 仍然是展开的。
    // 加上会话前缀后，key 全局唯一，跨会话不再串。
    const scope = detailScope();
    const rows = tools.map((t, i) => {
      const ok = t.ok !== false;
      const cls = ok ? "ok" : "bad";
      const args = toolArgsPreview(t.arguments);
      const result = toolResultSummary(t.result);
      const err = t.error ? `<div class="ag-tool-err">${esc(t.error)}</div>` : "";
      return `<details class="ag-tool ag-tool-${cls}" data-detail-key="${scope}m${mi}-tool-${i}" data-tool-index="${i}">
        <summary class="ag-tool-head">
          <span class="ag-tool-state" aria-hidden="true">${ok ? "✓" : "✕"}</span>
          <span class="ag-tool-name">${esc(t.name || "(未命名工具)")}</span>
          <span class="ag-tool-meta">${esc(t.ms != null ? t.ms + "ms" : "")}</span>
        </summary>
        <div class="ag-tool-body">
          ${args ? `<div class="ag-tool-args"><span class="ag-tool-label">参数</span><code>${esc(args)}</code></div>` : ""}
          ${result ? `<div class="ag-tool-result"><span class="ag-tool-label">结果</span><code>${esc(result)}</code></div>` : ""}
          ${err}
        </div>
      </details>`;
    }).join("");
    // 整体容器默认**展开**：工具轨迹是这次对话的重点，
    // 折起来等于把「Agent 干了什么」藏起来，用户只能看到一个标题。
    // 每个工具块仍可单独折叠 —— 粒度由用户控制。
    return `<details class="ag-block ag-tools" data-detail-key="${scope}m${mi}-tools" open>
      <summary class="ag-block-title">工具调用（${tools.length}）</summary>
      <div class="ag-tool-list">${rows}</div>
    </details>`;
  }

  // 折叠态 key 的**会话作用域前缀**。
  //
  // 【为什么必须有 —— 2026-11-13 实测 BUG】
  // data-detail-key 原来只有消息序号（m0-tool-0 / m0-steps / m0-tools）。
  // 消息序号在**每个会话里都从 0 开始**，所以两个不同会话的第一条消息
  // 共享同一个 key。
  //
  // 后果（真实浏览器实测）：
  //   在会话 A 里展开 m0-tool-0 -> 切到会话 B -> B 的 m0-tool-0 **也是展开的**
  //   -> 切回会话 A -> 又变回别的状态
  // 这就是用户报的「切换 session 后工具区密度会变」的另一半根因：
  // 折叠态跨会话串味，展开的折叠块一多，工具区自然就「变松」了。
  //
  // 前缀取**当前会话 id**；没有会话 id（刚开新会话、还没落库）时用一个
  // 进程内自增的临时号，保证同一会话内稳定、不同会话间不撞。
  function detailScope() {
    const sid = state.sessionId || state._ephemeralScope || "";
    if (!sid) {
      // 没有会话 id -> 造一个临时的（同一轮渲染内保持不变）。
      state._ephemeralScope = state._ephemeralScope
        || ("tmp-" + Math.random().toString(36).slice(2, 10));
      return state._ephemeralScope + "-";
    }
    return sid + "-";
  }

  // 澄清式提问：Agent 用 respond(needs_clarification=true) 反问用户时，
  // 渲染成**可回答的提问气泡**（带快捷选项 + 自由输入）。
  //
  // 【为什么每个控件都要带 msg-index —— 2026-11-13 实测 BUG】
  // 原来的点击处理是 `box.querySelector(".ag-clarify-input")` ——
  // 那是**消息区里的第一个**输入框，与「点了哪条消息的选项」毫无关系。
  // 于是页面上有两条 Agent 消息各自带一组快捷选项时，
  // 点**下面**那条的选项，文本被填进了**上面**那条的输入框，
  // 下面那条的输入框还停在「直接输入你的回答…」。
  //
  // 修法：每个选项按钮和输入框都带上**所属消息的序号**（data-msg-index），
  // 点击时用 `closest("[data-msg-index]")` 反查自己的作用域，
  // 只在自己那条消息的 .ag-clarify-input 里写值。
  // 序号来自 bubbleHtml(msg, index) 的 index —— 与 DOM 上的
  // `.ag-msg[data-msg-index]` 完全一致，是唯一可靠的「作用域锚点」。
  function renderClarify(msg, msgIndex) {
    const q = msg.clarify;
    if (!q) return "";
    const mi = msgIndex == null ? 0 : msgIndex;
    const options = (q.options || []).map((opt, i) => `
      <button type="button" class="ag-clarify-opt" data-act="clarify-answer"
              data-msg-index="${mi}" data-value="${esc(opt)}">${esc(opt)}</button>`).join("");
    const hint = q.hint ? `<div class="ag-clarify-hint">${esc(q.hint)}</div>` : "";
    return `<div class="ag-block ag-clarify" data-clarify-scope="${mi}">
      <div class="ag-block-title">Agent 需要你确认</div>
      <div class="ag-clarify-q">${esc(q.question || "")}</div>
      ${hint}
      ${options ? `<div class="ag-clarify-options">${options}</div>` : ""}
      <div class="ag-clarify-reply">
        <input type="text" class="ag-clarify-input" data-msg-index="${mi}"
               placeholder="直接输入你的回答…"
               data-act="clarify-input" aria-label="回答 Agent 的提问">
        <button type="button" class="ag-act" data-act="clarify-send"
                data-msg-index="${mi}">发送回答</button>
      </div>
    </div>`;
  }

  // 找出「某个快捷选项 / 发送按钮自己所属的那条澄清气泡」的输入框。
  //
  // 【为什么必须逐级向上查，不能用全局选择器】
  // 页面上可能同时存在多条 Agent 消息的澄清气泡（甚至跨会话残留），
  // 全局选择器（document.querySelector / box.querySelector 取第一个）
  // 永远只会命中第一条 —— 这就是串填的根因。
  // 这里从**被点击的元素自己**出发，沿 DOM 向上找最近的
  // `.ag-clarify` 容器，容器内的第一个 `.ag-clarify-input`
  // 才是这个选项真正的归属输入框。
  function clarifyInputOf(btn) {
    if (!btn) return null;
    // 1) 优先：从按钮自身所在的 .ag-clarify 容器里取（最可靠 —— DOM 作用域）
    const scope = btn.closest(".ag-clarify");
    if (scope) {
      const field = scope.querySelector(".ag-clarify-input");
      if (field) return field;
    }
    // 2) 兜底：用 data-msg-index 定位（万一 DOM 结构变了，
    //    但序号还在，仍能按消息作用域精确命中）。
    const idx = btn.getAttribute("data-msg-index");
    if (idx != null && idx !== "") {
      const host = btn.closest("[data-msg-index]") || document;
      const field = host.querySelector(
        `.ag-clarify-input[data-msg-index="${CSS.escape(idx)}"]`);
      if (field) return field;
    }
    return null;
  }

  // 用户气泡里的附件缩略图。
  //
  // 【为什么用户气泡要显示缩略图】
  // 用户发完消息后要能**看见自己附了什么** —— 否则传错图了都不知道。
  // 而且缩略图本身就是一种「我传了图」的视觉确认，
  // 比「附了 2 张图」这种文字更快被用户读到。
  //
  // 【2026-11-11 音视频扩展】
  // 音频/视频的缩略图分别是**波形图**和**首帧**（素材库入库时生成），
  // 所以 img 一样能用；额外加类型角标，让用户一眼分清
  // 「附的是音频还是视频」。
  function renderMessageAttachments(attachments) {
    if (!attachments || !attachments.length) return "";
    const items = attachments.map(item => {
      const kind = item.kind || "image";
      const badge = (kind === "image") ? "" :
        `<span class="ag-msg-attach-badge">${esc(KIND_LABEL[kind] || kind)}</span>`;
      return `
      <div class="ag-msg-attach" data-kind="${esc(kind)}">
        <img src="${esc(item.preview_url || "")}" alt="${esc(item.name || "附件")}"
             loading="lazy">
        ${badge}
      </div>`;
    }).join("");
    return `<div class="ag-msg-attachments">${items}</div>`;
  }

  function bubbleHtml(msg, index) {
    const isUser = msg.role === "user";
    // 工具轨迹只渲染**一次**。
    //
    // 【为什么不同时渲染 renderTools 和 renderSteps】
    //   两者展示的是同一件事（Agent 调了哪些工具），只是详细程度不同：
    //     renderTools —— 有参数、有结果摘要、有成功/失败态（流式事件驱动）
    //     renderSteps —— 只有工具名 + 耗时 + 错误（一次性结果驱动）
    //   同时渲染会出现两个几乎一样的折叠块（「工具调用」+「工具调用轨迹」），
    //   用户会以为是两件事。实测截图里就是这么暴露出来的。
    //   所以：有流式工具轨迹时只渲染它；没有才退回 steps。
    const toolBlock = (msg.tools && msg.tools.length)
      ? renderTools(msg.tools, index)
      : renderSteps(msg.steps, index);
    const body = isUser
      ? `<div class="ag-bubble-text">${esc(msg.content)}</div>
         ${renderMessageAttachments(msg.attachments)}`
      : `${statusBadge(msg.result || {})}
         <div class="ag-bubble-text">${esc(msg.content || "")}</div>
         ${msg.streaming ? `<div class="ag-streaming-hint">正在接收…</div>` : ""}
         ${renderJobs(msg.jobs)}
         ${renderAssets(msg.assets)}
         ${renderPrompt(msg.prompt)}
         ${toolBlock}
         ${renderClarify(msg, index)}
         ${renderStatusNote(msg)}
         ${renderError(msg)}
         ${renderFillButton(msg)}`;
    return `<div class="ag-msg ${isUser ? "ag-msg-user" : "ag-msg-bot"}" data-msg-index="${index}">
      <div class="ag-msg-role">${isUser ? "我" : "Agent"}</div>
      <div class="ag-msg-body">${body}</div>
    </div>`;
  }

  // 错误提示 + **重试入口**（断流/失败后必须能一键重发）。
  //
  // 重试的请求体挂在**消息自己**身上（msg.retryRequest），不是全局。
  // 全局的话：先失败一次、再成功一次之后，早先那条失败消息的
  // 重试按钮会在下次重绘时消失 —— 用户明明还想重试它。
  function renderError(msg) {
    const error = msg.error;
    const text = typeof error === "string" ? error : (error && error.message) || "未知错误";
    const canRetry = !!msg.retryRequest;
    // 【截断不显示红色失败条 —— 2026-11-11】
    // truncated 的说明由 renderStatusNote 渲染成**黄色中性条**。
    // 这里再渲染一次红条，用户就会同时看到「已截断（黄）」+
    // 「已达到步骤上限（红）」—— 同一个语义两种颜色，自相矛盾。
    // 真正的失败（status=failed）继续走红色错误条 + 重试入口。
    const status = (msg.result && msg.result.status) || "";
    if (status === "truncated") return "";
    if (!error) return "";
    return `<div class="ag-error">
      <div class="ag-error-text">${esc(text)}</div>
      ${canRetry ? `<button type="button" class="ag-act ag-act-retry" data-act="retry">重试</button>` : ""}
    </div>`;
  }

  // 中性提示条（**不是**错误条）。
  //
  // 【为什么需要 —— 2026-11-11 实测修复】
  // 截断（truncated）的语义是「达到步骤/工具上限，**返回已完成的部分结果**」，
  // 不是「执行失败」。但后端把截断原因也放进了 `error` 字段
  // （契约 §1.3 要求 truncated 给出原因），前端就把它渲染成了
  // `.ag-error` —— **红色**失败条。
  //
  // 实测线上截图：徽章是黄色的「已截断」，底部却是红色条，
  // 文案「已达到步骤上限（8 步），返回已完成的部分结果」。
  // 用户看到红条会以为整个请求挂了。
  //
  // 修法：**按 status 分流**。
  //   truncated  -> 黄色中性提示条（.ag-notice），不进 .ag-error
  //   failed     -> 红色错误条（.ag-error），保留重试入口
  //
  // 老服务端没有 `notice` 字段时，兜底用 error 文本渲染成中性条 ——
  // 这样「截断被显示成红色失败」的老数据也能变回中性。
  function renderStatusNote(msg) {
    const status = (msg.result && msg.result.status) || "";
    if (status !== "truncated") return "";
    const notice = (msg.result && msg.result.notice) || "";
    const text = notice || msg.error || "";
    if (!text) return "";
    return `<div class="ag-notice">
      <div class="ag-notice-text">${esc(text)}</div>
    </div>`;
  }

  // 「填入创作面板」—— 把 Agent 的产物回填到创作页，
  // 这样用户可以直接改参数再点生成（Agent 给的是初稿，不是黑盒）。
  function renderFillButton(msg) {
    const jobs = msg.jobs || [];
    const prompt = msg.prompt && msg.prompt.enhanced;
    if (!jobs.length && !prompt) return "";
    return `<div class="ag-actions">
      ${prompt ? `<button class="ag-act" data-act="fill-prompt" data-text="${esc(prompt)}">填入提示词</button>` : ""}
      ${jobs.length ? `<button class="ag-act" data-act="fill-job" data-job="${esc(jobs[0].job_id)}" data-mode="${esc(jobs[0].mode)}">填入创作面板</button>` : ""}
    </div>`;
  }

  // 当前会话 id 的**唯一**读取入口。
  //
  // 【为什么从这里读，而不是让调用方传 —— 2026-11-13】
  // 折叠态 key 的作用域前缀依赖「当前是哪个会话」。
  // 调用方（agent_sessions.js）在切会话时已经更新了自己的 state.currentId，
  // 所以这里直接问会话层 —— 省掉「每个调用点都要记得传 sessionId」这种
  // 容易漏的约定（漏一个 = 折叠态串到别的会话）。
  //
  // 返回 null = **问不到**（会话层还没加载 / 不存在）。
  // null 与空串必须区分：问不到时**不能**把 state.sessionId 清空。
  function currentSessionId() {
    const sessions = window.AgentSessions;
    if (sessions && sessions.state) return sessions.state.currentId || "";
    return null;
  }

  // 切换会话作用域：换 sessionId + 换折叠态作用域。
  //
  // 【顺序很重要 —— 必须先抓旧会话的折叠态，再改 sessionId】
  // captureDetailsState 会把当前 DOM 的折叠态存进「当前会话的桶」。
  // 如果先改了 sessionId 再抓，DOM 里明明还是**旧会话**的折叠块，
  // 却会被存进**新会话**的桶 -> 切回来时恢复成别的会话的状态，
  // 工具区密度就会在会话之间乱跳。
  //
  // 【为什么要按会话分桶 —— 2026-11-13 工具密度一致性修复】
  // data-detail-key 原来只有消息序号（m0-tool-0 / m0-steps / m0-tools）。
  // 消息序号在**每个会话里都从 0 开始**，所以两个不同会话的第一条消息
  // 共享同一个 key —— 折叠态跨会话串味。
  //
  // 加上会话作用域前缀后，key 全局唯一；再按会话分桶记住折叠态，
  // 切走再切回来 -> 恢复成**离开前的样子** -> 密度完全一致。
  function applySessionScope(nextSid) {
    if (nextSid === null || nextSid === undefined) return;   // 问不到 -> 不动
    const prev = state.sessionId || "";
    if ((nextSid || "") === prev) return;                    // 没变 -> 不动
    const box = $("#agMessages");
    if (box) captureDetailsState(box);   // 先把旧会话的折叠态存进旧桶
    state.sessionId = nextSid || "";
    // 会话变了 -> 临时的折叠态作用域也要换（否则新会话沿用旧作用域）
    state._ephemeralScope = "";
  }

  // 折叠态的保存与恢复。
  //
  // 为什么要存：任何一次重绘都会重建 innerHTML，`<details>` 的 open 属性
  // 是**元素状态**，重建后默认收起 —— 用户刚展开的工具轨迹会在下一个
  // token 到来时「弹回去」。所以重绘前先把 open 态抓出来，重绘后再贴回去。
  //
  // 【按会话分桶 —— 2026-11-13 工具密度一致性修复】
  // 原来只有一个全局 map。切会话时旧会话的 key 被清掉，而新会话渲染出来的
  // key 也是 m0-tool-0，但 map 里已经没有它了 -> 恢复不了 -> 一律回到
  // 默认展开态。结果就是「同一会话切走再切回来，工具区密度变了」。
  // 现在按会话 id 分桶：每个会话记住自己的折叠态。
  // 桶用 Map 而不是 localStorage（折叠态是**临时 UI 状态**，
  // 不该跨页面刷新保留；刷新后回到默认展开态是合理的）。
  const detailsStateBySession = new Map();

  // 当前会话的折叠态桶。
  //
  // 【桶只服务于「有会话 id」的场景 —— 2026-11-13】
  // 桶的用途是「切走再切回来时恢复这个会话自己的展开态」——
  // **前提是这个会话有身份**（session id）。
  //
  // 没有会话 id 时（还没落库 / 自动化测试）返回 null：
  //   · 折叠态只活在 DOM 里（由本次重绘前抓到的 map 负责）——
  //     这正是修复前的行为，也正是「同一段对话内重绘不丢展开态」所需；
  //   · **不跨段落继承**。如果无会话时也用桶，且桶的 key 是个共享的
  //     "__none__"，上一段对话的展开态就会漏到下一段对话里。
  //
  // 【实测踩到过 —— 2026-11-13】
  // 真实浏览器跑一批用例时，出现「工具块在点击前就已经是展开的」：
  // 点一下 summary 反而把它**收起**了。
  // 根因就是无会话场景共用一个桶，上一段的展开态漏了过来。
  //
  // 桶的 key 用 scope 前缀（有会话时就是 session id），
  // 这样不同会话、以及不同临时作用域，都不会互相污染。
  function currentDetailsBucket() {
    if (!state.sessionId) return null;    // 没有会话身份 -> 不分桶
    const key = detailScope();
    if (!detailsStateBySession.has(key)) detailsStateBySession.set(key, new Map());
    return detailsStateBySession.get(key);
  }

  function captureDetailsState(root) {
    const map = new Map();
    if (!root) return map;
    root.querySelectorAll("details[data-detail-key]").forEach(d => {
      map.set(d.getAttribute("data-detail-key"), d.open);
    });
    // 存进当前会话的桶（供下次切回本会话时恢复）。
    // 没有会话 id -> 不存（见 currentDetailsBucket 的说明）。
    const bucket = currentDetailsBucket();
    if (bucket) map.forEach((v, k) => bucket.set(k, v));
    return map;
  }

  // 把折叠态贴回去。
  //
  // 【为什么要合并「本次重绘的 map」和「当前会话的桶」—— 2026-11-13】
  // 两种来源各有各的用：
  //   · map（本次重绘前从 DOM 抓的）-> 同一会话内的**重绘**（流式 token
  //     到达、任务状态刷新）必须用它，否则用户展开的工具行会弹回去；
  //   · 桶（该会话历史上记住的）-> **切回该会话**时用它，
  //     否则离开时展开的工具行回到默认收起态 -> 密度变了。
  // 两者的 key 相同（都带会话作用域前缀），所以按「桶优先、map 兜底」
  // 合并：桶里有的用桶的（那是用户最后确认过的状态），桶里没有的用 map。
  function restoreDetailsState(root, map) {
    if (!root) return;
    const bucket = currentDetailsBucket();
    root.querySelectorAll("details[data-detail-key]").forEach(d => {
      const key = d.getAttribute("data-detail-key");
      if (bucket && bucket.has(key)) { d.open = bucket.get(key); return; }
      if (map && map.has(key)) d.open = map.get(key);
    });
  }

  function renderMessages() {
    const box = $("#agMessages");
    if (!box) return;
    if (!state.messages.length) {
      box.innerHTML = `<div class="ag-empty">
        <b>内置多模态 Agent</b>
        <p>用一句话说清你的创作意图，Agent 会自己查素材、选模式、组装参数，最后下发成可执行的生成任务。</p>
        <p class="ag-hint">例：「素材库里有没有雨夜的素材？有的话用它做参考图，生成一段 5 秒 16:9 的视频」</p>
        <p class="ag-hint">本地模型多步处理约需 1~5 分钟，处理期间下方会实时显示进度。</p>
      </div>`;
      return;
    }
    const wasAtBottom = isScrolledToBottom(box);
    const detailsState = captureDetailsState(box);
    box.innerHTML = state.messages.map((m, i) => bubbleHtml(m, i)).join("");
    restoreDetailsState(box, detailsState);
    // 只有用户本来就贴着底部时才自动跟随 —— 否则向上翻阅历史会被强行拽回底部。
    if (wasAtBottom || state.streaming) box.scrollTop = box.scrollHeight;
  }

  // 增量渲染：**只重绘正在流式的那一条气泡**，不动其它气泡。
  //
  // 为什么不全量重绘：长会话里每来一个 token 就重建整个消息列表，
  // 是 O(消息数) 的 DOM 操作；几百条消息时每秒几十个 token 会把主线程打满，
  // 表现为「打字卡顿 / 滚动掉帧」。而且全量重绘会连带干掉用户正在
  // 聚焦的输入框（澄清提问框）和已展开的折叠块。
  //
  // 两条路径：
  //   · token 事件 —— 只改文本节点的 textContent（最便宜，零结构变更）
  //   · tool_call / tool_result / job —— 结构变了，重绘这一条气泡
  function renderStreamingMessage(structural) {
    const box = $("#agMessages");
    const msg = state.streaming;
    if (!box || !msg) return;
    const index = state.messages.indexOf(msg);
    if (index < 0) return;
    const el = box.querySelector(`.ag-msg[data-msg-index="${index}"]`);
    if (!el) { renderMessages(); return; }
    const body = el.querySelector(".ag-msg-body");
    if (!body) { renderMessages(); return; }

    if (!structural) {
      // 纯文本增量：只更新文本节点，其余 DOM 一律不动。
      const text = body.querySelector(".ag-bubble-text");
      if (text) {
        text.textContent = msg.content || "";
        if (isScrolledToBottom(box)) box.scrollTop = box.scrollHeight;
        return;
      }
    }

    // 结构变了：只重建这一条气泡，并保留它的折叠态。
    const detailsState = captureDetailsState(el);
    const tmp = document.createElement("div");
    tmp.innerHTML = bubbleHtml(msg, index);
    const fresh = tmp.firstElementChild;
    restoreDetailsState(fresh, detailsState);
    el.replaceWith(fresh);
    if (isScrolledToBottom(box)) box.scrollTop = box.scrollHeight;
  }

  function isScrolledToBottom(box) {
    if (!box) return true;
    return box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  }

  // ---------- 进度反馈 ----------
  //
  // 本地模型单步就要 20~120 秒，一条完整链路 4~6 分钟（审查实测 179 秒无反馈）。
  // 静默等待会被当成卡死，所以这里显示：
  //   · 当前阶段（正在检索素材库 / 正在增强提示词 / …）
  //   · 已用时间（每秒跳动，不是静态文案）
  //   · 步骤进度（第 N/M 步）
  // 阶段文案由**服务端**给（agent.PROGRESS_STAGES / TOOL_STAGE_TEXT），
  // 前端不自己编 —— 两边各写一套必然对不上。

  const progressState = {
    stage: "",
    detail: "",
    done: 0,
    total: 0,
    startedAt: 0,
    timer: null,
  };

  function fmtElapsed(ms) {
    const s = Math.floor(ms / 1000);
    if (s < 60) return `${s} 秒`;
    const m = Math.floor(s / 60);
    const rest = s % 60;
    return rest ? `${m} 分 ${rest} 秒` : `${m} 分`;
  }

  function renderProgress() {
    const box = $("#agStatus");
    if (!box) return;
    if (!progressState.stage) { box.textContent = ""; return; }
    const elapsed = progressState.startedAt
      ? fmtElapsed(Date.now() - progressState.startedAt) : "";
    const parts = [];
    parts.push(`<span class="ag-progress-dot" aria-hidden="true"></span>`);
    parts.push(`<span class="ag-progress-stage">${esc(progressState.detail || progressState.stage)}</span>`);
    if (elapsed) parts.push(`<span class="ag-progress-elapsed">已用 ${esc(elapsed)}</span>`);
    if (progressState.total > 1) {
      parts.push(`<span class="ag-progress-step">第 ${esc(progressState.done + 1)}/${esc(progressState.total)} 步</span>`);
    }
    box.innerHTML = `<span class="ag-progress">${parts.join("")}</span>`;
    box.classList.add("ag-status-busy");
  }

  function startProgress() {
    stopProgress();
    progressState.stage = "start";
    progressState.detail = "正在理解你的意图";
    progressState.done = 0;
    progressState.total = 0;
    progressState.startedAt = Date.now();
    renderProgress();
    // 每秒刷新一次已用时间 —— 静态文案会让人以为是死图
    progressState.timer = setInterval(renderProgress, 1000);
  }

  function stopProgress() {
    if (progressState.timer) { clearInterval(progressState.timer); progressState.timer = null; }
    progressState.stage = "";
    progressState.detail = "";
    progressState.startedAt = 0;
    const box = $("#agStatus");
    if (box) box.classList.remove("ag-status-busy");
  }

  function updateProgress(event) {
    if (!event) return;
    if (event.stage) progressState.stage = event.stage;
    if (event.detail) progressState.detail = event.detail;
    if (typeof event.done === "number") progressState.done = event.done;
    if (typeof event.total === "number" && event.total > 0) progressState.total = event.total;
    renderProgress();
  }

  function setBusy(busy) {
    state.busy = busy;
    const btn = $("#agSend");
    const input = $("#agInput");
    if (btn) {
      btn.disabled = busy;
      btn.textContent = busy ? "处理中…" : "发送";
    }
    if (input) input.disabled = busy;
    const status = $("#agStatus");
    if (status && !busy) {
      stopProgress();
      status.textContent = "";
    }
  }

  // ---------- 流式事件契约 ----------
  //
  // 【新契约】POST /api/agent/chat  带 stream=1，SSE 事件类型：
  //   token       —— 增量文本   {text}
  //   tool_call   —— 工具开始   {name, arguments, call_id?}
  //   tool_result —— 工具结束   {name, result, ok, ms?, call_id?}
  //   job         —— 任务下发   {job_id, mode, label?, status?, prompt?, params?, summary?}
  //   done        —— 结束       {answer?, jobs?, assets?, prompt?, steps?, status?, ...}
  //   error       —— 失败       {error, code?}
  //
  // 【旧契约】POST /api/agent/chat/stream，SSE 事件类型：
  //   progress / result / error
  //
  // 两条契约并存，客户端按事件名分派；老服务端只发 progress/result，
  // 新服务端发 token/tool_call/...。**任何一条缺失都降级而不是报错**。
  //
  // 降级链：stream=1 的 SSE → 老 /chat/stream 的 SSE → 一次性 JSON。

  const STREAM_EVENT_TYPES = [
    "token", "tool_call", "tool_result", "job", "done", "error",
    // 旧契约
    "progress", "result",
  ];

  // 解析 SSE 文本块 -> {event, data}
  function parseSseBlock(raw) {
    let event = "message";
    const dataLines = [];
    for (const line of raw.split("\n")) {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) dataLines.push(line.slice(5).replace(/^ /, ""));
    }
    if (!dataLines.length) return null;
    const payload = dataLines.join("\n");
    try { return { event, data: JSON.parse(payload) }; }
    catch (e) { return null; }
  }

  // 消费一个 SSE 响应流。onEvent(event, data) 每收到一条就回调一次。
  // 返回 {done:boolean, result:object|null}
  async function consumeSse(resp, onEvent) {
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let result = null;

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buffer.indexOf("\n\n")) >= 0) {
        const raw = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        const parsed = parseSseBlock(raw);
        if (!parsed) continue;
        if (parsed.event === "result") result = parsed.data;
        onEvent(parsed.event, parsed.data);
      }
    }
    // 收尾：处理末尾没有空行结尾的残块
    if (buffer.trim()) {
      const parsed = parseSseBlock(buffer);
      if (parsed) {
        if (parsed.event === "result") result = parsed.data;
        onEvent(parsed.event, parsed.data);
      }
    }
    return { result };
  }

  // ---------- 增量渲染的状态变更 ----------

  function upsertTool(msg, patch) {
    if (!msg.tools) msg.tools = [];
    let tool = null;
    if (patch.call_id) {
      tool = msg.tools.find(t => t.call_id === patch.call_id);
    }
    if (!tool && patch.name) {
      tool = msg.tools.find(t => t.name === patch.name && !t.done);
    }
    if (!tool) {
      tool = { name: patch.name || "(未命名工具)", arguments: null, result: null,
               ok: true, error: "", ms: null, done: false, call_id: patch.call_id || null };
      msg.tools.push(tool);
    }
    if (patch.name) tool.name = patch.name;
    if (patch.call_id) tool.call_id = patch.call_id;
    if (patch.arguments !== undefined) tool.arguments = patch.arguments;
    if (patch.result !== undefined) tool.result = patch.result;
    if (patch.ok !== undefined) tool.ok = !!patch.ok;
    if (patch.error !== undefined) tool.error = patch.error || "";
    if (patch.ms !== undefined) tool.ms = patch.ms;
    if (patch.finished) tool.done = true;
    return tool;
  }

  function upsertJob(msg, job) {
    if (!msg.jobs) msg.jobs = [];
    const id = job.job_id || job.id;
    let found = msg.jobs.find(j => j.job_id === id);
    if (!found) {
      found = { job_id: id, mode: job.mode, label: job.label, status: job.status || "queued",
                prompt: job.prompt, params: job.params, summary: job.summary,
                outputs: [], error: "", progress_text: "" };
      msg.jobs.push(found);
    }
    Object.keys(job).forEach(k => {
      if (k === "job_id" || k === "id") return;
      if (job[k] !== undefined && job[k] !== null) found[k] = job[k];
    });
    return found;
  }

  // ============================================================ 契约归一化层
  //
  // 【为什么要有这一层 —— 2026-11-10 审查修复】
  // 前后端各自实现了一套 SSE 字段名，而且**对不上**：
  //   后端发：tool_call   {tool, args}          tool_result {tool, ok, summary}
  //   前端读：tool_call   {name, arguments, call_id?}
  //            tool_result {name, result, ok, ms?, error?, call_id?}
  // 结果：工具结果**永远渲染不出来**（result 恒 undefined），
  //       失败原因不显示、耗时不显示、call_id 恒 null
  //       （同名工具并发调用会串到同一张卡片）。
  //
  // 【修法】前端加一层**归一化适配**：把后端字段映射成前端内部字段。
  // 这样：
  //   * 后端保持自己的契约（已发布的接口契约文档不用改语义）
  //   * 前端对**两种**命名都认（向后兼容，也兼容更老的服务端）
  //   * 跨侧契约测试可以只测这一层
  //
  // 归一化后的**内部字段名**（前端唯一认的）：
  //   tool_call   -> {name, arguments, call_id}
  //   tool_result -> {name, result, ok, error, ms, call_id}

  // 工具名：后端用 tool，前端内部用 name
  function normalizeToolName(data) {
    return data.name || data.tool || "";
  }

  // 工具参数：后端用 args（对象），前端内部用 arguments（对象）
  function normalizeToolArguments(data) {
    if (data.arguments !== undefined) return data.arguments;
    if (data.args !== undefined) return data.args;
    return undefined;
  }

  // 工具结果摘要：后端用 summary（字符串），前端内部用 result
  //
  // 【为什么要把 summary 映射到 result 而不是新增字段】
  // 前端的展示链路（toolResultSummary -> .ag-tool-result）已经吃
  // 「result」这个字段；再新增一个 summary 字段就要改两处渲染。
  // 归一化层把差异**收敛在一处**，渲染层不用知道后端叫什么。
  function normalizeToolResult(data) {
    if (data.result !== undefined) return data.result;
    if (data.summary !== undefined) return data.summary;
    if (data.output !== undefined) return data.output;
    return undefined;
  }

  // 应用一条流式事件到正在渲染的消息上。
  function applyStreamEvent(msg, event, data) {
    if (!data || typeof data !== "object") return;
    switch (event) {
      case "token": {
        const text = typeof data.text === "string" ? data.text
          : (typeof data.delta === "string" ? data.delta : "");
        if (text) msg.content = (msg.content || "") + text;
        break;
      }
      case "tool_call": {
        upsertTool(msg, {
          name: normalizeToolName(data),
          arguments: normalizeToolArguments(data),
          call_id: data.call_id || data.id,
        });
        break;
      }
      case "tool_result": {
        const name = normalizeToolName(data);
        const raw = normalizeToolResult(data);
        upsertTool(msg, {
          name: name,
          result: raw !== undefined ? filterToolResult(name, raw) : undefined,
          ok: data.ok !== false,
          error: data.error || "",
          ms: data.ms,
          call_id: data.call_id || data.id,
          finished: true,
        });
        break;
      }
      case "job": {
        upsertJob(msg, data);
        break;
      }
      case "done": {
        if (typeof data.answer === "string" && data.answer) msg.content = data.answer;
        if (Array.isArray(data.jobs)) data.jobs.forEach(j => upsertJob(msg, j));
        // 产物面板同步：新建了任务 -> 「本次会话产物」列表要跟着更新。
        if (window.AgentSessions && window.AgentSessions.onJobsCreated) {
          window.AgentSessions.onJobsCreated();
        }
        if (Array.isArray(data.assets)) msg.assets = data.assets;
        // 【本轮附件 —— 2026-11-11】
        // 服务端回传它**实际收到**的附件（权威来源）。
        // 前端已经在用户气泡里显示过缩略图（发出前就显示了），
        // 这里存的是「服务端确认收到的那份」，用于失败时也能追溯。
        if (Array.isArray(data.attachments)) msg.attachments = data.attachments;
        if (data.prompt) msg.prompt = data.prompt;
        if (Array.isArray(data.steps)) msg.steps = data.steps;
        // 澄清态 -> 构造「可回答的提问气泡」。
        //
        // 【为什么必须在这里做 —— 2026-11-10 契约修复】
        // 原来这段逻辑只在 send() 的**一次性 JSON 路径**里（send() 函数体内），
        // 流式路径完全没有 —— 于是走 SSE 时（也就是正常路径）
        // 「Agent 需要你确认」气泡**根本不出现**，
        // 快捷选项更无从谈起。
        // 现在提到 applyStreamEvent 里，两条路径共用同一段逻辑。
        if (data.status === "needs_clarification") {
          msg.clarify = buildClarify(data);
        }
        msg.result = data;
        msg.streaming = false;
        break;
      }
      case "error": {
        msg.error = data.error || "Agent 执行失败";
        msg.result = { status: "failed" };
        msg.streaming = false;
        break;
      }
      case "progress": {
        updateProgress(data);
        break;
      }
      case "result": {
        // 旧契约：整包结果
        msg.content = data.answer || msg.content || "";
        msg.jobs = data.jobs || msg.jobs || [];
        msg.assets = data.assets || [];
        msg.prompt = data.prompt || null;
        msg.steps = data.steps || [];
        msg.result = data;
        msg.streaming = false;
        break;
      }
      default:
        break;
    }
  }

  // ---------- 动作 ----------

  // 新契约：POST /api/agent/chat（body 带 stream=1）
  //
  // 用 fetch + ReadableStream 而不是 EventSource —— EventSource 只能 GET，
  // 而 Agent 的输入在 POST body 里。
  //
  // 【stream 放在 body 里 —— 2026-11-10 审查修复】
  // 原来这里发的是 `/api/agent/chat?stream=1`（query 载体），
  // 而后端只读 body 的 `stream` 字段 —— 契约在两侧对不上，
  // 结果真实浏览器 12/12 请求拿到的都是 application/json，
  // **流式输出 100% 不生效**（界面从空串直接跳到整段结果）。
  // 现在两边都支持：
  //   A. body {"message":..., "stream":1}   ← 前端走这条
  //   B. query ?stream=1                      ← 后端也认（兼容旧前端）
  // 两种写法命中**同一条** SSE 契约。
  async function sendStreamNew(body, msg) {
    const payload = Object.assign({}, body, { stream: 1 });
    // ⚠️ 2026-11-10：SSE 加 **AbortController** —— 之前**没有任何取消/超时**：
    //   * 页面关掉/切走 -> 连接一直挂着，服务端那边 Agent 还在跑（浪费本地算力）
    //   * 后端卡住 -> 前端永远等，界面停在「思考中」
    // 现在：SSE_MAX_MS 超时自动 abort；并暴露 cancelAgentSSE() 给「停止」按钮用。
    const controller = new AbortController();
    activeSSE = controller;
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      try { controller.abort(); } catch (e) { /* 已断 */ }
    }, SSE_MAX_MS);
    let resp;
    try {
      resp = await fetch("/api/agent/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json", "Accept": "text/event-stream" },
        body: JSON.stringify(payload),
        signal: controller.signal,
      });
    } catch (err) {
      clearTimeout(timer);
      if (activeSSE === controller) activeSSE = null;
      if (timedOut) {
        const e = new Error("Agent 响应超时（" + Math.round(SSE_MAX_MS / 1000) + " 秒），已断开");
        e.code = "sse_timeout";
        throw e;
      }
      throw err;
    }
    clearTimeout(timer);

    if (!resp.ok) {
      const raw = await resp.text();
      let data;
      try { data = raw ? JSON.parse(raw) : {}; }
      catch (e) { data = { error: raw || ("请求失败（HTTP " + resp.status + "）") }; }
      const err = new Error(data.error || ("请求失败（HTTP " + resp.status + "）"));
      err.code = data.code || "";
      err.status = resp.status;
      err.payload = data;
      throw err;
    }

    const ct = resp.headers.get("Content-Type") || "";
    if (!ct.includes("text/event-stream")) {
      // 服务端退回了普通 JSON（说明它还不支持 stream=1）—— 照常渲染。
      const data = await resp.json();
      applyStreamEvent(msg, "result", data);
      return data;
    }

    let result;
    try {
      const consumed = await consumeSse(resp, (event, data) => {
        applyStreamEvent(msg, event, data);
        // token 是纯文本 -> 走轻量路径；
        // progress 只更新 #agStatus 进度条，不动气泡；
        // 其余（tool_call/tool_result/job/done/error/result）是结构变更 -> 重绘这一条气泡
        if (event === "token") renderStreamingMessage(false);
        else if (event !== "progress") renderStreamingMessage(true);
      });
      result = consumed.result;
    } catch (err) {
      if (activeSSE === controller) activeSSE = null;
      throw err;
    }
    if (activeSSE === controller) activeSSE = null;
    if (result) {
      applyStreamEvent(msg, "result", result);
    } else if (!msg.result) {
      // 流结束但没有 done/result —— 可能中途断了
      throw new Error("流式响应提前结束，没有拿到最终结果");
    }
    return msg.result || {};
  }

  // 旧契约：POST /api/agent/chat/stream（progress / result / error）
  async function sendStreamLegacy(body, msg) {
    const resp = await fetch("/api/agent/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json", "Accept": "text/event-stream" },
      body: JSON.stringify(body),
    });

    if (!resp.ok) {
      const raw = await resp.text();
      let data;
      try { data = raw ? JSON.parse(raw) : {}; }
      catch (e) { data = { error: raw || ("请求失败（HTTP " + resp.status + "）") }; }
      const err = new Error(data.error || ("请求失败（HTTP " + resp.status + "）"));
      err.code = data.code || "";
      err.status = resp.status;
      err.payload = data;
      throw err;
    }

    const ct = resp.headers.get("Content-Type") || "";
    if (!ct.includes("text/event-stream")) {
      const data = await resp.json();
      applyStreamEvent(msg, "result", data);
      return data;
    }

    const { result } = await consumeSse(resp, (event, data) => {
      applyStreamEvent(msg, event, data);
      if (event === "progress") { renderMessages(); }
    });
    if (result) {
      applyStreamEvent(msg, "result", result);
    } else if (!msg.result) {
      throw new Error("流式响应提前结束，没有拿到最终结果");
    }
    return msg.result || {};
  }

  // 错误分类：决定「要不要降级重试」。
  //
  // 【为什么必须分类】
  //   降级链是「新契约 -> 老契约 -> 一次性 JSON」。如果**任何**错误都降级，
  //   那么「服务端真的执行失败」也会被降级掩盖：用户看到的是
  //   「成功了」，但实际任务一个都没建成 —— 静默失败，最难查。
  //
  //   所以只有「这个端点根本不存在 / 不是流式」的**能力性**失败才降级：
  //     * 404 / 405 / 501 —— 端点不存在或方法不对
  //     * 415 —— 不接受这个媒体类型（老服务端不认 stream=1）
  //     * 没有 code 的错误 —— 网络层失败 / 代理不支持流式
  //   而带业务 code 的失败（provider_error / too_many_requests /
  //   agent_internal_error / bad_request …）**一律不降级**，直接报错。
  //
  // 【为什么 agent_internal_error 也不降级】
  //   它表示「服务端执行 Agent 时炸了」，不是「端点不存在」。
  //   降级重跑一次只会再炸一次，还可能重复下发任务。
  function shouldFallback(err) {
    if (!err) return false;
    // 有业务 code -> 直接报错
    if (err.code) return false;
    // HTTP 状态码：只有「端点不存在/方法不对/媒体类型不对」才降级
    const status = err.status;
    if (typeof status === "number") {
      return status === 404 || status === 405 || status === 415 || status === 501;
    }
    // 没有 status 也没有 code -> 网络层失败，尝试换契约
    return true;
  }

  async function send() {
    if (state.busy) return;
    const input = $("#agInput");
    const text = (input.value || "").trim();
    // 【附件 —— 2026-11-11】
    // 有附件时**允许只发图不写字**（用户可能只想说「照这张改」）。
    // 没附件也没字 -> 不发（避免空消息）。
    const pending = state.attachments.slice();
    if (!text && !pending.length) return;
    // 【就绪门槛 —— 2026-11-11 音视频扩展】
    // 分析**还没跑完**的素材不能作为参考素材发出去：
    //   * 模型会以为它有 BPM/乐器/人声信息，实际上一无所知
    //   * Agent 侧也会把「分析中」的条目排除出可引用范围 -> 静默失效
    // 所以这里**拦住发送**，明确告诉用户还在分析。
    const notReady = pending.filter(it => attachStatus(it) !== "ready");
    if (notReady.length) {
      const analyzing = notReady.filter(it => attachStatus(it) === "pending");
      const failed = notReady.filter(it => attachStatus(it) === "failed");
      const parts = [];
      if (analyzing.length) {
        parts.push(`${analyzing.length} 个素材还在分析中，`
          + "分析完成后才能作为参考素材发送");
      }
      if (failed.length) {
        parts.push(`${failed.length} 个素材分析失败，请移除后再发送`);
      }
      state.attachError = parts.join("；");
      renderAttachments();
      return;
    }
    input.value = "";
    // 附件随消息发出后清空（附件只属于这一条消息）。
    state.attachments = [];
    state.attachError = "";
    renderAttachments();
    // 用户气泡里也显示缩略图 —— 用户发完要能看见自己附了什么。
    state.messages.push({ role: "user", content: text, attachments: pending });
    renderMessages();
    setBusy(true);
    startProgress();

    const body = {
      message: text || "请看看我上传的这张图，按图片编辑（i2i_qwen）处理。",
      session_id: state.sessionId || undefined,
    };
    // 【会话 id 同步 —— 2026-11-11】
    // state.sessionId 与 AgentSessions.state.currentId 是**两份**状态。
    // 如果这里不先同步，请求会带着空 session_id 发出去，
    // 服务端就会**新建一个会话** —— 用户在侧栏点「会话 A」，
    // 发出的消息却落进了「会话 B」。那是最难查的一类串味。
    const sessions = window.AgentSessions;
    if (sessions && sessions.state && sessions.state.currentId) {
      state.sessionId = sessions.state.currentId;
      body.session_id = state.sessionId;
    }
    // 附件的 asset_id 列表 —— 服务端据此把素材放进 Agent 上下文，
    // 并在模型没显式指定参考素材时自动作为参考素材。
    if (pending.length) {
      body.attachments = pending.map(a => a.asset_id).filter(Boolean);
    }

    // 正在流式渲染的那条消息
    const msg = { role: "assistant", content: "", result: {}, jobs: [], assets: [],
                  prompt: null, steps: [], tools: [], streaming: true, error: "",
                  retryRequest: body };
    state.messages.push(msg);
    state.streaming = msg;
    renderMessages();

    try {
      // 降级链：新契约 SSE → 老契约 SSE → 一次性 JSON
      //
      // 只有「端点不存在 / 不支持流式」才降级（见 shouldFallback 的说明）。
      // 服务端真的执行失败时**不降级** —— 否则失败会被降级掩盖，
      // 用户看到「成功」但任务一个都没建成。
      let data;
      try {
        data = await sendStreamNew(body, msg);
      } catch (streamErr) {
        if (!shouldFallback(streamErr)) throw streamErr;
        // 流式不可用（服务端没有这个端点 / 代理不支持 / 网络中断）时退回老契约。
        try {
          data = await sendStreamLegacy(body, msg);
        } catch (legacyErr) {
          if (!shouldFallback(legacyErr)) throw legacyErr;
          data = await apiJSON("POST", "/api/agent/chat", body);
          applyStreamEvent(msg, "result", data);
        }
      }
      msg.streaming = false;
      // 一次性 JSON 路径：把 steps 映射成工具轨迹（统一的展示形态）
      syncToolsFromSteps(msg, data.steps);
      state.sessionId = data.session_id || state.sessionId;
      // 反向同步到会话管理层 —— 服务端可能**新建**了会话
      // （比如本地没有会话 id 时首次发送），那边必须知道当前是哪个会话，
      // 否则侧栏高亮错位、刷新后恢复到别的会话。
      if (data.session_id && window.AgentSessions) {
        window.AgentSessions.state.currentId = data.session_id;
        if (window.AgentSessions.loadSessions) {
          window.AgentSessions.loadSessions();
        }
      }
      state.lastResult = data;
      if (!msg.result || !Object.keys(msg.result).length) msg.result = data;
      if (!msg.content) msg.content = data.answer || "";
      if (Array.isArray(data.jobs)) data.jobs.forEach(j => upsertJob(msg, j));
      // 产物面板同步：新建了任务 -> 「本次会话产物」列表要跟着更新。
      if (window.AgentSessions && window.AgentSessions.onJobsCreated) {
        window.AgentSessions.onJobsCreated();
      }
      if (Array.isArray(data.assets)) msg.assets = data.assets;
      // 【本轮附件 —— 2026-11-11】与流式路径同一处理。
      if (Array.isArray(data.attachments)) msg.attachments = data.attachments;
      if (data.prompt) msg.prompt = data.prompt;
      // 需要澄清：渲染成可回答的提问气泡
      //
      // 【字段名兼容 —— 2026-11-10 审查修复】
      // 后端原来只发 `clarification`（一个字符串），
      // 前端却读 `clarify_options` / `clarify_hint` ——
      // 契约对不上，快捷选项 **100% 落空**（用户被迫手打「9:16」）。
      // 现在后端**同时**产出 clarify_options / clarify_hint，
      // 前端这里也做**多字段兼容**（万一对到老服务端）。
      // 逻辑与流式路径共用 buildClarify（避免两处各写一份必然漂移）。
      if (data.status === "needs_clarification") {
        msg.clarify = buildClarify(data);
      }
      if (data.error) msg.error = data.error;
      // 任务卡状态轮询启动
      startJobPolling(msg);
      // 成功了就清掉这条消息的重试上下文 —— 成功的消息不该挂着重试按钮
      msg.retryRequest = null;
    } catch (err) {
      // 失败也要让用户看到「到底怎么了」—— 不能只留一个空白的忙碌状态。
      stopProgress();
      msg.streaming = false;
      // 【2026-11-13 修：用户主动取消时不要显示浏览器的原始报错】
      //
      // cancelAgentSSE() 会给 msg.error 写上「已停止生成」，
      // 然后 abort() 让 fetch 抛 AbortError ——
      // 于是这里**无条件**用 err.message 覆盖，
      // 用户看到的是 "signal is aborted without reason"（浏览器原文），
      // 而不是「已停止生成」。
      //
      // 判据：msg.error 已经被 cancel 路径写过 -> 说明是**用户主动取消**，
      // 保留那条友好文案；否则才是真失败，用 err.message。
      if (!msg.error) {
        msg.error = err.message || "未知错误";
      } else if (err.name === "AbortError" || /aborted/i.test(err.message || "")) {
        // 保留 cancelAgentSSE 写的提示（用户主动取消）
      }
      msg.result = { status: "failed" };
      // msg.retryRequest 保留 -> 渲染出「重试」按钮
    } finally {
      state.streaming = null;
      setBusy(false);
      renderMessages();
    }
  }

  // 一次性结果里的 steps -> 工具轨迹（统一展示形态）。
  //
  // 【为什么要真的用 steps[].args —— 2026-11-10 审查修复】
  // 服务端的 steps[] **确实带**完整参数：
  //   {"tool": "create_job",
  //    "args": "{\"mode\": \"t2i\", \"prompt\": \"雨夜霓虹街道…\", \"aspect\": \"16:9\"}"}
  // 但旧实现硬编码 `arguments: null` —— 参数被**主动丢弃**。
  // 于是降级路径（服务端退回一次性 JSON）下工具卡只显示工具名和耗时，
  // 没有「参数」行 —— 审查实测截图里确实只有「✓ 工具名 耗时」三样。
  //
  // 注意：args 是 **JSON 字符串**，而 toolArgsPreview 只接受对象
  //（`if (!args || typeof args !== "object") return ""`）——
  // 直接塞字符串仍然渲染不出来。必须先 parse。
  // 解析失败就**原样当字符串**保留（不吞掉），让用户至少能看到原文。
  //
  // 【为什么要真的用 steps[].summary —— 2026-11-10 独立复验】
  // 第一次修复只补了「参数」行，**结果」行仍然是空的**：
  // 审查实测一次性路径下 .ag-tool-result 节点数 = 0。
  // 根因：SSE 路径发 tool_result 事件（带 summary），
  //       一次性路径只有 steps[]，而旧实现把 result 硬编码成 null。
  // 现在后端 steps[] 补发了 summary（与 SSE **同一个函数**的输出），
  // 这里通过 normalizeToolResult() 消费 —— 两条路径共用一套归一化，
  // 渲染层不用知道后端字段叫什么。
  function syncToolsFromSteps(msg, steps) {
    if (!Array.isArray(steps) || !steps.length) return;
    if (msg.tools && msg.tools.length) return;   // 已经有流式轨迹了，不覆盖
    msg.tools = steps.filter(s => s && s.tool).map(s => {
      const raw = normalizeToolResult(s);
      const hasResult = raw !== undefined && String(raw).trim() !== "";
      return {
        name: normalizeToolName(s),
        arguments: parseStepArgs(s.args),
        // 结果：走归一化层（result / summary / output 都认），
        // 与 SSE 的 tool_result 事件**同一条消费路径**。
        result: hasResult ? filterToolResult(s.tool, raw) : undefined,
        ok: s.ok !== false,
        // 【失败工具的错误文案只显示一次】
        // 后端 _tool_result_summary() 对失败工具返回的就是 result["error"]
        // —— 所以 summary 里**已经**是错误文案了。
        // SSE 路径同理：tool_result 事件只带 summary，不带 error 字段，
        //            于是前端只渲染「结果」行，不重复渲染 .ag-tool-err。
        // 这里如果再把 steps[].error 填上，同一句错误会**显示两遍**。
        // 所以：只有「没有结果摘要」时才用 steps[].error 兜底。
        error: (s.ok === false && !hasResult) ? (s.error || "") : "",
        ms: s.ms,
        done: true,
        call_id: s.call_id || null,
      };
    });
  }

  // steps[].args（JSON 字符串）-> 对象。解析失败返回原始字符串。
  function parseStepArgs(raw) {
    if (raw === undefined || raw === null || raw === "") return null;
    if (typeof raw === "object") return raw;   // 已经是对象（老契约直接给对象）
    try {
      const parsed = JSON.parse(raw);
      return (parsed && typeof parsed === "object") ? parsed : raw;
    } catch (e) {
      return raw;   // 不吞：让用户看到原文而不是空白
    }
  }

  // ---------- 任务卡状态轮询 ----------
  //
  // 【为什么不用 Studio 任务卡 / .model-run-token】
  //   Studio 的 MCP 任务卡靠 `profiles/<profile>/.model-run-token` 认证，
  //   而这个 token 是 **JWT，exp = iat + 3600（1 小时）**，Web UI 启动时写一次、
  //   此后不再刷新。实测 9 个 profile 里有 6 个已经过期
  //   （default/av/writing/ops/game/kuaishou），一旦过期：
  //     * MCP 工具路径必然失败（schema 注入 item + 过期 token 双重问题）
  //     * 任务卡永远停在初始状态，不更新
  //   所以前端**不依赖**那条链路：Agent 下发任务后，服务端把 job_id
  //   交给前端，前端直接轮询工作台自己的 `GET /api/jobs/{id}`
  //   —— 同一个 SQLite 库、同一份状态、无 token、无过期。
  //
  // 轮询只在「还有未完成的任务」时运行，全部终态后自动停。

  const jobPoll = {
    timer: null,
    ids: new Set(),
    messages: new Map(),   // job_id -> message
    startedAt: 0,          // 轮询开始时间（超时判定用）
    errors: 0,             // 连续失败次数
  };

  // SSE 的**硬超时**（2026-11-10 补）：Agent 一条完整链路实测 4~6 分钟，
  // 服务端自身有 TOTAL_TIMEOUT=420s 兜底；前端再给一层 10 分钟上限，
  // 避免后端异常时界面永远停在「思考中」。
  const SSE_MAX_MS = 10 * 60 * 1000;
  // 当前活跃的 SSE 控制器（用于「停止」按钮主动取消）
  let activeSSE = null;

  // 主动取消正在进行的 SSE（页面卸载 / 用户点「停止」时调用）
  function cancelAgentSSE(reason) {
    if (!activeSSE) return false;
    try { activeSSE.abort(); } catch (e) { /* 已断 */ }
    activeSSE = null;
    // 【2026-11-13 修：真实缺陷】
    //
    // 原来这里调 `setHint(reason, "error")` ——
    // 但 **agent.js 里从来没有定义过 setHint**（全文件只有这一处出现）。
    // 于是**每次点「停止」都会抛 ReferenceError**：
    //     Uncaught ReferenceError: setHint is not defined
    // 后果很具体：
    //   * abort() 本身已经执行了（流确实断了），但
    //   * 异常从 cancelAgentSSE 抛出后，调用方拿不到 `true`，
    //   * 界面也不会显示「已停止」—— 用户点了没反应。
    //
    // 【正确的做法】
    // 取消是**针对某条正在流式的气泡**的，所以提示要写到那条消息上
    // （msg.error），由 renderError() 渲染 —— 这跟 SSE 超时/失败
    // 走的是同一条展示路径（send() 的 catch 里也是设 msg.error）。
    // 这里不自己造第二套提示机制。
    if (reason) {
      const msg = state.streaming;
      if (msg) {
        msg.error = reason;
        msg.streaming = false;
        msg.result = msg.result || { status: "failed" };
        renderMessages();
      }
    }
    return true;
  }

  const JOB_POLL_INTERVAL = 3000;
  const JOB_TERMINAL = ["done", "failed", "canceled"];
  // 轮询的**硬终止条件**（2026-11-10 补）：
  //   1. 全部任务进终态 -> 停（原有逻辑）
  //   2. 超过 JOB_POLL_MAX_MS -> 停（原来**没有**这条：任务卡永远停在
  //      「排队中/运行中」，页面一直每 3 秒打一次 /api/jobs/{id}）
  //   3. 连续 JOB_POLL_MAX_ERRORS 次查询失败 -> 停（后端挂了不该无限轮询）
  // 停止时把超时的任务标成「状态未知（已停止轮询）」，不假装它还在跑。
  const JOB_POLL_MAX_MS = 30 * 60 * 1000;   // 30 分钟
  const JOB_POLL_MAX_ERRORS = 12;            // 连续 12 次失败（约 36 秒）

  function startJobPolling(msg) {
    (msg.jobs || []).forEach(j => {
      if (!j.job_id) return;
      jobPoll.ids.add(j.job_id);
      jobPoll.messages.set(j.job_id, msg);
    });
    if (jobPoll.timer) return;
    jobPoll.startedAt = Date.now();
    jobPoll.errors = 0;
    jobPoll.timer = setInterval(pollJobs, JOB_POLL_INTERVAL);
    pollJobs();
  }

  async function pollJobs() {
    const ids = [...jobPoll.ids];
    if (!ids.length) { stopJobPolling(); return; }
    // ---- 终止条件 2：超时 ----
    if (jobPoll.startedAt && Date.now() - jobPoll.startedAt > JOB_POLL_MAX_MS) {
      markPollingTimedOut();
      stopJobPolling();
      renderMessages();
      return;
    }
    let stillActive = false;
    let sawError = false;
    for (const id of ids) {
      try {
        const job = await apiJSON("GET", "/api/jobs/" + encodeURIComponent(id));
        const msg = jobPoll.messages.get(id);
        if (msg) {
          const entry = (msg.jobs || []).find(j => j.job_id === id);
          if (entry) {
            entry.status = job.status || entry.status;
            entry.progress = job.progress;
            entry.progress_text = job.progress_text || "";
            entry.error = job.error || "";
            entry.outputs = job.outputs || entry.outputs;
            entry.params = job.params || entry.params;
            if (job.gen_type_label && !entry.label) entry.label = job.gen_type_label;
          }
        }
        if (!JOB_TERMINAL.includes(job.status)) stillActive = true;
        else jobPoll.ids.delete(id);
      } catch (err) {
        // 单个任务查不到（可能被删了）—— 不致命，继续轮询其余任务。
        // 但不能因此判定「全部完成」，所以仍保留轮询。
        stillActive = true;
        sawError = true;
      }
    }
    // ---- 终止条件 3：连续失败太多次 ----
    if (sawError) {
      jobPoll.errors += 1;
      if (jobPoll.errors >= JOB_POLL_MAX_ERRORS) {
        markPollingTimedOut("后端连续查询失败，已停止轮询");
        stopJobPolling();
        renderMessages();
        return;
      }
    } else {
      jobPoll.errors = 0;
    }
    if (!stillActive && !jobPoll.ids.size) {
      stopJobPolling();
      renderMessages();
    } else if (stillActive) {
      renderMessages();
    }
  }

  // 把还在轮询的任务标成「状态未知」—— 不假装它还在跑，
  // 也不谎称它失败（用户可以手动刷新）。
  function markPollingTimedOut(reason) {
    const text = reason || "已超过轮询时限，状态未知（可手动刷新）";
    for (const id of jobPoll.ids) {
      const msg = jobPoll.messages.get(id);
      if (!msg) continue;
      const entry = (msg.jobs || []).find(j => j.job_id === id);
      if (!entry) continue;
      if (JOB_TERMINAL.includes(entry.status)) continue;
      entry.status = "unknown";
      entry.progress_text = text;
    }
    jobPoll.ids.clear();
  }

  function stopJobPolling() {
    if (jobPoll.timer) { clearInterval(jobPoll.timer); jobPoll.timer = null; }
  }

  // 手动刷新某个任务卡（点「刷新」按钮）。
  async function refreshJob(jobId) {
    try {
      const job = await apiJSON("GET", "/api/jobs/" + encodeURIComponent(jobId));
      for (const msg of state.messages) {
        const entry = (msg.jobs || []).find(j => j.job_id === jobId);
        if (!entry) continue;
        entry.status = job.status || entry.status;
        entry.progress = job.progress;
        entry.progress_text = job.progress_text || "";
        entry.error = job.error || "";
        entry.outputs = job.outputs || entry.outputs;
      }
      renderMessages();
    } catch (err) {
      const status = $("#agStatus");
      if (status) status.textContent = "刷新任务状态失败：" + (err.message || "未知错误");
    }
  }

  async function newSession() {
    // 【先把旧会话的折叠态存进旧会话的桶 —— 2026-11-13】
    // 下面马上要把 state.sessionId 清空。清空之后再抓，抓到的就是
    // 「空会话桶」而不是「旧会话桶」-> 旧会话的展开态丢了，
    // 切回旧会话时工具区密度会变。
    // 所以抓取必须**在**清 sessionId **之前**。
    const box = $("#agMessages");
    if (box) captureDetailsState(box);
    state.sessionId = "";
    state.messages = [];
    state.lastResult = null;
    state.streaming = null;
    // 【会话变了 -> 折叠态作用域前缀也要换 —— 2026-11-13】
    // 否则新会话沿用旧会话的 key 前缀，展开/收起状态会跨会话串味。
    state._ephemeralScope = "";
    stopProgress();
    jobPoll.ids.clear();
    jobPoll.messages.clear();
    stopJobPolling();
    // 新会话 = 旧附件不再属于任何消息 —— 清掉。
    state.attachments = [];
    state.attachError = "";
    state.attachBusy = false;
    state.attachStage = "";
    renderAttachments();
    renderMessages();
    const status = $("#agStatus");
    if (status) status.textContent = "";

    // 【真正新建一个会话 —— 2026-11-11】
    // 原来这里只是把内存里的 sessionId 清空，**服务端根本不知道**，
    // 于是：刷新页面回到空会话、刚发的那轮对话找不回来、侧栏列表里也没有。
    // 现在交给会话管理层去服务端建一个（并落库）。
    //
    // 【为什么不在这里直接调 API】
    // 会话的新建/删除/列表是**一整套**操作（建库 + 刷列表 + 切视图），
    // 分散在两个文件里必然漂移。统一由 agent_sessions.js 负责。
    if (window.AgentSessions && window.AgentSessions.createSession) {
      await window.AgentSessions.createSession();
    }
  }

  // 把 Agent 的任务回填到创作面板
  async function fillJob(jobId, mode) {
    try {
      const job = await apiJSON("GET", "/api/jobs/" + encodeURIComponent(jobId));
      // 切到创作页并选中对应模式
      const nav = document.querySelector(`.nav-item[data-mode="${mode}"]`);
      if (nav) nav.click();
      else {
        alert("该任务的模式在当前面板里没有对应入口：" + mode);
        return;
      }
      const prompt = $("#prompt");
      if (prompt && job.params && job.params.prompt) {
        prompt.value = job.params.prompt;
        prompt.dispatchEvent(new Event("input", { bubbles: true }));
      }
      const status = $("#agStatus");
      if (status) status.textContent = `已把任务 ${jobId} 的提示词填入创作面板，可改参数后直接生成。`;
    } catch (err) {
      alert("回填失败：" + err.message);
    }
  }

  function fillPrompt(text) {
    const nav = document.querySelector('.nav-item[data-mode="t2i"]');
    if (nav) nav.click();
    const prompt = $("#prompt");
    if (!prompt) { alert("找不到提示词输入框"); return; }
    prompt.value = text;
    prompt.dispatchEvent(new Event("input", { bubbles: true }));
    const status = $("#agStatus");
    if (status) status.textContent = "已把增强后的提示词填入创作面板。";
  }

  // ---------- 初始化 ----------

  function show() {
    const page = $("#agentPage");
    if (!page) return;
    // 显示 Agent 页：移除 .page-hidden，让 .agent-page 自己的 display:flex 生效。
    // （不要写 page.style.display="block" —— 内联 display 会覆盖 CSS 的
    //   display:flex，flex 布局塌掉后消息区不再收缩，输入框会被顶出视口。）
    page.classList.remove("page-hidden");
    page.style.removeProperty("display");
    const jobs = $("#jobsArea");
    const panel = $("#panel");
    const stitch = $("#stitchPanel");
    const system = $("#systemPage");
    const providers = $("#providersPage");
    const assets = $("#assetsPage");
    [jobs, panel, stitch, system, providers, assets].forEach(el => {
      if (el) { el.classList.add("page-hidden"); el.style.removeProperty("display"); }
    });
    $$(".nav-item").forEach(x => x.classList.remove("active"));
    const nav = $("#navAgent");
    if (nav) nav.classList.add("active");
    if (window.state) window.state.page = "agent";
    if (window.updateResponsiveChrome) window.updateResponsiveChrome();
    renderMessages();
  }

  function bind() {
    const nav = $("#navAgent");
    if (nav) nav.addEventListener("click", show);
    const btn = $("#agSend");
    if (btn) btn.addEventListener("click", send);
    const input = $("#agInput");
    if (input) {
      input.addEventListener("keydown", e => {
        if (e.key === "Enter" && !e.shiftKey) {
          e.preventDefault();
          send();
        }
      });
      // 【2026-11-13 审查目标 3】剪贴板粘贴媒体（图片 / 音频 / 视频）。
      // 绑在输入框上（不是 document）—— 只有焦点在输入框时才接管粘贴，
      // 否则用户从别处复制一段话粘到别的地方也会被劫持。
      input.addEventListener("paste", handlePaste);
    }
    const fresh = $("#agNew");
    if (fresh) fresh.addEventListener("click", newSession);

    // --- 附件上传入口（2026-11-11） ---
    const attachBtn = $("#agAttachBtn");
    const attachInput = $("#agAttachInput");
    if (attachBtn && attachInput) {
      attachBtn.addEventListener("click", () => attachInput.click());
      attachInput.addEventListener("change", event => {
        const files = event.target.files;
        if (files && files.length) uploadAttachments(files);
        // 清空 value —— 否则连续选同一个文件不会触发 change 事件
        event.target.value = "";
      });
    }
    // 拖拽上传：把图片拖进输入区也能传。
    const composer = $(".ag-composer");
    if (composer) {
      composer.addEventListener("dragover", event => {
        if (!event.dataTransfer) return;
        const hasFile = [...(event.dataTransfer.types || [])].includes("Files");
        if (!hasFile) return;
        event.preventDefault();
        event.dataTransfer.dropEffect = "copy";
        composer.classList.add("ag-composer-drag");
      });
      composer.addEventListener("dragleave", event => {
        if (event.target === composer) composer.classList.remove("ag-composer-drag");
      });
      composer.addEventListener("drop", event => {
        composer.classList.remove("ag-composer-drag");
        const files = event.dataTransfer && event.dataTransfer.files;
        if (!files || !files.length) return;
        event.preventDefault();
        uploadAttachments(files);
      });
    }
    // 附件缩略图上的「移除」按钮（事件委托）。
    //
    // 【为什么单独绑而不是复用 #agMessages 的委托】
    // 附件区在 composer 里，不在消息区里。
    const attachBox = $("#agAttachments");
    if (attachBox) {
      attachBox.addEventListener("click", event => {
        const btn = event.target.closest("[data-act='remove-attachment']");
        if (!btn) return;
        event.preventDefault();
        removeAttachment(btn.dataset.index);
      });
    }
    const box = $("#agMessages");
    if (box) {
      // 事件委托：所有交互元素都用 data-act 标记。
      //
      // 注意选择器是 [data-act] 而不是 .ag-act ——
      // 工具卡片的「刷新」和澄清选项不是 .ag-act 样式，
      // 用 .ag-act 会让它们点不动。
      box.addEventListener("click", e => {
        const btn = e.target.closest("[data-act]");
        if (!btn) return;
        const act = btn.dataset.act;
        if (act === "fill-prompt") fillPrompt(btn.dataset.text || "");
        else if (act === "fill-job") fillJob(btn.dataset.job, btn.dataset.mode);
        else if (act === "retry") {
          // 重试：找到这条按钮属于哪条消息，取出它自己的请求体。
          // （直接 send() 读的是 #agInput，此刻是空的 —— 必须先填回去。）
          const bubble = btn.closest(".ag-msg");
          const index = bubble ? Number(bubble.getAttribute("data-msg-index")) : -1;
          const target = index >= 0 ? state.messages[index] : null;
          const req = target && target.retryRequest;
          if (!req) return;
          const input2 = $("#agInput");
          if (input2) input2.value = req.message || "";
          send();
        } else if (act === "clarify-answer") {
          // 【作用域定位 —— 2026-11-13 实测 BUG 修复】
          // 原来是 `box.querySelector(".ag-clarify-input")` —— 取的是
          // 消息区里的**第一个**输入框，点下面那条的选项却填进了上面那条。
          // 现在只写**这个选项自己所属**的那条澄清气泡的输入框。
          const value = btn.dataset.value || "";
          const field = clarifyInputOf(btn);
          if (field) {
            field.value = value;
            field.focus();
            // 已选项高亮 + 本组其余选项禁用（避免重复提交同一个答案）。
            // 只影响**本组**（同一个 .ag-clarify 容器），不碰别的消息。
            const scope = btn.closest(".ag-clarify");
            if (scope) {
              scope.querySelectorAll(".ag-clarify-opt").forEach(opt => {
                opt.disabled = true;
                opt.classList.toggle("ag-clarify-opt-picked",
                  opt === btn);
              });
            }
          }
        } else if (act === "clarify-send") {
          // 同样按作用域取输入框 —— 否则「发送回答」会读到别的消息的输入框。
          const field = clarifyInputOf(btn);
          const value = (field && field.value || "").trim();
          if (!value) return;
          const input2 = $("#agInput");
          if (input2) {
            input2.value = value;
            send();
          }
        } else if (act === "refresh-job") {
          refreshJob(btn.dataset.job);
        }
      });
    }
  }

  // 暴露给 app.js（app.js 的 show* 系列需要能切到这个页）
  window.AgentUI = {
    show, state, renderMessages,

    // 2026-11-11：会话恢复 —— 把服务端会话库里的消息还原成界面。
    //
    // 【为什么必须有这个入口】
    // 会话持久化的完整链路是：
    //   服务端落库（agent_sessions.py）-> agent_sessions.js 拉取 ->
    //   **这里还原成消息结构** -> renderMessages 画出来。
    // 少了这一步，前端就只是「拿到了 JSON 但画不出来」。
    //
    // 【为什么直接替换 state.messages 而不是追加】
    // 恢复是**整段替换**当前会话的视图。追加会把旧会话的消息
    // 和新会话的消息混在一起 —— 用户切到 B 会话却看到 A 的对话。
    restoreMessages(messages, options) {
      // 【为什么要有「不要覆盖已有消息」的模式 —— 2026-11-11 实测回归】
      // 会话管理层是**异步**初始化的（要拉会话列表 + 会话详情）。
      // 它的 init() 会在某个时刻调用 restoreMessages()。
      //
      // 实测踩到的竞态：
      //   1. 用户（或测试）很快发了一条消息 -> state.messages 里有 1 条
      //   2. 会话层 init() 这时才拿到「恢复出来的历史」-> 调 restoreMessages
      //   3. restoreMessages 直接**替换** state.messages -> 那条刚发的消息**没了**
      //
      // 后果：用户发完消息刷新一下（或慢一点网络），消息凭空消失。
      // 这不是测试问题 —— 是真的会把用户消息弄丢。
      //
      // 修法：会话层初始化时用 "merge" 模式 ——
      // **只补历史，不删已经在的**。真正切换会话时仍然整体替换
      // （那是用户主动要的语义）。
      const opts = options || {};
      const incoming = Array.isArray(messages) ? messages.slice() : [];
      if (opts.merge) {
        // 已在的保留在前面（用户刚发的更新），恢复的历史追加在后面。
        const seen = new Set(state.messages.map(m => m._sid || ""));
        for (const m of incoming) {
          if (!m._sid || !seen.has(m._sid)) state.messages.push(m);
        }
      } else {
        state.messages = incoming;
        state.streaming = null;
        state.lastResult = null;
      }
      // 【会话作用域同步 —— 2026-11-13 工具密度修复】
      // 还原的是**某个具体会话**的消息，所以当前会话作用域必须跟着换。
      // 换作用域 = 换折叠态 key 的前缀 + 换折叠态桶，
      // 否则不同会话的 m0-tool-0 撞成同一个 key -> 折叠态跨会话串味 ->
      // 工具区展开/收起状态随会话乱跳，密度看起来就会「变」。
      //
      // 为什么以前没发现：agent.js 的 state.sessionId 只在**发消息**时被
      // 赋值，切会话（loadSession -> restoreMessages）这条路**从不更新它**。
      // 于是切会话后 state.sessionId 仍是旧会话 id（甚至是空串）。
      //
      // 【作用域从会话层读，不靠调用方传 —— 2026-11-13】
      // 会话层在 loadSession() 里已经更新了自己的 state.currentId，
      // 所以直接问它就行。少一个「调用方必须记得传 sessionId」的约定
      // = 少一个漏传导致串味的机会。
      // opts.sessionId 仍然被支持（测试与显式调用可以覆盖）。
      const scopeSid = opts.sessionId !== undefined
        ? opts.sessionId
        : currentSessionId();
      applySessionScope(scopeSid);
      // 恢复出来的消息**不该**再触发轮询（服务端已经落了状态）。
      // 但任务卡里的产物缩略图要能显示 —— 那依赖 msg.jobs[].outputs，
      // 由 meta 带过来（见 server._agent_message_meta）。
      renderMessages();
      return state.messages;
    },

    // 2026-11-11：新建/删除会话的内部接口（测试与验收用）。
    _newSession: newSession,
    _setSessionId: function (id) {
      state.sessionId = id || "";
      return state.sessionId;
    },
    // 2026-11-11：附件（图片上传）的内部接口 —— 测试与验收用。
    //
    // 【为什么不直接操作 DOM】
    // 上传走的是真实 HTTP（multipart）+ 真实素材库落库，
    // 验收必须打到真实服务端才有意义。
    // 这里暴露的是「把文件塞进上传流程」与「当前附件列表」两个入口。
    _renderAttachments: renderAttachments,
    _uploadAttachments: uploadAttachments,
    _removeAttachment: removeAttachment,
    _MAX_ATTACHMENTS: MAX_ATTACHMENTS,
    // 测试与调试用的内部接口
    _applyStreamEvent: applyStreamEvent,
    _parseSseBlock: parseSseBlock,
    _consumeSse: consumeSse,
    _pollJobs: pollJobs,
    _startJobPolling: startJobPolling,
    _renderStreamingMessage: renderStreamingMessage,
    // 2026-11-10：SSE 取消 / 轮询终止的内部接口（测试与「停止」按钮用）
    _cancelAgentSSE: cancelAgentSSE,
    _stopJobPolling: stopJobPolling,
    _markPollingTimedOut: markPollingTimedOut,
    _SSE_MAX_MS: SSE_MAX_MS,
    _JOB_POLL_MAX_MS: JOB_POLL_MAX_MS,
    _JOB_POLL_MAX_ERRORS: JOB_POLL_MAX_ERRORS,
    // 2026-11-11：把一份「结果」渲染成一条消息（测试与验收用）。
    //
    // 【为什么需要】
    // 「编造 job id 误报」「工具级 ✓/✗」「截断中性条」的验收要看
    // **用户真正看到的界面**：红色「失败」标签、底部红条、工具块的
    // ✓/✗、任务卡与缩略图是否渲染。
    // 直接构造 state 再调 renderMessages 会绕过 upsertJob / 轮询，
    // 与真实 SSE 路径不等价 —— 所以这里走**与 SSE 完全相同**的
    // applyStreamEvent 入口。
    //
    // 【2026-11-11 补齐 —— 让它与真实路径等价】
    // 真实链路（send() 里）除了 applyStreamEvent 还做两件事：
    //   1) syncToolsFromSteps(msg, data.steps) —— 一次性路径靠它把
    //      steps[] 转成工具轨迹（流式路径由 tool_result 事件驱动）。
    //      少了它，工具块根本不会出现（工具级 ✓/✗ 没法验收）。
    //   2) if (data.error) msg.error = data.error —— 截断原因就是靠
    //      这里进 msg.error，前端才渲染出说明条。
    // 少了第 2 条，截断场景在验收里会**假通过**（看不到条 = 看不到 bug）。
    _injectRenderedMessage(result, eventName) {
      const msg = {
        role: "assistant",
        content: "",
        streaming: false,
        tools: [],
        jobs: [],
        assets: [],
        steps: [],
      };
      state.messages.push(msg);
      applyStreamEvent(msg, eventName || "done", result || {});
      // 与真实一次性路径一致：steps[] -> 工具轨迹
      syncToolsFromSteps(msg, (result || {}).steps);
      // 与真实路径一致：错误/截断原因进 msg.error
      if (result && result.error) msg.error = result.error;
      renderMessages();
      return msg;
    },
  };

  // 页面卸载时**必须**断开 SSE —— 否则服务端那边的 Agent 还在跑，
  // 而前端连接一直挂着（本地算力被白白占用）。
  window.addEventListener("beforeunload", () => {
    try { cancelAgentSSE(); } catch (e) { /* 忽略 */ }
    try { stopJobPolling(); } catch (e) { /* 忽略 */ }
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => {
      bind();
      renderAttachments();
    });
  } else {
    bind();
    renderAttachments();
  }
})();
