# -*- coding: utf-8 -*-
"""
Agent 的**扩展工具集**（素材类 / 任务类 / 提示词预设 / 模式 schema / 记忆历史类）。

【为什么单独一个模块】
agent.py 的核心是**编排器**（跑几轮、怎么把工具结果喂回模型）。
本模块是**工具实现**（每个工具干什么、参数怎么校验、返回什么）。
分开的好处：
  * 编排逻辑与工具实现各自可读 —— 加 18 个工具不会把编排器淹没
  * 工具实现可以单独单测（不需要跑完整编排）
  * 工具通过 `register()` 挂进 agent.TOOLS，编排器完全不知道它们的存在

【所有工具的共同约束】
  1. **返回结构化 dict**，不是字符串 —— 编排器统一 JSON 序列化
  2. **失败抛 AgentError**，message 面向用户可读，带 code
  3. **不臆造数据** —— 列表类工具的内容必须来自真实查询
  4. **写操作有边界** —— 素材操作只能落在素材库目录内（agent_safety.safe_join）
"""
from __future__ import annotations

import inspect
import json
import os
import time
import uuid
from pathlib import Path

from . import agent_memory
from . import agent_safety
from . import agent_schema
from . import gen_types
from . import workflows as wf


def register(agent_mod, create_job_handler):
    """
    把本模块的工具注册进 agent.TOOLS。

    参数是 agent 模块本身（而不是 import agent）—— 避免循环依赖：
    agent.py import 本模块，本模块 import agent 就会成环。
    用「传进来」的方式打破环，同时保留单一声明点。
    """
    tool = agent_mod.tool
    AgentError = agent_mod.AgentError
    # create_job 在 agent.py 里定义（编排器模块），本模块通过参数拿到它 ——
    # 这样 batch_create_jobs 能复用**同一套**建任务逻辑，而不是复制一份。
    create_job = create_job_handler

    # ---------------------------------------------------------------- 素材类

    @tool(
        "upload_asset",
        "把本机文件上传进素材库（入库后会返回 asset_id，可作为参考图）。"
        "只接受素材库目录内的相对路径。",
        _schema({
            "path": {"type": "string",
                     "description": "待上传文件在素材库目录内的相对路径，"
                                    "如 originals/2026/xxx.png。不接受绝对路径。"},
            "name": {"type": "string", "description": "素材名称（留空用文件名）"},
            "category": {"type": "string",
                         "description": "素材类型：image / video / audio / doc / "
                                        "workflow / other（留空自动识别）"},
            "tags": {"type": "array", "items": {"type": "string"},
                     "description": "标签列表"},
            "note": {"type": "string", "description": "备注"},
        }, ["path"]),
    )
    async def _tool_upload_asset(ctx, args: dict) -> dict:
        from . import asset_repo
        from . import asset_service
        from . import assets as asset_store

        raw_path = str(args.get("path") or "").strip()
        if not raw_path:
            raise AgentError("upload_asset 需要 path 参数")

        # 路径边界：只允许素材库目录内的相对路径（防路径穿越）
        try:
            root = asset_store.library_root()
            source = agent_safety.safe_join(root, raw_path)
        except agent_safety.PathViolation as exc:
            raise AgentError(exc.message, status=400, code=exc.code) from exc
        if not source.is_file():
            raise AgentError(f"文件不存在：{raw_path}", status=404,
                             code="file_not_found")

        size = source.stat().st_size
        if size <= 0:
            raise AgentError("文件为空，无法入库", status=400)
        if size > asset_store.MAX_UPLOAD_BYTES:
            raise AgentError(
                f"文件超过上限 {asset_store.MAX_UPLOAD_BYTES // (1024 ** 3)} GB",
                status=413, code="file_too_large")

        category = str(args.get("category") or "").strip() or None
        if category and category not in asset_store.CATEGORIES:
            raise AgentError(
                f"category 只能是 {'/'.join(asset_store.CATEGORIES)}，"
                f"收到 {category}")
        tags = args.get("tags") or []
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.replace(",", "，").split("，") if t.strip()]
        tags = [str(t).strip()[:60] for t in tags if str(t).strip()]

        with asset_store.connect() as conn:
            try:
                result = asset_service.register_asset(
                    conn,
                    source_path=source,
                    filename=source.name,
                    name=str(args.get("name") or "").strip() or None,
                    category=category,
                    tags=tags,
                    note=str(args.get("note") or "").strip() or None,
                    source="import",
                    creator="agent")
            except asset_store.LibraryError as exc:
                raise AgentError(exc.message, status=exc.status,
                                 code=exc.code) from exc
            asset = result["asset"]
            entry = {
                "asset_id": asset["id"],
                "name": asset.get("name") or "",
                "category": asset.get("category") or "",
                "subtype": asset.get("subtype") or "",
                "tags": asset_repo.tags_of(conn, asset["id"]),
                "bytes": (result.get("version") or {}).get("bytes"),
                "duplicated": bool(result.get("duplicated")),
            }
        ctx.assets[entry["asset_id"]] = entry
        return {"ok": True, "asset": entry,
                "hint": "已入库，可用这个 asset_id 作为参考图传给 create_job"}

    @tool(
        "delete_asset",
        "删除素材（进回收站，可恢复）。素材正被任务引用时会拒绝删除。",
        _schema({
            "asset_id": {"type": "string", "description": "素材 ID"},
            "purge": {"type": "boolean",
                      "description": "true = 彻底删除（不可恢复），"
                                     "默认 false = 进回收站"},
        }, ["asset_id"]),
    )
    async def _tool_delete_asset(ctx, args: dict) -> dict:
        from . import asset_repo
        from . import assets as asset_store

        asset_id = str(args.get("asset_id") or "").strip()
        if not asset_id:
            raise AgentError("delete_asset 需要 asset_id 参数")
        # purge 用同一个布尔规整逻辑（模型可能传字符串 "False"）
        purge = agent_mod._as_bool(args.get("purge"))
        with asset_store.connect() as conn:
            asset = asset_repo.get_asset(conn, asset_id, include_deleted=True)
            if not asset:
                raise AgentError(f"素材不存在：{asset_id}", status=404,
                                 code="asset_not_found")
            if asset.get("deleted_at") and not purge:
                raise AgentError(f"素材已在回收站中：{asset.get('name') or asset_id}")
            try:
                if purge:
                    result = asset_repo.purge(conn, asset_id)
                else:
                    result = asset_repo.soft_delete(conn, asset_id)
            except asset_store.LibraryError as exc:
                raise AgentError(exc.message, status=exc.status,
                                 code=exc.code) from exc
        ctx.assets.pop(asset_id, None)
        return {"ok": True, "asset_id": asset_id,
                "purged": purge,
                "deleted_at": result.get("deleted_at")}

    @tool(
        "tag_asset",
        "给素材打标签或改标签。add 追加，remove 移除，replace 整体替换。",
        _schema({
            "asset_id": {"type": "string", "description": "素材 ID"},
            "add": {"type": "array", "items": {"type": "string"},
                    "description": "要添加的标签"},
            "remove": {"type": "array", "items": {"type": "string"},
                       "description": "要移除的标签"},
            "replace": {"type": "array", "items": {"type": "string"},
                        "description": "整体替换为这组标签"},
        }, ["asset_id"]),
    )
    async def _tool_tag_asset(ctx, args: dict) -> dict:
        from . import asset_repo
        from . import assets as asset_store

        asset_id = str(args.get("asset_id") or "").strip()
        if not asset_id:
            raise AgentError("tag_asset 需要 asset_id 参数")

        def _clean(items):
            if isinstance(items, str):
                items = items.replace(",", "，").split("，")
            out = []
            for item in (items or []):
                text = str(item or "").strip()[:60]
                if text and text not in out:
                    out.append(text)
            return out

        add = _clean(args.get("add"))
        remove = _clean(args.get("remove"))
        replace = _clean(args.get("replace"))
        if not (add or remove or replace):
            raise AgentError("tag_asset 需要 add / remove / replace 至少一个")

        with asset_store.connect() as conn:
            asset = asset_repo.get_asset(conn, asset_id, include_deleted=True)
            if not asset:
                raise AgentError(f"素材不存在：{asset_id}", status=404,
                                 code="asset_not_found")
            if asset.get("deleted_at"):
                raise AgentError(f"素材已在回收站中：{asset.get('name') or asset_id}")
            if replace is not None:
                asset_repo.update_asset(conn, asset_id, {"tags": replace})
            if add:
                asset_repo.add_tags(conn, asset_id, add, creator="agent")
            for tag in remove:
                asset_repo.remove_tag(conn, asset_id, tag)
            tags = asset_repo.tags_of(conn, asset_id)
        if asset_id in ctx.assets:
            ctx.assets[asset_id]["tags"] = tags
        return {"ok": True, "asset_id": asset_id, "tags": tags}

    @tool(
        "list_asset_tags",
        "列出素材库里所有标签及其使用次数（可用于给素材打标签时选词）。",
        _schema({"limit": {"type": "integer",
                           "description": "返回条数上限，默认 30"}}),
    )
    async def _tool_list_asset_tags(ctx, args: dict) -> dict:
        from . import asset_repo
        from . import assets as asset_store

        try:
            limit = int(args.get("limit") or 30)
        except (TypeError, ValueError):
            limit = 30
        limit = max(1, min(100, limit))
        with asset_store.connect() as conn:
            tags = asset_repo.all_tags(conn)
        return {"total": len(tags), "tags": tags[:limit],
                "hint": "用 tag_asset 把这些标签打到素材上"}

    # ---------------------------------------------------------------- 任务类

    @tool(
        "cancel_job",
        "取消一个排队中或运行中的生成任务。",
        _schema({"job_id": {"type": "string", "description": "任务 ID"}},
                ["job_id"]),
    )
    async def _tool_cancel_job(ctx, args: dict) -> dict:
        job_id = str(args.get("job_id") or "").strip()
        if not job_id:
            raise AgentError("cancel_job 需要 job_id 参数")
        server = ctx.server
        if server is None:
            raise AgentError("Agent 未接入服务端环境，无法取消任务", status=500)

        canceler = getattr(server, "cancel_job_record", None)
        if canceler is None:
            raise AgentError("服务端缺少 cancel_job_record，无法取消任务", status=500)
        try:
            result = canceler(job_id)
            # cancel_job_record 可能是 async（要撤 ComfyUI 队列）也可能是同步。
            # 用 inspect 判断而不是 isinstance —— 两种都支持，
            # 免得服务端换个实现方式这个工具就静默拿到 coroutine。
            if inspect.isawaitable(result):
                result = await result
        except AgentError:
            raise
        except ValueError as exc:
            raise AgentError(str(exc), status=400) from exc
        for entry in ctx.jobs:
            if entry.get("job_id") == job_id:
                entry["status"] = result.get("status") or "canceled"
        return result

    @tool(
        "retry_job",
        "用相同的参数重新下发一个生成任务（失败/取消后想再试一次时用）。",
        _schema({
            "job_id": {"type": "string", "description": "要重试的任务 ID"},
            "overrides": {"type": "object",
                          "description": "要覆盖的参数（如 {\"aspect\":\"9:16\"}）"},
        }, ["job_id"]),
    )
    async def _tool_retry_job(ctx, args: dict) -> dict:
        server = ctx.server
        if server is None:
            raise AgentError("Agent 未接入服务端环境，无法重试任务", status=500)
        job_id = str(args.get("job_id") or "").strip()
        if not job_id:
            raise AgentError("retry_job 需要 job_id 参数")

        getter = getattr(server, "get_job_params", None)
        if getter is None:
            raise AgentError("服务端缺少 get_job_params，无法重试任务", status=500)
        try:
            source = getter(job_id)
        except AgentError:
            raise
        except ValueError as exc:
            raise AgentError(str(exc), status=400) from exc
        if not source:
            raise AgentError(f"任务不存在：{job_id}", status=404, code="job_not_found")

        mode = source["mode"]
        params = dict(source["params"])
        overrides = args.get("overrides") or {}
        if not isinstance(overrides, dict):
            raise AgentError("overrides 必须是对象")
        params.update({str(k): v for k, v in overrides.items()})

        # 重试前走**同一套**校验（非法参数照样拦下并给替代方案）
        issues = agent_schema.validate_params(mode, params)
        if issues:
            raise AgentError(
                "重试参数校验未通过：\n" + agent_schema.format_issues(issues),
                status=400, code="invalid_params",
                extra={"issues": agent_schema.issues_to_payload(issues)})

        asset_refs = params.pop("asset_refs", None) or []
        prepare = getattr(server, "prepare_job_params", None)
        if prepare is None:
            raise AgentError("服务端缺少 prepare_job_params，无法重试任务", status=500)
        try:
            asset_refs = prepare(mode, params, asset_refs, None)
        except Exception as exc:
            message = getattr(exc, "message", None) or str(exc)
            code = getattr(exc, "code", "bad_request")
            raise AgentError(message, status=400, code=code) from exc
        if asset_refs:
            params["asset_refs"] = asset_refs

        new_id = agent_mod._create_job_record(ctx, mode, params)
        entry = {
            "job_id": new_id,
            "mode": mode,
            "label": gen_types.type_for_mode(mode).get("label") or mode,
            "prompt": params.get("prompt") or "",
            "params": {k: v for k, v in params.items() if k != "asset_refs"},
            "asset_refs": asset_refs,
            "status": "queued",
            "summary": f"重试自 {job_id}",
        }
        entry["_pending"] = True
        ctx.jobs.append(entry)
        return {"job_id": new_id, "mode": mode, "status": "queued",
                "retried_from": job_id, "label": entry["label"]}

    @tool(
        "batch_create_jobs",
        "批量创建多个生成任务（一次提交多个 prompt 或多个模式）。",
        _schema({
            "jobs": {"type": "array", "items": {"type": "object"},
                     "description": "任务数组，每项含 mode / prompt，"
                                    "可带 aspect / resolution / duration / asset_id"},
        }, ["jobs"]),
    )
    async def _tool_batch_create_jobs(ctx, args: dict) -> dict:
        items = args.get("jobs")
        if not isinstance(items, list) or not items:
            raise AgentError("batch_create_jobs 需要非空的 jobs 数组")
        if len(items) > 10:
            raise AgentError(f"一次最多批量创建 10 个任务，收到 {len(items)} 个")

        created = []
        errors = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                errors.append({"index": index, "error": "每项必须是对象"})
                continue
            try:
                result = await create_job(ctx, item)
                created.append(result)
            except AgentError as exc:
                errors.append({"index": index, "error": exc.message,
                               "code": exc.code})
        if not created:
            raise AgentError(
                "批量创建全部失败：" +
                "；".join(str(e.get("error")) for e in errors[:3]),
                status=400, code="batch_failed",
                extra={"errors": errors})
        return {"ok": True, "created": created, "failed": errors,
                "created_count": len(created), "failed_count": len(errors)}

    @tool(
        "list_jobs",
        "按状态/时间检索历史生成任务。",
        _schema({
            "status": {"type": "string",
                       "description": "任务状态：queued / running / done / "
                                      "failed / canceled（留空 = 全部）"},
            "mode": {"type": "string", "description": "生成模式过滤"},
            "since_hours": {"type": "integer",
                            "description": "只看最近 N 小时的任务"},
            "limit": {"type": "integer", "description": "返回条数上限，默认 10"},
        }),
    )
    async def _tool_list_jobs(ctx, args: dict) -> dict:
        server = ctx.server
        if server is None:
            raise AgentError("Agent 未接入服务端环境，无法查询任务", status=500)
        lister = getattr(server, "list_job_records", None)
        if lister is None:
            raise AgentError("服务端缺少 list_job_records，无法查询任务", status=500)

        status = str(args.get("status") or "").strip()
        mode = str(args.get("mode") or "").strip()
        try:
            since_hours = int(args.get("since_hours") or 0)
        except (TypeError, ValueError):
            since_hours = 0
        try:
            limit = int(args.get("limit") or 10)
        except (TypeError, ValueError):
            limit = 10
        limit = max(1, min(50, limit))
        if mode and mode not in wf.BUILDERS:
            raise AgentError(
                f"未知模式 {mode}。本工作台支持：{'、'.join(sorted(wf.BUILDERS.keys()))}")

        rows = lister(status=status or None, mode=mode or None,
                      since_hours=since_hours or None, limit=limit)
        return {"total": len(rows), "jobs": rows,
                "hint": "用 get_job_status 查单个任务的详细进度"}

    @tool(
        "get_job_status",
        "查一个生成任务的实时状态、进度与产物路径（用于给用户播报进度）。",
        _schema({"job_id": {"type": "string", "description": "任务 ID"}},
                ["job_id"]),
    )
    async def _tool_get_job_status(ctx, args: dict) -> dict:
        server = ctx.server
        if server is None:
            raise AgentError("Agent 未接入服务端环境，无法查询任务", status=500)
        getter = getattr(server, "get_job_status_payload", None)
        if getter is None:
            raise AgentError("服务端缺少 get_job_status_payload，无法查询任务",
                             status=500)
        job_id = str(args.get("job_id") or "").strip()
        if not job_id:
            raise AgentError("get_job_status 需要 job_id 参数")
        payload = getter(job_id)
        if not payload:
            raise AgentError(f"任务不存在：{job_id}", status=404,
                             code="job_not_found")
        return payload

    # ---------------------------------------------------------------- 提示词预设

    @tool(
        "list_prompt_presets",
        "列出工作台内置的提示词增强预设（真实存在的模板，不是编造的）。",
        _schema({}),
    )
    async def _tool_list_prompt_presets(ctx, args: dict) -> dict:
        presets = agent_schema.prompt_presets()
        return {"total": len(presets), "presets": presets,
                "hint": "用 get_prompt_preset 拿某个预设的完整说明"}

    @tool(
        "get_prompt_preset",
        "查看某个提示词增强预设的完整内容（系统提示词 + 模板 + 参数）。",
        _schema({"spec_id": {"type": "string",
                             "description": "预设 ID，来自 list_prompt_presets"}},
                ["spec_id"]),
    )
    async def _tool_get_prompt_preset(ctx, args: dict) -> dict:
        spec_id = str(args.get("spec_id") or "").strip()
        if not spec_id:
            raise AgentError("get_prompt_preset 需要 spec_id 参数")
        overrides = agent_mod._load_enhancer_overrides(ctx)
        try:
            return agent_schema.prompt_preset(spec_id, overrides)
        except KeyError as exc:
            raise AgentError(
                f"未知预设 {spec_id}。可用预设："
                + "、".join(p["spec_id"] for p in agent_schema.prompt_presets()),
                status=404, code="preset_not_found") from exc

    # ---------------------------------------------------------------- 模式类

    @tool(
        "describe_mode_params",
        "查看某个生成模式的全部合法参数取值（时长/分辨率/比例/模型/LoRA）。"
        "取值全部从工作台真实配置推导，不是硬编码。",
        _schema({
            "mode": {"type": "string",
                     "description": "生成模式，来自 list_modes",
                     "enum": sorted(wf.BUILDERS.keys())},
            "field": {"type": "string",
                      "description": "只看某一个参数（如 aspect / duration）"},
        }, ["mode"]),
    )
    async def _tool_describe_mode_params(ctx, args: dict) -> dict:
        mode = str(args.get("mode") or "").strip()
        if mode not in wf.BUILDERS:
            raise AgentError(
                f"未知模式 {mode}。本工作台支持：{'、'.join(sorted(wf.BUILDERS.keys()))}")
        schema = agent_schema.mode_schema(mode)
        field = str(args.get("field") or "").strip()
        if field:
            if field not in schema["params"]:
                raise AgentError(
                    f"模式 {mode} 没有参数 {field}。可用参数："
                    + "、".join(schema["params"].keys()))
            return {"mode": mode, "field": field,
                    "spec": schema["params"][field]}
        return schema

    @tool(
        "get_mode_schema",
        "返回某个模式真实合法的参数 schema（含合法取值集合与默认值）。"
        "建任务前拿不准参数时用它。",
        _schema({
            "mode": {"type": "string",
                     "description": "生成模式，来自 list_modes",
                     "enum": sorted(wf.BUILDERS.keys())},
        }, ["mode"]),
    )
    async def _tool_get_mode_schema(ctx, args: dict) -> dict:
        mode = str(args.get("mode") or "").strip()
        if mode not in wf.BUILDERS:
            raise AgentError(
                f"未知模式 {mode}。本工作台支持：{'、'.join(sorted(wf.BUILDERS.keys()))}")
        schema = agent_schema.mode_schema(mode)
        schema["defaults"] = agent_schema.mode_defaults(mode)
        schema["requires"] = agent_schema.mode_requires_refs(mode)
        return schema

    # ---------------------------------------------------------------- 记忆/历史类

    @tool(
        "recall_memory",
        "读取跨会话的用户偏好记忆（如常用比例、常用模型、命名习惯）。",
        _schema({
            "query": {"type": "string",
                      "description": "记忆键的关键词（留空返回最近的全部）"},
            "scope": {"type": "string",
                      "description": "作用域：global（跨会话）或 session:<id>"},
            "limit": {"type": "integer",
                      "description": ("返回条数上限，默认 20；"
                                       "填 0 或负数表示「返回全部」"
                                       "（上限 "
                                       f"{agent_memory.MAX_READ_LIMIT} 条）")},
        }),
    )
    async def _tool_recall_memory(ctx, args: dict) -> dict:
        store = _memory_store(ctx)
        # ⚠️ 2026-11-10 语义统一：limit <= 0 -> 返回全部（0 条/负数不再
        #   被夹成 1）。归一逻辑在 agent_memory._normalize_read_limit，
        #   工具层与存储层用**同一个**函数，不会再各说各话。
        limit = agent_memory._normalize_read_limit(args.get("limit"))
        query = str(args.get("query") or "").strip()
        scope = str(args.get("scope") or "").strip() or None
        entries = store.recall(query, scope=scope, limit=limit)
        return {"total": len(entries), "memories": entries,
                "scope": scope or "global",
                "limit": limit,
                "returned_all": limit == 0}

    @tool(
        "search_conversations",
        "检索历史会话里的对话内容（跨会话）。",
        _schema({
            "query": {"type": "string", "description": "要搜索的关键词"},
            "session_id": {"type": "string",
                           "description": "限定在某个会话内搜索（留空 = 全部）"},
            "limit": {"type": "integer", "description": "返回条数上限，默认 10"},
        }, ["query"]),
    )
    async def _tool_search_conversations(ctx, args: dict) -> dict:
        query = str(args.get("query") or "").strip()
        if not query:
            raise AgentError("search_conversations 需要 query 参数")
        store = _session_store(ctx)
        if store is None:
            raise AgentError("会话库未就绪，无法检索历史对话", status=500)
        try:
            limit = int(args.get("limit") or 10)
        except (TypeError, ValueError):
            limit = 10
        limit = max(1, min(50, limit))
        session_id = str(args.get("session_id") or "").strip()
        results = store.search(query, limit=limit, session_id=session_id)
        return {"total": len(results), "results": results, "query": query,
                "hint": ("没有命中就换关键词，或用 list_jobs 查任务历史"
                         if not results else "")}

    @tool(
        "search_asset_history",
        "检索素材的历史使用记录（哪些任务用过它、什么时候入库）。",
        _schema({
            "asset_id": {"type": "string", "description": "素材 ID"},
            "query": {"type": "string",
                      "description": "按名称/标签关键词搜索素材（不填 asset_id 时用）"},
            "limit": {"type": "integer", "description": "返回条数上限，默认 10"},
        }),
    )
    async def _tool_search_asset_history(ctx, args: dict) -> dict:
        from . import asset_repo
        from . import assets as asset_store

        try:
            limit = int(args.get("limit") or 10)
        except (TypeError, ValueError):
            limit = 10
        limit = max(1, min(50, limit))
        asset_id = str(args.get("asset_id") or "").strip()
        query = str(args.get("query") or "").strip()

        with asset_store.connect() as conn:
            if asset_id:
                asset = asset_repo.get_asset(conn, asset_id, include_deleted=True)
                if not asset:
                    raise AgentError(f"素材不存在：{asset_id}", status=404,
                                     code="asset_not_found")
                versions = asset_repo.versions_of(conn, asset_id)
                analyses = asset_repo.analyses_of(conn, asset_id)
                refs = asset_repo.refs_of(conn, asset_id)
                entry = {
                    "asset_id": asset_id,
                    "name": asset.get("name") or "",
                    "category": asset.get("category") or "",
                    "tags": asset_repo.tags_of(conn, asset_id),
                    "created_at": asset.get("created_at"),
                    "deleted_at": asset.get("deleted_at"),
                    "versions": [{"version": v.get("version"),
                                  "bytes": v.get("bytes"),
                                  "created_at": v.get("created_at"),
                                  "width": v.get("width"),
                                  "height": v.get("height")}
                                 for v in versions[:limit]],
                    "analyses": [{"kind": a.get("kind"),
                                  "created_at": a.get("created_at"),
                                  "summary": (a.get("text") or "")[:200]}
                                 for a in analyses[:limit]],
                    "used_by": [{"kind": r.get("kind"), "ref_id": r.get("ref_id"),
                                 "slot": r.get("slot"),
                                 "created_at": r.get("created_at")}
                                for r in refs[:limit]],
                }
                return {"total": 1, "assets": [entry]}

            if not query:
                raise AgentError(
                    "search_asset_history 需要 asset_id 或 query 至少一个")
            result = asset_repo.search_assets(
                conn, {"q": query, "sort": "created_desc",
                       "page": 1, "page_size": limit})
            items = []
            for asset in result.get("assets", []):
                items.append({
                    "asset_id": asset["id"],
                    "name": asset.get("name") or "",
                    "category": asset.get("category") or "",
                    "tags": asset.get("tags") or [],
                    "created_at": asset.get("created_at"),
                    "analyzed": bool(asset.get("analyzed")),
                })
            return {"total": result.get("total", len(items)),
                    "assets": items, "query": query}

    # ---------------------------------------------------------------- 记忆写入

    @tool(
        "remember_preference",
        "记住用户的长期偏好（跨会话有效），如「我习惯用 9:16」「默认用 z_image_turbo」。",
        _schema({
            "key": {"type": "string",
                    "description": "偏好键，如 preferred_aspect / preferred_model"},
            "value": {"type": "string", "description": "偏好值"},
            "scope": {"type": "string",
                      "description": "global（跨会话，默认）或 session:<id>"},
        }, ["key", "value"]),
    )
    async def _tool_remember_preference(ctx, args: dict) -> dict:
        key = str(args.get("key") or "").strip()
        value = args.get("value")
        if not key:
            raise AgentError("remember_preference 需要 key 参数")
        if value is None or str(value).strip() == "":
            raise AgentError("remember_preference 需要 value 参数")
        scope = str(args.get("scope") or "").strip() or None
        store = _memory_store(ctx)
        result = store.remember(key, str(value), scope=scope,
                                source="tool:remember_preference")
        if result.get("error"):
            raise AgentError(result["error"], status=400,
                             code=result.get("code", "bad_request"))
        return {"ok": True, "key": result["key"], "scope": result["scope"],
                "value": result["value"], "created": result.get("created")}


# ------------------------------------------------------------------ 辅助


def _schema(properties: dict, required: list | None = None) -> dict:
    """注册表用的 JSON Schema 构造器（与 agent._schema 同格式）。"""
    return {"type": "object", "properties": properties,
            "required": required or []}


def _memory_store(ctx):
    """取记忆库 store（测试可注入）。"""
    store = getattr(ctx, "memory_store", None)
    if store is not None:
        return store
    return agent_memory.get_store()


def _session_store(ctx):
    """取会话库 store（测试可注入）。"""
    store = getattr(ctx, "session_store", None)
    if store is not None:
        return store
    from . import agent_sessions
    return agent_sessions.get_store()
