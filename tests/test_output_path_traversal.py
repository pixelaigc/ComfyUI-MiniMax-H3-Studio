# -*- coding: utf-8 -*-
"""回归测试：产物路径判据必须是**路径边界**判定，不是字符串前缀。

【缺陷（2026-11-15 代码审查发现，High）】
``server.py`` 里有两处用 ``str(full).startswith(str(OUT_DIR.resolve()))``
判断「文件在产物目录内」：

  * ``files_handler``（读取口）—— server.py:8558
  * ``api_delete``（删除口）  —— server.py:4734

``startswith`` 是**字符串前缀**判定，不是路径边界判定：
同级目录 ``outputs_secret`` 的字符串前缀就是 ``.../outputs`` ->
判定为 True -> 越界文件被当成产物返回。

实测（8190 线上，2026-11-15）：
    /files/..%2Foutputs_secret%2Fcreds.txt        -> 200（读到了越界文件）
    /files/%2e%2e%2Foutputs_secret%2Fcreds.txt    -> 200（同上）
    /files/../outputs_secret/creds.txt             -> 404（aiohttp 规范化裸 ../）

裸 ``../`` 被 aiohttp 规范化掉，所以**必须用 %2F 编码**才能逃逸 ——
这正是它容易在人工检查里漏掉的原因（手打 ../ 测不出来）。

【修法】两处都改用 ``relative_to``（路径边界判定），
与仓库里已有的 ``h3studio/assets.py::resolve_inside`` 同源。

运行：
    python3 -m pytest tests/test_output_path_traversal.py -q --tb=short
"""

import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

SERVER_PY = REPO / "server.py"

def _strip_python_comments(src: str) -> str:
    """去掉 Python 注释与字符串字面量，只留**可执行代码**。

    【为什么需要】
    断言「某行代码存在」时，如果那行已经被注释掉，字符串匹配**照样**能命中 ——
    变异测试实测过这个坑（见 test_visible_text_pollution.py 的同类教训）。
    这里把注释剥掉再断言，才能真正区分「代码在跑」与「注释里提到过」。
    """
    import io
    import tokenize
    out = []
    try:
        tokens = tokenize.generate_tokens(io.StringIO(src).readline)
        for tok in tokens:
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            out.append(tok.string)
    except tokenize.TokenError:
        return src
    # 【关键】用**空格**连接 token，不能用换行 ——
    # 换行会把 `startswith` 与 `(` 拆到两行，正则就匹配不到了
    # （实测踩过：判据从「命中」变成「永远不命中」，断言静默失效）。
    return " ".join(out)




def _server_source() -> str:
    return SERVER_PY.read_text(encoding="utf-8")


class TestOutputPathBoundary(unittest.TestCase):
    """产物路径判据必须用 relative_to，不能用 startswith。"""

    def test_no_startswith_prefix_check_on_output_paths(self):
        """server.py 里不得再出现「startswith(OUT_DIR...)」这种前缀判定。

        【为什么要剥注释】修复时我在代码旁边写了「原来用的是
        `str(target).startswith(str(OUT_DIR.resolve()))`」这样的说明注释 ——
        不剥注释的话这条断言会命中自己的注释，说明文字。
        """
        src = _strip_python_comments(_server_source())
        bad = []
        # 【关键】正则必须容忍 tokenize 造成的空格 ——
        # tokenize 把源码拆成 token 再用空格连接，所以源码里的
        # `startswith(str(OUT_DIR` 变成 `startswith ( str ( OUT_DIR`。
        # 用不含空格的正则会**永远匹配不到** -> 断言静默失效
        # （变异测试实测：把两处改回 startswith，测试照样通过）。
        for m in re.finditer(r"startswith\s*\(\s*str\s*\(\s*OUT_DIR", src):
            bad.append(m.start())
        # 行号在剥注释后已失真，这里只报数量与片段
        bad = [src[max(0, b - 60): b + 60].replace("\n", " ") for b in bad]
        self.assertEqual(
            [], bad,
            "server.py 仍在用字符串前缀判定产物路径（行 "
            f"{bad}）—— 同级目录 outputs_secret 会被误判为在目录内")

    def test_files_handler_uses_relative_to(self):
        """读取口 files_handler 必须做路径边界判定。"""
        src = _server_source()
        m = re.search(r"async def files_handler\(request\):(.*?)(?=\nasync def |\ndef )",
                      src, re.S)
        self.assertIsNotNone(m, "找不到 files_handler")
        body = m.group(1)
        self.assertIn("relative_to", body,
                      "files_handler 没有用 relative_to 做路径边界判定")

    def test_api_delete_uses_relative_to(self):
        """删除口 api_delete 也必须做路径边界判定。

        【为什么单列】读取口和删除口消费的是**同一份** outputs 数据。
        只修读取口、留下删除口，等于把「越界读」换成「越界删」——
        严重程度不降反升。
        """
        src = _server_source()
        m = re.search(r"async def api_delete\(request\):(.*?)(?=\nasync def |\ndef )",
                      src, re.S)
        self.assertIsNotNone(m, "找不到 api_delete")
        body = m.group(1)
        self.assertIn("relative_to", body,
                      "api_delete 没有用 relative_to 做路径边界判定")


class TestPathTraversalSemantics(unittest.TestCase):
    """判据本身的语义：前缀相同但**路径不同**的目录必须被拒绝。"""

    def test_sibling_dir_with_same_prefix_is_rejected(self):
        """`outputs_secret` 与 `outputs` 前缀相同，但不在 outputs 内。"""
        import tempfile
        from pathlib import Path as P
        with tempfile.TemporaryDirectory(prefix="h3-path-") as tmp:
            root = P(tmp)
            out_dir = root / "outputs"
            out_dir.mkdir()
            secret = root / "outputs_secret"
            secret.mkdir()
            (secret / "creds.txt").write_text("canary", encoding="utf-8")
            (out_dir / "ok.txt").write_text("fine", encoding="utf-8")

            def inside(target: P) -> bool:
                """与修复后的 server.py 同款判据。"""
                try:
                    target.relative_to(out_dir.resolve())
                    return True
                except ValueError:
                    return False

            # 越界目标必须被拒
            self.assertFalse(inside((secret / "creds.txt").resolve()),
                             "outputs_secret 里的文件被判定为在 outputs 内 —— "
                             "路径穿越没被拦住")
            # 目录内的文件必须被放行
            self.assertTrue(inside((out_dir / "ok.txt").resolve()),
                            "outputs 里的正常文件被误拒")

    def test_startswith_would_have_been_wrong(self):
        """反证：旧的 startswith 判据**确实**会放行越界路径。

        【为什么要有这条】如果不锁住「旧判据是错的」这个事实，
        将来有人「简化」回 startswith 时，本文件上面的用例会挂 ——
        但这条会明确告诉他「你改回去的那个东西是 bug」。
        """
        import tempfile
        from pathlib import Path as P
        with tempfile.TemporaryDirectory(prefix="h3-path-") as tmp:
            root = P(tmp)
            out_dir = root / "outputs"
            out_dir.mkdir()
            secret = root / "outputs_secret"
            secret.mkdir()
            target = (secret / "creds.txt").resolve()
            old_ok = str(target).startswith(str(out_dir.resolve()))
            self.assertTrue(old_ok,
                            "startswith 判据竟然没放行越界路径 —— "
                            "本文件的前提变了，需要重新评估")


if __name__ == "__main__":
    unittest.main()
