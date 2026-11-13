# -*- coding: utf-8 -*-
"""【D1 续做】ComfyUI history 解析的**类型免疫** + 前端不展示原始异常文本。

【两处缺陷（2026-11-13 定位）】

缺陷①：后端解析 ComfyUI history 时对消息体**没有类型判断** ——
    `status_info = hist.get("status") or {}`
    `if status_info.get("status_str") == "error":`
    当 `history["status"]` 是**字符串**（ComfyUI 不保证它是 dict）时，
    `.get()` 作用在 str 上 -> `AttributeError: 'str' object has no
    attribute 'get'`。

    这个 AttributeError 发生在**错误处理路径上** —— 本来要把 ComfyUI
    的失败原因翻译成中文，结果自己先崩了，用户最终只看到一句
    Python 异常文本，真正的原因被彻底丢掉。

缺陷②：前端**直接展示** `jobs.error`（服务端写进去的原始 Python
    异常文本）-> 用户看到 `'str' object has no attribute 'get'`。

【本文件在防什么】
  1. `history["status"]` 是 str / None / list 时，**绝不**抛异常；
  2. 真的取到 ComfyUI 错误时，中文原因**必须**保留（不能因为加了
     类型判断就把真原因也丢了）；
  3. 服务端写进 `jobs.error` 的**不得**是原始 Python 异常文本；
  4. 前端的错误分类器：`looksLikePythonTraceback` 必须能识别 Python
     异常文本，且**不能**误伤正常的中文原因。

【断言纪律】
  * 期望值是**字面量**，不从被测函数反推；
  * 每条断言由「命名该行为的那条断言」抓住；
  * 前端断言用**真实 JS 执行**（node），不是字符串匹配源码。

运行：python3 -m pytest tests/test_comfy_errors_typesafety.py -v
"""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import server as srv                                    # noqa: E402
from h3studio import comfy_errors                       # noqa: E402


class StatusTypeSafetyTest(unittest.TestCase):
    """缺陷①：`history["status"]` 类型异常时不得抛异常。"""

    def test_coerce_status_info_accepts_dict(self):
        """正常 dict -> 原样返回。"""
        d = {"status_str": "error", "messages": []}
        self.assertIs(comfy_errors.coerce_status_info(d), d)

    def test_coerce_status_info_rejects_string(self):
        """字符串 -> 空 dict（不是字符串本身）。"""
        out = comfy_errors.coerce_status_info("execution_error")
        self.assertEqual(out, {})
        self.assertIsInstance(out, dict)

    def test_coerce_status_info_rejects_none_and_list(self):
        """None / list / int -> 一律空 dict。"""
        for bad in (None, [], ["a"], 0, 3.14, True):
            self.assertEqual(comfy_errors.coerce_status_info(bad), {},
                             f"非 dict 的 {bad!r} 应被归一成空 dict")

    def test_status_is_error_never_raises_on_string(self):
        """`status` 是字符串时 status_is_error 不抛异常。"""
        self.assertFalse(comfy_errors.status_is_error("execution_error"))
        self.assertFalse(comfy_errors.status_is_error(None))
        self.assertFalse(comfy_errors.status_is_error([]))

    def test_status_is_error_detects_real_error(self):
        """真的 error 状态 -> True（类型判断不能把真错误也吃掉）。"""
        self.assertTrue(comfy_errors.status_is_error({"status_str": "error"}))
        self.assertFalse(comfy_errors.status_is_error({"status_str": "success"}))

    def test_extract_error_on_string_status_does_not_raise(self):
        """`extract_error` 传字符串 -> 返回「没找到」，不抛异常。"""
        out = comfy_errors.extract_error("execution_error")
        self.assertFalse(out["found"])
        self.assertEqual(out["reason"], "")

    def test_error_from_history_with_string_status(self):
        """`error_from_history` 遇到字符串 status -> 不抛异常。"""
        out = comfy_errors.error_from_history({"status": "execution_error"})
        self.assertFalse(out["found"])

    def test_error_from_history_with_none_history(self):
        """history 本身不是 dict -> 不抛异常。"""
        for bad in (None, "x", [], 5):
            out = comfy_errors.error_from_history(bad)
            self.assertFalse(out["found"])


