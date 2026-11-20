# 6 · 실시간

재접속을 최소화 (NH 11/04 경고). 연결을 끊을 수 있는 시험은 미리 확인받기.

## 범위

| API | 문서명 | 경로 | 확인 |
| --- | --- | --- | --- |
| [국내_시세체결](https://apidev.futures.co.kr/#/api?g=123&a=191&v=1) | `KA` | WebSocket | |
| [국내_시세호가](https://apidev.futures.co.kr/#/api?g=123&a=192&v=1) | `KB` | WebSocket | |
| [국내_시세체결(야간)](https://apidev.futures.co.kr/#/api?g=123&a=193&v=1) | `KC` | WebSocket | |
| [국내_시세호가(야간)](https://apidev.futures.co.kr/#/api?g=123&a=194&v=1) | `KD` | WebSocket | |
| [국내_주문/체결](https://apidev.futures.co.kr/#/api?g=123&a=207&v=1) | `O1_K` | WebSocket | |
| [해외_시세체결](https://apidev.futures.co.kr/#/api?g=126&a=180&v=1) | `FA` | WebSocket | |
| [해외_시세호가](https://apidev.futures.co.kr/#/api?g=126&a=189&v=1) | `FB` | WebSocket | |
| [해외_주문/체결](https://apidev.futures.co.kr/#/api?g=126&a=190&v=1) | `O1` | WebSocket | |

## 확인할 것

- R05 보강: KB 를 `action_sym`/`action_code` 없이 보냈을 때, 구독 메시지 초당 제한
- 국내 `O1_K`·해외 `O1` 이 같은 `action_code` "O1" 인데 어떻게 구분되는지

## 제보

- [R04](R04-실시간-형식오류-처리.md) 실시간 형식오류 처리
- [R05](R05-실시간-요청명세-불일치.md) 실시간 요청명세 불일치
