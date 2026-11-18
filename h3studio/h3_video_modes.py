# -*- coding: utf-8 -*-
"""MiniMax H3 的**首尾帧**与**参考生视频**工作流（含音轨三选一）。

【为什么单独一个模块】
B1 / B2 的需求在旧的 ``h3studio/workflows.py`` 里被硬塞进
``_h3_video_workflow`` / ``build_r2v``：
  * 首帧 / 尾帧只有两个槽，且必须首帧必填；
  * 参考图最多 3 张（节点上限其实是 9）；
  * 参考视频最多 3 个、参考音频最多 3 个；
  * **音轨只有一种**：用 H3 自己生成的音频，没有「用参考音乐当音轨」
    这回事。

而且旧路径用的是 ``MiniMaxH3ImageToVideo`` / ``MiniMaxH3ReferenceToVideo``
（ComfyUI 核心节点）—— 它们**不接受音频输入**：
    MiniMaxH3ImageToVideo  required: clip/vae/prompt/width/height/length
                           optional: first_frame / last_frame
    MiniMaxH3ReferenceToVideo optional: ref_images / ref_videos /
                                      ref_video_audios / ref_audios
两者都**没有 drive_audio**。

【节点能力实测结论 —— B1「首尾帧模式是否支持音乐输入」】
**支持**，但必须换一个节点：
    MiniMaxH3AudioConditioningT8（comfyui-minimax-h3-audio-T8 插件）
      optional: drive_audio / final_audio / first_frame / last_frame /
                ref_images / ref_videos / ref_video_audios / ref_audios
      outputs:  CONDITIONING / LATENT(av_latent) / AUDIO(mux_audio) / ...
它一个节点就同时覆盖了首尾帧、参考图、参考视频、参考音频、驱动音频，
并且额外输出 ``mux_audio``（成片该贴哪条音轨）。

所以本模块统一用这个节点，而不是两个核心节点。

【音轨三选一】
    (a) generated —— H3 与画面一同采样出的音频（音画同源）。
    (b) reference —— 参考音乐**贴**到成片上（不参与采样）。
    (c) video_bg —— 参考视频的背景音乐**贴**到成片上（不参与采样）。
可见性判据见 ``h3studio/h3_audio_track.py``（没有对应素材就不出现）。
"""

from __future__ import annotations

import random

from . import h3_audio_track
from . import h3_audio_continuation as h3acont
from . import continuation as cont
from . import continuation_service as cont_service
from . import video_models as vmodels
from . import video_workflow as vworkflow
from . import frame_expr as frame_expr_mod
from . import twopass_policy

#: T8 条件节点的类名（comfyui-minimax-h3-audio-T8 插件）
T8_CONDITIONING = "MiniMaxH3AudioConditioningT8"

#: 参考图上限（节点 schema：ref_images max=9）
MAX_REF_IMAGES = 9
#: 参考视频上限（节点 schema：ref_videos max=3）
MAX_REF_VIDEOS = 3
#: 独立参考音频上限（节点 schema：ref_audios max=3）
MAX_REF_AUDIOS = 3

#: 视频 VAE / 音频 VAE（与旧路径同一套权重）
VAE_VIDEO = "minimax_h3_video_vae_fp16.safetensors"
VAE_AUDIO = "minimax_h3_audio_vae_fp32.safetensors"
#: 文本编码器
CLIP_H3 = "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"


class H3VideoRequestError(ValueError):
    """请求本身不合法（缺首帧 / 参考素材为空 / 超过上限）。"""


def _frame_expr_node(length_node_id: str, dur_node_id: str, duration: float) -> dict:
    """秒数 -> 帧数节点（**唯一真源**在 h3studio/frame_expr.py）。

    【2026-11-16 第5轮代码审查 C-13 修 —— 三份重复实现合一】
    原来这里有第二份 _frame_expr_node 实现，与 video_workflow.py /
    workflows.py 的两份重复。现在统一走 frame_expr.frame_expr_node。
    """
    return frame_expr_mod.frame_expr_node(length_node_id, dur_node_id, duration)


def _model_chain(p: dict, params: dict, model: dict) -> list:
    """底模 + 加速 LoRA + 效果 LoRA + 注意力 -> 模型引用。

    与文生视频面板**完全同一套**逻辑（统一槽位 + 按权重形态选加载节点），
    否则「同一个 LoRA 在文生视频里生效、在首尾帧里不生效」。

    【2026-11-15 修复③ 交付项 3】这里补上了**效果 LoRA 链**与
    **注意力机制** —— 三个视频模式现在共用同一套面板，
    面板上能调的效果 LoRA / 注意力必须**真的生效**；
    否则就成了「界面有控件、拖了没反应」的假控件。

    链路顺序与文生视频**完全一致**：
        底模 -> 加速 LoRA 链 -> 效果 LoRA 链 -> 注意力 -> sigma shift
    （加速 LoRA 改采样语义，效果 LoRA 改画面风格，两者并列共存。）
    """
    p["6"] = {"inputs": {"unet_name": model["unet"], "weight_dtype": "default"},
              "class_type": "UNETLoader", "_meta": {"title": "加载DiT"}}
    model_ref = ["6", 0]
    chain = vworkflow._resolve_accel_chain(params, model)
    if chain:
        model_ref = vworkflow.apply_accel_lora_nodes(p, model_ref, chain)

    # ---- 效果 LoRA 链（用户可多选，各自带强度）----
    effect_chain = vworkflow._resolve_effect_chain(params)
    if effect_chain:
        model_ref, _next_id = vworkflow._apply_effect_loras(
            p, model_ref, effect_chain, 320)
        # 触发词自动追加到提示词末尾（与文生视频同一套规则：
        # 已存在的词不重复追加，多枚 LoRA 按顺序追加）
        from . import workflows as wf
        merged = wf.merge_lora_triggers(params.get("prompt", ""), effect_chain)
        if merged != (params.get("prompt") or ""):
            params["prompt"] = merged
            params["_prompt_merged_with_triggers"] = True

    # ---- 注意力机制 ----
    model_ref, _next_id = vworkflow._apply_attention(
        p, "300", model_ref, params, 301)
    return model_ref


