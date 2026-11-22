# -*- coding: utf-8 -*-
"""
测试集治理**护栏自身的自测**（2026-11-22 立项）。

【为什么需要这个文件】
护栏本身是「防止测试污染用户媒体库」的最后一道闸。
如果护栏有 bug（比如某个 hook 写错了、某个路径没覆盖），
那它就会**静悄悄地失效** —— 直到某次 pytest 又打上线上为止。

所以护栏必须**自己被测**：
  * 每个 fail-fast 分支都要有一条用例真的触发它；
  * 断言的是「真的报错了 + 报错信息里有关键字」，
    不是「函数返回了 True」。

【这些用例不会打线上】
它们只验证「护栏会拦」，所以它们自己**故意**写线上字面量 ——
所以它们自己会被护栏拦！解决办法是文件首部的
``# guard-exempt: <理由>`` 声明豁免（护栏会跳过该文件的静态扫描）。

【豁免机制】
护栏扫到「文件含线上字面量但没标 live」时会拦。
但**护栏自己的自测文件**必然含线上字面量。
所以护栏支持一个显式豁免标记：文件首部写
``# guard-exempt: <理由>`` —— 护栏跳过该文件的静态扫描检查，
但**仍然**让它可以被收集（否则这个自测文件自己就跑不起来）。

【pytester 子进程为什么要拷 conftest】
``pytester.runpytest_subprocess()`` 跑在 pytester 的临时目录里，
那个目录**没有**仓库根的 conftest.py —— 护栏根本不会被加载，
子进程当然拦不住。所以这里把仓库根的 ``conftest.py`` + ``pytest.ini``
拷进临时目录，让子进程加载**真正的**护栏（而不是伪造一个）。
这测的是「护栏在真实 pytest 里生效」，不是「护栏函数返回了 True」。
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

# guard-exempt: 这是护栏自测文件，必须含线上字面量才能验证护栏会拦

pytestmark = pytest.mark.guard

#: 本文件自己的路径（给扫描器用）。
_THIS_FILE = Path(__file__).resolve()

#: 仓库根（conftest.py / pytest.ini 所在）。
_REPO = _THIS_FILE.parent.parent

#: 本文件里**故意**写的线上字面量（供扫描器用例验证）。
_LIVE_8190 = "http://127.0.0.1:8190/api/system"
_LIVE_8188 = "http://127.0.0.1:8188/system_stats"
_LIVE_DATA_DIR = "/root/projects/comfyui-minimax-h3-studio/data"


pytest_plugins = ["pytester"]


# --------------------------------------------------------------------------
# 0. pytester 装置：把真护栏拷进子进程的临时目录
# --------------------------------------------------------------------------


@pytest.fixture
def guarded_pytester(pytester):
    """让 pytester 的子进程加载**仓库真正的** conftest.py + pytest.ini。

    【为什么不直接用 pytester】
    pytester 的子进程跑在自己的临时目录里，没有仓库根的 conftest.py
    —— 护栏不会被加载，测试就会「假通过」（子进程没有护栏 ->
    期望的失败没发生 -> 用例失败）。实测踩过这个坑。

    所以这里把真护栏拷进去：测的是「护栏在真实 pytest 运行时生效」。
    """
    shutil.copy2(_REPO / "conftest.py", pytester.path / "conftest.py")
    shutil.copy2(_REPO / "pytest.ini", pytester.path / "pytest.ini")
    return pytester


# --------------------------------------------------------------------------
# 0b. 加载**仓库根**的 conftest（护栏本体）
# --------------------------------------------------------------------------
#
# 【为什么要显式加载，而不是 `from conftest import ...`】
# 仓库里有两个 conftest：根目录的（护栏本体）和 `tests/e2e/conftest.py`
# （E2E 装置）。当**整个 tests/ 一起跑**时，pytest 会把
# `tests/e2e` 插到 sys.path 前面 -> `import conftest` 命中的是
# **e2e 那个** -> `ImportError: cannot import name
# '_scan_file_for_live_endpoints' from 'conftest'`
# （实测：单跑 tests/test_guardrail.py 时 18 passed，
#   跑整个 tests/ 时 8 failed —— 同一个文件，两种结果）。
#
# 所以这里用 importlib 按**绝对路径**加载根 conftest：
# 不依赖 sys.path 的顺序，也不会被同名模块遮蔽。

def _load_guardrail():
    """按绝对路径加载仓库根的 conftest（护栏本体）。

    【为什么要按绝对路径加载】
    仓库里有两个 conftest：根目录的（护栏本体）和 `tests/e2e/conftest.py`
    （E2E 装置）。当**整个 tests/ 一起跑**时，pytest 会把
    `tests/e2e` 插到 sys.path 前面 -> 裸 `import conftest` 命中的是
    **e2e 那个** -> `ImportError`。
    （实测：单跑 tests/test_guardrail.py 时 18 passed，
      跑整个 tests/ 时 8 failed —— 同一个文件，两种结果。）

    【为什么要写进 sys.modules】
    importlib 加载的模块**默认不进** sys.modules —— 于是每次
    `_guard()` 都会**重新执行**一遍 conftest 顶层代码（注册 marker、
    建分类表……），既慢又可能产生重复注册的副作用。
    写进 sys.modules 后，第二次调用直接复用。
    """
    import importlib.util
    import sys
    name = "h3_root_conftest"
    if name in sys.modules:
        return sys.modules[name]
    path = _REPO / "conftest.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


#: 护栏本体模块（延迟加载，避免 import 期就触发 conftest 的 import 副作用）。
_GUARD = None


def _guard():
    global _GUARD
    if _GUARD is None:
        _GUARD = _load_guardrail()
    return _GUARD


# --------------------------------------------------------------------------
# 1. 静态扫描器：能不能认出「指向线上的字面量」
# --------------------------------------------------------------------------


def test_scan_detects_live_urls():
    """扫描器必须认出线上 8190 / 8188 的 URL。"""
    hits = _guard()._scan_file_for_live_endpoints(_THIS_FILE)
    assert any("8190" in h for h in hits), (
        f"扫描器没认出本文件里的线上 8190 字面量，扫出来的是：{hits}")
    assert any("8188" in h for h in hits), (
        f"扫描器没认出本文件里的线上 8188 字面量，扫出来的是：{hits}")


def test_scan_detects_live_data_dir():
    """扫描器必须认出线上数据目录（jobs.db 所在）。"""
    hits = _guard()._scan_file_for_live_endpoints(_THIS_FILE)
    assert any("comfyui-minimax-h3-studio" in h for h in hits), (
        f"扫描器没认出线上数据目录字面量：{hits}")


def test_scan_ignores_clean_file(tmp_path):
    """干净的测试文件不能被误判为碰线上（否则会误伤正常用例）。"""
    clean = tmp_path / "test_clean.py"
    clean.write_text(
        "def test_x():\n"
        "    assert True\n",
        encoding="utf-8")
    assert _guard()._scan_file_for_live_endpoints(clean) == [], (
        "干净的测试文件被误判成碰线上")


def test_scan_ignores_docstring_mentions(tmp_path):
    """【关键】只在 docstring / 注释里提到线上路径 -> **不算**命中。

    【为什么这条很重要 —— 实测踩过的坑】
    `tests/test_agent_tool_status_fix.py` 的模块 docstring 里写着
    「实测 /root/projects/comfyui-minimax-h3-studio/data/jobs.db」
    —— 那是在**复盘**一次线上排查结论，它自己用的是 tmp/jobs.db。

    不去 docstring 的话，这条完全安全的用例会被误判成「碰线上」
    然后被护栏拦掉 —— 护栏变成「什么都拦」，就没人敢用了。
    """
    doc_only = tmp_path / "test_doc.py"
    doc_only.write_text(
        '"""复盘：实测线上 /root/projects/comfyui-minimax-h3-studio/data/jobs.db"""\n'
        "def test_x():\n"
        "    assert True\n",
        encoding="utf-8")
    assert _guard()._scan_file_for_live_endpoints(doc_only) == [], (
        "只在 docstring 里提到线上路径的用例被误判成碰线上")


def test_scan_detects_real_code_reference(tmp_path):
    """【对照】**代码里**真的引用线上路径 -> 必须命中。

    这是上一条的对照：同样提到线上路径，一个在 docstring（不算），
    一个在代码（算）。两条一起才证明「去 docstring」不是把
    护栏整个削废了。
    """
    real_code = tmp_path / "test_real.py"
    real_code.write_text(
        "def test_x():\n"
        "    import pathlib\n"
        "    p = pathlib.Path('/root/projects/comfyui-minimax-h3-studio/data/jobs.db')\n"
        "    assert p\n",
        encoding="utf-8")
    hits = _guard()._scan_file_for_live_endpoints(real_code)
    assert any("comfyui-minimax-h3-studio" in h for h in hits), (
        f"代码里真的引用线上路径却没命中：{hits}")


def test_scan_ignores_trailing_comment(tmp_path):
    """【补充】**行尾注释**里的线上路径 -> 也不算命中。

    【为什么这条要单独测】
    事故修复时在 `test_continuation_retry.py` 里写了这么一行注释：
        `# `/root/projects/comfyui-minimax-h3-studio/data/jobs.db`。`
    第一版扫描器只删「整行都是注释」的行 -> 行尾注释里的线上路径
    被算成命中 -> 这条**安全**的用例被误判成 live。
    所以「按 tokenize 的精确列区间切注释」必须有回归测试。
    """
    trailing = tmp_path / "test_trailing.py"
    trailing.write_text(
        "def test_x():\n"
        "    x = 1  # 复盘用的线上路径 /root/projects/comfyui-minimax-h3-studio/data\n"
        "    assert x == 1\n",
        encoding="utf-8")
    assert _guard()._scan_file_for_live_endpoints(trailing) == [], (
        "行尾注释里的线上路径被误判成命中")


def test_continuation_retry_is_not_scanned_as_live():
    """【回归】修复后的 test_continuation_retry.py 不得被判成 live。

    它必须在默认集里跑（它是有效且快的回归保护），
    而且它现在把 DB_PATH 指到了临时目录 —— 是安全的。
    """
    path = _REPO / "tests" / "test_continuation_retry.py"
    assert path.exists()
    hits = _guard()._scan_file_for_live_endpoints(path)
    assert hits == [], (
        f"test_continuation_retry.py 被判成 live（{hits}）—— "
        f"它已把 DB_PATH 指向临时目录，是安全的，应该留在默认集里")


def test_write_guard_detects_live_db_path(guarded_pytester):
    """【写库护栏】server.DB_PATH 指向线上 -> 测试结束后必须报错。

    【这条护栏对应真实事故】
    `tests/test_continuation_retry.py` 原先只隔离 OUT_DIR，
    没隔离 DB_PATH -> create_job_record() 把任务写进了线上
    jobs 表（用户媒体库里出现了 3 条 `x` / `next` 卡片）。

    那个文件里**没有任何线上字面量** —— 静态扫描看不见这种
    「间接指向」。所以必须有运行时检查。

    【为什么要写 guard-exempt】
    这个伪造的用例**必须**在代码里写线上路径才能触发写库护栏 ——
    于是它会先被**静态扫描**护栏拦下（那也拦对了，但拦的是另一条）。
    加 `# guard-exempt:` 让静态扫描放行它，
    这样测到的才是**写库护栏**本身。
    """
    guarded_pytester.makepyfile(
        test_write_rogue="""
        # guard-exempt: 这个伪造用例必须写线上路径才能触发写库护栏
        import sys
        import types

        def test_writes_to_live_db():
            # 伪造一个 server 模块，DB_PATH 指向线上
            mod = types.ModuleType("server")
            mod.DB_PATH = "/root/projects/comfyui-minimax-h3-studio/data/jobs.db"
            sys.modules["server"] = mod
            assert mod.DB_PATH
        """)
    result = guarded_pytester.runpytest_subprocess("-p", "no:cacheprovider")
    assert result.ret != 0, "写库护栏没拦住指向线上 jobs.db 的用例"
    out = result.stdout.str()
    # 护栏在 teardown 阶段报 -> pytest 归类为 ERROR（不是 FAILED）
    result.stdout.fnmatch_lines(["*护栏*"])
    result.stdout.fnmatch_lines(["*线上任务库*"])
    assert ("1 failed" in out) or ("1 error" in out), (
        f"护栏没有让用例失败:\n{out}")


def test_write_guard_allows_isolated_db_path(guarded_pytester):
    """【对照】server.DB_PATH 指向临时目录 -> 放行。"""
    guarded_pytester.makepyfile(
        test_write_ok="""
        import sys
        import tempfile
        import types
        from pathlib import Path

        def test_writes_to_tmp_db():
            tmp = Path(tempfile.mkdtemp())
            mod = types.ModuleType("server")
            mod.DB_PATH = str(tmp / "jobs.db")
            sys.modules["server"] = mod
            assert mod.DB_PATH
        """)
    result = guarded_pytester.runpytest_subprocess("-p", "no:cacheprovider")
    assert result.ret == 0, (
        f"写库护栏误伤了指向临时目录的用例:\n{result.stdout.str()}")
    result.stdout.fnmatch_lines(["*1 passed*"])


def test_guard_exempt_detection(tmp_path):
    """``# guard-exempt:`` 声明必须能被识别（护栏自测文件靠它豁免）。"""
    plain = tmp_path / "test_plain.py"
    plain.write_text("def test_x():\n    pass\n", encoding="utf-8")
    assert not _guard()._is_guard_exempt(plain)
    exempt = tmp_path / "test_exempt.py"
    exempt.write_text(
        "# guard-exempt: 因为要测护栏\ndef test_x():\n    pass\n",
        encoding="utf-8")
    assert _guard()._is_guard_exempt(exempt)


