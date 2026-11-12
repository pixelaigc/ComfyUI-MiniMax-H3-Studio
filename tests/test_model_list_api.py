# -*- coding: utf-8 -*-
"""文生视频面板「模型清单以 ComfyUI API 为准」的回归测试。

用户 2026-11-11 报的缺陷：
  「文生视频」面板的 MiniMax-H3 模型区下方红字提示
  「权重在外硬盘 /mnt/nvme1t_colorful —— 尚未挂载或 ComfyUI 下拉框确实没有
    这个单卡，提交会被拒绝。」
  这条结论**是错的**：外置盘已挂载，ComfyUI 的模型列表里 5 个 H3 FL2VA
  权重全在（实测 151 条 diffusion_models）。

根因：
  1. 那行红字是 `VIDEO_MODELS[...]["warn"]` 里**写死的常量**，与实际
     可用性判定毫无关系；
  2. `model_available()` 只扫盘，**从未调用过 ComfyUI 的模型列表 API**；
  3. 面板的 `available` 与提交时的校验走**两条不同的路**，可能不一致。

本文件锁死这几条不再回归：

1. **静态 warn 不再声称「获取不到模型」**
   - `VIDEO_MODELS` 里**不得**残留任何「未挂载 / 没有这个名字 / 获取不到」
     类的**无条件**断言（条件句必须由运行时状态生成）。
2. **可用性以 ComfyUI API 为准**
   - ComfyUI 的 `/models/diffusion_models` 认 -> status=available
   - ComfyUI 不认但磁盘上有 -> status=external_missing（禁用 + 如实提示）
   - ComfyUI 不认且磁盘上也没有 -> status=missing
   - ComfyUI 连不上 -> 降级到扫盘，payload 标 filesystem_fallback + 原因
3. **面板与提交同源**
   - `model_status()` 与 `list_models()` 对同一 key 必须给**同一个**结论。
4. **下拉显示 = 提交所用**
   - payload 里每个 available 的模型，其 `unet` 必须真的在 ComfyUI 清单里。
5. **后端查询接口**
   - `comfy_models.fetch_model_list` 走真实 HTTP 语义（用假服务器验证）
   - `/models/unet` 这种不存在的路由要报错而不是静默返回空
   - `api_comfy_models` 端点标出每个面板模型 listed=True/False

运行：python3 -m pytest tests/test_model_list_api.py -v
"""
import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from h3studio import comfy_models                     # noqa: E402
from h3studio import video_models as VM               # noqa: E402


# ------------------------------------------------------------------
# 一个假的 ComfyUI（本地起 HTTP 服务，不碰真机）
# ------------------------------------------------------------------
class _FakeComfy(BaseHTTPRequestHandler):
    """按路径返回预置 JSON 的假 ComfyUI。"""

    payloads: dict = {}
    hits: list = []

    def do_GET(self):  # noqa: N802
        type(self).hits.append(self.path)
        path = self.path.split("?")[0]
        if path in self.payloads:
            body = json.dumps(self.payloads[path]).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def log_message(self, format, *args):  # 静音
        pass


class FakeComfyServer:
    """起一个假的 ComfyUI，用完即停。"""

    def __init__(self, payloads: dict):
        self.payloads = dict(payloads)
        self._server = HTTPServer(("127.0.0.1", 0), _FakeComfy)
        _FakeComfy.payloads = self.payloads
        _FakeComfy.hits = []
        self.port = self._server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        daemon=True)
        self._thread.start()

    def stop(self):
        self._server.shutdown()
        self._server.server_close()

    @property
    def hits(self):
        return list(_FakeComfy.hits)