def _sampling_defaults(params: dict, accel_chain: list) -> tuple[int, str, str]:
    """采样档位默认值（跟着**实际挂的那枚**加速 LoRA 走）。"""
    if accel_chain:
        meta = vmodels.accel_lora_meta(accel_chain[0].get("key") or "") or {}
        default_steps = int(meta.get("recommended_steps") or 4)
        default_scheduler = meta.get("recommended_scheduler") or "beta"
        default_sampler = meta.get("recommended_sampler") or "euler"
    else:
        default_steps = vmodels.VIDEO_STEPS_WITHOUT_ACCEL
        default_scheduler = vmodels.VIDEO_SCHEDULER_WITHOUT_ACCEL
        default_sampler = vmodels.VIDEO_SAMPLER_WITHOUT_ACCEL
    steps = vmodels.normalize_steps(params.get("steps"), default_steps)
    scheduler = vmodels.normalize_scheduler(params.get("scheduler"), default_scheduler)
    sampler = vmodels.normalize_sampler(params.get("sampler"), default_sampler)
    return steps, scheduler, sampler


def _ref_image_nodes(p: dict, filenames: list) -> dict:
    """参考图 -> 节点引用（最多 9 张）。"""
    out = {}
    for index, filename in enumerate(filenames[:MAX_REF_IMAGES]):
        node_id = f"136:{201 + index}"
        p[node_id] = {
            "inputs": {"image": filename},
            "class_type": "LoadImage",
            "_meta": {"title": f"加载参考图 {index + 1}"},
        }
        # 【关键】autogrow 槽位的键名格式是 "<容器名>.<前缀><序号>"，
        # 例如 "ref_images.ref_image_0"。写成 "ref_image_0" 会被节点
        # 当成未知输入 -> 参考图**静默不生效**（画面里看不到参考内容）。
        # 格式依据：nonebot 参考工作流 minimax_h3_v1.json 的 node 136。
        out[f"ref_images.ref_image_{index}"] = [node_id, 0]
    return out


def _ref_video_nodes(p: dict, filenames: list,
                     skip_frames: int = 0, frame_cap: int = 0) -> dict:
    """参考视频 -> 节点引用（最多 3 个）。

    【2026-11-15 修 —— VHS_LoadVideo 的 6 个 required 输入必须显式给】

    原来这里只给了 `video` 和 `format`：
        {"inputs": {"video": filename, "format": "None"}, ...}

    但 VHS_LoadVideo（ComfyUI-VideoHelperSuite）的 schema 里
    **required** 有 7 项（实测 /object_info）：
        video / force_rate / custom_width / custom_height /
        frame_load_cap / skip_first_frames / select_every_nth

    ComfyUI 通过 API 提交工作流时**不套用** schema 默认值 ——
    缺任何一个 -> 提交被拒（"Required input is missing"）。
    （这些输入在 UI 上带 `disable: 0`，所以只有走 API 才暴露。）

    实测证据：先用只给 video+format 的版本提交，ComfyUI 返回
        node_errors: {"1": {"errors": [
            {"type": "required_input_missing", "details": "custom_width"},
            ... 共 6 项 ]}}
    补齐后同一个视频正常解码出帧（status: success）。

    【为什么之前没暴露】参考视频这条路径**从来没有被真实跑过** ——
    线上 24 条 r2v 任务全部只用参考图 + 参考音乐。

    【2026-11-16 接续】skip_frames / frame_cap 用于**音画同步切割**：
    接续段要取参考视频的 [t_a_start, t_a_end) 那一段 ——
    视频侧跳 C_n 帧、音频侧从 C_n/24 秒起（见 h3_audio_continuation）。
    force_rate 必须给 **24**（H3 固定帧率）—— 否则「跳 N 帧」与
    「从 N/24 秒起」在音频侧就对不上了。
    """
    out = {}
    for index, filename in enumerate(filenames[:MAX_REF_VIDEOS]):
        node_id = f"136:{301 + index}"
        p[node_id] = {
            "inputs": {
                "video": filename,
                # 【以下 6 项是 VHS_LoadVideo 的 required 输入，必须显式给】
                # force_rate 24 = 归一到 H3 的 24fps（接续段音画同步切割
                #   的前提：视频侧跳帧数与音频侧秒数必须指向同一时刻）；
                #   非接续段沿用 0（保持原帧率）。
                # custom_width/height 0 = 不改尺寸；
                # frame_load_cap 0 = 不限帧数；skip_first_frames 0 = 从头开始；
                # select_every_nth 1 = 每帧都取。
                "force_rate": 24 if skip_frames else 0,
                "custom_width": 0,
                "custom_height": 0,
                "frame_load_cap": int(frame_cap or 0),
                "skip_first_frames": int(skip_frames or 0),
                "select_every_nth": 1,
                "format": "None",
            },
            "class_type": "VHS_LoadVideo",
            "_meta": {"title": f"加载参考视频 {index + 1}"},
        }
        out[f"ref_videos.ref_video_{index}"] = [node_id, 0]
    return out


def _ref_audio_nodes(p: dict, filenames: list,
                     start_seconds: float = 0.0,
                     duration: float = 0.0) -> dict:
    """独立参考音频 -> 节点引用（最多 3 个）。

    【2026-11-16 接续】start_seconds / duration 用于**按偏移切片**：
    接续段的采样用音频是「1s 空白 ⊕ 原始音频从第 N 秒起的切片」。
    服务端已经把切好的段音频上传到 ComfyUI input（参数
    params["segment_audio_file"]），所以这里**不需要**偏移 ——
    切片已是一个独立的文件。

    但「参考视频的音轨」那一路需要**音画同步切割**：视频侧跳
    C_n 帧、音频侧从 C_n/24 秒起 —— 两者指向同一时刻。
    这时 start_seconds / duration 才有用（见 _ref_video_audio_nodes）。
    """
    out = {}
    for index, filename in enumerate(filenames[:MAX_REF_AUDIOS]):
        node_id = f"136:{401 + index}"
        inputs: dict = {"audio": filename}
        if start_seconds > 0:
            # VHS_LoadAudioUpload 的 start_time 是**秒**（浮点）。
            # t_a_start(n) = C_n / 24，C_n 是整数帧 -> 无浮点误差。
            inputs["start_time"] = round(float(start_seconds), 6)
        if duration > 0:
            inputs["duration"] = round(float(duration), 6)
        p[node_id] = {
            "inputs": inputs,
            "class_type": "VHS_LoadAudioUpload",
            "_meta": {"title": f"加载参考音频 {index + 1}"},
        }
        out[f"ref_audios.ref_audio_{index}"] = [node_id, 0]
    return out


