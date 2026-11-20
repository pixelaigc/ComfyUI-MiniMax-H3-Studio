#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/gen_flow.py —— 从**真实代码**派生《人机交互流程图》

【为什么要有这个脚本】
流程图如果纯手写，一定会和代码漂移 —— 加一个生成模式、换一个增强方式、
改一次素材槽位，图就过期了，而且没人发现。
所以这里的做法是：**骨架手写、节点派生**。

  * 骨架（scripts/templates/*.mmd）：描述「流程长什么样」（人工维护）
  * 节点清单：**全部从代码 / 配置读出来**
      - 后端路由表          server.py + h3studio/asset_api.py + h3studio/ref_api.py
      - 生成模式注册表      h3studio/gen_types.py
      - 视频类别与模型      h3studio/video_categories.py
      - 素材槽位 / 上限     h3studio/agent_schema.py
      - Agent 工具表        h3studio/agent.py（TOOLS / TOOL_STAGE_TEXT）
      - 提示词增强注册表    web/enhance-menu.js
      - 左侧导航 / 面板字段 web/index.html
      - 卡片与右键菜单动作  web/*.js（data-act）
      - 前端实际调用的接口  web/*.js（字符串扫描）
  * 产物：docs/交互流程图.mmd + docs/交互流程图-内置Agent.mmd
          docs/交互流程图-派生清单.md（自动表格 = 与代码的对应证据）
          docs/*.svg / *.png（渲染产物）

【用法】
    python3 scripts/gen_flow.py            # 只生成 .mmd + 派生清单
    python3 scripts/gen_flow.py --render   # 再渲染 SVG / PNG
    python3 scripts/gen_flow.py --check    # 校验已提交文件与当前代码一致（CI 用）
    make flow                              # = --render

【硬规则】
  * 生成的文件头里带「请勿手改」标记；要改流程形状请改 scripts/templates/*.mmd。
  * 任何派生失败（模块导入不了 / 模板占位符对不上）都**直接报错退出**，
    绝不降级成「少画几个节点」—— 那样图会静默失真。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TPL_DIR = ROOT / "scripts" / "templates"
DOCS = ROOT / "docs"

OUT_MAIN_MMD = DOCS / "交互流程图-总览.mmd"
OUT_CREATE_MMD = DOCS / "交互流程图-创作链路.mmd"
OUT_ASSETS_MMD = DOCS / "交互流程图-素材库.mmd"
OUT_AGENT_MMD = DOCS / "交互流程图-内置Agent.mmd"
OUT_SYSTEM_MMD = DOCS / "交互流程图-系统配置.mmd"
OUT_LIST_MD = DOCS / "交互流程图-派生清单.md"

#: (模板, 产物) —— 一一对应。顺序即文档里的阅读顺序。
TEMPLATES = [
    (TPL_DIR / "flow-overview.mmd", OUT_MAIN_MMD),
    (TPL_DIR / "flow-create.mmd", OUT_CREATE_MMD),
    (TPL_DIR / "flow-assets.mmd", OUT_ASSETS_MMD),
    (TPL_DIR / "flow-agent.mmd", OUT_AGENT_MMD),
    (TPL_DIR / "flow-system.mmd", OUT_SYSTEM_MMD),
]

WEB = ROOT / "web"
SLOT_RE = re.compile(r"@([A-Z0-9_]+)@")


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
def read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def fail(msg: str) -> "None":
    raise SystemExit(f"[gen_flow] 致命错误：{msg}")


def mermaid_label(text: str) -> str:
    """把任意文本洗成 Mermaid 标签里安全的形式。

    规则：
      * 双引号 -> 单引号（标签用双引号包起来）
      * `#`    -> `＃`（`#` 在 Mermaid 里是实体转义前缀，会把后面吃掉）
      * `&`    -> `＆`（避免 HTML 实体歧义）
      * 换行   -> `<br/>`
    """
    out = (text or "").replace('"', "'").replace("#", "＃").replace("&", "＆")
    out = out.replace("\r\n", "\n").replace("\r", "\n")
    return out.strip()


def sanitize_mermaid_comments(text: str) -> str:
    """把 Mermaid 注释里**单独成行**的 `%%` 改成 `%% `。

    【为什么必须做】
    Mermaid 11.x 的解析器遇到「一行只有一个 %%」会把它当成注释块的结束标记，
    后面紧跟的 `flowchart TD` 就被当成节点文本 —— 报
    `Parse error ... Expecting 'NEWLINE', 'SPACE', 'GRAPH', got 'NODE_STRING'`。
    这坑很隐蔽：模板里只是多敲了一个空注释行，生成的 .mmd 就渲染不出来。
    所以这里统一在生成阶段把裸 `%%` 行修成 `%% `，从根上消灭这一类解析错误。
    """
    out = []
    for line in text.split("\n"):
        if line.strip() == "%%":
            out.append("%% ")
        else:
            out.append(line)
    return "\n".join(out)


def md_cell(text: str) -> str:
    return (text or "").replace("|", "／").replace("\n", " ").strip()


# --------------------------------------------------------------------------
# 1. 后端路由表
# --------------------------------------------------------------------------
ROUTE_RE = re.compile(
    r"app\.router\.add_(get|post|put|delete|patch)\(\s*[\"']([^\"']+)[\"']"
    r"(?:\s*,\s*([A-Za-z_][A-Za-z0-9_]*))?"
)


def parse_routes() -> list[dict]:
    """从后端源码里抽出 (method, path, handler)。

    单一真源就是代码本身：aiohttp 的 `app.router.add_*(...)` 注册语句。
    """
    routes: list[dict] = []
    for rel in ("server.py", "h3studio/asset_api.py", "h3studio/ref_api.py"):
        path = ROOT / rel
        if not path.exists():
            fail(f"找不到路由源文件 {rel}")
        for m, url, handler in ROUTE_RE.findall(read(path)):
            routes.append({
                "method": m.upper(),
                "path": url,
                "handler": handler or "",
                "source": rel,
            })
    if not routes:
        fail("一条路由都没解析到 —— 解析正则失效了吗？")
    seen = set()
    uniq = []
    for r in routes:
        key = (r["method"], r["path"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(r)
    return uniq


# --------------------------------------------------------------------------
# 2. 前端实际调用的接口
# --------------------------------------------------------------------------
UI_API_RE = re.compile(r"[\"'`](/api/[A-Za-z0-9_\-./$%{}\\[\]]*)[\"'`]")


def scan_ui_api_calls() -> list[str]:
    """扫描 web/*.js 里出现的 /api/... 字面量（前端真实调用的接口）。"""
    calls: set[str] = set()
    for js in sorted(WEB.glob("*.js")):
        for raw in UI_API_RE.findall(read(js)):
            # ${asset.id} -> {id}，统一成路由模板里的写法
            norm = re.sub(r"\$\{[^}]*\}", "{id}", raw)
            calls.add(norm)
    return sorted(calls)


def route_to_regex(path: str) -> re.Pattern:
    """把后端路由路径转成正则（{id} 之类占位符 → 任意单段）。"""
    pat = re.escape(path)
    pat = pat.replace(re.escape("{path:.+}"), r".+")
    pat = re.sub(r"\\\{[a-zA-Z_][a-zA-Z0-9_]*(:[^}]*)?\\\}", r"[^/]+", pat)
    return re.compile("^" + pat + "$")


def match_route(call: str, routes: list[dict]) -> dict | None:
    """把前端调用路径匹配到后端路由。

    三种形态都要覆盖：
      1. 完全一致：/api/jobs
      2. 带占位符：/api/jobs/{id}（前端写的是 /api/jobs/${id}，已归一）
      3. **前缀拼接**：前端写 "/api/agent/media/" + id + "/status"
         —— 这时字面量以 "/" 结尾，按前缀匹配后端路由。
    """
    for r in routes:
        if route_to_regex(r["path"]).match(call):
            return r
    if call.endswith("/"):
        for r in routes:
            if r["path"].startswith(call):
                return r
    return None


# --------------------------------------------------------------------------
# 3. 生成模式 / 视频类别 / 素材槽位 / Agent 工具
# --------------------------------------------------------------------------
def _bootstrap_path() -> None:
    """把仓库根目录塞进 sys.path —— 这样 `from h3studio import ...` 能直接用。"""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))


def derive_generation_types() -> list[dict]:
    """生成模式注册表（h3studio/gen_types.py）—— 右侧「生成类型」标签的真源。"""
    from h3studio import gen_types
    out = []
    for item in gen_types.list_types():
        if item.get("key") == "__other__":
            continue
        out.append({
            "key": item["key"],
            "label": item["label"],
            "media": item.get("media", ""),
            "category": item.get("category", ""),
        })
    if not out:
        fail("gen_types.list_types() 返回空")
    return out


def derive_categories() -> list[dict]:
    from h3studio import gen_types
    return [{"key": k, "label": v} for k, v in gen_types.GENERATION_CATEGORIES.items()]


def derive_video_categories() -> list[dict]:
    from h3studio import video_categories
    out = []
    for cat in video_categories.list_categories():
        out.append({
            "key": cat["key"],
            "label": cat["label"],
            "models": [{
                "key": m.get("key", ""),
                "label": m.get("label", ""),
                "mode": m.get("mode", ""),
                "note": m.get("note", ""),
            } for m in cat.get("models", [])],
        })
    return out


def derive_slots() -> dict:
    """Agent 素材槽位（h3studio/agent_schema.py）。"""
    from h3studio import agent_schema
    out = {}
    for mode, (single, multi) in agent_schema.MODE_SLOTS.items():
        plan = agent_schema.mode_slot_plan(mode)
        out[mode] = {
            "single": list(single),
            "multi": list(multi),
            "max_per_slot": plan.get("max_per_slot", {}),
            "media_slots": plan.get("media_slots", {}),
        }
    return out


def derive_enhancer_templates() -> list[dict]:
    """后端提示词增强模板清单（h3studio/enhancer.py 的 TEMPLATE_SPECS）。

    与前端的「增强方式」是**两套东西**：
      * TEMPLATE_SPECS = 后端模板（图像 / 视频 / 音乐 / 官方 PE …），共 12 个；
      * ENHANCE_METHODS = 前端下拉里的增强方式，共 6 种。
    两者数量不同，所以图里必须分开标，不能混成「5 个」。
    """
    from h3studio import enhancer
    specs = enhancer.list_specs()
    if not specs:
        fail("enhancer.list_specs() 返回空")
    return [{"id": s.get("id", ""), "label": s.get("label", "")} for s in specs]


def derive_agent_tools() -> list[dict]:
    """Agent 工具清单（h3studio/agent.py 的 TOOLS + TOOL_STAGE_TEXT）。"""
    try:
        from h3studio import agent
        tools = []
        for name, spec in agent.TOOLS.items():
            tools.append({
                "name": name,
                "stage": agent.TOOL_STAGE_TEXT.get(name, ""),
                "description": (spec.get("description") or "").strip(),
            })
        if tools:
            return tools
    except Exception as exc:  # pragma: no cover - 兜底
        print(f"[gen_flow] 警告：直接导入 agent 失败（{exc}），改用正则兜底解析")
    # 兜底：正则扫 @tool("name", "description"
    tools = []
    for rel in ("h3studio/agent.py", "h3studio/agent_tools.py"):
        src = read(ROOT / rel)
        for m in re.finditer(r'@tool\(\s*"([a-zA-Z0-9_]+)"\s*,\s*\n?\s*("(?:[^"\\]|\\.)*")',
                             src):
            name = m.group(1)
            desc = m.group(2)[1:-1]
            tools.append({"name": name, "stage": "", "description": desc})
    if not tools:
        fail("一个 Agent 工具都没解析到")
    return tools


#: Agent 工具分组（用于流程图里的分栏）。
#: 归类依据是工具**实际作用对象**，不是名字长相 —— 新增工具时，
#: 只要落在下面任一分组规则里就自动进对应分栏；都不匹配则进「其它工具」。
#:
# 【2026-11-16 内置 Agent 审查修复】补齐 10 个工具的归属。
#:
# 背景：这 10 个工具（媒体库管理 / 额外能力 / ffmpeg 音视频）分别在
# b7c8bdc（媒体库管理）与 367a169（额外能力 + ffmpeg）加入，
# 但**没有同步更新本表** —— 于是它们全被推进「其它工具」分栏，
# 而 docs/ 里的流程图更是自那以后就没再重跑，
# 导致 tests/test_flow_diagram.py 的「每个 Agent 工具都必须有节点」
# 断言转红（线上遗留失败，本轮修复）。
TOOL_GROUP_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("素材类", ("search_assets", "describe_asset", "upload_asset",
                "delete_asset", "tag_asset", "list_asset_tags")),
    ("任务类", ("create_job", "batch_create_jobs", "cancel_job", "retry_job",
                "list_jobs", "get_job_status")),
    ("模式与参数类", ("list_modes", "describe_mode_params", "get_mode_schema")),
    ("提示词类", ("enhance_prompt", "list_prompt_presets", "get_prompt_preset")),
    ("会话与记忆类", ("recall_memory", "remember_preference",
                      "search_conversations", "search_asset_history")),
    ("回应类", ("respond",)),
    # 2026-11-16 新增分栏 ——
    ("媒体库管理类", ("manage_media_library",)),
    ("外部能力类", ("read_file", "web_search", "run_code")),
    # 【2026-11-20 新增 4 个工具】抽取音轨 / 时长裁剪 / 响度归一 / 替换音轨
    # —— 与既有 6 个 ffmpeg 工具同类（都作用在素材库的音视频上）。
    #
    # 【为什么必须同步更新本表】不更新的话，这 4 个工具会被推进
    # 「其它工具」分栏，而 tests/test_flow_diagram.py 的
    # 「每个 Agent 工具都必须有节点」断言会转红
    # （实测：4 个新工具全部报「在图里没有节点」）。
    ("ffmpeg 音视频类", ("media_probe", "media_concat", "media_transcode",
                         "media_extract_frames", "media_mix_audio",
                         "media_merge_av", "media_extract_audio",
                         "media_trim", "media_normalize_audio",
                         "media_replace_audio")),
]


def group_agent_tools(tools: list[dict]) -> "dict[str, list[dict]]":
    """把工具按分组规则归类，返回 {组名: [工具...]}。"""
    known = {name: group for group, names in TOOL_GROUP_RULES for name in names}
    groups: dict[str, list[dict]] = {g: [] for g, _ in TOOL_GROUP_RULES}
    groups["其它工具"] = []
    unclassified = []
    for t in tools:
        g = known.get(t["name"])
        if g is None:
            groups["其它工具"].append(t)
            unclassified.append(t["name"])
            continue
        groups[g].append(t)
    if unclassified:
        print("[gen_flow] 提示：以下工具未归入任何分组，已放进「其它工具」分栏 —— "
              f"{unclassified}")
    return groups


# --------------------------------------------------------------------------
# 4. 前端 DOM：导航 / 面板字段 / 动作
# --------------------------------------------------------------------------
def _element_by_id(html: str, element_id: str) -> str:
    """按 id 取出对应元素的内部 HTML（用标签深度扫描，处理任意嵌套）。"""
    m = re.search(r"<(\w+)([^>]*\bid=\"" + re.escape(element_id) + r"\"[^>]*)>", html)
    if not m:
        return ""
    tag = m.group(1)
    if tag in ("input", "img", "br", "hr"):
        return ""
    start = m.end()
    depth = 1
    i = start
    open_re = re.compile(r"<" + tag + r"\b", re.I)
    close_re = re.compile(r"</" + tag + r"\s*>", re.I)
    while i < len(html) and depth > 0:
        no = open_re.search(html, i)
        nc = close_re.search(html, i)
        if nc is None:
            break
        if no and no.start() < nc.start():
            depth += 1
            i = no.end()
        else:
            depth -= 1
            i = nc.end()
    return html[start:i]


def derive_nav_items() -> list[dict]:
    """左侧导航项（web/index.html）。"""
    html = read(WEB / "index.html")
    out = []
    for m in re.finditer(r'<a class="nav-item"([^>]*)>(.*?)</a>', html, re.S):
        attrs, body = m.group(1), m.group(2)
        mode = re.search(r'data-mode="([^"]+)"', attrs)
        nid = re.search(r'id="([^"]+)"', attrs)
        text = re.sub(r"<[^>]+>", " ", body)
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            continue
        out.append({
            "mode": mode.group(1) if mode else "",
            "id": nid.group(1) if nid else "",
            "label": text,
        })
    # 去掉 <noscript> 里的兜底重复项
    seen = set()
    uniq = []
    for item in out:
        key = (item["mode"], item["label"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(item)
    return uniq


def derive_panel_fields() -> dict:
    """参数面板的字段标签（web/index.html）。"""
    html = read(WEB / "index.html")
    out = {}
    for pid in ("videoPanel", "imagePanel", "musicPanel", "stitchPanel"):
        block = _element_by_id(html, pid)
        labels = []
        for m in re.finditer(r'class="field-label"[^>]*>(.*?)</div>', block, re.S):
            text = re.sub(r"<[^>]+>", " ", m.group(1))
            text = re.sub(r"\s+", " ", text).strip()
            if text and text not in labels:
                labels.append(text)
        out[pid] = labels
    return out


def derive_actions() -> dict:
    """卡片 / 右键菜单 / 工具条上的动作（web/*.js 的 data-act）。"""
    out: dict[str, list[str]] = {}
    for js in sorted(WEB.glob("*.js")):
        acts = sorted(set(re.findall(r'data-act="([a-z\-]+)"', read(js))))
        if acts:
            out[js.name] = acts
    return out


def derive_enhance_methods() -> list[dict]:
    """提示词增强方式注册表（web/enhance-menu.js 的 ENHANCE_METHODS）。

    【解析方式】不能用「`{ ... },` 到下一个 `},`」的贪婪正则 ——
    注册表里有的条目带嵌套对象（`write: { kind: ... }`），
    贪婪匹配会把两条并成一条（实测漏掉 `yue2_struct`）。
    这里改成**按 `id:` 字段切块**：每条注册项都以 `id: "xxx"` 开头，
    切块后从「本条 id」到「下一条 id 之前」取内容 —— 不会串条目。
    """
    src = read(WEB / "enhance-menu.js")
    start = src.find("const ENHANCE_METHODS")
    if start < 0:
        fail("web/enhance-menu.js 里找不到 ENHANCE_METHODS 注册表")
    body = src[start:]
    # 找到注册表的结束（下一个顶层 `];`）
    end = body.find("\n  ];")
    if end > 0:
        body = body[:end]
    # 每个注册项以 `id: "xxx"` 开头
    id_positions = [(m.start(), m.group(1)) for m in
                    re.finditer(r'\bid:\s*"([a-z0-9_]+)"', body)]
    if not id_positions:
        fail("增强方式注册表解析不到任何 id")
    out = []
    for i, (pos, mid) in enumerate(id_positions):
        seg_end = id_positions[i + 1][0] if i + 1 < len(id_positions) else len(body)
        seg = body[pos:seg_end]
        label = re.search(r'label:\s*"([^"]+)"', seg)
        owner = re.search(r'owner:\s*"([^"]+)"', seg)
        spec = re.search(r'specId:\s*"([^"]*)"', seg)
        kind = re.search(r'kind:\s*"([^"]+)"', seg)
        modes = re.search(r'modes:\s*\[([^\]]*)\]', seg)
        out.append({
            "id": mid,
            "label": label.group(1) if label else mid,
            "owner": owner.group(1) if owner else "",
            "spec_id": spec.group(1) if spec else "",
            "kind": kind.group(1) if kind else "",
            "modes": [m.strip().strip('"\'') for m in modes.group(1).split(",")
                      if m.strip()] if modes else [],
        })
    if not out:
        fail("增强方式注册表解析为空")
    return out


# --------------------------------------------------------------------------
# 5. 模板渲染
# --------------------------------------------------------------------------
def render_template(template_path: Path, context: dict) -> str:
    """把模板里的 @KEY@ 占位符替换成派生内容。

    规则：
      * 模板里出现的每个占位符必须在 context 里 —— 否则直接报错；
      * context 里多出来的键**不算错**（两张模板共用一个 context）。
    """
    tpl = read(template_path)
    used = set(SLOT_RE.findall(tpl))
    unknown = sorted(k for k in used if k not in context)
    if unknown:
        fail(f"模板 {template_path.name} 引用了未知的占位符：{unknown}")
    text = SLOT_RE.sub(lambda m: str(context.get(m.group(1), "")), tpl)
    text = sanitize_mermaid_comments(text)
    # 自检：裸 %% 行必须在生成阶段被清掉，否则 Mermaid 解析器会炸
    for i, line in enumerate(text.split("\n"), 1):
        if line.strip() == "%%":
            fail(f"模板 {template_path.name} 第 {i} 行是裸 `%%`，"
                 "会触发 Mermaid 解析错误（已尝试自动修正但未生效）")
    return text.strip() + "\n"


# --------------------------------------------------------------------------
# 6. 派生清单（自动表格 = 与代码的对应证据）
# --------------------------------------------------------------------------
def build_derived_md(data: dict) -> str:
    L: list[str] = []
    add = L.append
    add("# 交互流程图 · 派生清单（自动生成）")
    add("")
    add("> 本文件由 `scripts/gen_flow.py` 生成，**请勿手改**。")
    add("> 每一张表都是**从代码读出来的**，与流程图里的节点一一对应 ——")
    add("> 代码改了，重跑脚本（`make flow`）这张表和流程图会一起更新。")
    add("")
    add("## 1. 生成模式（来源：`h3studio/gen_types.py`）")
    add("")
    add("| mode | 显示名 | 媒体 | 界面分类 | 素材槽位（单/多） | 槽位上限 |")
    add("|---|---|---|---|---|---|")
    slots = data["slots"]
    for t in data["types"]:
        s = slots.get(t["key"], {})
        single = "、".join(s.get("single", [])) or "—"
        multi = "、".join(s.get("multi", [])) or "—"
        lim = "、".join(f"{k}≤{v}" for k, v in (s.get("max_per_slot") or {}).items()) or "—"
        add(f"| `{t['key']}` | {md_cell(t['label'])} | {t['media']} | {t['category']} "
            f"| {single} / {multi} | {lim} |")
    add("")
    add("## 2. 视频类别与模型（来源：`h3studio/video_categories.py`）")
    add("")
    add("| 类别 | 显示名 | 模型项 | mode |")
    add("|---|---|---|---|")
    for cat in data["video_categories"]:
        for m in cat["models"]:
            add(f"| `{cat['key']}` | {md_cell(cat['label'])} | {md_cell(m['label'])} | `{m['mode']}` |")
    add("")
    add("## 3. 左侧导航项（来源：`web/index.html`）")
    add("")
    add("| 标签 | mode | 元素 id |")
    add("|---|---|---|")
    for n in data["nav_items"]:
        add(f"| {md_cell(n['label'])} | {('`'+n['mode']+'`') if n['mode'] else '—'} | "
            f"{('`'+n['id']+'`') if n['id'] else '—'} |")
    add("")
    add("## 4. 参数面板字段（来源：`web/index.html`）")
    add("")
    for pid, labels in data["panels"].items():
        add(f"### `{pid}`")
        add("")
        for lb in labels:
            add(f"- {lb}")
        add("")
    add("## 5. 提示词增强方式（来源：`web/enhance-menu.js`）")
    add("")
    add("> 前端的「增强方式」与后端的「模板」是**两套清单**，数量不同 ——")
    add("> 下表是前端下拉里的增强方式；后端模板清单见 §5b。")
    add("")
    add("| id | 显示名 | 归属功能 | 模板 id | 写回类型 | 可用模式 |")
    add("|---|---|---|---|---|---|")
    for e in data["enhance_methods"]:
        modes = "、".join(f"`{m}`" for m in e.get("modes", [])) or "全部模式"
        add(f"| `{e['id']}` | {md_cell(e['label'])} | {md_cell(e['owner']) or '通用'} "
            f"| {('`'+e['spec_id']+'`') if e['spec_id'] else '动态'} | {e['kind'] or '—'} | {modes} |")
    add("")
    add("## 5b. 后端提示词增强模板（来源：`h3studio/enhancer.py` 的 TEMPLATE_SPECS）")
    add("")
    add("| 模板 id | 显示名 |")
    add("|---|---|")
    for t in data["enhancer_templates"]:
        add(f"| `{t['id']}` | {md_cell(t['label'])} |")
    add("")
    add("## 6. 内置 Agent 工具（来源：`h3studio/agent.py`）")
    add("")
    add("| 工具 | 界面动作短语 | 说明 |")
    add("|---|---|---|")
    for t in data["agent_tools"]:
        add(f"| `{t['name']}` | {md_cell(t['stage']) or '—'} | {md_cell(t['description'])} |")
    add("")
    add("## 7. 后端 API 路由表（来源：`server.py` / `h3studio/asset_api.py` / `h3studio/ref_api.py`）")
    add("")
    add("| 方法 | 路径 | 处理函数 | 前端直接调用 | 来源文件 |")
    add("|---|---|---|---|---|")
    for r in data["routes"]:
        add(f"| {r['method']} | `{r['path']}` | {('`'+r['handler']+'`') if r['handler'] else '—'} "
            f"| {'✅' if r['ui_reachable'] else '—'} | `{r['source']}` |")
    add("")
    add("## 8. 前端实际调用的接口（来源：`web/*.js` 字符串扫描）")
    add("")
    add("| 前端调用路径 | 匹配到的后端路由 |")
    add("|---|---|")
    for c in data["ui_calls"]:
        r = data["ui_call_map"].get(c)
        add(f"| `{c}` | {('`'+r['method']+' '+r['path']+'`') if r else '⚠️ 未匹配到路由'} |")
    add("")
    add("## 9. 卡片 / 菜单动作（来源：`web/*.js` 的 `data-act`）")
    add("")
    for fn, acts in data["actions"].items():
        add(f"- **`web/{fn}`**：" + "、".join(f"`{a}`" for a in acts))
    add("")
    return "\n".join(L)


# --------------------------------------------------------------------------
# 7. 渲染（mmdc）
# --------------------------------------------------------------------------
def find_mmdc() -> str | None:
    p = shutil.which("mmdc")
    if p:
        return p
    for base in sorted(Path("/root/.nvm/versions/node").glob("*/bin/mmdc")):
        return str(base)
    return None


