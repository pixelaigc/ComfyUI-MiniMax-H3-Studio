# -*- coding: utf-8 -*-
"""
H3 Studio 文档静态托管 —— 把 docs/ 挂到 /docs，供界面里点开流程图与交付说明。

【为什么单独成文件（而不是塞进 server.py）】
  server.py 正被并发任务修改（图像/LoRA 面板），文档托管是**独立**的一条链路：
  一个目录 + 一个路由 + 一份 mimetype 表。它自己有必须单独验证的契约
  （中文文件名 / 目录穿越 / mimetype / 目录索引），放在 server.py 里
  没法单独起测试服务器。冲突面也最小。

【契约（每条都有对应的回归测试，见 tests/test_docs_static.py）】
  1. 中文文件名可用：URL 里的 %XX 由 aiohttp 解码后交给本模块，
     本模块再按「解码后的真实路径」做存在性判断与越界检查。
  2. 目录穿越一律 404：无论 ../ 还是 %2e%2e%2f 还是 %2f，
     解析后的路径必须仍在 docs/ 之内。
  3. mimetype 正确：.png -> image/png、.svg -> image/svg+xml、
     .md -> text/markdown; charset=utf-8、.mmd -> text/plain; charset=utf-8。
     【为什么 .mmd 要显式指定】Python 的 mimetypes 把 .mmd 认成
     application/vnd.chipnuts.karaoke-mmd（卡拉OK格式），会让浏览器
     当成二进制下载而不是可读文本 —— 源文件要看内容，必须纠正。
  4. 目录访问返回**索引列表**（不是 403），且列表里的中文名是
     正确的百分号编码链接（浏览器点得开）。
  5. 缺文件一律 404，不泄露文件系统路径。

【为什么不用 aiohttp 的 add_static】
  add_static 的 show_index 索引是英文模板、没有中文名的高亮；
  mimetype 由 mimetypes 猜（.mmd 猜错）；而且它对「目录」的语义是
  403（无 show_index 时）。这里自己实现，语义更贴合「给人看的文档目录」。
"""
import mimetypes
import urllib.parse
from html import escape
from pathlib import Path

from aiohttp import web

#: app 上的键：文档根目录。用 AppKey 而不是裸字符串 —— aiohttp 3.9+ 会对
#: 裸字符串键发 NotAppKeyWarning（不同 app 共用同一个字符串容易撞车）。
DOCS_ROOT_KEY = web.AppKey("h3studio_docs_root", Path)

# ------------------------------------------------------------------ 配置

#: 文档根目录名（相对 server.py 所在目录）
DOCS_DIRNAME = "docs"

#: 扩展名 -> Content-Type。
#:
#: 【为什么不用 mimetypes.guess_type】它把 .mmd 认成
#: application/vnd.chipnuts.karaoke-mmd（卡拉OK伴唱格式），
#: 浏览器会把 Mermaid 源文件当二进制下载 —— 必须纠正成 text/plain。
DOCS_CONTENT_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".md": "text/markdown; charset=utf-8",
    ".mmd": "text/plain; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".html": "text/html; charset=utf-8",
}

#: 目录索引里要展示的后缀（其余文件不展示，避免把证据目录也列出去）
INDEXABLE_SUFFIXES = {".png", ".svg", ".md", ".mmd", ".txt", ".pdf", ".json"}

#: 目录索引每页最多列多少项（docs/ 下有 100+ 文件，全列出来没法看）
INDEX_MAX_ENTRIES = 500


# ------------------------------------------------------------------ 工具


def _decode_url_path(raw: str) -> str:
    """把 URL 路径解码成「相对目录的路径」。

    【为什么不能直接用 request.match_info["path"]】aiohttp 的路由层
    已经做过一次百分号解码（含 %2f -> /），但它解码的是**整段**，
    没有做归一化。这里再解一次是为了兼容「前端把文件名整体 encodeURIComponent」
    这种用法 —— 但必须用 errors="replace" 且拒绝解码出 NUL 之类控制字符。

    返回值是相对路径（不含前导 /），可能包含 .. —— 调用方必须做越界检查。
    """
    if not raw:
        return ""
    # 反复解码直到不再变化（双重编码的情况）
    # 上限 3 轮：再多就是恶意构造，直接停手交给越界检查处理。
    path = raw
    for _ in range(3):
        decoded = urllib.parse.unquote(path, encoding="utf-8", errors="replace")
        if decoded == path:
            break
        path = decoded
    return path