def _ref_video_audio_nodes(p: dict, filenames: list,
                           start_seconds: float = 0.0,
                           duration: float = 0.0) -> dict:
    """参考视频的背景音 -> 节点引用（最多 3 个，与视频同序号配对）。

    【2026-11-16 接续】音画同步切割：
    视频侧 VHS_LoadVideo.skip_first_frames = C_n（**帧**），
    音频侧 VHS_LoadAudioUpload.start_time = C_n / 24（**秒**）。
    参考视频已用 force_rate=24 归一 —— 所以两者严格指向同一时刻，
    不存在浮点累计误差（C_n 是整数帧）。
    """
    out = {}
    for index, filename in enumerate(filenames[:MAX_REF_VIDEOS]):
        node_id = f"136:{501 + index}"
        inputs: dict = {"audio": filename}
        if start_seconds > 0:
            inputs["start_time"] = round(float(start_seconds), 6)
        if duration > 0:
            inputs["duration"] = round(float(duration), 6)
        p[node_id] = {
            "inputs": inputs,
            "class_type": "VHS_LoadAudioUpload",
            "_meta": {"title": f"加载参考视频背景音 {index + 1}"},
        }
        out[f"ref_video_audios.ref_video_audio_{index}"] = [node_id, 0]
    return out


def _continuation_media_slice(params: dict) -> dict:
    """接续段的**媒体切片参数**（参考视频跳帧 / 音频起点与时长）。

    返回：
      {
        "skip_frames":     参考视频要跳的帧数（= C_n，整数帧）,
        "frame_cap":       参考视频要取的帧数（= 本段交付帧数）,
        "audio_start":     音频起点（秒）= C_n / 24,
        "audio_duration":  音频时长（秒）= 本段交付时长,
        "segment_audio":   采样用段音频文件名（"1s 空白 ⊕ 切片"）,
        "active":          本段是否真的要切片,
      }

    【口径 —— 全部来自整数帧】
    C_n = 上游已累计交付的帧数（口径 B）。
    t_a_start(n) = C_n / 24 秒，视频侧跳 C_n 帧。
    两者严格对应，不存在浮点累计误差。

    【什么时候不切片】
      * 链首段（没有上游）-> 第 1 段用原始素材从 0 开始。
      * 音轨来源是「生成的音乐」-> 音画同源，不切。
      * 没有上游的音频偏移信息 -> 不切（保持原样，绝不猜）。
    """
    info = cont_service.chain_info(params)
    out = {"skip_frames": 0, "frame_cap": 0, "audio_start": 0.0,
           "audio_duration": 0.0, "segment_audio": "", "active": False}
    if not info:
        return out
    if not str(info.get("source_job") or "").strip():
        return out  # 链首段

    track_mode = str(info.get("audio_track_mode") or "").strip()
    external = h3acont.is_external_track(track_mode)

    skip_frames = int(info.get("audio_segment_skip_frames") or 0)
    audio_start = float(info.get("audio_segment_start") or 0.0)
    audio_duration = float(info.get("audio_segment_duration") or 0.0)
    segment_audio = str(params.get("segment_audio_file") or "").strip()

    if not external:
        return out  # 生成音频：不切

    out.update({
        "skip_frames": max(0, skip_frames),
        "frame_cap": max(0, int(round(audio_duration * cont.FPS))),
        "audio_start": max(0.0, audio_start),
        "audio_duration": max(0.0, audio_duration),
        "segment_audio": segment_audio,
        "active": True,
    })
    return out


