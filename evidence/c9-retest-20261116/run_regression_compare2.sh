#!/bin/bash
# c9 修复的回归对跑（两树隔离）
TREE="$1"; OUT="$2"
cd "$TREE" || exit 2
TESTS="tests/test_max_tokens.py tests/test_agent_contract.py"
if [ -f tests/test_agent_provider_timeout_budget.py ]; then
  TESTS="$TESTS tests/test_agent_provider_timeout_budget.py"
fi
timeout 900 python3 -m pytest $TESTS -q > "${OUT}.log" 2>&1
RC=$?
echo "exit=$RC" >> "${OUT}.log"
grep -E "^FAILED" "${OUT}.log" | sed 's/ - .*//' | sort > "${OUT}.fails"
PASSED=$(grep -oE "[0-9]+ passed" "${OUT}.log" | tail -1)
echo "{\"tree\":\"$TREE\",\"exit\":$RC,\"failed_count\":$(wc -l < "${OUT}.fails"),\"passed\":\"$PASSED\"}" > "${OUT}.meta"
cat "${OUT}.meta"
