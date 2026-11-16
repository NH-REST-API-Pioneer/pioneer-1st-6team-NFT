#!/usr/bin/env bash
# 5주차 실행기 — cron 감시용. 이미 돌고 있으면 live.py 가 잠금을 못 잡고 바로 끝납니다.
#   */5 * * * * cd <경로>/week05 && ./run.sh --orders
cd "$(dirname "$0")"
mkdir -p results/live
TZ=Asia/Seoul exec .venv/bin/python live.py "$@" >> results/live/runner.out 2>&1
