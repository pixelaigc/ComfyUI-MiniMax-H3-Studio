#!/bin/bash
# 监控受控跑进展
EV=/root/projects/h3studio-flf-lum-20261118/evidence/evidence-20261118-flf-lum-closure
LOG="$EV/run_materialA_5chains.log"
TARGET="${1:-3}"
MAXWAIT="${2:-360}"
END=$((SECONDS+MAXWAIT))
while [ $SECONDS -lt $END ]; do
  N=$(grep -c "段2 -> done" "$LOG" 2>/dev/null | head -1)
  N=${N:-0}
  if [ "$N" -ge "$TARGET" ]; then
    break
  fi
  sleep 20
done
echo "  $(date '+%H:%M:%S')  完成链数 = $(grep -c "段2 -> done" "$LOG" | head -1)"
grep -E "段[12] job=" "$LOG" | tail -6 | sed 's/^/  /'
