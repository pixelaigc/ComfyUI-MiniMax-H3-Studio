#!/usr/bin/env bash
# ==================================================================
# params 通道闸门探针 —— 修复前 / 修复后**同一组用例**对照实测
#
# 【为什么单独成脚本而不是每次手打 curl】
# 修复前后的证据必须**逐字可比**（同一组用例、同一顺序、
# 同样的输出格式）。手打 curl 每次格式都不同，证据不可比。
#
# 用例集（覆盖任务书要求的全部 7 类）：
#   1. /etc/hostname            —— 绝对路径
#   2. /tmp/<canary>.txt        —— 另一个绝对路径
#   3. ../../../..../etc/hostname —— 路径穿越
#   4. 符号链接                 —— 指向 /etc/hostname 的软链
#   5. ComfyUI input 直引      —— h3studio/<已存在文件>
#   6. 素材库内合法 asset 引用  —— 必须放行（不误杀）
#   7. 非法种子 -5 / 0 / abc    —— 种子校验不得回归
#
# 【输出】每个用例一行：HTTP 状态码 + 错误文案原文（或 accepted）。
# 同时输出 ComfyUI input/h3studio 目录的**前后对比**（新增了什么文件）。
#
# 用法: probe_params_channel.sh <base_url> <label> <out_jsonl>
#   base_url  例: http://127.0.0.1:8190
#   label     例: before / after
#   out_jsonl 每行一个 JSON 结果
# ==================================================================
set -uo pipefail

BASE="${1:?用法: probe_params_channel.sh <base_url> <label> <out_jsonl>}"
LABEL="${2:?}"
OUT="${3:?}"

COMFY_INPUT="/home/ubuntu/comfyui/input/h3studio"
STATE_DIR="$(mktemp -d /tmp/params-probe-XXXXXX)"
trap 'rm -rf "$STATE_DIR"' EXIT

CANARY="$STATE_DIR/canary.txt"
printf 'CANARY-PARAMS-GUARD-%s\n' "$LABEL" > "$CANARY"
# 符号链接：指向真实本地文件（绝对路径之外的另一种绕过写法）
SYMLINK="$STATE_DIR/link-to-canary.txt"
ln -sf "$CANARY" "$SYMLINK"
# ComfyUI input 里已有的一个文件（用来测「直引 ComfyUI input」）
COMFY_EXISTING=""
if [ -d "$COMFY_INPUT" ]; then
  COMFY_EXISTING="$(find "$COMFY_INPUT" -maxdepth 1 -type f -printf '%f\n' 2>/dev/null | head -1)"
fi

# ---- 记录 ComfyUI input 目录「前」的快照 ----
BEFORE_LIST="$STATE_DIR/before.txt"
AFTER_LIST="$STATE_DIR/after.txt"
if [ -d "$COMFY_INPUT" ]; then
  find "$COMFY_INPUT" -type f -printf '%P\n' 2>/dev/null | sort > "$BEFORE_LIST"
else
  : > "$BEFORE_LIST"
fi

# ---- 素材库里一个**真实存在**的图片素材（合法用例必须放行）----
ASSET_ID=""
if [ -f /mnt/usb2t/asset_library/asset_library.db ]; then
  ASSET_ID="$(python3 - <<'PY' 2>/dev/null
import sqlite3
conn = sqlite3.connect('/mnt/usb2t/asset_library/asset_library.db')
conn.row_factory = sqlite3.Row
row = conn.execute(
    "SELECT a.id FROM assets a "
    "WHERE a.deleted_at IS NULL AND a.category='image' "
    "ORDER BY a.updated_at DESC LIMIT 1").fetchone()
print(row['id'] if row else '')
PY
)"
fi

emit() {
  # emit <case> <http_code> <body>
  python3 - "$LABEL" "$1" "$2" "$3" >> "$OUT" <<'PY'
import json, sys
label, case, code, body = sys.argv[1:5]
try:
    payload = json.loads(body)
except Exception:
    payload = {"raw": body[:500]}
err = payload.get("error") if isinstance(payload, dict) else None
print(json.dumps({
    "label": label,
    "case": case,
    "http": int(code),
    "accepted": int(code) == 200,
    "error": err,
    "code": payload.get("code") if isinstance(payload, dict) else None,
    "job_id": payload.get("id") if isinstance(payload, dict) else None,
}, ensure_ascii=False))
PY
}