def _safe_join(root: Path, rel_path: str):
    """把相对路径安全地拼到 root 下。

    返回 (绝对路径, 错误码)：
      * 成功 -> (Path, None)
      * 越界（目录穿越）-> (None, "traversal")
      * 根目录本身 -> (None, "root")

    【越界判定为什么要用 relative_to 而不是字符串前缀比较】
    字符串前缀比较会把 /root/docs-evil 误判成 /root/docs 的子目录
    （经典前缀匹配漏洞）。relative_to() 是按**路径分段**比较的，
    /root/docs-evil 相对 /root/docs 一定抛 ValueError。
    """
    # 归一化：把 ../ 与 . 折叠掉
    parts = []
    for seg in rel_path.replace("\\", "/").split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            # 直接弹栈；弹到空就说明要跑出根目录了
            if not parts:
                return None, "traversal"
            parts.pop()
            continue
        parts.append(seg)
    if not parts:
        return None, "root"
    target = (root / "/".join(parts)).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return None, "traversal"
    return target, None


def _content_type(path: Path) -> str:
    """按扩展名给 Content-Type；未登记的扩展名按二进制流返回。"""
    suffix = path.suffix.lower()
    if suffix in DOCS_CONTENT_TYPES:
        return DOCS_CONTENT_TYPES[suffix]
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


# ------------------------------------------------------------------ 索引页

def _render_index(root: Path, rel_dir: str, entries) -> str:
    """渲染目录索引页。

    【为什么自己写而不用 aiohttp 的 show_index】
    aiohttp 的索引是英文的 "Index of /."，对中文文档目录不友好；
    而且它把**所有**文件都列出来（含子目录里的证据图片），
    这里只列「文档类」后缀，并把子目录也列出来（可点进去）。

    【为什么 href 用「从 /docs 起算的绝对路径」而不是相对路径】
    相对路径（href="xxx.png"）在 /docs（**没有**末尾斜杠）下会被浏览器
    解析成 /xxx.png —— 少了一层，直接 404。用户点索引里的中文链接
    会全部失效。所以这里统一拼成 /docs/<相对路径>，与末尾有没有斜杠无关。
    """
    title = escape(rel_dir or "/")
    rows = []
    if rel_dir:
        # 上一级：仍然锚在 /docs 下（不是真的文件系统根）
        rows.append('<li class="up"><a href="/docs/">↑ 文档根目录</a></li>')
    for name, is_dir, size in entries:
        # 关键：href 用 quote 编码，浏览器点得开；文本用 escape 防 XSS
        quoted = urllib.parse.quote(name)
        if rel_dir:
            href = "/docs/%s/%s" % (urllib.parse.quote(rel_dir), quoted)
        else:
            href = "/docs/%s" % quoted
        if is_dir:
            rows.append('<li class="dir"><a href="%s/">%s/</a></li>' % (href, escape(name)))
        else:
            rows.append('<li class="file"><a href="%s">%s</a> <span class="size">%s</span></li>'
                        % (href, escape(name), _fmt_size(size)))
    body = "\n".join(rows) or '<li class="empty">（空目录）</li>'
    return """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>文档目录 %s</title>
<style>
 body{font-family:system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
      margin:0;padding:24px;background:#f5f6f8;color:#1a1d21}
 h1{font-size:17px;margin:0 0 14px}
 ul{list-style:none;margin:0;padding:0;background:#fff;border:1px solid #e6e8ec;
    border-radius:12px;overflow:hidden;max-width:860px}
 li{border-bottom:1px solid #e6e8ec}
 li:last-child{border-bottom:0}
 a{display:block;padding:10px 14px;color:#1a1d21;text-decoration:none;font-size:13px}
 a:hover{background:#eef1fe;color:#4f6ef7}
 li.dir a{font-weight:600}
 li.up a{color:#8a919c}
 .size{float:right;color:#8a919c;font-size:11.5px}
 .empty{padding:14px;color:#8a919c;font-size:13px}
</style></head><body>
<h1>文档目录 %s</h1>
<ul>%s</ul>
</body></html>""" % (title, title, body)


def _fmt_size(size: int) -> str:
    if size < 1024:
        return "%d B" % size
    if size < 1024 * 1024:
        return "%.1f KB" % (size / 1024)
    return "%.1f MB" % (size / (1024 * 1024))


# ------------------------------------------------------------------ handler


def _list_entries(target: Path):
    """列出目录下的可展示项：子目录 + 文档类文件。"""
    entries = []
    try:
        items = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name))
    except OSError:
        return entries
    for p in items:
        try:
            if p.is_dir():
                entries.append((p.name, True, 0))
                continue
            if p.suffix.lower() not in INDEXABLE_SUFFIXES:
                continue
            entries.append((p.name, False, p.stat().st_size))
        except OSError:
            continue
    return entries[:INDEX_MAX_ENTRIES]