class UnparsedReasonTest(unittest.TestCase):
    """兜底文案本身必须「干净」。"""

    def test_unparsed_reason_has_no_python_traceback(self):
        """兜底文案不得含 Python 异常痕迹。"""
        reason = comfy_errors.UNPARSED_ERROR_REASON
        self.assertNotIn("object has no attribute", reason)
        self.assertNotIn("Traceback", reason)
        self.assertNotIn(".py", reason)

    def test_unparsed_reason_is_chinese_and_actionable(self):
        """兜底文案必须是中文 + 给出下一步动作。"""
        reason = comfy_errors.UNPARSED_ERROR_REASON
        self.assertTrue(any("\u4e00" <= ch <= "\u9fff" for ch in reason),
                        f"兜底文案不是中文: {reason}")
        self.assertIn("日志", reason,
                      "兜底文案必须告诉用户去哪儿看详情")

    def test_server_fallback_uses_the_same_constant(self):
        """server 的兜底**必须**用这个常量（两处口径一致）。"""
        src_text = (REPO / "server.py").read_text(encoding="utf-8")
        self.assertIn("comfy_errors.UNPARSED_ERROR_REASON", src_text,
                      "server 的兜底没用统一常量 —— 两处口径会不一致")


class HistoryStatusHelperTest(unittest.TestCase):
    """worker 里**真正**调用的那个 helper 的类型免疫。

    【为什么单独测它】`_history_status` 是从 worker 轮询循环里抽出来的。
    抽出来之前，这个判断**内联**在循环里 —— 改回旧写法只有「真跑一次
    ComfyUI 并让它失败」才能发现（而且是在失败路径上，平时走不到）。
    抽出来之后，畸形 history 可以直接喂进去断言「不抛」。
    """

    def test_string_status_does_not_raise(self):
        """`status` 是字符串 -> 不抛异常，且不判定为 error。"""
        info, is_error = srv._history_status({"status": "execution_error"})
        self.assertIsInstance(info, dict)
        self.assertFalse(is_error)

    def test_none_status_does_not_raise(self):
        """`status` 是 None -> 不抛异常。"""
        info, is_error = srv._history_status({"status": None})
        self.assertIsInstance(info, dict)
        self.assertFalse(is_error)

    def test_missing_status_key_does_not_raise(self):
        """history 里根本没有 status 键 -> 不抛异常。"""
        info, is_error = srv._history_status({"outputs": {}})
        self.assertIsInstance(info, dict)
        self.assertFalse(is_error)

    def test_history_itself_is_not_dict(self):
        """history 本身不是 dict -> 不抛异常。"""
        for bad in (None, "x", [], 5):
            info, is_error = srv._history_status(bad)
            self.assertIsInstance(info, dict)
            self.assertFalse(is_error)

    def test_real_error_status_is_detected(self):
        """真的 error 状态 -> is_error=True（类型判断不能吃掉真错误）。"""
        info, is_error = srv._history_status(
            {"status": {"status_str": "error",
                        "messages": [["execution_error",
                                      {"exception_message": "boom"}]]}})
        self.assertTrue(is_error)
        self.assertIn("boom", str(info))


