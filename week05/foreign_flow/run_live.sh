#!/bin/bash
# cron 용 실행기 — 08:55 전·15:35 뒤·주말이면 바로 끝나고, 이미 돌고 있으면 바로 끝남 (data/live.lock).
# 수집기(run.sh)와 같은 서버·같은 폴더에서 돌아야 함 (수집기가 쓰는 data/raw/오늘/rest.jsonl 을 읽음).
#   */5 8-15 * * 1-5 cd <경로>/week05/foreign_flow && ./run_live.sh
cd "$(dirname "$0")"
mkdir -p data
TZ=Asia/Seoul exec .venv/bin/python live.py "$@" >> data/live.out 2>&1