async def docs_static_handler(request):
    """GET /docs[/...] —— 返回 docs/ 下的文件或目录索引。

    路由形态：``app.router.add_get("/docs", h)`` + ``add_get("/docs/{path:.+}", h)``。
    """
    root = Path(request.app[DOCS_ROOT_KEY])
    raw = request.match_info.get("path", "")
    rel = _decode_url_path(raw)

    if rel == "":
        # /docs 或 /docs/ -> 目录索引
        if not root.is_dir():
            raise web.HTTPNotFound()
        body = _render_index(root, "", _list_entries(root))
        return web.Response(text=body, content_type="text/html", charset="utf-8")

    target, err = _safe_join(root, rel)
    if err is not None or target is None:
        # 越界 / 根目录 -> 一律 404（不泄露路径存在性）
        raise web.HTTPNotFound()

    if target.is_dir():
        body = _render_index(root, rel, _list_entries(target))
        return web.Response(text=body, content_type="text/html", charset="utf-8")

    if not target.is_file():
        raise web.HTTPNotFound()

    # 显式给 Content-Type + Content-Length（FileResponse 会自己算长度，
    # 但 mimetype 必须由我们指定，否则 .mmd 会被猜成卡拉OK格式）
    resp = web.FileResponse(target, headers={"Cache-Control": "no-cache"})
    resp.content_type = _content_type(target)
    return resp


# ------------------------------------------------------------------ 流程图清单

#: 5 套流程图的前缀（不含扩展名）。服务端按这个前缀**实际到磁盘上找**，
#: 找到才下发 —— 所以文件没进仓库时前端会拿到空列表（而不是一堆 404 链接）。
FLOW_PREFIXES = [
    "交互流程图-总览",
    "交互流程图-创作链路",
    "交互流程图-素材库",
    "交互流程图-内置Agent",
    "交互流程图-系统配置",
]

#: 说明文档文件名
FLOW_GUIDE_NAME = "交互流程图说明.md"


def _flow_url(filename: str) -> str:
    """把文件名变成可点的 URL（中文名做百分号编码）。"""
    return "/docs/" + urllib.parse.quote(filename)


def build_flow_manifest(root: Path):
    """产出「5 套流程图 + 说明文档」的清单。

    【为什么要走接口而不是让前端硬编码清单】
    前端硬编码的话，流程图文件一旦改名/增删，前端会静默指向不存在的
    路径（用户点了 404，却没有任何报错）。这里由服务端**按磁盘实际内容**
    生成清单：文件不在就不下发，前端也就不会列出坏链接。

    返回结构：
      {"diagrams": [{"name","png","svg","mmd","title"}...],
       "guide": {"name","url"} 或 None,
       "docs_index_url": "/docs"}
    """
    root = Path(root)
    diagrams = []
    for prefix in FLOW_PREFIXES:
        png = root / (prefix + ".png")
        if not png.is_file():
            continue
        entry = {
            "name": prefix,
            "title": prefix.replace("交互流程图-", ""),
            "png": _flow_url(prefix + ".png"),
        }
        svg = root / (prefix + ".svg")
        if svg.is_file():
            entry["svg"] = _flow_url(prefix + ".svg")
        mmd = root / (prefix + ".mmd")
        if mmd.is_file():
            entry["mmd"] = _flow_url(prefix + ".mmd")
        diagrams.append(entry)

    guide = None
    guide_path = root / FLOW_GUIDE_NAME
    if guide_path.is_file():
        guide = {"name": FLOW_GUIDE_NAME, "url": _flow_url(FLOW_GUIDE_NAME)}

    return {
        "diagrams": diagrams,
        "guide": guide,
        "docs_index_url": "/docs",
    }


async def api_docs_flows(request):
    """GET /api/docs/flows —— 5 套流程图 + 说明文档的清单。"""
    root = Path(request.app[DOCS_ROOT_KEY])
    return web.json_response(build_flow_manifest(root))


def register(app, docs_root: Path):
    """把文档目录挂到 app 上。

    路由顺序：先精确的 /docs，再通配的 /docs/{path:.+}。
    """
    root = Path(docs_root).resolve()
    app[DOCS_ROOT_KEY] = root
    app.router.add_get("/docs", docs_static_handler)
    app.router.add_get("/docs/{path:.+}", docs_static_handler)
    # 界面里的「流程图」入口靠这个接口拿清单（前端不硬编码文件名）
    app.router.add_get("/api/docs/flows", api_docs_flows)
    return root