# ------------------------------------------------------------------
# 1. 静态文案：不得再有「无条件断言获取不到模型」
# ------------------------------------------------------------------
def _strip_comments(text: str, is_js: bool = False) -> str:
    """剥掉注释与文档字符串，只留**会显示给用户的字面量**。

    Python 用 ast：收集所有字符串字面量，但**排除 docstring**
    （模块/类/函数的说明文字，不是给用户看的文案）；
    JS 用行级启发式（// 与 /* */）剥注释，保留字符串字面量。

    目的：让「源码里还有没有那句错话」的检查**只看真正会显示给用户的
    文案**，而不是命中开发者写的历史根因注释。
    """
    if not is_js:
        import ast
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return text
        # 收集所有 docstring 的节点 id，后面排除
        docstring_nodes = set()
        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            if isinstance(body, list) and body:
                first = body[0]
                if (isinstance(first, ast.Expr)
                        and isinstance(first.value, ast.Constant)
                        and isinstance(first.value.value, str)):
                    docstring_nodes.add(id(first.value))
        out = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and id(node) not in docstring_nodes):
                out.append(node.value)
        return "\n".join(out)
    # JS：逐行剥掉 // 注释与 /* */ 块注释（保持行数不变，便于定位）
    lines = text.splitlines()
    result = []
    in_block = False
    for line in lines:
        stripped = line
        if in_block:
            end = stripped.find("*/")
            if end == -1:
                result.append("")
                continue
            stripped = stripped[end + 2:]
            in_block = False
        while True:
            start = stripped.find("/*")
            slash = stripped.find("//")
            if slash != -1 and (start == -1 or slash < start):
                stripped = stripped[:slash]
                break
            if start != -1:
                stripped = stripped[:start]
                in_block = True
                break
            break
        result.append(stripped)
    return "\n".join(result)


class TestNoHardcodedUnavailableClaim(unittest.TestCase):
    """那行红字不能再是写死的常量。"""

    #: 出现这些词就算「无条件断言模型不可用」
    FORBIDDEN = ("尚未挂载", "根本没有这个名字", "下拉框确实没有",
                 "获取不到", "没有这个单卡", "提交会被拒绝")

    def test_video_models_have_no_forbidden_warn(self):
        for key, meta in VM.VIDEO_MODELS.items():
            warn = meta.get("warn", "") or ""
            for phrase in self.FORBIDDEN:
                self.assertNotIn(
                    phrase, warn,
                    f"模型 {key} 的静态 warn 仍含无条件断言「{phrase}」—— "
                    "可用性必须由 ComfyUI 模型列表现算，不能写死")

    def test_video_models_have_no_forbidden_note(self):
        for key, meta in VM.VIDEO_MODELS.items():
            note = meta.get("note", "") or ""
            for phrase in ("尚未挂载", "根本没有这个名字", "获取不到"):
                self.assertNotIn(
                    phrase, note,
                    f"模型 {key} 的静态 note 仍含「{phrase}」")

    def test_default_model_has_no_static_warn(self):
        """默认模型（用户截图里选中的那个）原来就挂着那句错的话。"""
        default = VM.VIDEO_DEFAULT_MODEL
        warn = VM.VIDEO_MODELS[default].get("warn", "") or ""
        self.assertEqual(
            warn, "",
            "默认模型的静态 warn 应清空（它的可用性由 ComfyUI API 现算）")

    def test_no_source_file_contains_the_old_claim(self):
        """全仓扫一遍：那句写死的断言不能还在**可执行代码**里。

        注意：要排除**注释** —— 源码里必然会引用这句错话来解释
        「以前错在哪」（那是文档，不是给用户看的文案）。
        所以这里先剥掉注释再扫。
        """
        offenders = []
        for rel in ("h3studio/video_models.py", "web/video-panel.js"):
            raw = (REPO / rel).read_text(encoding="utf-8", errors="ignore")
            code = _strip_comments(raw, is_js=rel.endswith(".js"))
            for phrase in ("根本没有这个名字", "下拉框确实没有", "获取不到模型"):
                if phrase in code:
                    offenders.append(f"{rel}: {phrase}")
        self.assertEqual(
            offenders, [],
            f"可执行代码里仍残留写死的「获取不到」断言：{offenders}")

    def test_comments_may_reference_the_old_claim(self):
        """反向自检：确认上面的剥离逻辑真的在工作（不是恒真断言）。

        源码注释里**应该**保留这句错话，用来解释历史根因。
        如果哪天注释里也没了，说明剥离逻辑退化成「什么都扫不到」，
        上面那条测试就成了空断言 —— 这里把它钉住。
        """
        raw = (REPO / "h3studio" / "video_models.py").read_text(
            encoding="utf-8", errors="ignore")
        self.assertIn("根本没有这个名字", raw,
                      "注释里应保留历史根因的原文（供后人对照）")
        code = _strip_comments(raw, is_js=False)
        self.assertNotIn("根本没有这个名字", code,
                         "剥离注释后不应再命中 —— 否则剥离逻辑坏了")
        # 剥离逻辑本身要有判别力：拿一段**含**该短语的可执行字符串，
        # 必须仍然能被扫出来（否则上面全是空断言）
        probe = 'X = "根本没有这个名字"\n'
        self.assertIn("根本没有这个名字", _strip_comments(probe, is_js=False),
                      "剥离逻辑对可执行字面量失效了 —— 检查会变成空断言")
        js_probe = 'const s = "根本没有这个名字";\n'
        self.assertIn("根本没有这个名字", _strip_comments(js_probe, is_js=True),
                      "JS 剥离逻辑对可执行字面量失效了")


