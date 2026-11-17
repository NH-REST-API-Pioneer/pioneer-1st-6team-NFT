#!/bin/bash
# cron 용 — 평일 08:30~15:50 에만 돌고, 이미 돌고 있으면 바로 끝남 (data/collector.lock).
# 시간 판단은 스크립트가 한국시간으로 하니, 서버 시간대와 상관없이 5분마다 부르면 됨:
#   */5 * * * * cd <경로>/week05/basis_obi && ./run.sh
cd "$(dirname "$0")"
mkdir -p data
TZ=Asia/Seoul exec .venv/bin/python -m collector.collect --cron "$@" >> data/collector.out 2>&1
