"""
Inject a stub for `h3studio.audio_deep.analyze` into the server process.

【为什么用 sitecustomize 而不是改产品代码】
真的深度音频管线在本机实测 116 秒素材要 ~600 秒（CLAP/AST/VAD/demucs/
whisper 全跑）。浏览器验收要跑 3 档宽度 x 8 个用例，等真管线不现实。

但**绝不能**为了测试去改产品代码（那会让「测试通过」变成「测试自己骗
自己」）。所以这里在**进程启动时**用 import hook 把
`h3studio.audio_deep.analyze` 换成即时返回的桩：

  * 导入路径、调用参数、异常类型、返回结构**全部不变**
  * analysis_jobs._run_audio_deep 走的仍是真代码
  * save_analysis / add_tags / 状态流转 / 回滚**全是真路径**
  * 只有「模型推理」这一层被替掉 —— 与既有图片验收脚本的做法一致

【怎么注入】
sitecustomize 在 CPython 启动时被 site 模块自动 import（只要它在
sys.path 上）。这里用 MetaPathFinder 拦截 `h3studio.audio_deep`：
先让真模块正常执行完，再把 `analyze` 换成桩。
"""
import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import os
import sys

TARGET = "h3studio.audio_deep"

STUB_TEXT = (
    "## 一、音频基本信息\n"
    "- 容器格式：wav\n"
    "- 时长：3.00 秒（00:03）\n"
    "- 采样率：16000 Hz\n"
    "\n"
    "## 二、节拍与速度\n"
    "- 测得 BPM：152\n"
    "- 节拍规整度：0.94\n"
    "\n"
    "## 三、乐器与音色\n"
    "- 人声：占比 0.98\n"
    "- 电子舞曲：占比 0.32\n"
)


def _stub_analyze(audio_path, options=None):
    """与 audio_deep.analyze 同签名的桩。"""
    opts = options or {}
    progress = opts.get("progress")
    if callable(progress):
        # 模拟阶段推进（真实管线也是这么回调的）
        for stage, frac in (("bpm", 0.15), ("instruments", 0.35),
                            ("vocals", 0.55), ("asr", 0.85),
                            ("llm", 0.95)):
            try:
                progress(stage, frac)
            except Exception:
                pass
    return {
        "text": STUB_TEXT,
        "extra": {
            "analyzer": "stub (browser verification)",
            "analyzer_mode": "deep_audio_pipeline",
            "bpm": {"bpm": 152.0},
            "instruments": {"summary": [{"name": "human singing voice",
                                         "weight": 0.98}]},
            "asr": {"engine": "stub", "full_text": ""},
            "elapsed_seconds": 0.1,
        },
        "raw": {"stub": True},
    }


class _PatchingLoader(importlib.abc.Loader):
    """加载真模块，然后把 analyze 换成桩。"""

    def __init__(self, real_loader):
        self._real = real_loader

    def create_module(self, spec):
        return self._real.create_module(spec)

    def exec_module(self, module):
        self._real.exec_module(module)
        module.analyze = _stub_analyze

    def get_filename(self, name):
        getter = getattr(self._real, "get_filename", None)
        return getter(name) if getter else None

    def get_source(self, name):
        getter = getattr(self._real, "get_source", None)
        return getter(name) if getter else None

    def is_package(self, name):
        checker = getattr(self._real, "is_package", None)
        return bool(checker(name)) if checker else False


class _PatchingFinder(importlib.abc.MetaPathFinder):
    """拦截目标模块的导入。"""

    def find_spec(self, fullname, path=None, target=None):
        if fullname != TARGET:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if spec is None or spec.loader is None:
            return None
        spec.loader = _PatchingLoader(spec.loader)
        return spec


if os.environ.get("H3STUDIO_AUDIO_DEEP_STUB") == "1":
    sys.meta_path.insert(0, _PatchingFinder())