# ------------------------------------------------------------------
# 2. 可用性以 ComfyUI API 为准
# ------------------------------------------------------------------
class TestComfyApiIsAuthority(unittest.TestCase):
    def setUp(self):
        VM.reset_comfy_snapshot()

    def tearDown(self):
        VM.reset_comfy_snapshot()

    def _snapshot(self, unets, unet_enum=None, loras=None):
        return VM.ComfyModelSnapshot(
            unets=unets,
            unet_enum=unets if unet_enum is None else unet_enum,
            loras=loras or [])

    def test_comfy_listed_model_is_available(self):
        name = VM.VIDEO_MODELS["fl2va_pruned_int8"]["unet"]
        snap = self._snapshot([name])
        item = next(m for m in VM.list_models(snap)
                    if m["key"] == "fl2va_pruned_int8")
        self.assertEqual(item["status"], "available")
        self.assertTrue(item["available"])
        self.assertTrue(item["comfy_listed"])

    def test_comfy_missing_external_is_external_missing(self):
        """ComfyUI 不认、但文件在外置盘上 -> external_missing（不是 available）。"""
        name = VM.VIDEO_MODELS["fl2va_pruned_int8"]["unet"]
        snap = self._snapshot([])          # ComfyUI 认不到任何一个
        item = next(m for m in VM.list_models(snap)
                    if m["key"] == "fl2va_pruned_int8")
        self.assertEqual(item["status"], "external_missing")
        self.assertFalse(item["available"])
        self.assertIn("不可选", item["status_note"])

    def test_external_missing_note_says_not_unmounted_blame(self):
        """外置盘权重不可选时，提示要说「外置未挂载」而不是「获取不到模型」。"""
        snap = self._snapshot([])
        item = next(m for m in VM.list_models(snap)
                    if m["key"] == "fl2va_pruned_int8")
        self.assertNotIn("获取不到", item["status_note"])
        self.assertIn("外置", item["status_note"])

    def test_available_note_does_not_claim_unavailable(self):
        """ComfyUI 认得的时候，提示**绝不能**说不可用。"""
        name = VM.VIDEO_MODELS["fl2va_pruned_int8"]["unet"]
        snap = self._snapshot([name])
        item = next(m for m in VM.list_models(snap)
                    if m["key"] == "fl2va_pruned_int8")
        for phrase in ("获取不到", "未挂载", "不可选", "根本没有"):
            self.assertNotIn(phrase, item["status_note"],
                             f"可用模型的提示不该出现「{phrase}」")

    def test_payload_reports_source_and_counts(self):
        name = VM.VIDEO_MODELS["fl2va_pruned_int8"]["unet"]
        payload = VM.options_payload(snapshot=self._snapshot([name], loras=["a"]))
        self.assertEqual(payload["model_list_source"], "comfyui_api")
        self.assertEqual(payload["model_list_error"], "")
        self.assertEqual(payload["model_list_counts"]["diffusion_models"], 1)
        self.assertEqual(payload["model_list_counts"]["loras"], 1)

    def test_two_unet_views_are_cross_checked(self):
        """目录视角没有、节点枚举有 -> 仍算 available（并集）。"""
        name = VM.VIDEO_MODELS["fl2va_pruned_int8"]["unet"]
        snap = self._snapshot(unets=[], unet_enum=[name])
        self.assertTrue(snap.has_unet(name))
        item = next(m for m in VM.list_models(snap)
                    if m["key"] == "fl2va_pruned_int8")
        self.assertEqual(item["status"], "available")

    def test_missing_unets_reports_names_not_in_comfy(self):
        snap = self._snapshot([])
        missing = snap.missing_unets()
        self.assertIn(VM.VIDEO_MODELS["fl2va_pruned_int8"]["unet"], missing)