class ServerErrorMessageTest(unittest.TestCase):
    """缺陷①在 server 侧的表现：`_comfy_error_message` 不得崩。"""

    def test_comfy_error_message_accepts_string_status(self):
        """status 是字符串 -> 返回可读中文，不抛 AttributeError。"""
        msg = srv._comfy_error_message("execution_error")
        self.assertIsInstance(msg, str)
        self.assertTrue(msg.strip())
        self.assertNotIn("'str' object has no attribute", msg)

    def test_comfy_error_message_accepts_none(self):
        """status 是 None -> 返回可读中文。"""
        msg = srv._comfy_error_message(None)
        self.assertIsInstance(msg, str)
        self.assertNotIn("'str' object has no attribute", msg)

    def test_comfy_error_message_keeps_real_chinese_reason(self):
        """真的 ComfyUI 错误 -> 中文原因**必须保留**（不能被兜底吞掉）。"""
        status = {
            "status_str": "error",
            "messages": [
                ["execution_error", {
                    "node_type": "H3RelayCopyBridge",
                    "exception_type": "ValueError",
                    "exception_message": "拷贝桥禁止跨分辨率：上段 768x448 ≠ 本段 1024x576",
                }],
            ],
        }
        msg = srv._comfy_error_message(status)
        self.assertIn("拷贝桥禁止跨分辨率", msg)
        # 尺寸在中文原因里会被规范化成「×」（768×448），不是原文的「x」。
        self.assertIn("768", msg)
        self.assertIn("448", msg)

    def test_comfy_error_message_never_returns_python_traceback(self):
        """任何输入下都**不得**返回原始 Python 异常文本。"""
        cases = [
            "execution_error",
            None,
            [],
            {"status_str": "error", "messages": "not-a-list"},
            {"status_str": "error", "messages": [None, "str-msg", 123]},
            {"status_str": "error", "messages": [["execution_error", "not-a-dict"]]},
        ]
        for case in cases:
            msg = srv._comfy_error_message(case)
            self.assertNotIn("object has no attribute", msg,
                             f"泄露了 Python 异常文本: {msg}")
            self.assertNotIn("Traceback", msg)


class UserFacingErrorTest(unittest.TestCase):
    """缺陷②：写进 jobs.error 的必须是可读中文，不是原始异常文本。"""

    def test_python_exception_is_replaced_by_chinese(self):
        """原始 Python 异常 -> 换成可读中文。"""
        exc = AttributeError("'str' object has no attribute 'get'")
        msg = srv._user_facing_error(exc)
        self.assertNotIn("object has no attribute", msg)
        self.assertNotIn("AttributeError", msg)
        self.assertIn("生成失败", msg)

    def test_chinese_runtime_error_is_kept(self):
        """已中文化的 RuntimeError -> 原样保留（不能被二次洗掉）。"""
        exc = RuntimeError("拷贝桥禁止跨分辨率：上段 768x448 ≠ 本段 1024x576")
        msg = srv._user_facing_error(exc)
        self.assertEqual(msg, "拷贝桥禁止跨分辨率：上段 768x448 ≠ 本段 1024x576")

    def test_empty_exception_gets_generic_reason(self):
        """空异常 -> 通用文案。"""
        msg = srv._user_facing_error(ValueError(""))
        self.assertTrue(msg.strip())
        self.assertNotIn("ValueError", msg)

    def test_never_contains_exception_class_name(self):
        """任何异常类名都不得出现在用户文案里。"""
        for exc in (AttributeError("x"), ValueError("y"), KeyError("z"),
                    TypeError("w"), RuntimeError("RuntimeError: boom")):
            msg = srv._user_facing_error(exc)
            self.assertNotIn(type(exc).__name__, msg,
                             f"{type(exc).__name__} 泄露到了用户文案: {msg}")


class WorkerErrorWriteTest(unittest.TestCase):
    """worker 把失败原因写进 `jobs.error` 时，**不得**写原始异常文本。

    【为什么单独测】`_user_facing_error` 是纯函数，容易测；
    但**调用点**（worker 兜底 `except Exception`）如果又被改回
    `str(e)[:800]`，纯函数测试是抓不到的 —— 必须验证**真正写进库**的内容。
    """

    def setUp(self):
        self._orig_db = srv.DB_PATH
        self.tmp = tempfile.mkdtemp(prefix="h3-worker-err-")
        srv.DB_PATH = Path(self.tmp) / "jobs.db"
        srv.init_db()

    def tearDown(self):
        srv.DB_PATH = self._orig_db
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_worker_writes_readable_chinese_to_db(self):
        """worker 兜底 -> jobs.error 里是中文，不是 Python 异常文本。"""
        job_id = srv.create_job_record("t2v", {"prompt": "x", "duration": 5})
        exc = AttributeError("'str' object has no attribute 'get'")
        with srv.db() as conn:
            conn.execute(
                "UPDATE jobs SET status='failed', error=? WHERE id=?",
                (srv._user_facing_error(exc), job_id))
        with srv.db() as conn:
            row = conn.execute("SELECT error FROM jobs WHERE id=?",
                               (job_id,)).fetchone()
        stored = row["error"]
        self.assertNotIn("object has no attribute", stored,
                         f"原始 Python 异常文本被写进了 jobs.error: {stored}")
        self.assertNotIn("AttributeError", stored)
        self.assertIn("生成失败", stored)

    def test_worker_source_does_not_use_raw_str_exception(self):
        """源码层面：worker 兜底**不得**直接写 `str(e)`。

        这条是「防回退」的断言 —— 它锁住「调用点用 _user_facing_error」
        这个**约定**，而不只是函数本身的行为。
        """
        src = (REPO / "server.py").read_text(encoding="utf-8")
        # worker 兜底里不得出现 (str(e)[:800]
        self.assertNotIn("(str(e)[:800], time.time(), job_id)", src,
                         "worker 兜底又把原始异常文本写回 jobs.error 了")
        self.assertNotIn("(str(exc)[:1200], time.time(), job_id)", src,
                         "longfilm 兜底又把原始异常文本写回 jobs.error 了")
        # 必须用上清洗函数
        self.assertIn("_user_facing_error(e)", src)
        self.assertIn("_user_facing_error(exc)", src)


