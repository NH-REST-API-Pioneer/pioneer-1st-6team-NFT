# 1 · 인증

11/20 점검 끝 (발급·폐기·웹소켓 키 재사용 모두).

## 범위

| API | 문서명 | 경로 | 확인 |
| --- | --- | --- | --- |
| [접근토큰 발급](https://apidev.futures.co.kr/#/api?g=10&a=8&v=1) | `token` | POST /auth-service/v1/token | 끝 |
| [접근토큰 폐기](https://apidev.futures.co.kr/#/api?g=10&a=79&v=1) | `revoke` | POST /auth-service/v1/revoke | 끝 |
| [웹소켓 키 발급](https://apidev.futures.co.kr/#/api?g=10&a=80&v=1) | `web-socket-key` | GET /permission/v1/web-socket-key | 끝 |

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
| 웹소켓 키 재사용 (`check_ws_key_reuse.py`) | 같은 키로 두 번째 접속 → 403 `EST01`, 새 키는 정상 | 명세 「1회 사용」과 같음 |
| 토큰 폐기 (`check_revoke.py`) | Authorization 토큰 + appkey·appsecret 둘 다 필요. 성공 `{"status":200,"message":"토큰 폐기 성공"}` | 어떤 토큰인지 명세에 없음 → 설명서에 적음 |
| 폐기한 토큰으로 호출 | 403 `EG049` 「현재 토큰은 폐기 되었습니다」 | **문서에 없는 코드 → R10** |
| 폐기 뒤 재발급 | 새 토큰 | |
| 폐기 헤더 없음 / secret 틀림 | `EGW023` / 400 `EAU011` (내부 경로 `revokeP`) | **EAU011 문서에 없음 → R10, 경로 → R09** |

## 제보

- [R08](R08-토큰발급-GET-200.md) 토큰 발급을 GET 으로 부르면 200 + 빈 본문 (PUT 404, 웹소켓 키 POST 405 도 다른 형식)
- [R09](R09-인증-오류응답-형식.md) 토큰 발급 오류 응답 형식 혼재(errorStatus 문자열/숫자, 내부 경로), JSON 으로 보내면 원인과 다른 「appkey 누락」
- [R10](R10-문서에없는-인증오류코드.md) 문서에 없는 인증 오류 코드 EG049(폐기된 토큰, 403)·EAU011·EGW000 — 「오류 코드 정의서」 파일 확인 후 올림

## 우리 코드에 고칠 것

- ~~팀 클라이언트가 폐기된 토큰(403 `EG049`)에서 재발급하지 않음~~ → 브랜치 `nh-client-eg049` 커밋 9f5e9e9 에서 7개 `nh_client.py` 모두 고침 (가짜 응답으로 분기 시험, 권한 없음 `EGW016` 은 그대로 오류)