# ------------------------------------------------------------------
# 3. 面板与提交同源（不允许「下拉显示 A、提交用 B」）
# ------------------------------------------------------------------
class TestPanelAndSubmitAgree(unittest.TestCase):
    def setUp(self):
        VM.reset_comfy_snapshot()

    def tearDown(self):
        VM.reset_comfy_snapshot()

    def test_model_status_matches_list_models(self):
        """model_status() 与 list_models() 必须给同一个结论。"""
        names = [VM.VIDEO_MODELS["fl2va_int8"]["unet"]]
        snap = VM.ComfyModelSnapshot(unets=names, unet_enum=names, loras=[])
        for item in VM.list_models(snap):
            self.assertEqual(
                VM.model_status(item["key"], snap), item["status"],
                f"模型 {item['key']} 的两条路径结论不一致")

    def test_available_models_are_actually_in_comfy_list(self):
        """payload 里标 available 的，其 unet 必须真的在 ComfyUI 清单里。"""
        names = [VM.VIDEO_MODELS[k]["unet"] for k in VM.VIDEO_MODELS]
        snap = VM.ComfyModelSnapshot(unets=names, unet_enum=names, loras=[])
        for item in VM.list_models(snap):
            if item["available"]:
                self.assertIn(item["unet"], names,
                              f"{item['key']} 标了可用但不在 ComfyUI 清单里")

    def test_status_is_never_silently_available_when_comfy_denies(self):
        """ComfyUI 明确不认 -> 绝不能标成 available。"""
        snap = VM.ComfyModelSnapshot(unets=[], unet_enum=[], loras=[])
        for item in VM.list_models(snap):
            self.assertFalse(item["available"],
                             f"{item['key']} 被 ComfyUI 否认却仍标 available")
            self.assertNotEqual(item["status"], "available")

    def test_model_available_uses_comfy_api(self):
        """model_available() 必须看 ComfyUI，不能恒为 True。

        （这条单独钉住：model_available 是对外的薄封装，
        很容易在重构中被改成常量返回而测试察觉不到。）
        """
        name = VM.VIDEO_MODELS["fl2va_pruned_int8"]["unet"]
        # ComfyUI 认它
        VM._SNAPSHOT = VM.ComfyModelSnapshot(unets=[name], unet_enum=[name],
                                             loras=[])
        VM._SNAPSHOT_TS = 1e12
        self.assertTrue(VM.model_available("fl2va_pruned_int8"))
        # ComfyUI 不认它（但磁盘上有）-> 必须 False
        VM._SNAPSHOT = VM.ComfyModelSnapshot(unets=[], unet_enum=[], loras=[])
        VM._SNAPSHOT_TS = 1e12
        self.assertFalse(
            VM.model_available("fl2va_pruned_int8"),
            "ComfyUI 不认的模型，model_available() 必须返回 False")

    def test_lora_available_uses_comfy_api(self):
        """lora_available() 同样必须看 ComfyUI 的 /models/loras。"""
        name = VM.VIDEO_ACCEL_LORAS["ckpt850"]["name"]
        VM._SNAPSHOT = VM.ComfyModelSnapshot(unets=[], unet_enum=[],
                                             loras=[name])
        VM._SNAPSHOT_TS = 1e12
        self.assertTrue(VM.lora_available(name))
        VM._SNAPSHOT = VM.ComfyModelSnapshot(unets=[], unet_enum=[], loras=[])
        VM._SNAPSHOT_TS = 1e12
        self.assertFalse(VM.lora_available(name),
                         "ComfyUI 不认的 LoRA，lora_available() 必须返回 False")

    def test_unknown_model_key_is_not_available(self):
        VM._SNAPSHOT = VM.ComfyModelSnapshot(unets=["x"], unet_enum=["x"],
                                             loras=[])
        VM._SNAPSHOT_TS = 1e12
        self.assertFalse(VM.model_available("no_such_model"))


