#!/bin/bash
# c9 修复的回归对跑脚本（两树隔离，各跑一次同一组用例）
# 用法：bash run_regression_compare.sh <tree_dir> <out_json>
TREE="$1"; OUT="$2"
cd "$TREE" || exit 2
TESTS="tests/test_max_tokens.py tests/test_agent_contract.py tests/test_agent_legacy_fixes.py"
if [ -f tests/test_agent_provider_timeout_budget.py ]; then
  TESTS="$TESTS tests/test_agent_provider_timeout_budget.py"
fi
timeout 600 python3 -m pytest $TESTS -q > "${OUT}.log" 2>&1
echo "exit=$?" >> "${OUT}.log"
# 提取失败名单
grep -E "^FAILED" "${OUT}.log" | sed 's/ - .*//' | sort > "${OUT}.fails"
echo "{\"tree\":\"$TREE\",\"exit\":$(grep -o 'exit=[0-9]*' "${OUT}.log" | tail -1 | cut -d= -f2),\"failed_count\":$(wc -l < "${OUT}.fails")}" > "${OUT}.meta"
cat "${OUT}.meta"