def find_chrome() -> str | None:
    env = os.environ.get("PUPPETEER_EXECUTABLE_PATH")
    if env and Path(env).exists():
        return env
    for name in ("google-chrome-stable", "google-chrome", "chromium", "chromium-browser"):
        p = shutil.which(name)
        if p:
            return p
    cache = Path.home() / ".cache" / "puppeteer"
    if cache.exists():
        for cand in sorted(cache.glob("chrome-*/chrome-linux64/chrome")):
            return str(cand)
    return None


def render(mmd: Path, out_base: Path) -> list[Path]:
    mmdc = find_mmdc()
    if not mmdc:
        print("[gen_flow] 警告：找不到 mmdc（mermaid-cli），跳过渲染。"
              "安装：npm i -g @mermaid-js/mermaid-cli")
        return []
    chrome = find_chrome()
    cfg = None
    if chrome:
        cfg = Path(tempfile.gettempdir()) / "gen_flow_puppeteer.json"
        cfg.write_text(json.dumps({
            "executablePath": chrome,
            "args": ["--no-sandbox", "--disable-setuid-sandbox",
                     "--disable-dev-shm-usage", "--disable-gpu"],
        }), encoding="utf-8")
    made = []
    for ext in ("svg", "png"):
        out = out_base.with_suffix("." + ext)
        cmd = [mmdc, "-i", str(mmd), "-o", str(out), "-b", "white"]
        if ext == "png":
            cmd += ["-s", "2"]
        if cfg:
            cmd += ["-p", str(cfg)]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if proc.returncode != 0:
            fail(f"渲染失败（{ext}）：\n{proc.stdout}\n{proc.stderr}")
        if not out.exists() or out.stat().st_size < 512:
            fail(f"渲染产物异常：{out}")
        made.append(out)
    return made


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="生成《人机交互流程图》")
    ap.add_argument("--render", action="store_true", help="同时渲染 SVG / PNG")
    ap.add_argument("--check", action="store_true",
                    help="校验已提交文件与当前代码一致（不写文件）")
    args = ap.parse_args()

    _bootstrap_path()

    routes = parse_routes()
    ui_calls = scan_ui_api_calls()
    ui_call_map = {}
    for c in ui_calls:
        r = match_route(c, routes)
        if r:
            ui_call_map[c] = r
    # 注意：这是**路径级**的可达性 —— 前端字面量里不带方法，
    # 所以同一个路径上 GET / DELETE 两个路由都会标成「前端引用到」。
    ui_reachable_paths = {r["path"] for r in ui_call_map.values()}
    for r in routes:
        r["ui_reachable"] = r["path"] in ui_reachable_paths

    types = derive_generation_types()
    categories = derive_categories()
    video_categories = derive_video_categories()
    slots = derive_slots()
    agent_tools = derive_agent_tools()
    nav_items = derive_nav_items()
    panels = derive_panel_fields()
    actions = derive_actions()
    enhance_methods = derive_enhance_methods()
    enhancer_templates = derive_enhancer_templates()

    data = {
        "routes": routes, "ui_calls": ui_calls, "ui_call_map": ui_call_map,
        "types": types, "categories": categories, "video_categories": video_categories,
        "slots": slots, "agent_tools": agent_tools, "nav_items": nav_items,
        "panels": panels, "actions": actions, "enhance_methods": enhance_methods,
        "enhancer_templates": enhancer_templates,
    }

    ctx = build_context(data)
    outputs = {}
    for tpl, out in TEMPLATES:
        outputs[out] = render_template(tpl, ctx)
    outputs[OUT_LIST_MD] = build_derived_md(data)

    if args.check:
        bad = []
        for path, content in outputs.items():
            if not path.exists():
                bad.append(f"{path.relative_to(ROOT)} 不存在")
            elif read(path) != content:
                bad.append(f"{path.relative_to(ROOT)} 与当前代码不一致（需重跑 make flow）")
        if bad:
            print("[gen_flow] 检查未通过：")
            for b in bad:
                print("  - " + b)
            return 1
        print("[gen_flow] 检查通过：已提交的流程图与当前代码一致。")
        return 0

    for path, content in outputs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        print(f"[gen_flow] 写出 {path.relative_to(ROOT)}")

    if args.render:
        for _, out in TEMPLATES:
            for made in render(out, out.with_suffix("")):
                print(f"[gen_flow] 渲染 {made.relative_to(ROOT)} "
                      f"({made.stat().st_size} bytes)")
    return 0


