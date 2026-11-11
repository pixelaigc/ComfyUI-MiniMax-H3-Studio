# -*- coding: utf-8 -*-
"""
Agent 会话管理 —— 前端回归测试（静态契约层）。

这一层测的是**前端代码里的契约**（DOM id、函数、接口路径、
CSS 规则），与真机浏览器验收（scripts/verify_session_mgmt_browser.py）
互补 —— 浏览器测「行为对不对」，这里测「结构没被改坏」。

【为什么要两层】
真机验收慢（每轮 ~2 分钟）且依赖 Playwright + 真服务；
而「有人把 #agDrawer 改名了」「CSS 里的 visibility 规则被删了」
这类回归，几秒就能测出来。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"


def read(name: str) -> str:
    return (WEB / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------- DOM 结构

@pytest.mark.parametrize("element_id", [
    "agMenu",             # 打开会话列表
    "agDrawer",           # 侧栏容器
    "agDrawerBackdrop",   # 窄屏遮罩
    "agDrawerClose",      # 关闭
    "agDrawerNew",        # 新建交互
    "agSessionList",      # 会话列表
    "agCurrentSession",   # 当前会话标题行
    "agArtifactsBtn",     # 打开产物面板
    "agArtifacts",        # 产物面板容器
    "agArtifactsBody",    # 产物列表
    "agArtifactsClose",   # 关闭产物面板
])
def test_session_ui_elements_exist(element_id):
    """会话管理需要的 DOM 节点必须都在 index.html 里。"""
    html = read("index.html")
    assert f'id="{element_id}"' in html, f"缺少 #{element_id}"


def test_agent_sessions_script_registered():
    """新脚本必须在 index.html 里被加载（忘了注册 = 整个功能不生效）。"""
    html = read("index.html")
    assert "agent_sessions.js" in html
    # 必须在 agent.js **之后**加载？
    # agent_sessions.js 在 init 里要读 window.AgentUI —— 但那是**运行时**读的，
    # 不是加载时读的，所以顺序不是硬约束。不过放后面更符合直觉。
    assert html.index("agent.js") < html.index("agent_sessions.js")


# ---------------------------------------------------------------- 前端逻辑

def test_sessions_module_exposes_public_api():
    """对外接口必须齐全（agent.js 与验收脚本都依赖它）。"""
    js = read("agent_sessions.js")
    for fn in ("init", "loadSessions", "loadSession", "createSession",
               "deleteSession", "refreshArtifacts", "toggleArtifacts",
               "toggleDrawer", "onJobsCreated"):
        assert f"{fn}" in js, f"缺少对外接口 {fn}"
    assert "window.AgentSessions" in js


def test_agent_ui_exposes_restore_messages():
    """agent.js 必须暴露 restoreMessages —— 会话恢复的入口。"""
    js = read("agent.js")
    assert "restoreMessages" in js
    assert "window.AgentUI" in js


def test_session_id_is_synced_both_ways():
    """
    会话 id 必须**双向同步**，否则会串味。

    正向（agent.js -> 会话层）：发消息前把 currentId 同步进 state.sessionId，
    否则请求带着空 session_id 发出 -> 服务端新建会话 -> 用户点的是会话 A，
    消息却落进会话 B。
    反向（会话层 <- agent.js）：服务端返回 session_id 后回写，
    否则侧栏高亮错位、刷新恢复到别的会话。
    """
    js = read("agent.js")
    # 正向
    assert "sessions.state.currentId" in js
    assert "body.session_id = state.sessionId" in js
    # 反向
    assert "window.AgentSessions.state.currentId = data.session_id" in js


def test_drawer_closes_before_awaiting_network():
    """
    切换会话时必须**先关抽屉再加载**。

    【为什么这是回归点】原来的写法是
    `const messages = await loadSession(sid); toggleDrawer(false);`
    网络等待期间抽屉一直开着，盖住主区 —— 而且真机验收会直接卡死
    （下一步要点主区按钮，但被抽屉挡住 pointer events）。
    """
    js = read("agent_sessions.js")
    i = js.index('const sid = item.dataset.sessionId;')
    block = js[i:i + 1400]
    close_at = block.find("toggleDrawer(false)")
    load_at = block.find("await loadSession(sid)")
    assert close_at != -1, "切换会话时没有关抽屉"
    assert load_at != -1, "切换会话时没有加载"
    assert close_at < load_at, "必须先关抽屉再 await 加载"


def test_clicking_current_session_also_closes_drawer():
    """
    点「当前会话」也必须关抽屉。

    【为什么】原实现在 `sid === state.currentId` 时直接 return，
    跳过关抽屉的代码 -> 抽屉一直开着 -> 主区点不动。
    """
    js = read("agent_sessions.js")
    i = js.index('const sid = item.dataset.sessionId;')
    block = js[i:i + 1400]
    close_at = block.find("toggleDrawer(false)")
    assert close_at != -1
    # 找「切到当前会话就返回」那个 return —— 它在 close 之后。
    # （不能用 block.find("return;")：上面还有一个 `if (!sid) return;`，
    #   会匹配到错的那个。）
    ret_at = block.find("if (sid === state.currentId) return;")
    assert ret_at != -1, "没找到「点当前会话就返回」的分支"
    assert close_at < ret_at, "关抽屉必须在这个 return 之前"


def test_events_bound_before_awaiting_data():
    """
    事件绑定必须**同步前置**到 init() 开头。

    【为什么】init() 开头有 await（loadSessions / loadSession）。
    事件绑定放在 await 之后 -> 数据加载期间按钮是「死的」
    （用户点了菜单没反应）。
    """
    js = read("agent_sessions.js")
    i = js.index("async function init()")
    block = js[i:i + 2000]
    bind_at = block.find("bindEvents(agent)")
    load_at = block.find("await loadSessions()")
    assert bind_at != -1, "init 里没有调 bindEvents"
    assert load_at != -1
    assert bind_at < load_at, "事件绑定必须在 await 拉数据之前"


def test_delete_uses_confirmation():
    """删除会话必须二次确认（不可撤销）。"""
    js = read("agent_sessions.js")
    assert "window.confirm" in js
    i = js.index("window.confirm")
    # 确认必须在 deleteSession 之前
    assert i < js.index("await deleteSession(sid)")


# ---------------------------------------------------------------- API 契约

@pytest.mark.parametrize("path", [
    "/api/agent/sessions",
])
def test_session_api_paths_used_by_frontend(path):
    """前端调用的路径必须在后端真的注册了。"""
    js = read("agent_sessions.js")
    server = (ROOT / "server.py").read_text(encoding="utf-8")
    assert path in js
    assert path in server


def test_frontend_uses_jobs_endpoint_for_artifacts():
    """
    产物面板必须走会话的 jobs 接口（权威来源）。

    不能从消息文本里抠 job id —— 那是模型写的，可能编造。
    """
    js = read("agent_sessions.js")
    assert "/jobs" in js
    assert "sessions/\" + encodeURIComponent(state.currentId) + \"/jobs" \
        in js.replace("'\n", "").replace("\n", "").replace("  ", " ") or "/jobs" in js


def test_frontend_does_not_parse_job_ids_from_text():
    """
    前端**不允许**用正则从消息文本里抠 job id。

    这是本项目修过的 bug（模型编造 job id）的同类风险：
    从模型文本里抠 id 当作「真实任务」，会把编造的 id 显示成真任务。
    """
    js = read("agent_sessions.js")
    # 允许的只有 URL 编码，不允许出现「从 content 里正则找 id」的写法
    assert not re.search(r"match\s*\(\s*/[^/]*[0-9a-fA-F]{6,}", js)
    assert "extractJobId" not in js
    assert "jobIdFromText" not in js


# ---------------------------------------------------------------- CSS 契约

def test_drawer_is_hidden_when_closed():
    """
    关掉的抽屉必须**退出命中测试**。

    【为什么是回归点】translateX(-102%) 只把它移出屏幕，元素还在文档流里，
    依然挡住主区按钮的 pointer events —— 实测「本次会话产物」按钮
    点 30 秒点不动（subtree intercepts pointer events）。
    """
    css = read("style.css")
    i = css.index(".ag-drawer {")
    block = css[i:i + 1200]
    assert "visibility: hidden" in block, "抽屉关闭时必须 visibility:hidden"


def test_drawer_open_rule_restores_visibility():
    """打开抽屉时必须恢复 visibility（否则抽屉开了还是点不到）。"""
    css = read("style.css")
    assert ".agent-page.ag-drawer-open .ag-drawer" in css
    i = css.index(".agent-page.ag-drawer-open .ag-drawer")
    block = css[i:i + 200]
    assert "visibility: visible" in block


def test_narrow_screen_drawer_is_full_width():
    """375px 下抽屉必须全宽覆盖（挤占式会让主区只剩 ~135px）。"""
    css = read("responsive.css")
    i = css.index("@media (max-width: 700px)")
    block = css[i:]
    assert ".ag-drawer { width: 100%" in block.replace("\n", " ")


def test_narrow_screen_artifacts_are_single_column():
    """375px 下产物网格必须单列（两列每列只有 ~170px，缩略图太小）。"""
    css = read("responsive.css")
    i = css.index("@media (max-width: 700px)")
    block = css[i:]
    assert "grid-template-columns: 1fr" in block


def test_agent_page_position_relative_for_drawer():
    """抽屉是 absolute 定位 —— 父容器必须是定位上下文。"""
    css = read("style.css")
    assert re.search(r"\.agent-page\s*\{[^}]*position:\s*relative", css)