# ------------------------------------------------------------------
# 4. ComfyUI 客户端（真实 HTTP 语义）
# ------------------------------------------------------------------
class TestComfyModelsClient(unittest.TestCase):
    def setUp(self):
        comfy_models.clear_cache()

    def tearDown(self):
        comfy_models.clear_cache()

    def test_fetch_model_list_reads_array(self):
        srv = FakeComfyServer({"/models/diffusion_models": ["a.safetensors", "b"]})
        try:
            names = comfy_models.fetch_model_list(srv.url, "diffusion_models")
            self.assertEqual(names, ["a.safetensors", "b"])
            self.assertIn("/models/diffusion_models", srv.hits)
        finally:
            srv.stop()

    def test_fetch_model_list_raises_on_404(self):
        """/models/unet 这种不存在的路由要**报错**，不能静默返回空。"""
        srv = FakeComfyServer({})
        try:
            with self.assertRaises(comfy_models.ComfyModelListError):
                comfy_models.fetch_model_list(srv.url, "diffusion_models")
        finally:
            srv.stop()

    def test_unknown_folder_rejected_before_http(self):
        with self.assertRaises(comfy_models.ComfyModelListError):
            comfy_models.fetch_model_list("http://127.0.0.1:1", "unet")

    def test_connection_refused_raises(self):
        with self.assertRaises(comfy_models.ComfyModelListError):
            comfy_models.fetch_model_list("http://127.0.0.1:59999",
                                          "diffusion_models", ttl=0)

    def test_fetch_object_info_enum(self):
        srv = FakeComfyServer({
            "/object_info/UNETLoader": {
                "UNETLoader": {
                    "input": {"required": {"unet_name": [["x.safetensors", "y"]]}}}}})
        try:
            opts = comfy_models.fetch_object_info_enum(srv.url, "UNETLoader",
                                                       "unet_name")
            self.assertEqual(opts, ["x.safetensors", "y"])
        finally:
            srv.stop()

    def test_fetch_object_info_enum_missing_node_raises(self):
        srv = FakeComfyServer({"/object_info/Nope": {}})
        try:
            with self.assertRaises(comfy_models.ComfyModelListError):
                comfy_models.fetch_object_info_enum(srv.url, "Nope", "unet_name")
        finally:
            srv.stop()

    def test_cache_avoids_second_http_call(self):
        srv = FakeComfyServer({"/models/loras": ["z"]})
        try:
            comfy_models.fetch_model_list(srv.url, "loras")
            comfy_models.fetch_model_list(srv.url, "loras")
            self.assertEqual(
                sum(1 for h in srv.hits if h == "/models/loras"), 1,
                "同一目录在 TTL 内应命中缓存")
        finally:
            srv.stop()

    def test_force_bypasses_cache(self):
        srv = FakeComfyServer({"/models/loras": ["z"]})
        try:
            comfy_models.fetch_model_list(srv.url, "loras")
            comfy_models.fetch_model_list(srv.url, "loras", force=True)
            self.assertEqual(
                sum(1 for h in srv.hits if h == "/models/loras"), 2)
        finally:
            srv.stop()