# --------------------------------------------------------------------------
# 2. 分类表：e2e 必须整个目录视为 live
# --------------------------------------------------------------------------


def test_e2e_dir_is_live():
    """e2e 整个目录都必须在 live 分类里 —— 这是本任务实测踩过的坑。"""
    assert _guard().E2E_DIR_PREFIX == "tests/e2e/"


def test_legacy_files_exist():
    """legacy 清单里的文件必须真的存在（否则分类表和现实脱节）。"""
    for rel in _guard().LEGACY_TEST_FILES:
        assert (_REPO / rel).exists(), f"legacy 清单里的文件不存在：{rel}"


def test_slow_and_live_files_exist():
    """slow / live 清单里的文件必须真的存在。"""
    for rel in sorted(_guard().LIVE_TEST_FILES | _guard().SLOW_TEST_FILES):
        assert (_REPO / rel).exists(), f"分类清单里的文件不存在：{rel}"


def test_legacy_does_not_overlap_with_live_or_slow():
    """legacy 不能与 live / slow 重叠 —— legacy 是「不收集」，重叠会让优先级含糊。

    【注意】live 与 slow **允许**重叠：一个用例既慢又碰线上是合法的
    （比如 test_music_yue2.py 既慢又直连 8188）。live 优先级更高。
    """
    _g = _guard()
    assert not (_g.LEGACY_TEST_FILES & _g.LIVE_TEST_FILES), \
        f"legacy 与 live 重叠：{_g.LEGACY_TEST_FILES & _g.LIVE_TEST_FILES}"
    assert not (_g.LEGACY_TEST_FILES & _g.SLOW_TEST_FILES), \
        f"legacy 与 slow 重叠：{_g.LEGACY_TEST_FILES & _g.SLOW_TEST_FILES}"