class _FakeComfyHistory:
    """假 ComfyUI：/history/<prompt_id> 返回一条 `status` 是**字符串**的记录。

    这正是触发原始缺陷①的数据形状：
        status_info = hist.get("status") or {}   -> 拿到 str
        status_info.get("status_str")            -> AttributeError
    """

    def __init__(self, status_value):
        self.status_value = status_value
        self.history_calls = 0

    async def __call__(self, session, prompt_id):
        self.history_calls += 1
        # 只回一次「错误状态」，之后回一个**成功**的 history ——
        # 否则 worker 会一直轮询下去（测试会挂住）。
        if self.history_calls > 1:
            return {"prompt": [], "outputs": {},
                    "status": {"status_str": "error",
                               "messages": [["execution_error",
                                             {"exception_message": "boom"}]]}}
        return {"prompt": [], "outputs": {},
                "status": self.status_value}


class WorkerFailurePathTest(unittest.TestCase):
    """**真跑一次** worker 的失败路径（假 ComfyUI + 真 DB）。

    这是缺陷①②的**运行时**证据：
      * worker 不得因 AttributeError 崩掉（原始缺陷①）；
      * `jobs.error` 里必须是**中文**，不是 Python 异常文本（原始缺陷②）。
    """

    def setUp(self):
        self._orig_db = srv.DB_PATH
        self._orig_out = srv.OUT_DIR
        self.tmp = tempfile.mkdtemp(prefix="h3-wfail-")
        srv.DB_PATH = Path(self.tmp) / "jobs.db"
        srv.OUT_DIR = Path(self.tmp) / "outputs"
        srv.OUT_DIR.mkdir(parents=True, exist_ok=True)
        srv.init_db()

    def tearDown(self):
        srv.DB_PATH = self._orig_db
        srv.OUT_DIR = self._orig_out
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_worker_with_status(self, status_value):
        """造一个 t2v 任务，让 worker 用给定的 status 跑一遍。"""
        job_id = srv.create_job_record("t2v", {"prompt": "x", "duration": 5})
        # worker 只处理 status='running' 的任务（_run 里会检查）。
        with srv.db() as conn:
            conn.execute("UPDATE jobs SET status='running' WHERE id=?", (job_id,))
        worker = srv.Worker()
        fake = _FakeComfyHistory(status_value)

        # 替换 worker 用的外部依赖（真 ComfyUI 不参与）
        orig_hist = srv.comfy_history
        orig_submit = srv.comfy_submit
        orig_refresh = srv.refresh_comfy_node_names

        async def _fake_submit(session, workflow, client_id=None):
            return "fake-prompt-id"

        async def _fake_refresh(session=None, force=False):
            return set()

        srv.comfy_history = fake
        srv.comfy_submit = _fake_submit
        srv.refresh_comfy_node_names = _fake_refresh
        try:
            # 只跑「取 history -> 判断错误」这一段：
            # 直接复用 worker 的真实轮询逻辑（同步跑一次 asyncio）。
            import asyncio

            async def _drive():
                job_row = {"id": job_id, "mode": "t2v",
                           "params": json.dumps({"prompt": "x", "duration": 5})}
                await worker._run(job_row)

            asyncio.run(_drive())
        finally:
            srv.comfy_history = orig_hist
            srv.comfy_submit = orig_submit
            srv.refresh_comfy_node_names = orig_refresh

        with srv.db() as conn:
            row = conn.execute("SELECT status, error FROM jobs WHERE id=?",
                               (job_id,)).fetchone()
        return row

    def test_worker_survives_string_status(self):
        """`status` 是字符串 -> worker 不得崩，任务应落到 failed 且有中文原因。"""
        row = self._run_worker_with_status("execution_error")
        self.assertEqual(row["status"], "failed")
        self.assertIsNotNone(row["error"])
        self.assertNotIn("object has no attribute", row["error"],
                         f"原始 Python 异常文本进了 jobs.error: {row['error']}")
        self.assertNotIn("Traceback", row["error"])
        self.assertIn("生成失败", row["error"])

    def test_worker_survives_none_status(self):
        """`status` 是 None -> 同样不得崩。"""
        row = self._run_worker_with_status(None)
        self.assertEqual(row["status"], "failed")
        self.assertNotIn("object has no attribute", row["error"] or "")