def _build_common(p: dict, params: dict, *, condition_inputs: dict,
                  duration: float, width: int, height: int,
                  unet_name: str, task_type: str) -> dict:
    """首尾帧 / 参考生视频共用的骨架。

    返回工作流 dict。
    """
    job_id = params["job_id"]
    seed = params.get("seed") or random.randint(0, 2**48)

    # ---- 底模（参考生视频用 ref2va 权重；首尾帧用 fl2va）----
    model = {"unet": unet_name}
    model_ref = _model_chain(p, params, model)

    # ---- 条件节点（T8：同时管首尾帧 / 参考素材 / 驱动音频）----
    p["105:11"] = {"inputs": {"vae_name": VAE_VIDEO}, "class_type": "VAELoader",
                   "_meta": {"title": "视频 VAE"}}
    p["13"] = {"inputs": {"clip_name": CLIP_H3, "type": "minimax", "device": "default"},
               "class_type": "CLIPLoader", "_meta": {"title": "加载文本编码器"}}
    p["24"] = {"inputs": {"vae_name": VAE_AUDIO}, "class_type": "VAELoader",
               "_meta": {"title": "音频 VAE"}}

    expr = _frame_expr_node("105:107", "105:111", duration)
    p.update(expr)

    cond = {
        "clip": ["13", 0],
        "video_vae": ["105:11", 0],
        "audio_vae": ["24", 0],
        "prompt": params.get("prompt", ""),
        "width": width,
        "height": height,
        "length": ["105:107", 1],
        "task_type": task_type,
        # 音画同源生成（默认档）。用户若要「用参考音乐当音轨」，
        # 那条音轨是**贴上去**的，不进 DiT 采样。
        "audio_mode": "native",
        "ref_image_size": "match",
        # 【以下四项必须显式给】T8 节点的 required 输入 —— 即使有默认值，
        # 通过 API 提交工作流时**不会**套用 schema 默认值（ComfyUI 只对
        # 提交 payload 里实际存在的键做校验/取值）。
        # 漏掉任何一个 -> 提交被拒（"Required input is missing"）。
        "audio_denoise_strength": 0.35,
        "add_source_as_reference": True,
        "prompt_primary_audio_ordinal": 1,
        "strict_prompt_tags": True,
        "reference_video_policy": "official_2_to_15s",
    }
    cond.update(condition_inputs)
    p["136"] = {"inputs": cond, "class_type": T8_CONDITIONING,
                "_meta": {"title": "H3 条件（首尾帧 / 参考 / 音轨）"}}

    # ---- 采样 ----
    accel_chain = vworkflow._resolve_accel_chain(params, model)
    steps, scheduler, sampler = _sampling_defaults(params, accel_chain)

    # shift（MiniMaxH3SigmaShift）
    model_ref = _shift_node(p, model_ref, params)

    # 【2026-11-15 修复③ 交付项 3】二采（SelfLift）——
    # 三个视频模式共用同一套面板，面板上的「二采」开关必须**真的生效**。
    #
    # 【为什么原来没有】build_flf / build_r2v 走的是 T8 条件节点
    # （136），而 SelfLiftH3Sampler 的输入里写死了文生视频的条件节点
    # id（105:104）—— 直接接会引用一个不存在的节点。
    # 现在把条件节点 id 参数化传给 _twopass_block（见 video_workflow.py）。
    twopass = params.get("twopass") or {}
    # 【2026-11-18 接续段默认走标准采样 —— 与 t2v 完全同款】
    # 【为什么要在这里也兜一层】服务端归一层（server.normalize_t2v_params）
    # 已经把接续段的二采关掉；这里是**第二道防线**，保证无论谁来调
    # build_flf / build_r2v，接续段产出的必须是标准 SamplerCustomAdvanced
    # 等价节点（nonebot minimax_h3_seg2 的姿势），而不是 SelfLift。
    # 【实测依据】二采 ON 的接续链段头断点比 2.58x / 4.54x、
    # 3 帧亮度降 +4.7 / +7.2 灰阶；OFF 的 11 条全部 ≤1.58x / ≤+0.26。
    # 【为什么调统一入口】与 video_workflow / server 归一层**同一个函数**，
    # 避免两层口径分裂（显式放行口子失效 / 接续段走回 SelfLift）。
    use_twopass, _tp_disposition = twopass_policy.resolve_effective_twopass(params)
    if not use_twopass:
        twopass = {}

    p["9"] = {"inputs": {"scheduler": scheduler, "steps": steps, "denoise": 1,
                          "model": model_ref},
              "class_type": "BasicScheduler", "_meta": {"title": "调度器"}}

    # ---- 接续（continuation）----
    #
    # 【与 t2v 完全同款的接法】见 video_workflow.build_t2v_panel：
    #   读回上游潜空间 -> 拷贝桥 -> 采样 -> 落盘本段接力棒
    #   -> decode 后裁钉住区（音画同裁）。
    # 差别只有一处：条件节点 id（t2v 是 105:104，这里是 136）。
    #
    # 【为什么必须在这里接，而不是只放开 mode 白名单】
    # flf/r2v 以前**不落盘**潜空间。只把模式加进 CONTINUABLE_MODES
    # 会让「点接续 → 生成」静默退化成普通生成（看起来成功，其实
    # 没接上）—— 最坏的一种失败。现在：
    #   * 本段**落盘**接力棒（300:save，输出节点）-> 下一段能读；
    #   * 续接段**读回**上游（300:load + explicit_path）-> 真的接上。
    cont_state = vworkflow._continuation_state(params)
    latent_in_ref = ["136", 1]
    cond_ref = ["136", 0]
    trim_ref = None

    # 【2026-11-17 接续】二采（selflift）现在**也**走接续链路。
    # 原来这里是 `not use_twopass` —— 二采路径下整个接续分支被跳过
    # （不读回、不接桥），二采段只能产出接力棒、不能消费接力棒。
    # 放开的依据（独立复核，2026-11-17）：
    #   (a) 二采落盘的 latent 时间步与 17k+5 网格天然对齐
    #       （4/4 真实落盘样本吻合，tail_aligned 全 True）；
    #   (b) SelfLiftH3Sampler 原生读 latent 的 noise_mask
    #       （nodes.py:116-155），且 masked_model() 对**低分辨率段与
    #       高分辨率段都**装 post-CFG 钉住钩子（nodes.py:280-297）。
    #   (c) 接线姿势照 nonebot minimax_h3_v1.json 的 171 节点：
    #       SelfLift 消费**拷贝桥的 latent**（第 0 路）+
    #       **桥第 4 路 conditioning**。
    if cont_state["enabled"] and cont_state["has_source"]:
        p["300:load"] = {
            "inputs": {
                "run_id": cont_state["source_run_id"],
                "stage_index": cont_state["source_stage"] + 1,
                # 精确读上游那一份（每段一个目录 -> 不能按链段号推）。
                "explicit_path": cont_state["source_path"],
            },
            "class_type": "H3RelayLatentLoad",
            "_meta": {"title": "接续·读回上游潜空间"},
        }
        # 外观锚：链首段那份（防长程漂移）。找不到就不接。
        anchor_latent_ref = None
        if cont_state["anchor_path"]:
            p["300:anchor_load"] = {
                "inputs": {
                    "run_id": cont_state["anchor_run_id"],
                    "stage_index": cont_state["anchor_stage"] + 1,
                    "explicit_path": cont_state["anchor_path"],
                },
                "class_type": "H3RelayLatentLoad",
                "_meta": {"title": "接续·读回链首段潜空间（外观锚）"},
            }
            anchor_latent_ref = ["300:anchor_load", 0]
        p["300:bridge"] = {
            "inputs": {
                # 本段的初始 AV latent 来自 T8 条件节点（136 的第 2 路）。
                "latent": latent_in_ref,
                "context_frames": cont_state["context_frames"],
                "context_latent": ["300:load", 0],
                # hard = 钉住区零重绘（真续接唯一推荐档）。
                "mask_mode": "hard",
                "pin_audio": True,
                "stage_index": cont_state["source_stage"],
                "run_id": cont_state["source_run_id"],
                "ref_anchor_latent": anchor_latent_ref,
                "ref_anchor_stage": -1,
                "ref_anchor_frames": cont_state["anchor_frames"],
                # 第 4 路：conditioning 钉帧（管取景/构图）。
                "conditioning": cond_ref,
            },
            "class_type": "H3RelayCopyBridge",
            "_meta": {"title": "接续·拷贝桥（零重编码潜空间桥）"},
        }
        latent_in_ref = ["300:bridge", 0]
        cond_ref = ["300:bridge", 3]
        trim_ref = ["300:bridge", 2]

    if use_twopass:
        # 二采固定用 Euler（SelfLift 只支持标准 Euler）
        p["17"] = {"inputs": {"sampler_name": "euler"},
                   "class_type": "KSamplerSelect",
                   "_meta": {"title": "采样器（二采固定 Euler）"}}
        cfg_value = params.get("cfg")
        if cfg_value is None:
            cfg_value = vmodels.VIDEO_CFG_FIXED
        samples_ref = vworkflow._twopass_block(
            p, model_ref, steps, scheduler, sampler, seed,
            float(cfg_value), twopass, width, height, duration,
            latent_in_ref, cond_node_id="136",
            # 【2026-11-17 接续】接续段 positive 走**拷贝桥第 4 路**；
            # 非接续段 cond_ref 还是 ["136", 0]（条件节点直接出）。
            # 与 nonebot minimax_h3_v1.json 的 171 节点同姿势。
            positive_ref=cond_ref)
    else:
        p["17"] = {"inputs": {"sampler_name": sampler}, "class_type": "KSamplerSelect",
                   "_meta": {"title": "采样器"}}
        p["15"] = {"inputs": {"noise_seed": int(seed)}, "class_type": "RandomNoise",
                   "_meta": {"title": "噪波"}}
        p["16"] = {"inputs": {"model": model_ref, "conditioning": cond_ref},
                   "class_type": "BasicGuider", "_meta": {"title": "引导器"}}
        p["14"] = {"inputs": {"noise": ["15", 0], "guider": ["16", 0],
                               "sampler": ["17", 0], "sigmas": ["9", 0],
                               "latent_image": latent_in_ref},
                   "class_type": "SamplerCustomAdvanced", "_meta": {"title": "采样"}}
        samples_ref = ["14", 0]

    # ---- 解码 + 合成 ----
    p["10"] = {"inputs": {"samples": samples_ref, "vae": ["105:11", 0]},
               "class_type": "VAEDecode", "_meta": {"title": "解码视频"}}
    p["23"] = {"inputs": {"samples": samples_ref, "vae": ["24", 0]},
               "class_type": "VAEDecodeAudio", "_meta": {"title": "解码音频"}}

    # ---- 裁重叠：去掉钉住区（音画同裁，保住 A/V 同步）----
    if trim_ref is not None:
        p["300:trim"] = {
            "inputs": {
                "images": ["10", 0],
                "audio": ["23", 0],
                "trim_frames": trim_ref,
                "fps": 24,
                "settle_frames": 0,
                "seam_ghost": 0,
            },
            "class_type": "H3RelayTrimAV",
            "_meta": {"title": "接续·裁重叠（裁掉钉住区，音画同裁）"},
        }
        # ---- 画质域：跨段统计匹配（2026-11-17 修：接续点闪烁）----
        #
        # 【为什么需要】用户实测反馈「接续点有一次明显闪烁」。
        # 取证结论（线上真实产物，帧级证据）：
        #   · 钉住区**逐位保住**（段2 钉住区 vs 段1 尾 7 步：max 差
        #     4.8e-7 = float32 舍入）→ 拷贝桥没问题；
        #   · 但接续点的**色档跳变**：二采 ON 时是段内量级的 29.52x，
        #     同参数二采 OFF 时只有 5.48x；
        #   · 机理：SelfLift 的低分辨率段把钉住区内容**降采样**后当锚点
        #     （nodes.py:299-306），抬升再回高分辨率 -> 新生成区继承了
        #     低分辨率段的色档，缝头几帧是「低色档过渡区」。
        #
        # 【为什么用 H3RelayPost.match_prev】节点 tooltip 明写
        #   「目标：压『成片缝上的亮度阶跃』」+「真正的用武之地是**拷贝桥**
        #   （那里 prev_tail 才是真·上段末帧）」—— 我们**就是**拷贝桥路径。
        #   逐通道 Reinhard 一阶+二阶矩对齐，**帧数守恒、不动音频、零采样开销**。
        #
        # 【guide】300:trim 第 4 路 prev_tail = 钉住区末帧（拷贝桥下即上段末帧）。
        # 【stats_frames=1】节点 tooltip：1 = 与单帧 guide 同口径 ⇒ 首帧被拉向
        #   guide 且**不会越过它**；0 = 旧口径 ⇒ 首帧被推过 guide、缝上多出
        #   阶跃（真渲染实测 ×12.6）→ 产线必须给 1。
        p["300:post"] = {
            "inputs": {
                "images": ["300:trim", 0],
                "guide": ["300:trim", 3],
                "match_prev": 0.5,
                "match_prev_frames": 12,
                "match_prev_gain_max": 1.15,
                "match_prev_offset_max": 0.06,
                "match_prev_stats_frames": 1,
                # 【跨段两项的总闸】确认 guide 是**真参照**才打勾。
                # 节点 tooltip：「不打勾（默认）⇒ match_prev 与 lowfreq_pull
                # **自动弃权**」+「**真参照只在拷贝桥下成立**」。
                # 我们的路径**就是拷贝桥**（300:bridge → 300:trim），
                # prev_tail 就是上段末帧 → 必须打勾。
                # （2026-11-17 实测踩到：不打勾 -> match_prev 完全没生效，
                #   ComfyUI 日志会写「自动弃权 —— guide 未确认是真参照」）
                "cross_seg_ack": True,
                "lowfreq_pull": 0,
                "hist_match": 0,
                "wb_match": 0,
                "settle_sharpen": 0,
                "settle_auto": 0,
            },
            "class_type": "H3RelayPost",
            "_meta": {"title": "接续·跨段统计匹配（压缝上的亮度阶跃）"},
        }
        video_images_ref = ["300:post", 0]
        video_audio_ref = ["300:trim", 1]
    else:
        video_images_ref = ["10", 0]
        video_audio_ref = ["23", 0]

    # ---- 落盘接力棒 ----
    # 采样器的输出落盘，供下一段读回。**链首段也要落盘** —— 否则
    # 「点接续」永远没有接力棒可读（这正是 flf/r2v 以前不能接续的
    # 根因：它们的工作流里根本没有这个节点）。
    if cont_state["enabled"]:
        p["300:save"] = {
            "inputs": {
                "latent": samples_ref,
                "run_id": cont_state["own_run_id"],
                "stage_index": 0,
                "note": f"h3studio job {job_id}",
            },
            "class_type": "H3RelayLatentSave",
            "_meta": {"title": "接续·落盘本段潜空间（下一段的接力棒）"},
        }

    # ---- 音轨三选一 ----
    #
    # 【接续段的口径】
    #  * generated —— H3 采样生成的音频（音画同源），直接用；
    #  * reference / video_bg —— 外部贴上去的音轨。本段采样时用
    #    「1s 空白 ⊕ 原始音频切片」（由服务端切好，见 server.py 的
    #    音频接续时序），成片音轨用「原始参考音频整条」。
    #    详见 h3studio/h3_audio_continuation.py。
    audio_ref = _pick_audio_track(p, params, duration,
                                   video_images_ref=video_images_ref,
                                   video_audio_ref=video_audio_ref)

    p["91"] = {"inputs": {"fps": 24, "bit_depth": 8, "images": video_images_ref,
                          "audio": audio_ref},
               "class_type": "CreateVideo", "_meta": {"title": "合成视频"}}
    p["92"] = {"inputs": {"filename_prefix": f"h3studio/{params.get('mode', 'video')}_{job_id}",
                           "format": "auto", "codec": "auto", "video": ["91", 0]},
               "class_type": "SaveVideo", "_meta": {"title": "保存视频"}}
    return p


