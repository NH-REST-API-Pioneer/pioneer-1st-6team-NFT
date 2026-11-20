# 1 · 인증

11/20 조회·발급 검증 끝. 토큰 폐기와 웹소켓 키 재사용(실제 접속)은 확인받고 진행.

## 범위

| API | 문서명 | 경로 | 확인 |
| --- | --- | --- | --- |
| [접근토큰 발급](https://apidev.futures.co.kr/#/api?g=10&a=8&v=1) | `token` | POST /auth-service/v1/token | 끝 |
| [접근토큰 폐기](https://apidev.futures.co.kr/#/api?g=10&a=79&v=1) | `revoke` | POST /auth-service/v1/revoke | 보류 (확인받고) |
| [웹소켓 키 발급](https://apidev.futures.co.kr/#/api?g=10&a=80&v=1) | `web-socket-key` | GET /permission/v1/web-socket-key | 발급 끝, 재사용 보류 |

## 확인한 것 (11/20, `check_auth.py` → `logs/auth_20261120_233527.json`)

| 확인 | 결과 | 명세와 |
| --- | --- | --- |
| 토큰 발급 응답 필드 | `access_token`(JWT 883자)·`token_type` "Bearer"·`scopes` "S"·`expires_in`·`expires_at` "YYYY-MM-DD HH:mm:ss" | 같음 |
| `expires_in` 타입 | 문자열 `"86400"` (재발급 응답) | team-07(10/25)은 숫자였음 → 설명서에 「둘 다 처리」 |
| 유효한 토큰이 있을 때 재발급 | 같은 토큰이 다시 옴, `expires_in` 86400 고정 | team-04 제보·운영진 답변과 같음 |
| 필수 누락·틀린 값 | grant_type·appkey 누락 `EAU030`, grant_type 틀림 `EAU023`, secret 틀림 `EAU025` | 코드는 문서대로. **형식이 섞임 → R09** |
| JSON 본문으로 발급 | `EAU030 필수 파라미터 누락: appkey` | **원인과 다른 메시지 → R09** |
| GET·PUT 으로 발급 | GET 200 빈 본문, PUT 404 (다른 형식) | **→ R08** |
| 웹소켓 키 | 부를 때마다 새 UUID 36자 | 명세 길이 36 같음 |
| 웹소켓 키 잘못된 인증 | 헤더 없음 `EGW023`, 깨진 토큰 `EG047`, Bearer 없이 `EGW000`(문서에 없음) | **→ R09** |
| 웹소켓 키를 POST 로 | 405, 다른 형식·내부 경로 | **→ R08** |

## 확인받고 할 것

- **토큰 폐기**: 어떤 토큰이 폐기되는지, 폐기한 토큰으로 호출하면 어떤 오류인지, 폐기 후 새로 받으면 다른 토큰인지. 같은 APP Key 를 쓰는 다른 프로그램(서버 실행기 등)의 토큰도 같이 끊길 수 있어 그 시간에 돌고 있는 게 없을 때.
- **웹소켓 키 재사용**: 한 번 쓴 키로 다시 접속되는지 (명세 「1회 사용」). 접속 2번.

## 제보

- [R08](R08-토큰발급-GET-200.md) 토큰 발급을 GET 으로 부르면 200 + 빈 본문 (PUT 404, 웹소켓 키 POST 405 도 다른 형식)
- [R09](R09-인증-오류응답-형식.md) 토큰 발급 오류 응답 형식 혼재, 문서에 없는 EGW000, 원인과 다른 EAU030 메시지