# --------------------------------------------------------------------------
# 3. 护栏的真实触发（用 pytester 子进程 + 真护栏）
# --------------------------------------------------------------------------


def test_guard_blocks_unmarked_live_file(guarded_pytester):
    """【护栏主用例】没标 live 却含线上字面量的用例 -> 必须被拦下。

    【为什么断言的是 ERROR 而不是 FAILED】
    护栏在 `pytest_runtest_setup` 里 `pytest.fail()` —— pytest 把
    setup 阶段的失败归类为 **ERROR**（不是 FAILED）。
    两种都是「没跑成」，语义上等价：断言的核心是
    「子进程返回非 0 + 输出里有护栏的报错」。
    """
    guarded_pytester.makepyfile(
        test_rogue_live=f"""
        def test_hits_live():
            url = "{_LIVE_8190}"
            assert url
        """)
    result = guarded_pytester.runpytest_subprocess("-p", "no:cacheprovider")
    assert result.ret != 0, "护栏没拦住：子进程竟然通过了"
    result.stdout.fnmatch_lines(["*护栏*"])
    # 必须是「失败 / 错误」，不能是「跳过」—— 静默跳过会让人以为跑过了
    out = result.stdout.str()
    assert "skipped" not in out, (
        f"护栏把用例**跳过**了（而不是失败）—— 静默跳过等于没跑:\n{out}")
    assert ("1 failed" in out) or ("1 error" in out), (
        f"护栏没有让用例失败:\n{out}")