def _shift_node(p: dict, model_ref: list, params: dict) -> list:
    """插入 MiniMaxH3SigmaShift（shift 视频/音频）。"""
    if not params.get("use_sigma_shift", True):
        return model_ref
    shift_video = vmodels.normalize_shift(
        params.get("shift_video"), vmodels.VIDEO_SHIFT_DEFAULT_VIDEO)
    shift_audio = vmodels.normalize_shift(
        params.get("shift_audio"), vmodels.VIDEO_SHIFT_DEFAULT_AUDIO)
    p["139"] = {"inputs": {"model": model_ref, "shift_video": shift_video,
                           "shift_audio": shift_audio},
                "class_type": "MiniMaxH3SigmaShift",
                "_meta": {"title": "sigma shift（视频/音频）"}}
    return ["139", 0]


def _pick_audio_track(p: dict, params: dict, duration: float,
                      video_images_ref: list | None = None,
                      video_audio_ref: list | None = None) -> list:
    """按「音轨三选一」选出成片该贴哪条音轨，返回 CreateVideo 的 audio 引用。

    (a) generated —— 用 H3 生成的音频（VAEDecodeAudio 的输出）。
    (b) reference —— 用参考音乐（裁到画面时长）。
    (c) video_bg  —— 用参考视频的背景音乐（裁到画面时长）。

    【为什么 reference / video_bg 要裁时长】
    外部音轨长度与画面**不一定相等**。直接接上去，CreateVideo 会按
    音轨长度截断画面（或补静音）—— 用户会看到「视频短了一截」。
    所以这里用 TrimAudioDuration 显式裁到画面时长。

    【接续段的特殊处理 —— 音轨来源为「参考音频」时】
    接续段里，**采样用**的音轨不是原始音频，而是服务端切好的
    「1s 空白 ⊕ 原始音频从第 N 秒起的切片」（见
    h3studio/h3_audio_continuation.py 与 server.py 的音频接续时序）。
    所以：
      * 段音频文件（params["segment_audio_file"]）直接接进 ref_audios
        槽位（**不再**从原始音频 Trim）；
      * 成片音轨仍用**原始参考音频整条**（合并阶段贴，见 server.py），
        绝不把 1s 空白累加进成片。
    """
    ref_audios = params.get("ref_audios") or []
    ref_video_audios = params.get("ref_video_audios") or []
    source = h3_audio_track.normalize_track_source(
        params.get("audio_track_source"),
        has_reference_audio=bool(ref_audios),
        has_reference_video_audio=bool(ref_video_audios))

    # ---- 接续段：段音频（1s 空白 ⊕ 切片）已经是**采样用**的那条 ----
    #
    # 【为什么不走 TrimAudioDuration】段音频文件的长度已经是
    # 「1s 空白 + 本段交付时长」—— 它要**原样**进 DiT 采样。
    # 再 Trim 一次会把 1s 空白裁掉（正好裁掉），语义就错了。
    #
    # 【成片音轨怎么办】成片音轨在**合并阶段**贴「原始参考音频整条」
    # （server._concat_chain_video），不在这里 —— 这里贴的这条只
    # 影响**本段视频文件**（段视频的音轨 = 该段该听到的内容）。
    segment_audio = str(params.get("segment_audio_file") or "").strip()
    if segment_audio and source in (h3_audio_track.TRACK_REFERENCE,
                                    h3_audio_track.TRACK_VIDEO_BG):
        # 段音频替换掉「原始音频」这一路的输入：
        #  * reference  -> 用段音频（它已经是「1s 空白 ⊕ 切片」）；
        #  * video_bg   -> 参考视频的音轨也要做音画同步切割 ->
        #                  服务端把切好的那段放进 segment_audio_file。
        return _connect_segment_audio(p, params, segment_audio, source)

    if source == h3_audio_track.TRACK_REFERENCE and ref_audios:
        node_id = "150:track"
        p[node_id] = {
            "inputs": {
                "audio": ["136:401", 0],
                "start_index": 0.0,
                "duration": float(duration),
            },
            "class_type": "TrimAudioDuration",
            "_meta": {"title": "音轨·参考音乐（裁到画面时长）"},
        }
        return [node_id, 0]

    if source == h3_audio_track.TRACK_VIDEO_BG and ref_video_audios:
        node_id = "150:track"
        p[node_id] = {
            "inputs": {
                "audio": ["136:501", 0],
                "start_index": 0.0,
                "duration": float(duration),
            },
            "class_type": "TrimAudioDuration",
            "_meta": {"title": "音轨·参考视频背景音（裁到画面时长）"},
        }
        return [node_id, 0]

    # (a) 生成的音乐 —— H3 与画面一同采样出来的那条
    # 接续段里，裁重叠节点（300:trim）已经把音频裁过了 —— 必须用
    # 裁完的那条，否则音画会错位（画面裁了 22 帧、音频没裁）。
    if video_audio_ref is not None:
        return video_audio_ref
    return ["23", 0]


