#!/bin/bash
# 금요일(11/27) 장 끝난 뒤 맥에서: 27일 가져오기 → 최종 판정 → 주간 요약 → 재생 대조.
#   FF_SERVER=root@<서버주소> ./friday.sh
# 서버 비밀번호는 한 번 물어봄. 결과: results/test_2026112[3-7]*.md, results/week_summary_*.md
set -euo pipefail
cd "$(dirname "$0")"
DAYS=20261123,20261124,20261125,20261126,20261127
./nightly.sh 20261127
echo "== 최종 판정"
.venv/bin/python analyze.py --test "$DAYS"
echo "== 주간 요약"
.venv/bin/python diag/week_summary.py "$DAYS" > /dev/null && echo "results/week_summary_${DAYS//,/_}.md"
echo "== 27일 재생 대조"
.venv/bin/python live.py --replay 20261127 | grep json
cd ../.. && git add week06/foreign_flow/results && git commit -q -m "week06 foreign_flow: 최종 판정·주간 요약" && git log --oneline -1
