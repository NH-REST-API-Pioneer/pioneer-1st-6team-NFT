# R19 · 실시간 구독에서 action_sym 을 빼면 서버가 그 자리를 사용자 ID 로 채워, 오류 메시지에 로그인 ID 가 나옵니다

- 섹터: 6 · 종류: 서버 동작 결함 (기본값 처리·정보 노출) · 검증: 실제 호출 (국내·해외 키) · AI 영향: 중
- 상태: 올릴 준비 됨. **제보문에 실제 ID·계좌번호를 넣지 않음** (로그도 가림)

```text
[제보] 실시간 구독 메시지에서 action_sym 을 빼면 서버가 사용자 ID 를 종목코드로 써서 「종목정보를 찾을 수 없습니다 [종목: (사용자 ID)]」가 옵니다

팀: team-06
환경: 모의 (wss://apidemo.futures.co.kr), 2026-11-22 23:17 (국내 모의 키·해외 모의 키, 연결 각 1회)
대상: 실시간 구독 (KB, O1_K 로 확인)  https://apidev.futures.co.kr/#/api?g=123&a=192&v=1 (KB)
      명세: body.action_sym 필수 N (KA 만 Y)

재현 절차:
  1. 웹소켓 키 발급 → wss 접속 → session_init (INIT_SUCCESS)
  2. {"header":{"api_ver":"1","action":"A"},"body":{"action_code":"KB"}}  (action_sym 없음)
  3. 비교: action_code 를 명세 문서명 그대로 "O1_K" 로, action_sym 없이

기대 결과:
  명세상 action_sym 이 선택(N)이므로 받아들이거나, 필요하다면 「action_sym 은 필수입니다」 같은 오류

실제 결과:
  2 → {"code":"RT_FAIL","data":{"action":"A","action_code":"KB","action_sym":"<사용자 ID>"},"msg":"해당 종목에 대한 종목정보를 찾을 수 없습니다. [종목: <사용자 ID>]"}
  3 → {"code":"RT_FAIL","data":{"action":"A","action_code":"O1_K","action_sym":"<사용자 ID>"},"msg":"예외 발생 허용된 action_code가 아닙니다."}
  → 보내지 않은 action_sym 자리에 APP Key 사용자의 로그인 ID(로 보이는 값)가 채워져 응답·오류 메시지에 나옴 (국내 키는 KB·O1_K, 해외 키는 O1_K 에서 각각 그 키의 ID 확인)
  → 오류 메시지도 「종목 없음」으로 나와 원인(action_sym 누락)을 알 수 없음
  참고: action_code 를 빼면 {"type":"VALIDATION_ERROR","message":"action_code는 필수입니다."} 뒤 서버가 연결을 끊음 (명세는 action_code 도 KA 만 Y, 나머지 7개는 N)

로그·캡처: (저장소) error_reports/6-실시간/logs/realtime_20261122_231702.json (사용자 ID·계좌번호는 `<사용자ID?>`·`<계좌번호>`로 가림 처리; 필요 시 실제 노출 형식은 운영진 DM 으로 전달 가능), 재현 스크립트 error_reports/6-실시간/check_realtime.py
영향:
  - 오류 메시지·로그에 사용자 ID 가 남아, 로그를 공유하거나 화면에 띄우면 ID 가 노출됨
  - action_sym 누락을 「종목 없음」으로 오해해 종목코드를 고치게 됨
개선 제안:
  1) action_sym 이 필요한 코드(KA·KB·FA·FB 등)는 없으면 「action_sym 은 필수입니다」로 RT_FAIL, 명세 필수 표시도 Y 로
  2) 빠진 필드를 사용자 ID 로 채우지 말고, 응답·메시지에 사용자 ID 를 넣지 않기
```

## 근거 메모 (내부)

- 실제 값은 대화 중에만 봤고 저장소에는 남기지 않음. 국내 키 응답의 ID 는 팀원 이름을 영문 자판으로 친 모양, 해외 키는 다른 사람 ID 모양.
- `O1` 구독 성공 응답에는 `data.action_acnt` 로 계좌번호가 옴 (명세 응답 표에 없는 필드). 국내 키 → 국내 계좌, 해외 키 → 해외 계좌라서 **`O1_K`/`O1` 구분은 action_code 가 아니라 APP Key(계좌)로 됨**.
