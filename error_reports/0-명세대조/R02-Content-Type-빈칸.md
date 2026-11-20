# R02 · 8개 REST API 개별 명세의 `Content-Type` 샘플과 요청 샘플 값이 비어 있습니다

- 섹터: 0 · 종류: 명세 누락 (문서 일관성) · 검증: 라이브 화면·엑셀 전수 대조 + 실제 호출 · AI 영향: 중
- 상태: 수정 완료 (단독 문서 누락 제보 또는 R07 병합 검토 — 공통 개요 `response-overview`에는 이미 `application/json`이 명시되어 있으므로, 서버 415 오류 제보가 아니라 **개별 API 명세·요청 샘플 누락**으로 제출해야 함)

```text
[제보] 8개 REST API 개별 명세의 요청 헤더 Content-Type 샘플 및 요청 샘플 값이 비어 있습니다 (웹 가이드·엑셀)

팀: team-06
환경: 개발자센터 웹 가이드·「Excel 다운로드」(2026-11-20), 모의 서버(apidemo.futures.co.kr) 2026-11-20 23:14 확인
대상: 개발자센터 API 가이드 「요청 헤더」의 Content-Type 샘플과 「요청 샘플」 값이 빈칸인 REST API 8개
      국내_호가, 국내_신규주문, 국내_정정주문, 국내_취소주문, 국내_취소주문(야간), 국내_주문가능수량, 국내_주문내역, 해외_만기일조회
      예) 국내_호가 https://apidev.futures.co.kr/#/api?g=118&a=244&v=1
          국내_신규주문 https://apidev.futures.co.kr/#/api?g=120&a=221&v=1
          국내_주문내역 https://apidev.futures.co.kr/#/api?g=120&a=216&v=1
      대조) 국내_현재가 https://apidev.futures.co.kr/#/api?g=118&a=242&v=1 (샘플: application/json)
      공통 개요) 응답 테스트 개요 및 설명 https://apidev.futures.co.kr/#/api/info/response-overview

기대 결과:
  공통 개요(「응답 테스트 개요 및 설명」 → API 요청 정보 안내)에 「Content-Type 헤더를 application/json로 설정합니다」라고 안내돼 있고,
  나머지 31개 POST REST API(토큰 발급 제외)에도 요청 헤더 샘플과 요청 샘플에 `Content-Type: application/json`이 적혀 있으므로,
  위 8개 API 역시 동일하게 `application/json`이 기입되어 있어야 함.

실제 결과:
  1. 웹 가이드: 위 8개 API는 「요청 헤더」 표에 Content-Type(필수 Y) 줄은 있지만 `샘플` 칸이 비어 있고,
     그에 따라 우측 「요청 샘플」에도 `Content-Type:` 뒤 값이 빈 채로 표시됨.
  2. 「Excel 다운로드」(11/20): 위 8개 API 전부(8/8) 상단 Content-Type 칸이 빈칸 (GET 방식인 웹소켓 키 발급 포함 총 9개 빈칸).
  3. 실제 호출 확인(모의 서버): 위 8개 중 국내_호가·국내_주문내역 모두 실제로는 다른 API(대조군 국내_현재가)와 동일하게
     `Content-Type: application/json` 일 때만 HTTP 200 정상 응답하며, 헤더 생략 또는 `text/plain` 시 HTTP 415 ERC000 거절.

로그·캡처: (저장소) error_reports/0-명세대조/logs/sector0_20261120_231413.json 의 D 항목, 국내_호가·국내_신규주문·국내_주문내역 웹 가이드 화면
영향:
  - 개별 API 페이지의 「요청 샘플」을 복사하거나 명세 표를 파싱하는 클라이언트·코드 생성기·AI 에이전트가
    이 8개(신규·정정·취소 주문 포함)에서만 Content-Type 값을 비워 보내 HTTP 415(ERC000)로 실패함
개선 제안:
  1) 위 8개 API 요청 헤더의 `Content-Type` 샘플에 `application/json` 기입 (웹 가이드 「요청 샘플」 및 「Excel 다운로드」 동시 반영)
```

## 근거 메모 (내부)

- 웹 명세 원본(`spec/spec_web.json`)에서 `parmKey: "Content-Type"` 의 `parmSample` 이 빈 값인 POST REST API: 위 8개 (나머지 31개 POST REST API는 `application/json`, 토큰 발급은 `application/x-www-form-urlencoded`, 웹소켓 키 발급은 GET이라 헤더 행 없음).
- 11/20 엑셀(`전체_API_문서.xlsx`)도 위 8개 전부 빈칸 확인 (기존 초안의 「7개」는 오기였음).
- 공통 개요(`/#/api/info/response-overview`)에 이미 `application/json`이 명시되어 있고 개별 표도 `필수: Y`이므로, 415 거절 자체는 정상 동작임 → 순수 문서 샘플 누락 제보로 수정.
- 테스트 스크립트 `check_sector0.py` D 부분. `requests.post(data=json.dumps(...))` 로 헤더만 바꿔 보냄.
