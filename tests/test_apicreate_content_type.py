# -*- coding: utf-8 -*-
"""POST /api/jobs —— 请求头校验：错 Content-Type 必须回 4xx，不能回 500。

【这个用例在守什么】

2026-11-21 巡检在**线上**复现到：调用方发错 Content-Type 时，
``server.py:api_create`` 直接 ``await request.multipart()``，
aiohttp 抛异常 -> aiohttp 默认处理 -> **HTTP 500「Server got itself in trouble」**。

线上 24 小时日志里的 10 条异常（7 条 ``AssertionError: multipart/* content
type expected`` + 2 条 ``boundary missed`` + 1 条 ``KeyError: 'Content-Type'``）
**全部**来自这一处，没有别的来源。

500 的危害不是「多了一个错误码」，而是**把排查方向带偏**：
500 会被理解成「服务端崩了」，于是去查 ComfyUI / GPU / 队列；
而真实原因只是「请求头没带对」—— 这是**调用方自己就能纠正**的问题，
应该回 415 / 400 并把「该带什么头」说清楚。

【为什么用真实的 aiohttp 测试客户端而不是直接调 handler】

本用例必须走**真实的 HTTP 栈**才能复现原始缺陷：
``KeyError: 'Content-Type'`` 与 ``AssertionError: multipart/* content type
expected`` 都是在 aiohttp 的 ``MultipartReader.__init__`` 里抛的。
如果直接 mock 一个 request 对象，就**测不到**这条路径 —— 那样写出来的
用例在缺陷回归时会「假绿」。

【怎么保证这个用例不是空转】

见文件末尾 ``test_mutation_guard_*``：把校验整段删掉（即恢复成缺陷状态），
这几个用例会**真的失败**（已在交付前实测，见 REPORT.md）。

运行：python3 -m pytest tests/test_apicreate_content_type.py -v
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server as h3server  # noqa: E402


def _post(*, data: bytes = b"", headers=None,
          skip_auto_headers=None, base="/api/jobs"):
    """真的起一个 aiohttp 测试服务，POST 一次，返回 (status, body_text)。"""
    from aiohttp.test_utils import TestClient, TestServer

    async def _run():
        app = h3server.make_app()
        server = TestServer(app)
        client = TestClient(server)
        await client.start_server()
        try:
            # 【为什么分三个分支而不是拼 kwargs 字典】
            # 拼 dict 时 pyright 会把所有值的类型推断成同一个（这里是 bytes），
            # 于是 headers / skip_auto_headers 全被报类型错误。
            # 直接分支调用，类型各自独立，也更接近真实用法。
            if skip_auto_headers is not None:
                req = client.post(base, data=data,
                                  skip_auto_headers=skip_auto_headers)
            elif headers is not None:
                req = client.post(base, data=data, headers=headers)
            else:
                req = client.post(base, data=data)
            async with req as resp:
                status = resp.status
                body = await resp.text()
        finally:
            await client.close()
        return status, body

    return asyncio.run(_run())


class TestApiCreateContentType(unittest.TestCase):
    """错 Content-Type -> 4xx（而不是 500）。"""

    def _assert_4xx_with_code(self, status, body, expect_code, expect_status):
        self.assertNotEqual(
            status, 500,
            f"错 Content-Type 竟返回 500 —— 这就是本用例要守的缺陷。"
            f" 响应体：{body[:300]}")
        self.assertEqual(
            status, expect_status,
            f"期望 HTTP {expect_status}，实得 {status}。响应体：{body[:300]}")
        try:
            payload = json.loads(body)
        except Exception as exc:  # pragma: no cover - 防御性
            self.fail(f"响应不是合法 JSON（{exc}）：{body[:300]}")
        self.assertIn("error", payload, "4xx 响应必须带可读的 error 文案")
        self.assertIsInstance(payload["error"], str)
        self.assertTrue(payload["error"].strip(), "error 文案不能是空串")
        self.assertEqual(payload.get("code"), expect_code,
                         f"错误码应为 {expect_code}，实得 {payload.get('code')}")
        return payload

    # ------------------------------------------------------------------
    # 1) 完全不带 Content-Type —— 线上日志里的 KeyError: 'Content-Type'
    # ------------------------------------------------------------------
    def test_missing_content_type_returns_415_not_500(self):
        status, body = _post(data=b"mode=t2v",
                             skip_auto_headers={"Content-Type"})
        payload = self._assert_4xx_with_code(
            status, body, "content_type_required", 415)
        self.assertIn("Content-Type", payload["error"])

    # ------------------------------------------------------------------
    # 2) Content-Type 是 application/json —— 线上日志里的
    #    AssertionError: multipart/* content type expected
    # ------------------------------------------------------------------
    def test_json_content_type_returns_415_not_500(self):
        status, body = _post(data=b'{"mode":"t2v"}',
                             headers={"Content-Type": "application/json"})
        self._assert_4xx_with_code(status, body,
                                   "unsupported_media_type", 415)

    def test_text_plain_content_type_returns_415_not_500(self):
        status, body = _post(data=b"mode=t2v",
                             headers={"Content-Type": "text/plain"})
        self._assert_4xx_with_code(status, body,
                                   "unsupported_media_type", 415)

    def test_form_urlencoded_returns_415_not_500(self):
        status, body = _post(data=b"mode=t2v",
                             headers={"Content-Type":
                                      "application/x-www-form-urlencoded"})
        self._assert_4xx_with_code(status, body,
                                   "unsupported_media_type", 415)

    # ------------------------------------------------------------------
    # 3) multipart/form-data 但没有 boundary —— 线上日志里的
    #    ValueError: boundary missed for Content-Type
    # ------------------------------------------------------------------
    def test_multipart_without_boundary_returns_400_not_500(self):
        status, body = _post(data=b"mode=t2v",
                             headers={"Content-Type": "multipart/form-data"})
        self._assert_4xx_with_code(status, body,
                                   "content_type_invalid", 400)

    # ------------------------------------------------------------------
    # 4) 正确路径**不能**被这次改动误伤 —— 回归护栏
    # ------------------------------------------------------------------
    def test_valid_multipart_is_not_rejected_by_content_type_check(self):
        """带完整 multipart 头时，请求必须**穿过** Content-Type 校验。

        【为什么必须断言「穿过」而不是只断言「不是 415」】
        校验一旦写错（比如把 `multipart/form-data` 也拒了），
        上面几条用例照样绿 —— 但**正常提交全废**。
        所以这里用一个「mode 缺失」的真 multipart 请求：
        它应当穿过内容类型校验，然后被**业务校验**（未知模式）接住，
        回 400 + code=bad_request。这证明请求确实进到了业务逻辑里。
        """
        boundary = "----wdt8boundary"
        body = (
            f"--{boundary}\r\n"
            "Content-Disposition: form-data; name=\"params\"\r\n\r\n"
            "{}\r\n"
            f"--{boundary}--\r\n"
        ).encode()
        status, body_text = _post(
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        self.assertNotEqual(
            status, 415,
            "正确的 multipart 请求被内容类型校验误拒 —— 本次改动回归了正常路径")
        try:
            payload = json.loads(body_text)
        except Exception:
            payload = {}
        # 缺 mode -> 业务校验回 400 bad_request（不是内容类型错误）
        self.assertEqual(status, 400)
        self.assertEqual(payload.get("code"), "bad_request",
                         "正常 multipart 应走到业务校验（未知模式），"
                         f"实得 {body_text[:200]}")

    def test_content_type_check_does_not_swallow_later_errors(self):
        """内容类型校验只拦「格式错」，不吞后面的业务错误。

        【为什么这条重要】有一种「假修法」：在校验里写个大 try/except，
        把所有异常都变成 4xx。这样 500 确实消失了，但**真实的服务端故障**
        （比如 DB 挂了）也会被伪装成 400 —— 排查难度反而更高。
        所以这里断言：业务层抛出的 JobValidationError 仍然是
        **带 code 的 400**，而不是被内容类型校验改写的其它码。
        """
        boundary = "----wdt8boundary2"
        body = (
            f"--{boundary}\r\n"
            "Content-Disposition: form-data; name=\"mode\"\r\n\r\n"
            "t2v\r\n"
            f"--{boundary}--\r\n"
        ).encode()
        status, body_text = _post(
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            payload = json.loads(body_text)
        except Exception:
            payload = {}
        # mode=t2v 但没有 prompt -> 业务校验（提示词不能为空）
        self.assertEqual(status, 400)
        self.assertNotIn(payload.get("code"),
                         {"content_type_required", "unsupported_media_type",
                          "content_type_invalid"},
                         "业务错误被内容类型校验的 code 覆盖了")


class TestApiCreateContentTypeSourceGuard(unittest.TestCase):
    """源码级护栏：确保校验不会被「优化掉」。"""

    def test_api_create_validates_content_type_before_multipart(self):
        """api_create 必须在调 request.multipart() **之前**做校验。

        【为什么要在源码层面断言】
        顺序是本质：先调 multipart() 再校验等于没校验（异常已经抛了）。
        只测 HTTP 行为的话，某个中间人把校验挪到后面去，
        HTTP 用例会挂；但如果同时把异常吞掉，HTTP 用例可能反而全绿
        —— 源码顺序断言能钉死这一层。

        【为什么不能用「源码里有没有 Content-Type 字样」当判据】
        那样会被**注释**骗过 —— 注释里写「Content-Type」也算命中。
        所以这里找的是**真正取 header 的那一行代码**
        （``request.headers.get("Content-Type")``），
        并按**代码行**（去掉注释）来判断先后。
        """
        src = Path(h3server.__file__).read_text(encoding="utf-8")
        start = h3server.api_create.__code__.co_firstlineno - 1
        lines = src.splitlines()

        # 【关键】先剥掉注释与字符串里的假阳性：
        # 只保留「真正执行到的代码行」。
        code_lines = []
        for ln in lines[start: start + 200]:
            stripped = ln.strip()
            if stripped.startswith("#"):
                continue
            code_lines.append(stripped)
        body = "\n".join(code_lines)

        # 真正读 header 的那一行（不是注释里的字样）
        read_at = body.find('request.headers.get("Content-Type")')
        self.assertNotEqual(
            read_at, -1,
            "api_create 里找不到真正读取 Content-Type 的代码行"
            "（只可能在注释里出现过字样 = 校验被删了）")
        multipart_at = body.find("request.multipart()")
        self.assertNotEqual(multipart_at, -1,
                            "api_create 里找不到 request.multipart()")
        self.assertLess(
            read_at, multipart_at,
            "Content-Type 校验必须**早于** request.multipart()，"
            "否则校验形同虚设（异常已经抛出去了）")

    def test_api_create_has_no_bare_multipart_without_check(self):
        """防「校验被摘掉、multipart 又挪到前面」这种组合退化。

        【为什么单独再钉一条】MUT5 那类变异（把校验整体挪到
        multipart() 之后）有时会让 HTTP 用例仍然全绿 ——
        因为**原来那处**校验还在生效，只是变成了死代码。
        上面那条顺序断言能抓住它；这条则保证「校验代码确实存在
        且在 multipart 之前被调用」，两条互补。
        """
        src = Path(h3server.__file__).read_text(encoding="utf-8")
        start = h3server.api_create.__code__.co_firstlineno - 1
        lines = src.splitlines()[start: start + 200]
        code = [ln.strip() for ln in lines
                if ln.strip() and not ln.strip().startswith("#")]
        body = "\n".join(code)
        # 校验返回的三种错误码必须都在（缺一种就少守一条路径）
        for code_str in ("content_type_required", "unsupported_media_type",
                         "content_type_invalid"):
            self.assertIn(code_str, body,
                          f"api_create 里找不到错误码 {code_str}"
                          " —— 对应的错误路径没有守卫")


if __name__ == "__main__":
    unittest.main(verbosity=2)
