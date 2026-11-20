# R02 · 8개 API 명세의 Content-Type 값이 빈칸인데, 실제로는 없으면 415 로 거절됩니다

- 섹터: 0 · 종류: 명세 누락 · 검증: 실제 호출 · AI 영향: 중 (명세대로 만든 클라이언트가 헤더를 안 보냄)
- 상태: 올릴 준비 됨

```text
[제보] 8개 API 명세의 요청 헤더 Content-Type 값이 비어 있는데, 실제로는 application/json 이 없으면 415(ERC000)로 거절됩니다

팀: team-06
환경: 모의 (apidemo.futures.co.kr), 2026-11-20 23:14
대상: 개발자센터 API 가이드 「요청 헤더」의 Content-Type 샘플이 빈칸인 8개
      국내_호가, 국내_신규주문, 국내_정정주문, 국내_취소주문, 국내_취소주문(야간), 국내_주문가능수량, 국내_주문내역, 해외_만기일조회
      예) https://apidev.futures.co.kr/#/api?g=118&a=244&v=1 (국내_호가)

재현 절차:
  1. 접근토큰 발급
  2. 국내_호가 POST /trade/v1/domestic/price-bid 에 같은 본문({"exch_cd":"KSP","sym":"A756C000"})을 세 가지 헤더로 전송
     (a) Content-Type: application/json  (b) Content-Type 없음  (c) Content-Type: text/plain
  3. 국내_주문내역(조회)과, 대조군으로 Content-Type 이 명세에 적힌 국내_현재가에도 같은 시험

기대 결과:
  명세 요청 헤더에 Content-Type 값이 없으므로 (b)도 받아들여지거나, 받아들이지 않는다면 명세에 application/json 이 적혀 있어야 함

실제 결과:
  국내_호가     (a) HTTP 200 정상 / (b) HTTP 415 ERC000 「지원하지 않는 Content-Type입니다. 현재 요청: application/octet-stream, 지원 형식: application/json」 / (c) HTTP 415 ERC000
  국내_주문내역 (a) 200 / (b) 415 ERC000 / (c) 415 ERC000
  국내_현재가   (a) 200 / (b) 415 ERC000 / (c) 415 ERC000   ← 대조군도 같음
  → 모든 API 가 application/json 을 요구하는데, 위 8개만 명세의 Content-Type 값이 빈칸
  (개발자센터 「Excel 다운로드」(11/20)도 이 중 7개 + 웹소켓 키 발급이 빈칸)

로그·캡처: (저장소) error_reports/0-명세대조/logs/sector0_20261120_231413.json 의 D 항목, 국내_호가 요청 헤더 표 캡처
영향:
  - 명세 표를 보고 헤더를 만드는 클라이언트·코드 생성기·AI 에이전트가 이 8개(신규·정정·취소 주문 포함)에서만 Content-Type 을 빼게 되고, 주문이 415 로 거절됨
개선 제안:
  1) 8개 API 요청 헤더의 Content-Type 샘플에 application/json 기입
  2) 오류 코드 페이지의 ERC000 설명에 「REST 요청은 토큰 발급(form-urlencoded)을 빼고 모두 application/json」 이라고 적기
```

## 근거 메모 (내부)

- 웹 명세 원본에서 `parmKey: "Content-Type"` 의 `parmSample` 이 빈 값인 API: 위 8개 (`spec/spec_web.json`).
- 테스트 스크립트 `check_sector0.py` D 부분. `requests.post(data=json.dumps(...))` 로 헤더만 바꿔 보냄.
