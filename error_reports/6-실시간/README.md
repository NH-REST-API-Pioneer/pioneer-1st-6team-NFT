# 6 · 실시간

11/22(일) 국내 키·해외 키로 **연결 각 1번만** 열어 시험 (NH 11/04 재접속 경고). 국내 연결은 5번째 시험(`action_code` 누락)에서 서버가 끊어, 뒤의 구독 속도·20건 상한·header 시험은 못 했습니다. 일요일이라 실시간 데이터 자체(body 필드)는 받지 못했습니다.

## 범위

| API | 문서명 | 경로 | 확인 |
| --- | --- | --- | --- |
| [국내_시세체결](https://apidev.futures.co.kr/#/api?g=123&a=191&v=1) | `KA` | WebSocket || 구독·해제 끝 (데이터는 평일) |
| [국내_시세호가](https://apidev.futures.co.kr/#/api?g=123&a=192&v=1) | `KB` | WebSocket || 끝 (action_sym·action_code 누락 시험) |
| [국내_시세체결(야간)](https://apidev.futures.co.kr/#/api?g=123&a=193&v=1) | `KC` | WebSocket || 모의 미지원 (RT_FAIL) |
| [국내_시세호가(야간)](https://apidev.futures.co.kr/#/api?g=123&a=194&v=1) | `KD` | WebSocket || 모의 미지원 (RT_FAIL) |
| [국내_주문/체결](https://apidev.futures.co.kr/#/api?g=123&a=207&v=1) | `O1_K` | WebSocket || action_code "O1" 로 구독 끝 |
| [해외_시세체결](https://apidev.futures.co.kr/#/api?g=126&a=180&v=1) | `FA` | WebSocket || 구독·해제 끝 |
| [해외_시세호가](https://apidev.futures.co.kr/#/api?g=126&a=189&v=1) | `FB` | WebSocket || 구독·해제 끝 |
| [해외_주문/체결](https://apidev.futures.co.kr/#/api?g=126&a=190&v=1) | `O1` | WebSocket || 구독·해제 끝 |

## 확인한 것

스크립트: `check_realtime.py` → `logs/realtime_20261122_231702.json` (계좌번호·사용자 ID 가림), 10/27 `check_ws_subscribe.py`·`check_ws_fail.py`

| 확인 | 결과 | 명세와 |
| --- | --- | --- |
| 구독 응답 키 | `code`(RT_INIT/RT_FAIL)·`data`·`msg` | **명세 샘플 코드는 `rt_cd`** (team-08 제보됨) |
| KC·KD (야간) | RT_FAIL 「허용된 action_code가 아닙니다」 | 설명 「모의 미지원」과 같음 |
| O1_K / O1 | 둘 다 `action_code` "O1", 「O1_K」는 RT_FAIL. 응답 `data.action_acnt` 에 그 키의 계좌번호 → 키로 구분 | 설명서에 적음 (명세에 action_acnt 없음) |
| `action_sym` 누락 | RT_FAIL 「종목 없음 [종목: (사용자 ID)]」 | **→ R19**, R05 근거 |
| `action_code` 누락 | VALIDATION_ERROR 뒤 연결 끊김 | **→ R04 에 추가**, R05 근거 |
| FB 구독 | "FB" + 6AZ26 → RT_INIT | 명세 샘플 빈칸 (R05) |
| 키와 시장이 다름 | 해외 키 KA → 「해외 종목에서만 사용 가능」, FA 국내 종목 → 종목 없음 | 명세에 없음, 설명서에 적음 |

## 남은 것 (연결을 새로 열어야 함, 확인받고)

- 구독·해제 메시지 속도 제한 (1초에 6번)
- APP Key 당 20건 상한 (21번째에 `EST06`?)
- 평일 장중 실시간 데이터 body 필드가 명세 응답 표와 같은지 (KA·KB·FA·FB)

## 제보

- [R04](R04-실시간-형식오류-처리.md) 형식 오류 처리 제각각 (action_code 누락 사례 추가)
- [R05](R05-실시간-요청명세-불일치.md) 8개 요청 명세 불일치 (호출로 보강, 올릴 준비 됨)
- [R19](R19-action_sym-빠지면-사용자ID.md) action_sym 을 빼면 사용자 ID 로 채워짐
