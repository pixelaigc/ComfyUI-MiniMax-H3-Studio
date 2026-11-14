"""产物实际生成元数据的**反解与回写**（2026-11-15 媒体库卡片 LoRA/步数修复）。

【为什么要有这个模块】
用户报出两个缺陷：

  BUG 1  媒体库卡片上的 LoRA 名称与实际生成产物**根本不是同一个**
          （存在「根本就没这个 LoRA」的误显示）。
  BUG 2  卡片不显示步数；也没有地方能看完整生成参数。

【根因 —— 一句话】
`jobs.params` 存的是**任务提交参数**，不是**产物实际使用的参数**。
两者会脱钩，因为：

  1. **默认值在「构建工作流」时才生效**。
     `workflows._h3_video_workflow` / `video_workflow.build_t2v_panel`
     里的规则是「params 没给 steps -> 按**实际挂了哪枚加速 LoRA** 回落
     （有 -> 该 LoRA 推荐档 4 步 / beta / euler；无 -> 20 步 / simple /
     res_multistep）」。
     这意味着 **步数是 LoRA 的函数**。
     但落库的 params 既没有 LoRA 记录、也没有 steps 记录 ->
     显示层只能靠猜 -> 猜错。

  2. **展示层猜的默认值与工作流的默认值不是同一套**。
     `job_meta.resolve_job_lora` 对「params 里没有 LoRA 记录」的旧视频路径
     **硬编码回落到 `ckpt850`**（那是工作流里写死的一枚）。
     于是：一条**实际挂了别的 LoRA（或没挂）** 的任务，卡片却恒显
     `Turbo 4 步 · ckpt850` —— 这就是用户看到的「根本就没这个 LoRA」。
     同理 steps/sampler/scheduler 在 params 里缺失 -> `gen_params`
     返回空串 -> 卡片与详情模态框只能显示「—」。

  3. **有些提交路径根本绕过了参数归一**。
     `prepare_job_params` / `normalize_t2v_params` 只在
     `POST /api/jobs` 的面板路径上跑。潜空间接续（continuation）、
     内部脚本、Agent 工具链各自落库时，params 可能只有
     `{prompt, duration, gen_type, continuation}` —— 连 model_key 都没有。

【本模块的解法 —— 以「工作流」为真源】
ComfyUI 实际执行的那份 workflow 才是**产物实际元数据**的唯一权威：
它里面有真正生效的 `BasicScheduler(steps/scheduler)`、
`KSamplerSelect(sampler_name)`、`LoraLoader*(lora_name/strength_model)`、
`UNETLoader(unet_name)`、`LatentEmptyTensor`/`Empty*LatentImage(width/height)`、
`KSampler(seed)` 等节点。

所以：

  * `extract_from_workflow(workflow)` —— 从**构建好的 workflow**
    反解出真实生成参数（构建器已经把所有默认值都算完了，
    所以这里拿到的是「真正会跑」的那一份）。
  * `apply_to_params(params, extracted)` —— 把真实值回写进 params，
    让 params **自己不再说谎**。

【为什么回写 params 而不是只在显示层改】
显示层每次读都要重新推导一遍，而推导规则必然又会漂移。
回写让「落库的参数 == 实际跑的参数」，显示层只读就行 ——
和 `job_meta.gen_params` 的纪律一致（服务端算好下发，前端只渲染）。

【缺失 = 缺失】
反解不到就是反解不到（返回空串 / None）。
**不猜、不顶替、不编** —— 这是本仓库的硬要求。
"""
from __future__ import annotations

import re

#: 反解结果里「LoRA 权重文件名 -> 显示名」的兜底清洗。
#: 只做**保守**清洗（去目录前缀、去扩展名），不做任何猜测性映射。
_EXT_RE = re.compile(r"\.(safetensors|ckpt|pt|bin)$", re.IGNORECASE)


