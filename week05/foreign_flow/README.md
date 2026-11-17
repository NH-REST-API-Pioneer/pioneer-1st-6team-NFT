# 외국인 흐름 → 미니코스피200 (5주차, 상재)

국내 코스피200 선물 시장에서 외국인 순매수 흐름이 다음 몇 분 선물 수익률을 예측하는지 보고, 그 신호로 미니코스피200을 모의매매한다. 근거·화요일 분석은 [week05-외국인흐름-전략-근거.md](week05-외국인흐름-전략-근거.md).

**규칙 두 개** (투자자 스냅샷이 120초 넘게 늦은 분은 둘 다 진입 안 함)
- 규칙 1 (`results/rules.json`, 화요일 고정, 기록만): 직전 5분 개인 선물 순매수 z 가 ±1.5 를 넘으면 반대 방향 1계약, 5분 보유.
- 규칙 2 (`results/rules_combo.json`, 모의계좌 주문): 외국인 5분 z − 개인 5분 z 가 ±θ 를 넘으면 그 방향 1계약, 5분 보유. 매일 밤 그날까지 데이터로 sd·θ 만 다시 맞춤 (`results/rules_combo/for_날짜.json`, 수요일용 θ 0.5).

## 왜 이걸 하나

- 외국인이 코스피200 선물·옵션에서 정보를 더 가진 쪽이라는 연구가 여럿 있다. Ahn·Kang·Ryu (2008, *J. Futures Markets*)는 옵션 호가 스프레드의 약 1/3이 정보 비대칭 비용이고 외국인이 가장 정보가 많다고 했고, Ryu (2015)는 외국 기관 매매의 가격 영향이 가장 오래간다고 했다. 다만 거의 다 일별 연구다. 장중 몇 분 단위로도 그런지가 이번 질문이다.
- NH API에 투자자별 매매동향(`domestic_trend-investor`)이 있다. 국내 모의 시간(09:00\~15:45)이 장 전체라 수집과 매매가 다 된다.

## 데이터를 어떻게 모으나

투자자 동향은 **분별 과거를 주지 않는다** (2026-11-17 모의 서버에서 확인). 그날 누적 + 직전 값 스냅샷 하나가 30초 지연으로 오고, 과거 날짜를 넣으면 그날 마감 누적만 온다(9월부터 드문드문 34일). 웹소켓에도 투자자별 항목은 없다. 그래서 장중에 직접 쌓는다.

| 데이터 | 방식 | 주기 | 파일 |
| --- | --- | --- | --- |
| 투자자 동향: 코스피200 선물(011)·미니(051)·콜(012)·풋(013) × 수량·금액 | REST | 30초 | `data/raw/YYYYMMDD/rest.jsonl` |
| 코스피200·미니 현재가 (웹소켓 끊김 대비) | REST | 30초 | 같은 파일 |
| 코스피200 `A016C000`·미니 `A056C000` 체결(KA)·호가(KB) | 웹소켓 | 틱 | `data/raw/YYYYMMDD/HH.jsonl.gz` |
| 보조: 투자자 동향 일별 마감 34일, 코스피200 선물 일봉 | REST | 한 번 | `data/daily_investor.csv`, `data/daily_k200.csv` |

웹소켓 부분은 은석 `week05/ws_recorder/record.py` 를 가져왔다 (접속·재접속 백오프·시간별 압축 저장 그대로). 국내 키를 써서 해외 기록기와 연결·구독 한도가 겹치지 않는다.

## 일정

| 날 | 할 일 |
| --- | --- |
| 화 11/17 | 수집만. 장 끝나고 `convert.py` → `analyze.py --train 20261117` 로 신호·규칙을 골라 `results/rules.json` 에 고정 |
| 수\~금 | 규칙 그대로 미니코스피200 모의매매 (`live.py`) + 계속 수집 |
| 수·목 밤 | `FF_SERVER=root@<서버> ./nightly.sh 20261118 20261119` — 서버 원본·실행 기록 가져와 1분 표, 다음 날 규칙 2 갱신, 푸시. 서버는 08:55 전 `git pull` |
| 금 | `analyze.py --train 20261117 --test 20261118,20261119,20261120` (규칙 1), `analyze.py --combo-test 20261118,20261119,20261120` (규칙 2) 로 표본 밖 판정 |

## 실행

```
cd week05/foreign_flow
uv venv .venv --python 3.10 && uv pip install --python .venv -r requirements.txt
.venv/bin/python record.py                 # 장 시간(평일 08:50~15:50)에만 돌고 15:50 에 스스로 끝남
.venv/bin/python record.py --seconds 60    # 시험용 (장 시간 검사 안 함)
./run.sh                                   # cron 용. 이미 돌고 있으면 바로 끝남
#   */5 8-15 * * 1-5 cd <경로>/week05/foreign_flow && ./run.sh
.venv/bin/python live.py                   # 모의매매 (08:55~15:35, 수집기와 같은 서버에서)
.venv/bin/python live.py --paper           # 주문 없이 호가로 종이 체결
.venv/bin/python live.py --replay 20261117 # 저장된 표로 판단만 다시 돌려 analyze 와 같은지 확인
./run_live.sh                              # cron 용
#   */5 8-15 * * 1-5 cd <경로>/week05/foreign_flow && ./run_live.sh
.venv/bin/python convert.py 20261117       # 원본 → data/panel/20261117.csv (1분 표)
.venv/bin/python analyze.py --train 20261117
.venv/bin/python fetch_daily.py            # 보조 자료
.venv/bin/python -m pytest -q tests
```

서버와 노트북에서 동시에 켜지 않는다 (같은 키로 이중 접속).

## 파일

| 파일 | 내용 |
| --- | --- |
| `record.py` | 수집기 (웹소켓 KA·KB + REST 투자자 동향·현재가) |
| `live.py` | 모의매매 실행기. 수집기의 rest.jsonl 을 읽어 analyze 와 같은 코드로 신호 계산, 규칙 2 시장가 진입·5분 뒤 청산, 규칙 1은 종이 체결 기록. 서버 `data/live/` → 밤에 `results/live/` |
| `nightly.sh` | 밤 갱신 (맥에서) |
| `convert.py` | 원본 → 1분 표. 투자자 값은 받은 시각 기준 as-of 라 미래 정보가 안 섞임 |
| `analyze.py` | 신호(외국인 선물·옵션 흐름, 개인, 주문 흐름) × 보유 길이 회귀(Newey-West), 규칙 고르기·시험일 판정 |
| `fetch_daily.py` | 보조 일별 자료 |
| `nh_client.py` | 2주차 국내 클라이언트 복사 |