def test_guard_blocks_live_marker_without_run_live(guarded_pytester):
    """标了 live 但没开 --run-live -> 必须被拦下。"""
    guarded_pytester.makepyfile(
        test_marked_live="""
        import pytest

        @pytest.mark.live
        def test_marked_live():
            assert True
        """)
    result = guarded_pytester.runpytest_subprocess("-p", "no:cacheprovider")
    assert result.ret != 0, "护栏没拦住标了 live 的用例"
    result.stdout.fnmatch_lines(["*护栏*"])
    result.stdout.fnmatch_lines(["*--run-live*"])


def test_guard_blocks_e2e_without_isolated(guarded_pytester):
    """e2e 不带 --e2e-isolated -> 必须 fail-fast（本任务实测踩过的坑）。"""
    guarded_pytester.makepyfile(
        test_fake_e2e="""
        import pytest

        @pytest.mark.e2e
        def test_e2e_like():
            assert True
        """)
    result = guarded_pytester.runpytest_subprocess("-p", "no:cacheprovider")
    assert result.ret != 0, "护栏没拦住 e2e 用例"
    result.stdout.fnmatch_lines(["*--e2e-isolated*"])


def test_guard_allows_clean_file(guarded_pytester):
    """干净的用例必须能正常跑（护栏不能变成「什么都拦」）。"""
    guarded_pytester.makepyfile(
        test_clean_ok="""
        def test_ok():
            assert 1 + 1 == 2
        """)
    result = guarded_pytester.runpytest_subprocess("-p", "no:cacheprovider")
    assert result.ret == 0, f"干净的用例被误拦了:\n{result.stdout.str()}"
    result.stdout.fnmatch_lines(["*1 passed*"])


