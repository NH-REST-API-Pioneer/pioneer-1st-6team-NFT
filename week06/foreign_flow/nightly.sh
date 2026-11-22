#!/bin/bash
# 장 끝난 뒤 맥에서: 서버 원본·실행기 기록 가져오기 → 1분 표 → 커밋 (푸시는 직접).
#   FF_SERVER=root@<서버주소> ./nightly.sh 20261123
# 6주차 규칙은 고정이라 밤에 바꾸는 것 없음. 서버 비밀번호는 한 번 물어봄.
set -euo pipefail
cd "$(dirname "$0")"
DAY=${1:?매매일 YYYYMMDD}
: "${FF_SERVER:?FF_SERVER=root@서버주소 를 넣어 주세요}"
REMOTE=/root/foreign-flow/week06/foreign_flow/data

echo "== 서버에서 $DAY 가져오기"
ssh "$FF_SERVER" "cd $REMOTE && tar czf - raw/$DAY \$(ls -d live/${DAY}* 2>/dev/null)" | tar xzf - -C data
mkdir -p results/live
cp data/live/${DAY}* results/live/ 2>/dev/null || echo "(실행기 기록 없음)"

echo "== 1분 표"
.venv/bin/python convert.py "$DAY" | cut -c1-120

echo "== 커밋"
cd ../..
git add week06/foreign_flow/data/panel/${DAY}.csv week06/foreign_flow/results
git commit -q -m "week06 foreign_flow: ${DAY} 수집·실행 기록" && git log --oneline -1
