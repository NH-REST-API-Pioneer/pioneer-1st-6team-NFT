# 1 · 인증

다음 차례.

## 범위

| API | 문서명 | 경로 | 확인 |
| --- | --- | --- | --- |
| [접근토큰 발급](https://apidev.futures.co.kr/#/api?g=10&a=8&v=1) | `token` | POST /auth-service/v1/token | |
| [접근토큰 폐기](https://apidev.futures.co.kr/#/api?g=10&a=79&v=1) | `revoke` | POST /auth-service/v1/revoke | |
| [웹소켓 키 발급](https://apidev.futures.co.kr/#/api?g=10&a=80&v=1) | `web-socket-key` | GET /permission/v1/web-socket-key | |

## 확인할 것

- 토큰 발급: 응답 필드·타입(`expires_in` 숫자는 team-07 제보됨), 잘못된 grant_type·키 (`EAU023`·`EAU025`·`EAU030`)
- 토큰 폐기: 어떤 토큰이 폐기되는지 (요청에 토큰 지정 필드가 없음), 폐기한 토큰으로 호출하면 어떤 오류인지
- 웹소켓 키: 같은 키로 두 번 접속되는지 (명세 「1회 사용」), Authorization 없이 / 만료 토큰으로

## 제보

- 아직 없음