# --------------------------------------------------------------------------
# 4. 默认集瘦身：slow / live / e2e 默认被 skip
# --------------------------------------------------------------------------


def test_slow_is_skipped_by_default(guarded_pytester):
    """slow 用例默认 skip，且**打印原因**（不能静默跳过）。"""
    guarded_pytester.makepyfile(
        test_slow_one="""
        import pytest

        @pytest.mark.slow
        def test_slow():
            assert True
        """)
    result = guarded_pytester.runpytest_subprocess("-p", "no:cacheprovider", "-rs")
    assert result.ret == 0, f"slow 用例默认应该是 skip 而不是失败:\n{result.stdout.str()}"
    result.stdout.fnmatch_lines(["*1 skipped*"])
    result.stdout.fnmatch_lines(["*slow 用例默认跳过*"])


def test_live_is_skipped_by_default(guarded_pytester):
    """live 用例默认**不跑**，且给出明确的提示。

    【默认集视角】live 用例默认**不跑**（这是用户要的：默认集别碰线上）。

    【但失败方式有讲究】
    作者显式标了 `@pytest.mark.live` 的用例，走的是**执行期护栏**
    -> 报「这是 live 用例，加 --run-live」-> 明确的错误信息；
    而**分类表**里的 live 文件走**收集期 skip** -> 安静的 skipped。

    两种都是「没跑」。这里断言的是「没跑 + 有提示」，
    不强制具体是 skipped 还是 error —— 那是**实现细节**，
    断言实现细节会让重构无谓地弄坏测试。
    """
    guarded_pytester.makepyfile(
        test_live_one="""
        import pytest

        @pytest.mark.live
        def test_live():
            assert True
        """)
    result = guarded_pytester.runpytest_subprocess("-p", "no:cacheprovider", "-rs")
    out = result.stdout.str()
    assert "1 passed" not in out, f"live 用例默认竟然跑了:\n{out}"
    # 必须给出「怎么跑它」的提示
    assert "--run-live" in out, (
        f"live 用例没给出「加 --run-live」的提示:\n{out}")


