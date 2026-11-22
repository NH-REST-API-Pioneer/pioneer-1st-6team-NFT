#!/bin/bash
# cron 용 — 장 시간(평일 08:50~15:50) 밖이면 바로 끝나고, 이미 돌고 있으면 바로 끝남 (data/recorder.lock).
# 5분마다 불러 두면 죽어도 다시 살아남:
#   */5 8-15 * * 1-5 cd <경로>/week06/foreign_flow && ./run.sh
cd "$(dirname "$0")"
mkdir -p data
TZ=Asia/Seoul exec .venv/bin/python record.py "$@" >> data/recorder.out 2>&1
