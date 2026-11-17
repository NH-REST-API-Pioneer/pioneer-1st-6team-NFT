#!/bin/bash
# 장 끝난 뒤 맥에서: 서버 원본·실행기 기록 가져오기 → 1분 표 → 조합 규칙 갱신(다음 매매일용) → 커밋·푸시.
#   FF_SERVER=root@<서버주소> ./nightly.sh 20261118 20261119     # 수요일 밤 (목요일용 규칙)
#   FF_SERVER=root@<서버주소> ./nightly.sh 20261120               # 금요일 밤 (갱신 없이 가져오기만)
# 서버 비밀번호는 한 번 물어봄. 서버에선 다음 날 08:55 전에 git pull 만 하면 실행기가 새 규칙을 읽음.
set -euo pipefail
cd "$(dirname "$0")"
DAY=${1:?매매일 YYYYMMDD}
NEXT=${2:-}
: "${FF_SERVER:?FF_SERVER=root@서버주소 를 넣어 주세요}"
REMOTE=/root/foreign-flow/week05/foreign_flow/data

echo "== 서버에서 $DAY 가져오기"
ssh "$FF_SERVER" "cd $REMOTE && tar czf - raw/$DAY \$(ls -d live/${DAY}* 2>/dev/null)" | tar xzf - -C data
mkdir -p results/live
cp data/live/${DAY}* results/live/ 2>/dev/null || echo "(실행기 기록 없음)"

echo "== 1분 표"
.venv/bin/python convert.py "$DAY" | cut -c1-120

if [ -n "$NEXT" ]; then
  TRAIN=$(ls data/panel/*.csv | xargs -n1 basename | sed 's/.csv//' | awk -v d="$DAY" '$1 >= "20261117" && $1 <= d' | paste -sd, -)
  echo "== 조합 규칙 갱신: 학습 $TRAIN → $NEXT"
  .venv/bin/python analyze.py --combo-train "$TRAIN" --for "$NEXT"
fi

echo "== 커밋·푸시"
cd ../..
git add week05/foreign_flow/data/panel/${DAY}.csv week05/foreign_flow/results
git commit -q -m "week05 foreign_flow: ${DAY} 수집·실행 기록${NEXT:+, ${NEXT}용 조합 규칙}" && git push -q
git log --oneline -1