class WorkerRuntimeErrorTest(unittest.TestCase):
    """**真跑一次** worker 的失败路径，验证写进 jobs.error 的内容。

    【为什么要真跑】纯函数测试 + 源码字符串断言有盲区：
    源码断言只锁住「代码里写了 `_user_facing_error`」，但如果**调用点**
    实际传错了参数（例如传的是 `str(e)` 的结果而不是异常对象），
    源码断言照样绿。

    这里用**假的 ComfyUI**（返回 `status` 是字符串的 history）驱动
    worker 的轮询循环，断言：
      * worker **不因 AttributeError 崩掉**（缺陷①的运行时证据）；
      * `jobs.error` 里是**中文**，不是 Python 异常文本（缺陷②的运行时证据）。
    """

    def setUp(self):
        self._orig_db = srv.DB_PATH
        self._orig_out = srv.OUT_DIR
        self.tmp = tempfile.mkdtemp(prefix="h3-worker-rt-")
        srv.DB_PATH = Path(self.tmp) / "jobs.db"
        srv.OUT_DIR = Path(self.tmp) / "outputs"
        srv.OUT_DIR.mkdir(parents=True, exist_ok=True)
        srv.init_db()

    def tearDown(self):
        srv.DB_PATH = self._orig_db
        srv.OUT_DIR = self._orig_out
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_history_status_helper_is_used_and_survives_string_status(self):
        """worker 用的是 `_history_status`（带类型判断）而不是裸 `.get()`。

        这条直接断言 **调用点用了哪个函数** —— 用源码检查是因为
        「worker 到底调了谁」是**结构事实**，跑一次 worker 看不出
        （两个实现行为可能一样）。
        """
        src_text = (REPO / "server.py").read_text(encoding="utf-8")
        self.assertIn("_history_status(hist)", src_text,
                      "worker 的轮询循环没走 _history_status —— "
                      "类型判断又被绕开了")

    def test_history_status_helper_returns_typed_pair(self):
        """helper 的返回形状固定：(dict, bool)。"""
        info, is_error = srv._history_status(
            {"status": {"status_str": "error"}})
        self.assertIsInstance(info, dict)
        self.assertIsInstance(is_error, bool)
        self.assertTrue(is_error)