def _clean_weight_name(raw: str) -> str:
    """把 LoRA 权重文件名保守地清洗成可读名。

    例：`h3_lightx2v/minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors`
      -> `minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16`

    【不做什么】不做大小写变换、不做驼峰拆分、不替换任何词 ——
    任何「美化」都是编造。
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    # 目录前缀（ComfyUI 的 LoRA 相对路径）
    if "/" in text or "\\" in text:
        text = re.split(r"[/\\]", text)[-1]
    text = _EXT_RE.sub("", text)
    return text.strip()


def lora_key_for_weight(raw_name: str) -> str:
    """权重文件名 -> 本机的加速 LoRA key（认不出来就返回 ""）。

    【为什么需要】
    产物实际挂的 LoRA 在 workflow 里只有**权重文件名**
    （`lora_name`），而面板下拉里是**友好 key**（`ckpt850` /
    `lightx2v_4step_768p`）。用户看到的应该是友好名，但**必须**是
    这枚 LoRA 的友好名 —— 不能张冠李戴。

    做法：拿权重文件名去本机清单里**反查**（清单是权威）。
    查不到 -> 返回 ""（显示层用清洗后的权重名，如实显示，不编）。

    【为什么必须反查而不是直接显示权重名】
    权重名 `minimax_h3_turbo_4step_ema_ckpt850.safetensors` 对用户
    来说是一串噪音；反查成 `ckpt850` 才能显示成
    「Turbo 4 步 · ckpt850」，而且**保证**是同一枚。
    """
    text = str(raw_name or "").strip()
    if not text:
        return ""
    try:
        from . import workflows as wf
        for key, filename in (getattr(wf, "LORAS", {}) or {}).items():
            if filename and filename == text:
                return key
    except Exception:
        pass
    try:
        from . import video_models as vmodels
        for table in (getattr(vmodels, "VIDEO_ACCEL_LORAS", {}) or {},
                      getattr(vmodels, "VIDEO_ACCEL_LORA_COMBOS", {}) or {}):
            for key, meta in table.items():
                if not isinstance(meta, dict):
                    continue
                name = str(meta.get("name") or "")
                if name and name == text:
                    return key
    except Exception:
        pass
    return ""


#: 会挂 LoRA 的节点 class_type 前缀集合。
#:
#: 【为什么按 class_type 前缀而不是白名单精确匹配】
#: ComfyUI 生态里挂 LoRA 的节点有很多种（内置的 LoraLoader /
#: LoraLoaderModelOnly、H3 插件的 MiniMaxH3TurboLoRA、
#: 各种第三方节点）。维护一份精确白名单必然漏 —— 漏一个就又变成
#: 「漏显示 LoRA」。这里按**语义前缀**匹配，覆盖面广且不会误判。
_LORA_NODE_MARKERS = (
    "lora",
)

#: 采样步数节点（ComfyUI 原生 + 常见改名）。
_SCHEDULER_NODE_TYPES = ("basicscheduler", "ksampler", "ksampleradvanced",
                         "samplercustom", "samplercustomadvanced")

#: 采样器选择节点。
_SAMPLER_SELECT_NODE_TYPES = ("ksamplerselect", "samplerselect")

#: 模型（扩散模型）加载节点。
_MODEL_NODE_TYPES = ("unetloader", "checkpointloader", "checkpointloadersimple",
                      "unetloadergguf")


def _node_inputs(node: dict) -> dict:
    inputs = node.get("inputs")
    return inputs if isinstance(inputs, dict) else {}


def _to_int(value) -> int | None:
    """宽松转 int：失败返回 None（**不编造默认值**）。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float(value) -> float | None:
    """宽松转 float：失败返回 None（**不编造默认值**）。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _class_type(node: dict) -> str:
    return str(node.get("class_type") or "")


def extract_from_workflow(workflow: dict) -> dict:
    """从**构建好的 workflow** 反解出真实生成参数。

    返回固定形状（缺失就是 None / ""，绝不编造）：

        {
          "steps":      int | None,
          "scheduler":  str | None,
          "sampler":    str | None,
          "cfg":        float | None,
          "seed":       int | None,
          "width":      int | None,
          "height":     int | None,
          "model_file": str | None,   权重文件名
          "loras": [ {name, strength, loader} ],   工作流里实际挂的 LoRA
        }

    【为什么按 class_type 找，而不是按节点 id】
    节点 id（"9" / "17" / "138:0"）是**构建器的内部实现细节**，
    改一次构建器就全变。按 class_type 找才稳。
    """
    out = {
        "steps": None,
        "scheduler": None,
        "sampler": None,
        "cfg": None,
        "seed": None,
        "width": None,
        "height": None,
        "model_file": None,
        "loras": [],
    }
    if not isinstance(workflow, dict):
        return out

    for node in workflow.values():
        if not isinstance(node, dict):
            continue
        ct = _class_type(node)
        ct_lower = ct.lower()
        inputs = _node_inputs(node)

        # ---- LoRA ----
        if any(marker in ct_lower for marker in _LORA_NODE_MARKERS):
            raw_name = inputs.get("lora_name")
            if raw_name:
                name = _clean_weight_name(raw_name)
                if name:
                    strength = _to_float(inputs.get("strength_model"))
                    if strength is None:
                        strength = _to_float(inputs.get("strength"))
                    out["loras"].append({
                        "name": name,
                        "raw_name": str(raw_name),
                        "strength": strength,
                        "loader": ct,
                    })
            continue

        # ---- 采样步数 / 调度器 ----
        if ct_lower in _SCHEDULER_NODE_TYPES:
            raw_steps = inputs.get("steps")
            steps = _to_int(raw_steps)
            if steps is not None and out["steps"] is None:
                out["steps"] = steps
            scheduler = inputs.get("scheduler")
            if scheduler and not out["scheduler"]:
                out["scheduler"] = str(scheduler)
            # KSampler / KSamplerAdvanced 自带 cfg / seed
            if out["cfg"] is None:
                cfg = _to_float(inputs.get("cfg"))
                if cfg is not None:
                    out["cfg"] = cfg
            if out["seed"] is None:
                seed = _to_int(inputs.get("seed"))
                if seed is not None:
                    out["seed"] = seed
            continue

        # ---- 采样器选择 ----
        if ct_lower in _SAMPLER_SELECT_NODE_TYPES:
            sampler = inputs.get("sampler_name")
            if sampler and not out["sampler"]:
                out["sampler"] = str(sampler)
            continue

        # ---- 模型权重 ----
        if ct_lower in _MODEL_NODE_TYPES and not out["model_file"]:
            for key in ("unet_name", "ckpt_name", "model_name"):
                value = inputs.get(key)
                if value:
                    out["model_file"] = str(value)
                    break
            continue

        # ---- 尺寸（latent 尺寸节点）----
        if out["width"] is None and out["height"] is None:
            width = inputs.get("width")
            height = inputs.get("height")
            if isinstance(width, (int, float)) and isinstance(height, (int, float)):
                # 注意：这类节点的 width/height 有时是**链接**（["115", 0]），
                # 上面 isinstance 已经把它排除了。
                out["width"] = int(width)
                out["height"] = int(height)

    return out


def apply_to_params(params: dict, extracted: dict,
                    include_seed: bool = True) -> dict:
    """把反解出来的**真实**生成参数回写进 params（就地修改，返回同一个 dict）。

    【写回纪律】
      * 只写**反解得到**的字段（None / 空 -> 不写）。
      * 已有值**以反解值为准**（反解值是实际执行的那份），
        但**不覆盖**掉 params 里那些不属于生成参数的字段
        （prompt / asset_refs / continuation …）。
      * LoRA 写成**结构化**的 `artifact_loras`（清单 + 强度 + 加载节点），
        并且**同时**把 `accel_loras` / `fixed_accel_lora` 补齐成
        「工作流实际挂了哪枚」—— 这样显示层（job_meta.resolve_job_lora）
        读到的就是真实值，而不是靠猜。

    【为什么 include_seed 可关】
    批次任务（一次出 N 张）：每张有**各自的 seed**，而 job 记录里
    只有一个 params。批次路径回写时必须 `include_seed=False` ——
    否则第一批的 seed 会盖掉 job 记录里的种子信息。
    采样参数（步数 / 调度器 / 采样器 / LoRA）在批次间是一致的，可以写。

    【为什么 LoRA 要写两份】
      * `artifact_loras` —— 新字段，**权威**（含全部 LoRA + 强度 + 节点）；
      * `accel_loras` / `fixed_accel_lora` —— 旧字段，读侧已经在用。
        只写新字段的话，读侧还是猜 -> 还是会误显示。
        所以两份都写，让读侧无需改动即可拿到真值。
    """
    if not isinstance(params, dict):
        return params
    if not isinstance(extracted, dict):
        return params

    # ---- 采样参数 ----
    keys = ("steps", "scheduler", "sampler", "cfg")
    if include_seed:
        keys = keys + ("seed",)
    for key in keys:
        value = extracted.get(key)
        if value is None or value == "":
            continue
        params[key] = value

    # ---- 尺寸（只在反解得到时写；不覆盖已有值）----
    width = extracted.get("width")
    height = extracted.get("height")
    if width and height:
        params.setdefault("width", int(width))
        params.setdefault("height", int(height))

    # ---- 模型权重文件 ----
    model_file = extracted.get("model_file")
    if model_file:
        params.setdefault("artifact_model_file", model_file)

    # ---- LoRA ----
    loras = extracted.get("loras") or []
    if not loras:
        # 工作流里**确实没有**挂任何 LoRA -> 显式记「没有」，
        # 而不是留给显示层去猜（那才是误显示的源头）。
        params["artifact_loras"] = []
        params["accel_loras"] = []
        params.pop("fixed_accel_lora", None)
        params.pop("accel_lora", None)
        return params

    params["artifact_loras"] = [
        {
            "name": item.get("name") or "",
            "raw_name": item.get("raw_name") or "",
            "strength": item.get("strength"),
            "loader": item.get("loader") or "",
        }
        for item in loras
    ]
    # 旧读侧字段：把**第一枚**同时写成「用户选的那枚」与「工作流固定的那枚」，
    # 因为工作流层面我们无法区分二者（它们都只是挂在模型上的 LoRA）。
    # 两者写同一个值 -> 显示层读到的就是真实挂载的那枚，不会猜错。
    first = loras[0]
    strength = first.get("strength")
    params["accel_loras"] = [{
        "key": first.get("name") or "",
        "name": first.get("name") or "",
        "strength": strength if strength is not None else 1.0,
        "enabled": True,
    }]
    params["fixed_accel_lora"] = first.get("name") or ""
    params["accel_lora"] = first.get("name") or ""
    if strength is not None:
        params["accel_lora_strength"] = strength
        params["fixed_accel_strength"] = strength
    return params