# --------------------------------------------------------------------------
# 上下文：把派生数据变成模板占位符
# --------------------------------------------------------------------------
MODE_PANEL = {
    "t2v": ("P_VIDEO", "文生视频面板"),
    "flf": ("P_LEGACY_VIDEO", "旧面板 + 采样参数区块"),
    "r2v": ("P_LEGACY_VIDEO", "旧面板 + 采样参数区块"),
    "longfilm": ("P_STITCH", "长片拼接面板"),
    "t2i": ("P_IMAGE", "图像面板"),
    "t2i_qwen": ("P_IMAGE", "图像面板"),
    "t2i_moss": ("P_IMAGE", "图像面板"),
    "t2i_h3image": ("P_IMAGE", "图像面板"),
    "i2i_qwen": ("P_IMAGE", "图像面板"),
    "i2i_h3image": ("P_IMAGE", "图像面板"),
    "t2a": ("P_MUSIC", "音乐面板"),
    "a2a": ("P_MUSIC", "音乐面板"),
}


def build_context(data: dict) -> dict:
    ctx: dict = {}

    # ---- 导航 ----
    nav_pages = []
    for n in data["nav_items"]:
        if n["mode"]:
            continue
        nav_pages.append(n["label"])
    ctx["NAV_PAGES"] = "、".join(nav_pages)

    # ---- 模式节点（按界面分类分组，派生自 gen_types.py）----
    cat_label = {c["key"]: c["label"] for c in data["categories"]}
    by_cat: dict[str, list[dict]] = {}
    for t in data["types"]:
        by_cat.setdefault(t["category"] or "other", []).append(t)

    def mode_block(cat_key: str) -> str:
        items = by_cat.get(cat_key, [])
        if not items:
            return "      （无）"
        lines = []
        for t in items:
            slot = data["slots"].get(t["key"], {})
            bits = [f"mode={t['key']}"]
            if slot.get("single") or slot.get("multi"):
                s = "、".join(slot.get("single", []) + slot.get("multi", []))
                bits.append(f"素材槽位 {s}")
            lines.append(
                f'      M_{t["key"]}["{mermaid_label(t["label"])}<br/>' + "<br/>".join(bits) + '"]')
        return "\n".join(lines)

    ctx["MODE_NODES_VIDEO"] = mode_block("video")
    ctx["MODE_NODES_T2I"] = mode_block("t2i")
    ctx["MODE_NODES_I2I"] = mode_block("i2i")
    ctx["MODE_NODES_MUSIC"] = mode_block("music")
    ctx["CAT_LABEL_VIDEO"] = mermaid_label(cat_label.get("video", "视频"))
    ctx["CAT_LABEL_T2I"] = mermaid_label(cat_label.get("t2i", "文生图"))
    ctx["CAT_LABEL_I2I"] = mermaid_label(cat_label.get("i2i", "图片编辑"))
    ctx["CAT_LABEL_MUSIC"] = mermaid_label(cat_label.get("music", "音乐"))

    # ---- 模式 -> 面板 连线（派生：新增 mode 自动补一条边）----
    links = []
    for t in data["types"]:
        target, _ = MODE_PANEL.get(t["key"], ("P_IMAGE", ""))
        links.append(f"  M_{t['key']} --> {target}")
    ctx["MODE_TO_PANEL"] = "\n".join(links)

    # ---- 面板字段 ----
    def panel_lines(pid: str, node: str) -> str:
        labels = data["panels"].get(pid, [])
        body = "<br/>".join(mermaid_label(x) for x in labels)
        return f'  {node}["{body}"]'

    ctx["PANEL_VIDEO"] = panel_lines("videoPanel", "P_VIDEO")
    ctx["PANEL_IMAGE"] = panel_lines("imagePanel", "P_IMAGE")
    ctx["PANEL_MUSIC"] = panel_lines("musicPanel", "P_MUSIC")
    ctx["PANEL_STITCH"] = panel_lines("stitchPanel", "P_STITCH")

    # ---- 上传区 ----
    ctx["UPLOAD_ZONE"] = (
        '  P_UPLOAD["上传区 #uploadZone<br/>flf：首帧 + 尾帧<br/>'
        'r2v：参考图 ×3<br/>i2i / a2a：参考素材必须来自素材库"]')

    # ---- 增强方式 ----
    enh_lines = []
    for e in data["enhance_methods"]:
        owner = f" · {mermaid_label(e['owner'])}" if e["owner"] else ""
        enh_lines.append(f'    E_{e["id"]}["{mermaid_label(e["label"])}{owner}<br/>'
                         f'spec={e["spec_id"] or "动态"}"]')
    ctx["ENHANCE_NODES"] = "\n".join(enh_lines)

    # ---- Agent 工具节点（按作用对象分组，派生自 agent.py 的 TOOLS）----
    groups = group_agent_tools(data["agent_tools"])
    # 分组 id 固定：TG1..TG6 对应 TOOL_GROUP_RULES 的顺序，TG7 = 其它工具。
    # id 固定是为了让「工具分栏 → 落地侧」的连线也能自动生成。
    group_ids: dict[str, str] = {}
    tool_blocks: list[str] = []
    side_edges: list[str] = []
    group_side = {
        "素材类": "AS", "任务类": "JB", "模式与参数类": "MP",
        "提示词类": "PP", "会话与记忆类": "CM", "回应类": "RS",
        # 2026-11-16 新增分栏的落地侧（见 templates/flow-agent.mmd 的 SIDE 子图）
        "媒体库管理类": "ML", "外部能力类": "EX", "ffmpeg 音视频类": "FF",
        "其它工具": "AS",
    }
    idx = 0
    for group_name, _ in TOOL_GROUP_RULES:
        items = groups.get(group_name) or []
        if not items:
            continue
        idx += 1
        gid = f"TG{idx}"
        group_ids[group_name] = gid
        lines = [f'    subgraph {gid}["{mermaid_label(group_name)}"]', "      direction TB"]
        for t in items:
            stage = mermaid_label(t["stage"]) if t["stage"] else mermaid_label(t["name"])
            lines.append(f'      T_{t["name"]}["{stage}<br/><code>{t["name"]}</code>"]')
        lines.append("    end")
        tool_blocks.append("\n".join(lines))
        side_edges.append(f"  {gid} --> {group_side.get(group_name, 'AS')}")
    others = groups.get("其它工具") or []
    if others:
        idx += 1
        gid = f"TG{idx}"
        group_ids["其它工具"] = gid
        lines = [f'    subgraph {gid}["其它工具"]', "      direction TB"]
        for t in others:
            stage = mermaid_label(t["stage"]) if t["stage"] else mermaid_label(t["name"])
            lines.append(f'      T_{t["name"]}["{stage}<br/><code>{t["name"]}</code>"]')
        lines.append("    end")
        tool_blocks.append("\n".join(lines))
        side_edges.append(f"  {gid} --> AS")
    ctx["AGENT_TOOL_NODES"] = "\n".join(tool_blocks)
    ctx["AGENT_TOOL_SIDE_EDGES"] = "\n".join(side_edges)

    # ---- API 节点（UI 可达的那批）----
    api_lines = []
    for r in data["routes"]:
        if not r["ui_reachable"]:
            continue
        api_lines.append(f'    API_{re.sub(r"[^A-Za-z0-9]", "_", r["method"] + r["path"])}'
                         f'["{r["method"]} {mermaid_label(r["path"])}"]')
    ctx["API_NODES"] = "\n".join(api_lines)

    # ---- 卡片动作（换行排版：动作多时一行会溢出节点框）----
    def acts_of(filename: str) -> list[str]:
        return data["actions"].get(filename, [])

    def acts_block(filename: str, per_line: int = 4) -> str:
        """把 data-act 列表洗成多行 <br/> 文本，避免单行过长撑爆节点框。"""
        acts = acts_of(filename)
        if not acts:
            return "（无）"
        lines = []
        for i in range(0, len(acts), per_line):
            lines.append("、".join(f"`{a}`" for a in acts[i:i + per_line]))
        return "<br/>".join(lines)

    ctx["JOB_CARD_ACTIONS"] = acts_block("app.js")
    ctx["ASSET_CARD_ACTIONS"] = acts_block("assets.js")
    ctx["PROVIDER_ACTIONS"] = acts_block("providers.js")
    ctx["AGENT_ACTIONS"] = acts_block("agent.js")
    ctx["SESSION_ACTIONS"] = acts_block("agent_sessions.js")

    # ---- 素材槽位混合输入 ----
    mixed = []
    for mode, plan in data["slots"].items():
        ms = plan.get("media_slots") or {}
        if len(ms) > 1:
            mixed.append(f"{mode}：" + "、".join(f"{k}→{v}" for k, v in ms.items()))
    ctx["MIXED_SLOTS"] = "<br/>".join(mermaid_label(x) for x in mixed) or "（无）"

    # ---- 统计 ----
    ctx["STAT_TYPES"] = str(len(data["types"]))
    ctx["STAT_ROUTES"] = str(len(data["routes"]))
    ctx["STAT_TOOLS"] = str(len(data["agent_tools"]))
    ctx["STAT_UI_CALLS"] = str(len(data["ui_calls"]))
    ctx["STAT_NAV"] = str(len(data["nav_items"]))
    ctx["STAT_ENH_SPECS"] = str(len(data["enhance_methods"]))
    ctx["STAT_TPL_SPECS"] = str(len(data["enhancer_templates"]))

    return ctx


if __name__ == "__main__":
    sys.exit(main())