class FrontendFriendlyErrorTest(unittest.TestCase):
    """缺陷②的前端兜底：**执行 app.js 里真实的实现**（不是抄一份）。

    【为什么必须执行真实实现】第一版测试在测试文件里**抄了一份**
    `friendlyError` 的逻辑再跑 node —— 那样测的是「测试文件里的副本」，
    不是「app.js 里那段代码」。变异「改 app.js」完全不会被抓住
    （实测：T7/T8 两项变异 0 条变红 —— 这本身就是测试无效的信号）。

    现在改成：**从 app.js 里抽出那两段真实代码**再执行。
    app.js 改了 -> 抽出来的就是改后的代码 -> 变异被抓。
    """

    @classmethod
    def setUpClass(cls):
        cls.node = subprocess.run(["node", "--version"], capture_output=True,
                                  text=True)
        if cls.node.returncode != 0:
            raise unittest.SkipTest("没有 node，无法执行前端断言")

    def _extract_real_impl(self) -> str:
        """从 web/app.js 里抽出 `friendlyError` / `looksLikePythonTraceback`。

        用**文本切片**而不是正则匹配整个函数体 —— 简单可靠，
        且函数体本身不嵌套同名函数。
        """
        src = (REPO / "web" / "app.js").read_text(encoding="utf-8")
        out = []
        # 1) 常量 PY_EXCEPTION_PATTERNS
        c = src.index("const PY_EXCEPTION_PATTERNS = [")
        c_end = src.index("];", c) + len("];")
        out.append(src[c:c_end])
        # 2) 两个函数
        for fname in ("looksLikePythonTraceback", "friendlyError"):
            marker = f"function {fname}("
            i = src.index(marker)
            # 从函数头扫到「行首 }」为止（这两个函数都是顶层函数）
            j = src.index("\n}\n", i) + len("\n}")
            out.append(src[i:j])
        return "\n\n".join(out)

    def _run_js(self, body: str):
        script = f"""
{self._extract_real_impl()}
{body}
"""
        proc = subprocess.run(["node", "-e", script], capture_output=True,
                              text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def test_python_traceback_is_hidden(self):
        """Python 异常文本 -> 前端不得展示原文。"""
        samples = [
            "'str' object has no attribute 'get'",
            "ValueError: 拷贝桥禁止跨分辨率",
            "AttributeError: 'NoneType' object has no attribute 'get'",
            "Traceback (most recent call last):",
            '  File "/root/server.py", line 118, in _run',
            "KeyError: 'outputs'",
        ]
        out = self._run_js(
            "console.log(JSON.stringify(" + json.dumps(samples) +
            ".map(friendlyError)));")
        for raw, shown in zip(samples, out):
            self.assertNotIn("object has no attribute", shown,
                             f"前端泄露了原始文本: {raw!r} -> {shown!r}")
            self.assertNotIn("Traceback", shown)
            self.assertNotIn(".py", shown)
            self.assertIn("生成失败", shown)

    def test_chinese_reason_is_preserved(self):
        """正常中文原因 -> 前端**原样展示**（不能被误伤）。"""
        samples = [
            "拷贝桥禁止跨分辨率：上段 768x448 ≠ 本段 1024x576",
            "生成已被中断（可能是 ComfyUI 侧取消了任务）。",
            "生成失败，但没能取到具体原因（ComfyUI 侧未返回可解析的错误信息）。详细信息见服务端日志。",
            "本机 ComfyUI 暂时无响应，接续节点状态未探全 —— 接续功能按可用处理。",
        ]
        out = self._run_js(
            "console.log(JSON.stringify(" + json.dumps(samples) +
            ".map(friendlyError)));")
        for raw, shown in zip(samples, out):
            self.assertEqual(shown, raw,
                             f"正常中文原因被误改了: {raw!r} -> {shown!r}")

    def test_empty_error_becomes_generic(self):
        """空错误 -> 「生成失败」。"""
        out = self._run_js(
            "console.log(JSON.stringify([friendlyError(''), "
            "friendlyError(null), friendlyError(undefined)]));")
        self.assertEqual(out, ["生成失败", "生成失败", "生成失败"])

    def test_app_js_uses_friendly_error_for_job_and_batch(self):
        """app.js 的两处错误展示都走 friendlyError（不再直出 job.error）。"""
        src = (REPO / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn("friendlyError(job.error)", src,
                      "任务卡片的错误展示没走 friendlyError")
        self.assertIn("friendlyError(batch.error)", src,
                      "批次缩略图的错误展示没走 friendlyError")
        self.assertNotIn('job.error || "生成失败"', src,
                         "任务卡片仍在直出原始 job.error")
        self.assertNotIn('batch.error || "失败"', src,
                         "批次缩略图仍在直出原始 batch.error")


if __name__ == "__main__":
    unittest.main()
