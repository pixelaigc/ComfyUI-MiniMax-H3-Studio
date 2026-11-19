#!/usr/bin/env bash
# 合并「240s 超时文件」的长跑结果，产出最终对拍名单。
set -u
EV="/root/projects/h3s-r4-20261119/evidence/20261119-r4-closure/pytest-compare"
BASE_T="$EV/base.txt"
BR_T="$EV/branch.txt"

# 清掉原先标记为 TIMEOUT 的行（它们已被长跑取代）
grep -v '^TIMEOUT ' "$BASE_T" > "$BASE_T.tmp" && mv "$BASE_T.tmp" "$BASE_T"
grep -v '^TIMEOUT ' "$BR_T" > "$BR_T.tmp" && mv "$BR_T.tmp" "$BR_T"

add() {  # $1=side tag  $2=file stem
  local tag="$1" name="$2"
  local log="$EV/logs/$tag-$name-long.log"
  [ -f "$log" ] || return 0
  grep -E '^FAILED ' "$log" | sed "s|^|  $name :: |" >> "$3"
}

add wsbase test_enhance_menu "$BASE_T"
add wsbase test_music_flow_browser "$BASE_T"
add wsbase test_qwen_pe_enhance "$BASE_T"
add wsbase test_responsive_mobile "$BASE_T"
add wsbase test_studio "$BASE_T"

add h3s-r4-20261119 test_enhance_menu "$BR_T"
add h3s-r4-20261119 test_music_flow_browser "$BR_T"
add h3s-r4-20261119 test_qwen_pe_enhance "$BR_T"
add h3s-r4-20261119 test_responsive_mobile "$BR_T"
add h3s-r4-20261119 test_studio "$BR_T"

sort -o "$BASE_T" "$BASE_T"
sort -o "$BR_T" "$BR_T"

grep '^  ' "$BASE_T" | sort > "$EV/base_ids.txt"
grep '^  ' "$BR_T" | sort > "$EV/branch_ids.txt"

echo "baseline 失败项 = $(wc -l < "$EV/base_ids.txt")"
echo "branch   失败项 = $(wc -l < "$EV/branch_ids.txt")"
echo
echo "=== 仅在 baseline（基线红 -> 分支已修好） ==="
comm -23 "$EV/base_ids.txt" "$EV/branch_ids.txt"
echo
echo "=== 仅在 branch（新增红） ==="
comm -13 "$EV/base_ids.txt" "$EV/branch_ids.txt"
echo
NEW=$(comm -13 "$EV/base_ids.txt" "$EV/branch_ids.txt" | wc -l)
echo "新增失败数 = $NEW"