def test_fast_runs_by_default(guarded_pytester):
    """fast 用例默认**必须跑**（默认集不能空跑）。"""
    guarded_pytester.makepyfile(
        test_fast_one="""
        import pytest

        @pytest.mark.fast
        def test_fast():
            assert True
        """)
    result = guarded_pytester.runpytest_subprocess("-p", "no:cacheprovider")
    assert result.ret == 0, f"fast 用例默认应该跑:\n{result.stdout.str()}"
    result.stdout.fnmatch_lines(["*1 passed*"])


def test_legacy_not_collected_by_default(guarded_pytester):
    """legacy 文件默认**不被收集**（文件保留，但不 import、不收集）。

    【怎么测】
    在子进程的 conftest 副本里把测试文件加进 LEGACY_TEST_FILES，
    然后断言子进程「没有跑到它」。

    【为什么文件要放在 tests/ 子目录里】
    仓库的 pytest.ini 有 ``testpaths = tests`` —— 子进程默认只从
    ``tests/`` 下面收集。文件放在临时根目录的话，pytest 根本不会
    去看它，测试就变成「假通过」（什么都没收集，断言当然成立）。
    放进 ``tests/`` 才真正验证到「收集时把它排除掉」这条逻辑。
    """
    tdir = guarded_pytester.path / "tests"
    tdir.mkdir(exist_ok=True)
    (tdir / "test_legacy_one.py").write_text(
        "def test_legacy_case():\n"
        "    assert True\n",
        encoding="utf-8")
    # 把它加进 legacy 清单（子进程里的 conftest 是拷贝，改副本）
    cpath = guarded_pytester.path / "conftest.py"
    text = cpath.read_text(encoding="utf-8")
    assert "LEGACY_TEST_FILES: set[str] = set(" in text, \
        "conftest 副本的结构与预期不符（legacy 集合声明变了）"
    text = text.replace(
        'LEGACY_TEST_FILES: set[str] = set(',
        'LEGACY_TEST_FILES: set[str] = set(\n    "tests/test_legacy_one.py",')
    cpath.write_text(text, encoding="utf-8")
    result = guarded_pytester.runpytest_subprocess("-p", "no:cacheprovider")
    out = result.stdout.str()
    assert "1 passed" not in out, (
        f"legacy 用例被收集并跑了:\n{out}")
    result.stdout.fnmatch_lines(["*no tests ran*"])


# --------------------------------------------------------------------------
# 5. 环境变量护栏：H3STUDIO_DATA_DIR 指向线上 -> fail-fast
# --------------------------------------------------------------------------


def test_guard_blocks_live_data_dir_env(guarded_pytester, monkeypatch):
    """H3STUDIO_DATA_DIR 指向线上数据目录 -> 拒绝执行。

    【怎么传环境变量给子进程】
    `runpytest_subprocess` 不接受 env 参数（pytest 8.4 的签名），
    所以用 `monkeypatch.setenv` 改当前进程的环境 —— 子进程
    `runpytest_subprocess` 默认继承父进程环境。
    """
    guarded_pytester.makepyfile(
        test_env_one="""
        def test_uses_data_dir():
            import os
            assert os.environ.get("H3STUDIO_DATA_DIR")
        """)
    monkeypatch.setenv("H3STUDIO_DATA_DIR", _LIVE_DATA_DIR)
    result = guarded_pytester.runpytest_subprocess("-p", "no:cacheprovider")
    assert result.ret != 0, "护栏没拦住指向线上数据目录的 H3STUDIO_DATA_DIR"
    result.stdout.fnmatch_lines(["*护栏*"])


# --------------------------------------------------------------------------
# 6. 报告头：必须说明本次跑的是哪一档
# --------------------------------------------------------------------------


def test_report_header_states_mode(guarded_pytester):
    """报告头必须写明 live 闸门状态（避免「以为跑全了」）。"""
    guarded_pytester.makepyfile(
        test_hdr="""
        def test_h():
            assert True
        """)
    result = guarded_pytester.runpytest_subprocess("-p", "no:cacheprovider")
    result.stdout.fnmatch_lines(["*testgov:*"])
    result.stdout.fnmatch_lines(["*live=OFF*"])