def _connect_segment_audio(p: dict, params: dict, segment_audio: str,
                           source: str) -> list:
    """把「段音频」接进成片音轨。

    【两种来源的接法不同】
      * reference（独立参考音频）-> 段音频**替换** ref_audios 那一路。
        但 ref_audios 的槽位在 T8 条件节点（136）里已经接好了
        （136:401 是第 1 段参考音乐的 LoadAudioUpload）—— 所以这里
        改接 136:401 的**输入文件**，而不是另开一路。
      * video_bg（参考视频的音轨）-> 段音频替换 ref_video_audios
        那一路（136:501）。
    """
    # 段音频是 ComfyUI input 里的文件名 -> 直接替换对应槽位的输入。
    # 注意：**不能**在这里新建 LoadAudioUpload 节点 —— T8 节点的
    # ref_audios / ref_video_audios 槽位是 autogrow 的，键名必须
    # 保持 "ref_audios.ref_audio_0" 这种形态。
    if source == h3_audio_track.TRACK_VIDEO_BG:
        target = "136:501"
    else:
        target = "136:401"
    node = p.get(target)
    if not isinstance(node, dict):
        # 没有这一路（比如用户没给参考音频）-> 回落到生成的音乐。
        return ["23", 0]
    node.setdefault("inputs", {})["audio"] = segment_audio
    node.setdefault("_meta", {})["title"] = (
        f"加载{'参考视频背景音' if source == h3_audio_track.TRACK_VIDEO_BG else '参考音乐'}"
        f"（接续段·已切片）")
    return [target, 0]


