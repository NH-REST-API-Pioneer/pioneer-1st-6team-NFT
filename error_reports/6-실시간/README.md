# 6 · 실시간

11/22(일) 국내·해외 키 각 1회 접속으로 구독 시험 끝, **11/23(월) 장중 14:10 국내·해외 키 각 1회 추가 접속으로 실제 푸시 body 필드 4종·구독 속도·20건 상한·`header.action` 누락 시험까지 완료** (NH 11/04 재접속 경고 준수).

## 범위

| API | 문서명 | 경로 | 확인 |
| --- | --- | --- | --- |
| [국내_시세체결](https://apidev.futures.co.kr/#/api?g=123&a=191&v=1) | `KA` | WebSocket | 끝 (11/23 body 46/46 키 일치) |
| [국내_시세호가](https://apidev.futures.co.kr/#/api?g=123&a=192&v=1) | `KB` | WebSocket | 끝 (11/23 body 176/176 키 일치) |
| [국내_시세체결(야간)](https://apidev.futures.co.kr/#/api?g=123&a=193&v=1) | `KC` | WebSocket | 11/15 공지대로 `KC` 전송 시 RT_FAIL (요청 샘플·소켓 안내·엑셀 미갱신 → R05) |
| [국내_시세호가(야간)](https://apidev.futures.co.kr/#/api?g=123&a=194&v=1) | `KD` | WebSocket | 11/15 공지대로 `KD` 전송 시 RT_FAIL (요청 샘플·소켓 안내·엑셀 미갱신 → R05) |
| [국내_주문/체결](https://apidev.futures.co.kr/#/api?g=123&a=207&v=1) | `O1_K` | WebSocket | action_code "O1" 로 구독 끝 |
| [해외_시세체결](https://apidev.futures.co.kr/#/api?g=126&a=180&v=1) | `FA` | WebSocket | 끝 (11/23 body 34/34 키 일치) |
| [해외_시세호가](https://apidev.futures.co.kr/#/api?g=126&a=189&v=1) | `FB` | WebSocket | 끝 (11/23 body 51/51 키 일치) |
| [해외_주문/체결](https://apidev.futures.co.kr/#/api?g=126&a=190&v=1) | `O1` | WebSocket | 구독·해제 끝 |

## 확인한 것

스크립트: `check_realtime.py` → `logs/realtime_20261122_231702.json` (계좌번호·사용자 ID 가림), `check_realtime_weekday.py` → `logs/realtime_weekday_20261123_141022.json`, 10/27 `check_ws_subscribe.py`·`check_ws_fail.py`

| 확인 | 결과 | 명세와 |
| --- | --- | --- |
| 구독 응답 키 | `code`(RT_INIT/RT_FAIL)·`data`·`msg` | **명세 샘플 코드는 `rt_cd`** (team-08 제보됨) |
| 실시간 데이터 body 필드 (11/23) | `KA` 46/46 · `KB` 176/176 · `FA` 34/34 · `FB` 51/51 모두 문자열, 키 100% 일치 | 같음 |
| KC·KD (야간) | `KC`·`KD` 전송 시 RT_FAIL 「허용된 action_code가 아닙니다」 | 11/15 통합 공지(KA/KB로 통일) 이후에도 샘플·소켓 안내·엑셀은 `KC`·`KD` 모순 **→ R05** |
| 소켓 통신 안내(`socket-overview`) | Step 03 예시가 `wss://apidev.futures.co.kr/...`, Step 3-2 필수 칸 `N` | **도메인·필수 표기 오류 → R05** |
| O1_K / O1 | 둘 다 `action_code` "O1", 「O1_K」는 RT_FAIL. 응답 `data.action_acnt` 에 그 키의 계좌번호 → 키로 구분 | 설명서에 적음 (명세에 action_acnt 없음) |
| `action_sym` 누락 | RT_FAIL 「종목 없음 [종목: (사용자 ID)]」 | **→ R19**, R05 근거 |
| `action_code` 누락 | VALIDATION_ERROR 뒤 연결 끊김 | **→ R04**, R05 근거 |
| `header.action` 누락 (11/23) | RT_FAIL 「예외 발생 잘못된 action 입니다. A: 구독, D: 해지만 사용 가능합니다.」 (연결 유지) | **→ R04·R05 보강** |
| 구독·해제 6연속 속도 (11/23) | `0.0004초` 내 6건 모두 `RT_INIT` | 초당 속도 제한 없음 → R05 |
| APP Key 20건 상한 (11/23) | 같은 키 타 실행기 3건 + 신규 17건 `RT_INIT` 후 21번째부터 `RT_FAIL` 「현재 appkey로 20개 이상의 메시지를 등록할 수 없습니다.」 | 발대식 PPT `EST06` 과 다름 → R05 |
| FB 구독 | "FB" + 6AZ26·ESZ26 → RT_INIT 및 정상 수신 | 명세 샘플 빈칸 (R05) |
| 키와 시장이 다름 | 해외 키 KA → 「해외 종목에서만 사용 가능」, FA 국내 종목 → 종목 없음 | 명세에 없음, 설명서에 적음 |

## 제보

- [R04](R04-실시간-형식오류-처리.md) 형식 오류 처리 제각각 (action_code·header.action 누락 사례 포함)
- [R05](R05-실시간-요청명세-불일치.md) 8개 요청 명세·소켓 안내 불일치 (11/15 공지·평일 실측 반영 완료)
- [R19](R19-action_sym-빠지면-사용자ID.md) action_sym 을 빼면 사용자 ID 로 채워짐
