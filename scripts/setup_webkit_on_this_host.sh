#!/usr/bin/env bash
# 让 Playwright WebKit（webkit-2272）在本机 Ubuntu 25.10 上能跑。
#
# 【为什么需要】
# webkit-2272 是一套较老的构建，它要的依赖 soname 与本机（Ubuntu 25.10）不同：
#   · libxml2.so.2   ← 本机是 libxml2.so.16
#   · libjxl.so.0.8   ← 本机是 libjxl.so.0.11
#   · libicuuc.so.70  ← 本机是 libicu*.so.74/.76
#   · libwoff2dec.so.1.0.2 ← 本机没有
# 而且 playwright 的 headless 模式默认切到 **WPE** 变体，
# 本机仓库里根本没有 libwpe-1.0 / libWPEWebKit-2.0 ⇒ 一律报
# "Host system is missing dependencies"。
#
# 【本脚本做什么】
#  1) headless 也走 GTK 变体（改 pw_run.sh）
#  2) 把缺的老 soname 库从**别的来源**补进 minibrowser-gtk/lib/
#     （优先用 docker overlay 里现成的同 soname 库 —— 那是真正的 ABI 匹配版本）
#
# 【安全性】
# 只动 ~/.cache/ms-playwright/webkit-2272/ 里的东西，**不改系统库**。
set -uo pipefail

WK="$HOME/.cache/ms-playwright/webkit-2272"
GTK="$WK/minibrowser-gtk"
LIB="$GTK/lib"
OVERLAY="/var/lib/docker/overlay2/976aa8fee34a16f1cc473bce1a39bfa1c00d26cea1a357060804d25e2b01dc43/merged/usr/lib/x86_64-linux-gnu"

echo "== 1) headless 走 GTK =="
python3 - <<'PY'
from pathlib import Path
p = Path("/root/.cache/ms-playwright/webkit-2272/pw_run.sh")
s = p.read_text()
old = '''  if [[ "$*" == *--headless* ]]; then
    DEPENDENCIES_FOLDER="WebKitBuild/DependenciesWPE";
    MINIBROWSER_FOLDER="minibrowser-wpe";
    BUILD_FOLDER="WebKitBuild/WPE";
  fi'''
new = '''  # 【本机修补 2026-11-21】headless 也走 GTK 变体。
  # 本机没装 WPE 通道的依赖（libwpe-1.0 / libWPEWebKit-2.0 在 Ubuntu 25.10 仓库里不存在）。
  if false; then
    DEPENDENCIES_FOLDER="WebKitBuild/DependenciesWPE";
    MINIBROWSER_FOLDER="minibrowser-wpe";
    BUILD_FOLDER="WebKitBuild/WPE";
  fi'''
if old in s:
    p.write_text(s.replace(old, new))
    print("   已改")
else:
    print("   已是 GTK（无需改）")
PY

# playwright 的依赖检查会扫 minibrowser-wpe 目录 —— 移开它
if [ -d "$WK/minibrowser-wpe" ]; then
  mv "$WK/minibrowser-wpe" "$WK/minibrowser-wpe.disabled"
  echo "== 2) minibrowser-wpe 已移出 playwright 依赖扫描范围 =="
fi

echo "== 3) 补老 soname 库 =="
mkdir -p "$LIB"

# libxml2.so.2 —— 用 overlay 里真正的同 soname 版本
if [ ! -e "$LIB/libxml2.so.2" ]; then
  cp -L "$OVERLAY/libxml2.so.2.9.13" "$LIB/libxml2.so.2" && chmod 755 "$LIB/libxml2.so.2"
  echo "   libxml2.so.2  <- docker overlay"
fi
# libjxl.so.0.8 —— webkit 自带（在被移开的 wpe 目录里）
if [ ! -e "$LIB/libjxl.so.0.8" ]; then
  cp -L "$WK/minibrowser-wpe.disabled/sys/lib/libjxl.so.0.8" "$LIB/libjxl.so.0.8" && chmod 755 "$LIB/libjxl.so.0.8"
  echo "   libjxl.so.0.8  <- webkit 自带"
fi
# libicuuc.so.70 —— **必须是真 _70 符号版**（Ubuntu 22.04 的 libicu70）
#   ⚠️ 本机 Ubuntu 25.10 的 libicuuc.so.74 实际导出的是 _76 符号（发行版重打包），
#      而 webkit 自带的 libxml2.so.2 要 _70 符号、webkit 的 JSC 要 _74 符号
#      ⇒ 两套都得有，且都得是真版本。
#   实测：换错版本时 ldd -r 报 263 个 undefined symbol（u_strToUpper_74 等）。
for f in libicuuc.so.70 libicui18n.so.70 libicudata.so.70; do
  if [ ! -e "$LIB/$f" ]; then
    if [ -d /tmp/icu70x/usr/lib/x86_64-linux-gnu ]; then
      base=${f%.so.70}
      cp -L "/tmp/icu70x/usr/lib/x86_64-linux-gnu/${base}.so.70" "$LIB/$f" \
        && chmod 755 "$LIB/$f" && echo "   $f  <- Ubuntu 22.04 libicu70 (真 _70 符号)"
    elif [ -e "$OVERLAY/$f" ]; then
      cp -L "$OVERLAY/$f" "$LIB/$f" && chmod 755 "$LIB/$f"
      echo "   $f  <- docker overlay"
    fi
  fi
done
# libicu*.so.74 —— Ubuntu 24.04 libicu74（真 _74 符号，给 JSC 用）
for f in libicuuc.so.74 libicui18n.so.74 libicudata.so.74; do
  if [ ! -e "$LIB/$f" ] && [ -d /tmp/icu74x/usr/lib/x86_64-linux-gnu ]; then
    base=${f%.so.74}
    cp -L "/tmp/icu74x/usr/lib/x86_64-linux-gnu/${base}.so.74" "$LIB/$f" \
      && chmod 755 "$LIB/$f" && echo "   $f  <- Ubuntu 24.04 libicu74 (真 _74 符号)"
  fi
done
# libxml2.so.2 —— 真 ABI 匹配版（导 LIBXML2_2.4.30 版本符号）
if [ ! -e "$LIB/libxml2.so.2" ]; then
  cp -L "$OVERLAY/libxml2.so.2.9.13" "$LIB/libxml2.so.2" && chmod 755 "$LIB/libxml2.so.2"
  echo "   libxml2.so.2  <- docker overlay (LIBXML2_2.4.30)"
fi
# libwoff2dec.so.1.0.2 —— snap 里有
if [ ! -e "$LIB/libwoff2dec.so.1.0.2" ]; then
  SNAP=$(find /snap -name 'libwoff2dec.so.1.0.2' 2>/dev/null | head -1)
  if [ -n "$SNAP" ]; then
    cp -L "$SNAP" "$LIB/libwoff2dec.so.1.0.2" && chmod 755 "$LIB/libwoff2dec.so.1.0.2"
    echo "   libwoff2dec.so.1.0.2  <- $SNAP"
  fi
fi

echo "== 4) 复查 ldd =="
MISS=""
for f in "$GTK"/bin/MiniBrowser "$GTK"/lib/*.so; do
  m=$(LD_LIBRARY_PATH="$GTK/lib:$GTK/sys/lib" ldd "$f" 2>/dev/null | grep "not found" | awk '{print $1}')
  [ -n "$m" ] && MISS="$MISS $m"
done
if [ -n "$MISS" ]; then
  echo "   仍缺:$MISS"
  exit 1
fi
echo "   ✅ 无缺失"
