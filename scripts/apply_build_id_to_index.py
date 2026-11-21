# -*- coding: utf-8 -*-
"""一次性把「构建版本标识」加进 web/index.html（CRLF 安全）。

【为什么要脚本而不是手改】
index.html 是 **CRLF** 契约文件。用 patch/手改极易把行尾混成 CRLF+LF
（第一版就混了 6 个裸 LF）。
而且本轮反复移动这段 DOM —— 每次手改都要重新对齐，
中途出过一次「整块 nav 被复制成两份（7 组变 14 组）」的事故。

【本脚本做三件事，全部 CRLF 安全】
  1) 顶栏加版本号（.brand 之后、.top-status 之前）
  2) 系统页加「构建版本」明细卡片
  3) 引入 build-id.js
"""
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
IDX = REPO / "web" / "index.html"

TOP_BLOCK = '''  <!-- 【2026-11-21】构建版本标识【让用户自行确认手机在跑哪一版】
       【为什么在顶栏 level（.brand 之后）】
       · 侧栏在窄屏是收起的抽屉，放那儿得先点汉堡才看得到 ——
         而「确认自己跑的是哪一版」应当**不用任何交互**就能看到；
       · 放进 .brand-name 内也不行：.brand-name 的内容
         （strong 79px + v1.00 27px）已超过 320px 下分给它的宽度，
         版本号作为最后一个子项被挤成 w=0（实测）。
       · 作为 .brand 的兄弟节点时，.brand 是 flex:1 1 auto 会抢空间，
         导致版本号与 v1.00 视觉重叠（实测 320px 重叠 213px²，
         vision_analyze 复核截图也看到了重叠）。
       ⇒ 修法见 favorites.css：窄屏下 .brand 改成 flex:0 1 auto
         + overflow:hidden，让它自己先收，把位置让给版本号。
       【只显示 commit 短哈希】320px 下顶栏富余仅 ~33px；
         完整信息（静态资源指纹 / 服务启动时间 / 部署目录）
         在系统页「构建版本」卡片里。 -->
  <span class="build-id build-id-top" id="buildId" title="构建版本">
    <span class="build-id-value" id="buildIdValue">…</span>
  </span>
'''

SYS_CARD = '''    <!-- 【2026-11-21】构建版本明细卡片。
         【为什么放在系统页】顶栏只放得下 commit 短哈希
         （实测 320px 下顶栏富余仅 ~33px），
         完整信息（静态资源版本指纹 / 服务启动时间 / 部署目录）放这里。
         【不新增导航分组】系统页是既有页面，不动左侧 7 个顶层分组。 -->
    <div class="sys-card build-id-card">
      <div class="sys-card-title">构建版本</div>
      <div class="build-id-detail" id="buildIdDetail">读取中…</div>
    </div>
'''

SCRIPT_TAG = ('<!-- 【2026-11-21】构建版本标识【让用户自行确认手机在跑哪一版】\r\n'
              '     来源：GET /api/version（服务端 h3studio/build_info.py）。\r\n'
              '     反映部署目录的真实 git 规格。\r\n'
              '     版本号 buildid20261121a。 -->\r\n'
              '<script src="/static/build-id.js?v=buildid20261121a"></script>\r\n')


def crlf(text: str) -> bytes:
    """统一成 CRLF 字节。"""
    return text.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8")


def main():
    raw = IDX.read_bytes()
    orig = raw

    # --- 1) 顶栏：插到 top-status 之前 ---
    anchor = b'  <div class="top-status" id="topStatus">'
    if raw.count(anchor) != 1:
        print(f"❌ top-status anchor 出现 {raw.count(anchor)} 次", file=sys.stderr)
        return 1
    if b'id="buildId"' not in raw:
        raw = raw.replace(anchor, crlf(TOP_BLOCK) + anchor)

    # --- 2) 系统页：插到 systemPage 的 sysGrid 之后 ---
    if b'id="buildIdDetail"' not in raw:
        syspage = raw.find(b'id="systemPage"')
        assert syspage > 0, "找不到 systemPage"
        grid = b'    <div class="sys-grid" id="sysGrid"></div>\r\n'
        i = raw.find(grid, syspage)
        assert i > 0, "systemPage 里找不到 sysGrid"
        raw = raw[:i + len(grid)] + crlf(SYS_CARD) + raw[i + len(grid):]

    # --- 3) 引入 build-id.js ---
    if b'/static/build-id.js' not in raw:
        fav = b'<script src="/static/favorites.js?v=mobnav20261121a"></script>\r\n'
        assert raw.count(fav) == 1, "favorites.js 引入点异常"
        raw = raw.replace(fav, fav + crlf(SCRIPT_TAG))

    # --- 4) favorites.css 的 ?v= 升档（内容变了：加了 .build-id 样式）---
    old_v = b'/static/favorites.css?v=mobnav20261121a'
    new_v = b'/static/favorites.css?v=buildid20261121a'
    if raw.count(old_v) == 1:
        raw = raw.replace(old_v, new_v)

    # --- 5) 行尾统一：所有裸 LF -> CRLF ---
    raw = re.sub(rb'(?<!\r)\n', b'\r\n', raw)

    if raw == orig:
        print("ℹ️ 已经是目标状态，无需改动")
        return 0

    IDX.write_bytes(raw)
    bare = len(re.findall(rb'(?<!\r)\n', raw))
    groups = len(re.findall(rb'<button[^>]*class="nav-group"[^>]*data-nav-key="', raw))
    print(f"✅ index.html 已更新")
    print(f"   裸 LF = {bare}（必须为 0）")
    print(f"   顶层 nav-group 数 = {groups}（必须为 7）")
    print(f"   buildId 出现 = {raw.count(b'id=\"buildId\"')}（必须为 1）")
    print(f"   buildIdDetail 出现 = {raw.count(b'id=\"buildIdDetail\"')}（必须为 1）")
    if bare or groups != 7 or raw.count(b'id="buildId"') != 1:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