def build_flf(params: dict) -> dict:
    """首尾帧生视频（首帧 / 尾帧 / 首帧+尾帧）。

    【与旧实现的关键差别】
      * 尾帧**可以单独给**（旧实现要求首帧必填，尾帧可省 —— 这条一致，
        但现在两帧都可以从素材库选，且都不传时给出明确报错）。
      * 支持**音轨三选一**（旧实现没有）。
      * 用 T8 条件节点（支持音频输入）。

    params:
      first_frame / last_frame —— 首帧 / 尾帧的 ComfyUI input 文件名
      audio_track_source      —— 音轨三选一（见 h3_audio_track）
      ref_audios              —— 参考音乐（文件名列表）
      ref_video_audios        —— 参考视频背景音（文件名列表）
    """
    from . import video_models as _vm
    first = params.get("first_frame")
    last = params.get("last_frame")
    if not first and not last:
        raise H3VideoRequestError(
            "首尾帧模式需要至少提供首帧或尾帧图片。"
            "请从素材库选择首帧 / 尾帧，或直接上传图片。")

    # 首尾帧用 FL2VA 权重（它有 I2VA / FL2VA / L2VA 任务类型）
    unet_name = _vm.model_for_mode("flf", params.get("model_key"))["unet"]

    # 【2026-11-15】宽高走**统一归一**（h3studio/h3_video_size.py）：
    # 面板选了「544p · 9:16」就必须真的跑那个尺寸，
    # 而不是硬回退 1344×768（那个回退是「面板说谎」的根因）。
    from . import h3_video_size as _vsize
    width, height = _vsize.dimensions_for(
        params.get("resolution"), params.get("aspect"), mode="flf")
    explicit_w = params.get("width")
    explicit_h = params.get("height")
    if explicit_w and explicit_h:
        width, height = int(explicit_w), int(explicit_h)
    duration = float(params.get("duration") or 5)

    p: dict = {}
    condition_inputs: dict = {}
    if first:
        p["105:201"] = {"inputs": {"image": first}, "class_type": "LoadImage",
                        "_meta": {"title": "加载首帧"}}
        condition_inputs["first_frame"] = ["105:201", 0]
    if last:
        p["105:202"] = {"inputs": {"image": last}, "class_type": "LoadImage",
                        "_meta": {"title": "加载尾帧"}}
        condition_inputs["last_frame"] = ["105:202", 0]

    # 参考视频 / 参考音乐 / 参考视频背景音（音轨三选一的素材）
    #
    # 【2026-11-15 修 —— 线上实测暴露的真实缺陷】
    # 原来 build_flf 只接了 ref_audios 与 ref_video_audios，
    # **没有接 ref_videos** —— 于是 flf 模式下选了参考视频，
    # 服务端把它的音轨抽出来贴上去（ref_video_audios），
    # 但 T8 节点的 ref_videos 槽位是空的。
    # 节点随即抛：
    #   ValueError: Reference-video soundtrack(s) have no same-numbered video: 0
    # （节点硬要求「第 N 个背景音必须配第 N 个参考视频」）
    #
    # build_r2v 一直是接的（见下面 build_r2v 里的 ref_videos 分支），
    # flf 漏了 —— 因为 flf 之前根本没有参考视频的入口，
    # 漏了也没人发现。这次把面板入口补上，缺陷就浮出来了。
    ref_videos = list(params.get("ref_videos") or [])[:MAX_REF_VIDEOS]
    ref_audios = list(params.get("ref_audios") or [])[:MAX_REF_AUDIOS]
    ref_video_audios = list(params.get("ref_video_audios") or [])[:MAX_REF_VIDEOS]
    # 【2026-11-16 接续】音画同步切割参数（参考视频跳帧 / 音频偏移）。
    slice_info = _continuation_media_slice(params)
    if ref_videos:
        condition_inputs.update(_ref_video_nodes(
            p, ref_videos,
            skip_frames=slice_info["skip_frames"],
            frame_cap=slice_info["frame_cap"]))
    if ref_audios:
        condition_inputs.update(_ref_audio_nodes(p, ref_audios))
    if ref_video_audios:
        condition_inputs.update(_ref_video_audio_nodes(
            p, ref_video_audios,
            start_seconds=slice_info["audio_start"],
            duration=slice_info["audio_duration"]))

    # 【2026-11-15 修 —— 线上实测暴露的第二处真实缺陷】
    #
    # T8 节点的 resolve_task_type() 有硬规则：
    #   requirements = {
    #     "t2va":  (无首帧, 无尾帧, 不带参考),
    #     "i2va":  (有首帧, 无尾帧, 不带参考),
    #     "fl2va": (有首帧, 有尾帧, 不带参考),
    #     "l2va":  (无首帧, 有尾帧, 不带参考),
    #     "ref2va":(无首帧, 无尾帧, 必须带参考),
    #     "hybrid":(首尾随意, 必须带参考),
    #   }
    # 并且明确：
    #   if requested not in {"ref2va", "hybrid"} and has_refs:
    #       raise ValueError(f"{requested.upper()} cannot include reference
    #                         media; use Auto or Hybrid")
    #
    # 也就是说：**只要带了参考素材，任务类型必须是 hybrid（或 ref2va）**。
    # 原来 build_flf 恒定按「有首尾帧 -> FL2VA / 只有首帧 -> I2VA /
    # 只有尾帧 -> L2VA」来定类型，于是 flf + 参考视频 时报：
    #   ValueError: L2VA cannot include reference media; use Auto or Hybrid
    #
    # 修法：带参考素材时用 HYBRID（首尾帧 + 参考素材共存正是 hybrid 的定义）。
    has_refs = bool(ref_videos or ref_audios or ref_video_audios)
    if has_refs:
        # HYBRID 要求至少给一张首尾帧（build_flf 开头已经校验过）
        #
        # 【大小写】节点 combo 的选项是
        #   ["auto", "T2VA", "I2VA", "FL2VA", "L2VA", "Ref2VA", "Hybrid"]
        # 必须严格用 "Hybrid"（和 build_r2v 用 "Ref2VA" 一样），
        # 写成 "HYBRID" 会被 ComfyUI 的 combo 校验拒掉。
        task_type = "Hybrid"
    else:
        task_type = ("FL2VA" if (first and last)
                     else ("I2VA" if first else "L2VA"))
    p.update(_build_common(p, params, condition_inputs=condition_inputs,
                            duration=duration, width=width, height=height,
                            unet_name=unet_name, task_type=task_type))
    return p


