#!/usr/bin/env bash
# L-A：补齐 3 个损坏 LoRA 本体（原子替换 + 校验）。
#
# 三个来源都已在下载阶段用 **上游 SHA256** 校验过（见报告）：
#   1) AWPortrait-QW.safetensors (0 B)
#      上游同名文件不存在；上游仓库里只有 AWPortrait-QW_1.0.safetensors，
#      其 SHA256 == 本机已有的 AWPortrait-QW_1.0.safetensors
#      -> 0 字节文件是同内容的**下载残留**，用同源完整文件补齐。
#   2) Qwen-Image-Edit-Semi-Realistic-Detailer-v1.safetensors (0 B)
#      HF: rzgar/Qwen-Image-Edit-semi-realistic-detailer
#          -> Qwen-Image-Edit-Anime-Semi-Realistic-Detailer-v1.safetensors
#      SHA256 与 CivitAI model 2129322 / version 2408631 的文件完全一致。
#   3) ip-adapter-faceid-plus_sd15_lora.safetensors (截断，缺 240772 B)
#      HF: h94/IP-Adapter-FaceID -> ip-adapter-faceid-plus_sd15_lora.safetensors
#
# 用法: bash scripts/restore_damaged_loras.sh
set -uo pipefail

LORAS=/mnt/usb2t/comfyui-models/loras
DL=/tmp/labc_dl
LOG=/tmp/labc_restore.log
: > "$LOG"

log() { echo "$*" | tee -a "$LOG"; }

restore() {
  local dest="$1" src="$2" want_size="$3" want_sha="$4"
  local dir base tmp
  dir="$(dirname "$dest")"; base="$(basename "$dest")"; tmp="$dest.tmp_install"
  log "=== $base ==="
  log "  src  : $src"
  log "  dest : $dest"
  local cur=0
  [ -f "$dest" ] && cur=$(stat -c %s "$dest")
  log "  替换前磁盘字节数: $cur"
  if [ "$cur" = "$want_size" ]; then
    local cur_sha; cur_sha=$(sha256sum "$dest" | cut -d' ' -f1)
    if [ "$cur_sha" = "$want_sha" ]; then
      log "  已就位且 SHA256 一致 -> 跳过"
      return 0
    fi
  fi
  log "  复制中（-> $tmp）..."
  cp "$src" "$tmp" || { log "  复制失败"; return 1; }
  local got_size got_sha
  got_size=$(stat -c %s "$tmp")
  got_sha=$(sha256sum "$tmp" | cut -d' ' -f1)
  log "  复制后字节数: $got_size (期望 $want_size)"
  log "  复制后 SHA256: $got_sha"
  if [ "$got_size" != "$want_size" ] || [ "$got_sha" != "$want_sha" ]; then
    log "  ✗ 校验不通过 -> 删除临时文件，不动原文件"
    rm -f "$tmp"
    return 1
  fi
  mv -f "$tmp" "$dest" || { log "  原子替换失败"; rm -f "$tmp"; return 1; }
  log "  ✓ 已原子替换（md5=$(md5sum "$dest" | cut -d' ' -f1)）"
}

restore "$LORAS/AWPortrait-QW/AWPortrait-QW.safetensors" \
        "$LORAS/AWPortrait-QW/AWPortrait-QW_1.0.safetensors" \
        472055792 \
        9c1d8a829bcd5fedf5274cf27a683d37be9fcd3f1a2296ca66ee135d251ff7b3

restore "$LORAS/QIE-SRD/Qwen-Image-Edit-Semi-Realistic-Detailer-v1.safetensors" \
        "$DL/Qwen-Image-Edit-Semi-Realistic-Detailer-v1.safetensors" \
        755106832 \
        5b376624ff76cf94a587b95f01b75871aa57e41d313ea2f6cbfe8755b2293f91

restore "$LORAS/ipadapter/ip-adapter-faceid-plus_sd15_lora.safetensors" \
        "$DL/ip-adapter-faceid-plus_sd15_lora.safetensors" \
        51059544 \
        3f00341d11e5e7b5aadf63cbdead09ef82eb28669156161cf1bfc2105d4ff1cd

log ""
log "=== 最终状态 ==="
for f in "$LORAS/AWPortrait-QW/AWPortrait-QW.safetensors" \
         "$LORAS/QIE-SRD/Qwen-Image-Edit-Semi-Realistic-Detailer-v1.safetensors" \
         "$LORAS/ipadapter/ip-adapter-faceid-plus_sd15_lora.safetensors"; do
  log "$(stat -c '%s bytes  %n' "$f")"
  log "  sha256=$(sha256sum "$f" | cut -d' ' -f1)"
  log "  md5=$(md5sum "$f" | cut -d' ' -f1)"
done
log ""
log "DONE"