# ------------------------------------------------------------------
# 4b. 异步客户端（**服务端实际走的就是这条**）
# ------------------------------------------------------------------
class TestAsyncClient(unittest.IsolatedAsyncioTestCase):
    """异步版客户端 —— 服务端的 refresh_comfy_snapshot_async 用它。

    为什么单独测：同步版在事件循环里会阻塞整个服务，而 ComfyUI
    首次列目录实测 43 秒 —— 必须保证异步版真的不阻塞、且并发发出。
    """

    async def test_async_fetch_model_list(self):
        srv = FakeComfyServer({"/models/diffusion_models": ["a", "b"]})
        try:
            from aiohttp import ClientSession, ClientTimeout
            async with ClientSession(timeout=ClientTimeout(total=10)) as s:
                names = await comfy_models.fetch_model_list_async(
                    s, srv.url, "diffusion_models")
            self.assertEqual(names, ["a", "b"])
        finally:
            srv.stop()

    async def test_async_fetch_raises_on_404(self):
        srv = FakeComfyServer({})
        try:
            from aiohttp import ClientSession, ClientTimeout
            async with ClientSession(timeout=ClientTimeout(total=10)) as s:
                with self.assertRaises(comfy_models.ComfyModelListError):
                    await comfy_models.fetch_model_list_async(
                        s, srv.url, "diffusion_models")
        finally:
            srv.stop()

    async def test_async_connection_refused_raises(self):
        from aiohttp import ClientSession, ClientTimeout
        async with ClientSession(timeout=ClientTimeout(total=5)) as s:
            with self.assertRaises(comfy_models.ComfyModelListError):
                await comfy_models.fetch_model_list_async(
                    s, "http://127.0.0.1:59999", "diffusion_models")

    async def test_async_object_info_enum(self):
        srv = FakeComfyServer({
            "/object_info/UNETLoader": {
                "UNETLoader": {
                    "input": {"required": {"unet_name": [["x.safetensors"]]}}}}})
        try:
            from aiohttp import ClientSession, ClientTimeout
            async with ClientSession(timeout=ClientTimeout(total=10)) as s:
                opts = await comfy_models.fetch_object_info_enum_async(
                    s, srv.url, "UNETLoader", "unet_name")
            self.assertEqual(opts, ["x.safetensors"])
        finally:
            srv.stop()

    async def test_snapshot_async_uses_comfy_list(self):
        """异步快照：ComfyUI 认的模型 -> status=available。"""
        VM.reset_comfy_snapshot()
        name = VM.VIDEO_MODELS["fl2va_pruned_int8"]["unet"]
        srv = FakeComfyServer({
            "/models/diffusion_models": [name],
            "/models/loras": [],
            "/object_info/UNETLoader": {
                "UNETLoader": {"input": {"required": {"unet_name": [[name]]}}}}})
        try:
            snap = await VM.refresh_comfy_snapshot_async(
                srv.url, force=True, timeout=10)
            self.assertTrue(snap.ok)
            self.assertTrue(snap.has_unet(name))
            item = next(m for m in VM.list_models(snap)
                        if m["key"] == "fl2va_pruned_int8")
            self.assertEqual(item["status"], "available")
        finally:
            srv.stop()
            VM.reset_comfy_snapshot()

    async def test_snapshot_async_degrades_with_reason(self):
        """ComfyUI 连不上：降级到扫盘，且**带上原因**（不谎称获取不到）。"""
        VM.reset_comfy_snapshot()
        try:
            snap = await VM.refresh_comfy_snapshot_async(
                "http://127.0.0.1:59999", force=True, timeout=5)
            self.assertFalse(snap.ok)
            self.assertIn("连不上", snap.error)
            payload = VM.options_payload(snapshot=None)
            self.assertEqual(payload["model_list_source"], "filesystem_fallback")
            self.assertNotEqual(payload["model_list_error"], "")
        finally:
            VM.reset_comfy_snapshot()

    async def test_snapshot_async_does_not_block_event_loop(self):
        """并发三个请求：总耗时应接近**最慢的那个**，不是三个相加。

        假服务器给 /models/diffusion_models 故意加 1.5 秒延迟，
        另外两个接口立刻返回 —— 若串行则需 >1.5s，并发则 ~1.5s。
        这里用较宽松的阈值，只抓「退化成串行」这种明显退化。
        """
        import asyncio
        import time

        class SlowFake(_FakeComfy):
            def do_GET(self):  # noqa: N802
                if self.path.startswith("/models/diffusion_models"):
                    time.sleep(1.5)
                _FakeComfy.do_GET(self)

        payloads = {
            "/models/diffusion_models": ["a"],
            "/models/loras": ["l"],
            "/object_info/UNETLoader": {
                "UNETLoader": {"input": {"required": {"unet_name": [["a"]]}}}}}
        srv = HTTPServer(("127.0.0.1", 0), SlowFake)
        SlowFake.payloads = payloads
        SlowFake.hits = []
        port = srv.server_address[1]
        url = f"http://127.0.0.1:{port}"
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            VM.reset_comfy_snapshot()
            t0 = time.time()
            await VM.refresh_comfy_snapshot_async(url, force=True, timeout=30)
            elapsed = time.time() - t0
            self.assertLess(elapsed, 3.0,
                            f"三个请求应并发（耗时 {elapsed:.2f}s），不该串行")
        finally:
            srv.shutdown()
            srv.server_close()
            VM.reset_comfy_snapshot()