def build_r2v(params: dict) -> dict:
    """参考生视频：最多 9 张参考图 + N 个参考视频 + 1 段参考音乐 + 音轨三选一。

    params:
      ref_images        —— 参考图文件名列表（≤9）
      ref_videos        —— 参考视频文件名列表（≤3）
      ref_audios        —— 独立参考音乐文件名列表（≤3）
      ref_video_audios  —— 参考视频背景音文件名列表（≤3）
      audio_track_source —— 音轨三选一
    """
    from . import video_models as _vm
    ref_images = list(params.get("ref_images") or [])[:MAX_REF_IMAGES]
    ref_videos = list(params.get("ref_videos") or [])[:MAX_REF_VIDEOS]
    ref_audios = list(params.get("ref_audios") or [])[:MAX_REF_AUDIOS]
    ref_video_audios = list(params.get("ref_video_audios") or [])[:MAX_REF_VIDEOS]

    if not (ref_images or ref_videos or ref_audios or ref_video_audios):
        raise H3VideoRequestError(
            "参考生视频需要至少 1 个参考素材（图片 / 视频 / 音乐）。"
            "请从素材库选择参考图（最多 9 张）、参考视频（最多 3 个）"
            "或参考音乐（最多 3 段）。")

    # 参考生视频必须用 **Ref2VA 权重**（它才有 Ref2VA 通道）——
    # FL2VA 权重加载后参考通道不可用，等于「参考没生效」。
    # model_for_mode() 会按模式挑（用户选了 FL2VA 权重时自动换 Ref2VA）。
    unet_name = _vm.model_for_mode("r2v", params.get("model_key"))["unet"]

    # 【2026-11-15】宽高走**统一归一**（h3studio/h3_video_size.py）：
    # 面板选了「544p · 9:16」就必须真的跑那个尺寸，
    # 而不是硬回退 1344×768（那个回退是「面板说谎」的根因）。
    from . import h3_video_size as _vsize
    width, height = _vsize.dimensions_for(
        params.get("resolution"), params.get("aspect"), mode="r2v")
    explicit_w = params.get("width")
    explicit_h = params.get("height")
    if explicit_w and explicit_h:
        width, height = int(explicit_w), int(explicit_h)
    duration = float(params.get("duration") or 5)

    p: dict = {}
    condition_inputs: dict = {}
    # 【2026-11-16 接续】音画同步切割参数（参考视频跳帧 / 音频偏移）。
    slice_info = _continuation_media_slice(params)
    if ref_images:
        condition_inputs.update(_ref_image_nodes(p, ref_images))
    if ref_videos:
        condition_inputs.update(_ref_video_nodes(
            p, ref_videos,
            skip_frames=slice_info["skip_frames"],
            frame_cap=slice_info["frame_cap"]))
    if ref_audios:
        condition_inputs.update(_ref_audio_nodes(p, ref_audios))
    if ref_video_audios:
        condition_inputs.update(_ref_video_audio_nodes(
            p, ref_video_audios,
            start_seconds=slice_info["audio_start"],
            duration=slice_info["audio_duration"]))

    p.update(_build_common(p, params, condition_inputs=condition_inputs,
                            duration=duration, width=width, height=height,
                            unet_name=unet_name, task_type="Ref2VA"))
    return p
