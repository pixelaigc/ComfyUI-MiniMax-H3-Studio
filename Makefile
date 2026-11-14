# ComfyUI-MiniMax-H3-Studio —— 常用本地任务入口
#
# 【只放「不会改动运行时行为」的文档类目标】
# 服务启动 / 部署 / 测试都不在这里 —— 那些走 scripts/deploy_to_8190.sh 等既有脚本，
# 这里只放「文档/资源再生成」这类纯输出任务，避免两条路径打架。
#
# 用法：
#   make flow        # 重新生成交互流程图（.mmd + SVG + PNG + 派生清单）
#   make flow-check  # 校验已提交的流程图与当前代码是否一致（CI 用，不写文件）

PYTHON ?= python3

.PHONY: flow flow-check help

help:
	@echo "make flow        重新生成交互流程图（docs/交互流程图-*.mmd/.svg/.png + 派生清单）"
	@echo "make flow-check  校验已提交的流程图与当前代码一致"

flow:
	$(PYTHON) scripts/gen_flow.py --render

flow-check:
	$(PYTHON) scripts/gen_flow.py --check
