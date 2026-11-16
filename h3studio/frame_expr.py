# -*- coding: utf-8 -*-
"""H3 的「秒数 -> 帧数」表达式节点（**唯一真源**）。

【为什么单独一个模块 —— 第5轮代码审查 C-13】

第4轮报告 C-13 记的是：「``_frame_expr_node`` 三份重复实现（已分叉）」。
现场核实（三份都在，公式字符串**恰好相同**，但**签名与语义分叉**）：

  h3studio/video_workflow.py:36
      def _frame_expr_node(length_node_id, dur_node_id, duration)
      -> dur_node 的 value = float(duration)
      -> 调用方：build_t2v_panel（传真实 duration）

  h3studio/h3_video_modes.py:68
      def _frame_expr_node(length_node_id, dur_node_id, duration)
      -> dur_node 的 value = float(duration)
      -> 调用方：build_flf / build_r2v（传真实 duration）

  h3studio/workflows.py:260
      def _frame_expr_node(length_node_id, dur_node_id)
      -> dur_node 的 value = **5（写死！）**
      -> 调用方：_h3_video_workflow（旧路径）靠**调用方事后覆盖**
         ``expr["105:111"]["inputs"]["value"] = duration``

三份里 workflows.py 那份**没有 duration 形参**，靠调用方手动覆盖
``expr["105:111"]["inputs"]["value"]`` —— 这是「值正确但形状脆弱」的典型：
哪天调用方忘了覆盖，就会静默用 5 秒出片。

【为什么不能简单合并到现有模块】
  * 放进 video_workflow.py -> workflows.py 得 import video_workflow，
    而 video_workflow.py:427 又 import workflows -> **循环依赖**；
  * 放进 workflows.py -> video_workflow / h3_video_modes 都得 import
    workflows -> 同样会把 workflows 拖进它们的 import 链（更糟）。

所以这里单独开一个**零依赖**模块：谁都能 import，不产生环。

【公式 —— 与官方工作流一致，不要改】
    max(5, round(a * 24)) + (5 - (max(5, round(a * 24)) % 17)) % 17
含义：24fps；至少 5 帧；并对齐到 H3 的「17k+5」帧网格。
"""

from __future__ import annotations


#: 秒 -> 帧 的表达式（H3 的 17k+5 网格，24fps）。
#: 【与官方工作流一致】改动它 = 改变所有视频的时长语义。
FRAME_EXPR = "max(5, round(a * 24)) + (5 - (max(5, round(a * 24)) % 17)) % 17"


def frame_expr_node(length_node_id: str, dur_node_id: str,
                    duration: float) -> dict:
    """构造「秒数 -> 帧数」的两个节点。

    参数
      length_node_id   ComfyMathExpression 的节点 id
      dur_node_id      PrimitiveFloat（时长秒数）的节点 id
      duration         时长（秒）

    返回 {length_node_id: {...}, dur_node_id: {...}} —— 两个节点。

    【为什么 duration 是必填参数】
    旧的三份实现里，workflows.py 那份**没有**这个参数（写死 5 秒，
    靠调用方事后覆盖）。统一后 duration 必填 ->
    「忘了覆盖」这种静默错误在**类型层面**就不可能再发生。
    """
    return {
        length_node_id: {
            "inputs": {
                "expression": FRAME_EXPR,
                "values.a": [dur_node_id, 0],
            },
            "class_type": "ComfyMathExpression",
            "_meta": {"title": "时长->帧数"},
        },
        dur_node_id: {
            "inputs": {"value": float(duration)},
            "class_type": "PrimitiveFloat",
            "_meta": {"title": "时长(秒)"},
        },
    }
