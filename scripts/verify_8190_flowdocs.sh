#!/usr/bin/env bash
# 8190 线上探活：D12 流程图可达性。
# 每条都打印**实际数字**（HTTP 状态 / Content-Type / 字节数 / md5 是否一致），
# 不接受「看起来对了」。
set -u
BASE=http://127.0.0.1:8190
LIVE=/root/projects/h3studio-live
PASS=0
FAIL=0

enc() { python3 -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))" "$1"; }

check_file() {
  local label="$1" name="$2"
  local url="$BASE/docs/$(enc "$name")"
  local tmp; tmp=$(mktemp)
  local meta
  # 用 | 分隔而不是空格 —— Content-Type 里带空格
  # （text/plain; charset=utf-8），按空格切会把 size 切错。
  meta=$(curl -s -o "$tmp" -w "%{http_code}|%{content_type}|%{size_download}" "$url")
  local code ctype len
  code=$(echo "$meta" | cut -d'|' -f1)
  ctype=$(echo "$meta" | cut -d'|' -f2)
  len=$(echo "$meta" | cut -d'|' -f3)
  local disk; disk=$(stat -c %s "$LIVE/docs/$name" 2>/dev/null || echo -1)
  local sm dm
  sm=$(md5sum "$tmp" | cut -d' ' -f1)
  dm=$(md5sum "$LIVE/docs/$name" 2>/dev/null | cut -d' ' -f1)
  local ok=1
  [ "$code" = "200" ] || ok=0
  [ "$len" = "$disk" ] || ok=0
  [ "$sm" = "$dm" ] || ok=0
  if [ "$ok" = "1" ]; then
    PASS=$((PASS+1))
    printf "  [PASS] %-8s %-28s HTTP %s  ctype=%-32s len=%s(disk %s)  md5一致\n" \
      "$label" "$name" "$code" "$ctype" "$len" "$disk"
  else
    FAIL=$((FAIL+1))
    printf "  [FAIL] %-8s %-28s HTTP %s  ctype=%s  len=%s(disk %s)  md5服务=%s md5磁盘=%s\n" \
      "$label" "$name" "$code" "$ctype" "$len" "$disk" "$sm" "$dm"
  fi
  rm -f "$tmp"
}

echo "=== 1) 5 套流程图 PNG ==="
for n in 总览 创作链路 素材库 内置Agent 系统配置; do
  check_file "PNG" "交互流程图-$n.png"
done

echo
echo "=== 2) 5 套流程图 Mermaid 源文件 (.mmd) ==="
for n in 总览 创作链路 素材库 内置Agent 系统配置; do
  check_file "MMD" "交互流程图-$n.mmd"
done

echo
echo "=== 3) SVG + 说明文档 + 派生清单 ==="
check_file "SVG" "交互流程图-总览.svg"
check_file "GUIDE" "交互流程图说明.md"
check_file "DERIVED" "交互流程图-派生清单.md"

echo
echo "=== 4) 目录索引 ==="
code=$(curl -s -o /tmp/idx.html -w "%{http_code}" "$BASE/docs")
ctype=$(curl -s -o /dev/null -w "%{content_type}" "$BASE/docs")
n_links=$(grep -o 'href="/docs/' /tmp/idx.html | wc -l)
echo "  /docs -> HTTP $code  ctype=$ctype  含 /docs 链接数=$n_links"
if [ "$code" = "200" ] && [ "$n_links" -ge 16 ]; then PASS=$((PASS+1)); echo "  [PASS] 目录索引可访问且列出了条目";
else FAIL=$((FAIL+1)); echo "  [FAIL] 目录索引异常"; fi

echo
echo "=== 5) 清单接口 /api/docs/flows ==="
curl -s "$BASE/api/docs/flows" -o /tmp/flows.json -w "  HTTP %{http_code}\n"
python3 - <<'PY'
import json
d = json.load(open("/tmp/flows.json"))
print("  diagrams:", len(d["diagrams"]))
for x in d["diagrams"]:
    print("   -", x["title"], "| png:", "png" in x, "| mmd:", "mmd" in x, "| svg:", "svg" in x)
print("  guide:", (d.get("guide") or {}).get("name"))
PY

echo
echo "=== 6) 目录穿越防护（必须全部 404）==="
for a in "/docs/../server.py" "/docs/%2e%2e%2fserver.py" "/docs/%2e%2e/%2e%2e/etc/passwd" "/docs/..%2fserver.py" "/docs/%252e%252e%252fserver.py"; do
  code=$(curl -s -o /dev/null -w "%{http_code}" --path-as-is "$BASE$a")
  if [ "$code" = "404" ]; then PASS=$((PASS+1)); echo "  [PASS] $a -> $code";
  else FAIL=$((FAIL+1)); echo "  [FAIL] $a -> $code (应为 404)"; fi
done

echo
echo "=== 7) 前端资源版本已破缓存 ==="
for f in flow-docs.js; do
  code=$(curl -s -o /dev/null -w "%{http_code}" "$BASE/static/$f")
  echo "  /static/$f -> $code"
done
echo "  index.html 含 flow-docs.js: $(curl -s $BASE/ | grep -c 'flow-docs.js')"

echo
echo "=== 汇总：PASS=$PASS FAIL=$FAIL ==="
[ "$FAIL" = "0" ] && exit 0 || exit 1