# ------------------------------------------------------------------
# 5. 服务端提交校验（normalize_t2v_params）与面板同源
# ------------------------------------------------------------------
class TestSubmitValidationUsesSameSource(unittest.TestCase):
    def setUp(self):
        VM.reset_comfy_snapshot()
        self.server = _load_server_module()

    def tearDown(self):
        VM.reset_comfy_snapshot()

    def test_submit_accepts_model_comfy_lists(self):
        """ComfyUI 认的模型 -> 提交通过。"""
        names = [VM.VIDEO_MODELS["fl2va_int8"]["unet"]]
        snap = VM.ComfyModelSnapshot(unets=names, unet_enum=names, loras=[])
        VM._SNAPSHOT = snap
        VM._SNAPSHOT_TS = 1e12        # 永不过期
        params = {
            "model_key": "fl2va_int8",
            "width": 864, "height": 480,
            "fixed_accel_lora": "ckpt850",
            "accel_loras": [],
            "attention": "default",
            "twopass": {"enabled": False},
        }
        # 固定加速 LoRA 也要在清单里
        snap.loras = [VM.VIDEO_ACCEL_LORAS["ckpt850"]["name"]]
        out, error = self.server.normalize_t2v_params(params)
        self.assertEqual(error, "", f"不该被拒：{error}")
        self.assertEqual(out["model_key"], "fl2va_int8")

    def test_submit_rejects_model_comfy_denies(self):
        """ComfyUI 不认的模型 -> 提交被拒，且理由里带「外置未挂载」语义。"""
        snap = VM.ComfyModelSnapshot(unets=[], unet_enum=[], loras=[])
        VM._SNAPSHOT = snap
        VM._SNAPSHOT_TS = 1e12
        params = {
            "model_key": "fl2va_pruned_int8",
            "width": 864, "height": 480,
            "fixed_accel_lora": "ckpt850",
            "accel_loras": [],
            "attention": "default",
            "twopass": {"enabled": False},
        }
        out, error = self.server.normalize_t2v_params(params)
        self.assertNotEqual(error, "", "ComfyUI 不认的模型应被拒")
        self.assertIn("外置", error)
        self.assertIn("不可选", error)


def _load_server_module():
    """导入 server.py 做提交校验测试（它很重，延迟导入）。"""
    sys.path.insert(0, str(REPO))
    import importlib
    import server as server_mod
    importlib.reload(server_mod)
    return server_mod


# ------------------------------------------------------------------
# 6. 前端：下拉必须是数据驱动的
# ------------------------------------------------------------------
class TestFrontendDataDriven(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = (REPO / "web" / "video-panel.js").read_text(
            encoding="utf-8", errors="ignore")
        cls.html = (REPO / "web" / "index.html").read_text(
            encoding="utf-8", errors="ignore")

    def test_has_source_note_element(self):
        self.assertIn('id="vidModelSourceNote"', self.html)

    def test_uses_status_field(self):
        """下拉的禁用/文案必须看 status（服务端现算），不是旧的 available。"""
        self.assertIn("m.status", self.js)
        self.assertIn("modelOptionLabel", self.js)

    def test_external_missing_label_says_unmounted(self):
        self.assertIn("外置未挂载，当前不可选", self.js)

    def test_does_not_render_static_warn_only(self):
        """红字不能只取静态 warn —— 必须并上 status_note。"""
        self.assertIn("model.status_note", self.js)
        self.assertIn("model.warn", self.js)

    def test_source_note_renders_api_source(self):
        self.assertIn("model_list_source", self.js)
        self.assertIn("ComfyUI 模型列表 API", self.js)

    def test_fallback_is_disclosed(self):
        """降级时必须告诉用户用的是磁盘扫描 + 原因。

        注意：前端不硬编码 "filesystem_fallback" 这个字符串 ——
        它是服务端下发的**值**，前端只判断「是不是 comfyui_api」，
        否则就落到降级分支。所以这里断言**分支结构**而不是字面量。
        """
        self.assertIn("model_list_error", self.js)
        # 「不是 comfyui_api -> 走降级分支」这一结构必须存在
        self.assertIn('src === "comfyui_api"', self.js)
        # 降级分支必须把原因显示出来
        self.assertIn("当前清单来自本机磁盘扫描", self.js)
        # 并且必须用 img-warn 样式（红字）提示这是异常状态
        self.assertIn('box.className = "img-warn"', self.js)

    def test_api_source_branch_shows_counts(self):
        """正常分支要显示条目数（可核对的事实）。"""
        self.assertIn("model_list_counts", self.js)
        self.assertIn("ComfyUI 模型列表 API", self.js)

    def test_source_note_toggles_between_branches(self):
        """两个分支都要设置 className —— 否则降级时不会变红。"""
        self.assertGreaterEqual(self.js.count('box.className = "img-'), 2,
                                "来源说明的两个分支都应设置样式类")

    def test_fallback_error_is_truncated(self):
        """降级原因可能很长（三个接口各一条）—— 界面上只留第一条。"""
        self.assertIn('err.split("；")[0]', self.js)
        self.assertIn("slice(0, 160)", self.js)

    def test_submit_uses_same_state_model(self):
        """提交时用的 model 就是下拉里那个（同一个 videoState.model）。"""
        self.assertIn("model_key: videoState.model", self.js)
        self.assertIn("opt.value = key", self.js)


if __name__ == "__main__":
    unittest.main()
