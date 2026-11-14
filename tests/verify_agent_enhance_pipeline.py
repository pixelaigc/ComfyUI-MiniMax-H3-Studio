# -*- coding: utf-8 -*-
"""验证需求 2a/2b/2c/2d：Agent 默认参数 + 增强提示词管线。

【纪律】
  * 用**真实的** agent 代码路径（_tool_create_job 的合并逻辑），
    不是另写一份模拟。
  * 断言打在真实的 params / 任务记录上。
  * 增强器用**替身**注入（不真跑 9B 模型），但**注入点在管线内部**
    —— 这样能验证「增强结果确实进了 params、进了媒体库字段」。
"""
import asyncio
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from h3studio import agent as agent_mod          # noqa: E402
from h3studio import agent_enhance as AE          # noqa: E402


class FakeProvider:
    name = "fake-provider"


class FakeCtx:
    def __init__(self):
        self.provider = FakeProvider()
        self.server = FakeServer()
        self.jobs = []
        self.session_id = "test-session"
        self.assets = []
        self.attempted_modes = []
        self.enhanced = None
        self.session_store = None
        self.memory_store = None


class FakeServer:
    """最小服务端替身：只实现 create_job 用到的那几个入口。"""

    def __init__(self):
        self.created = []

    def prepare_job_params(self, mode, params, asset_refs, files):
        return asset_refs, []

    async def prepare_agent_t2v_params(self, params):
        return params, None

    def create_job_record(self, mode, params):
        self.created.append((mode, dict(params)))
        jid = f"job-{len(self.created)}"
        return jid


def _stub_text_enhance(caption):
    """把文本增强替身注入到管线内部。"""
    async def fake(spec_id, original, ctx):
        return caption
    AE._text_enhance = fake


def _restore():
    import importlib
    importlib.reload(AE)


async def main():
    handler = agent_mod.TOOLS["create_job"]["handler"]

    # ============ 需求 2a/2b：默认参数 + 用户优先 ============
    print("=" * 70)
    print("需求 2a/2b：默认参数（用户没说才用；用户说了以用户为准）")
    print("=" * 70)
    _stub_text_enhance("ENHANCED-PROMPT")
    ctx = FakeCtx()
    await handler(ctx, {"mode": "t2i_moss", "prompt": "a red teapot"})
    mode, params = ctx.server.created[-1]
    print("用户什么都没说 ->")
    print("   model  =", params.get("model"), "(来自 mode_defaults)")
    print("   aspect =", params.get("aspect"), "(来自 mode_defaults)")
    assert params.get("model") == "moss_image_base", "默认模型没套上"
    assert params.get("aspect") == "16:9", "默认比例没套上"

    ctx2 = FakeCtx()
    await handler(ctx2, {"mode": "t2i_moss", "prompt": "a red teapot",
                         "aspect": "9:16", "model": "moss_image_sft"})
    mode, params = ctx2.server.created[-1]
    print("用户显式指定 aspect=9:16 / model=moss_image_sft ->")
    print("   model  =", params.get("model"))
    print("   aspect =", params.get("aspect"))
    assert params.get("aspect") == "9:16", "默认比例覆盖了用户的显式值！"
    assert params.get("model") == "moss_image_sft", "默认模型覆盖了用户的显式值！"
    print("   -> 用户显式值未被默认覆盖 ✓")

    # ============ 需求 2c：增强提示词真的进了 params ============
    print()
    print("=" * 70)
    print("需求 2c：增强后的提示词 = 实际生成提示词")
    print("=" * 70)
    ctx3 = FakeCtx()
    await handler(ctx3, {"mode": "t2i_moss", "prompt": "用 moss-image 生成一张女性人像，全身，写实，站在沙滩边"})
    mode, params = ctx3.server.created[-1]
    print("params['prompt']          =", repr(params.get("prompt")))
    print("params['original_prompt'] =", repr(params.get("original_prompt")))
    print("params['prompt_enhanced']=", params.get("prompt_enhanced"))
    assert params.get("prompt") == "ENHANCED-PROMPT", "增强后的提示词没进 params"
    assert params.get("original_prompt", "").startswith("用 moss-image"), "原始提示词没保留"
    assert params.get("prompt_enhanced") is True, "没标记为已增强"
    print("   -> 增强结果进了 params ✓（工作流与媒体库都读 params['prompt']）")

    # ============ 需求 2d：任务记录带完整标签 ============
    print()
    print("=" * 70)
    print("需求 2d：产物带完整标签与元数据")
    print("=" * 70)
    entry = ctx3.jobs[-1]
    print("entry['prompt']           =", repr(entry.get("prompt")))
    print("entry['original_prompt']  =", repr(entry.get("original_prompt")))
    print("entry['prompt_enhanced']  =", entry.get("prompt_enhanced"))
    print("entry['defaults_applied'] =", entry.get("defaults_applied"))
    print("entry['enhance_notices']  =", entry.get("enhance_notices"))
    assert entry.get("prompt") == "ENHANCED-PROMPT"
    assert entry.get("prompt_enhanced") is True
    assert entry.get("defaults_applied"), "默认参数清单为空"

    # ============ 增强失败 -> 降级 + 如实告知 ============
    print()
    print("=" * 70)
    print("增强失败：降级到原始提示词 + 如实告知（不假装增强过）")
    print("=" * 70)

    async def failing(spec_id, original, ctx):
        raise RuntimeError("模拟 Provider 挂了")
    AE._text_enhance = failing
    ctx4 = FakeCtx()
    await handler(ctx4, {"mode": "t2i_moss", "prompt": "原始提示词"})
    mode, params = ctx4.server.created[-1]
    print("params['prompt'] =", repr(params.get("prompt")))
    print("entry['prompt_enhanced'] =", ctx4.jobs[-1].get("prompt_enhanced"))
    print("entry['enhance_notices'] =", ctx4.jobs[-1].get("enhance_notices"))
    assert params.get("prompt") == "原始提示词", "失败时应该用原始提示词"
    assert ctx4.jobs[-1].get("prompt_enhanced") is False, "失败时不该标成已增强"
    assert ctx4.jobs[-1].get("enhance_notices"), "失败时必须如实告知"
    print("   -> 降级 + 如实告知 ✓")

    # ============ gen_params（媒体库读的字段）============
    print()
    print("=" * 70)
    print("媒体库 gen_params：增强后的提示词 + 原始提示词 + 增强标记")
    print("=" * 70)
    from h3studio import job_meta
    gp = job_meta.gen_params("t2i_moss", {
        "prompt": "ENHANCED-PROMPT",
        "original_prompt": "原始提示词",
        "prompt_enhanced": True,
    })
    print("gen_params['prompt']           =", repr(gp.get("prompt")))
    print("gen_params['original_prompt']  =", repr(gp.get("original_prompt")))
    print("gen_params['prompt_enhanced']  =", repr(gp.get("prompt_enhanced")))
    assert gp.get("prompt") == "ENHANCED-PROMPT"
    assert gp.get("original_prompt") == "原始提示词"
    assert gp.get("prompt_enhanced") == "1"
    print("   -> 媒体库能看到增强后的提示词 ✓")

    _restore()
    print()
    print("ALL CHECKS PASSED")


asyncio.run(main())