probe() {
  # probe <case> <mode> <params_json> [asset_refs_json]
  local case="$1" mode="$2" params="$3" refs="${4:-}"
  local body_file="$STATE_DIR/body.txt"
  local code
  local args=(-s -o "$body_file" -w '%{http_code}' -X POST "$BASE/api/jobs"
              -F "mode=$mode" -F "params=$params")
  [ -n "$refs" ] && args+=(-F "asset_refs=$refs")
  code="$(curl "${args[@]}")"
  emit "$case" "$code" "$(cat "$body_file")"
  echo "  [$LABEL] $case -> HTTP $code" >&2
}

echo "=== params 通道闸门探针 ($LABEL) -> $OUT ===" >&2
echo "素材库合法图片素材: ${ASSET_ID:-（无）}" >&2
echo "ComfyUI input 已有文件（直引用例）: ${COMFY_EXISTING:-（无）}" >&2

# ---------- 1. 绝对路径 ----------
probe "abs_etc_hostname" flf \
  '{"first_frame":"/etc/hostname","width":832,"height":480,"duration":5,"prompt":"p"}'

# ---------- 2. 另一个绝对路径 ----------
probe "abs_tmp_canary" flf \
  "{\"first_frame\":\"$CANARY\",\"width\":832,\"height\":480,\"duration\":5,\"prompt\":\"p\"}"

# ---------- 3. 路径穿越 ----------
probe "dotdot_traversal" flf \
  '{"first_frame":"../../../../../../../../etc/hostname","width":832,"height":480,"duration":5,"prompt":"p"}'

# ---------- 4. 符号链接 ----------
probe "symlink" flf \
  "{\"first_frame\":\"$SYMLINK\",\"width\":832,\"height\":480,\"duration\":5,\"prompt\":\"p\"}"

# ---------- 5. ComfyUI input 直引 ----------
if [ -n "$COMFY_EXISTING" ]; then
  probe "comfy_input_direct" flf \
    "{\"first_frame\":\"h3studio/$COMFY_EXISTING\",\"width\":832,\"height\":480,\"duration\":5,\"prompt\":\"p\"}"
else
  echo "  [$LABEL] comfy_input_direct -> 跳过（ComfyUI input/h3studio 为空）" >&2
fi

# ---------- 6. 素材库内合法 asset 引用（必须放行）----------
if [ -n "$ASSET_ID" ]; then
  probe "library_asset_ok" flf \
    '{"width":832,"height":480,"duration":5,"prompt":"p"}' \
    "[{\"asset_id\":\"$ASSET_ID\",\"slot\":\"first_frame\",\"ordinal\":0}]"
else
  echo "  [$LABEL] library_asset_ok -> 跳过（素材库里没有可用图片素材）" >&2
fi

# ---------- 7. 非法种子（不得回归）----------
probe "seed_negative" flf \
  '{"seed":-5,"width":832,"height":480,"duration":5,"prompt":"p"}'
probe "seed_zero" flf \
  '{"seed":0,"width":832,"height":480,"duration":5,"prompt":"p"}'
probe "seed_nonnumeric" flf \
  '{"seed":"abc","width":832,"height":480,"duration":5,"prompt":"p"}'

# ---- ComfyUI input 目录「后」的快照 + 差集 ----
if [ -d "$COMFY_INPUT" ]; then
  find "$COMFY_INPUT" -type f -printf '%P\n' 2>/dev/null | sort > "$AFTER_LIST"
else
  : > "$AFTER_LIST"
fi
DIFF_FILE="$STATE_DIR/new_files.txt"
comm -13 "$BEFORE_LIST" "$AFTER_LIST" > "$DIFF_FILE"

python3 - "$LABEL" "$DIFF_FILE" "$COMFY_INPUT" >> "$OUT" <<'PY'
import json, sys, os
label, diff_file, input_dir = sys.argv[1:4]
try:
    new_files = [l for l in open(diff_file, encoding='utf-8').read().splitlines() if l]
except OSError:
    new_files = []
print(json.dumps({
    "label": label,
    "case": "__comfy_input_diff__",
    "input_dir": input_dir,
    "new_files": new_files,
    "new_file_count": len(new_files),
}, ensure_ascii=False))
PY

echo "=== 探针结束 ($LABEL)：ComfyUI input 新增文件 $(wc -l < "$DIFF_FILE") 个 ===" >&2
cat "$DIFF_FILE" >&2
