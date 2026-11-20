# 3 · 국내 주문·계좌

## 범위

| API | 문서명 | 경로 | 확인 |
| --- | --- | --- | --- |
| [국내_신규주문](https://apidev.futures.co.kr/#/api?g=120&a=221&v=1) | `domestic_order` | POST /trade/v1/domestic/order | |
| [국내_정정주문](https://apidev.futures.co.kr/#/api?g=120&a=230&v=1) | `domestic_modify` | POST /trade/v1/domestic/modify | |
| [국내_취소주문](https://apidev.futures.co.kr/#/api?g=120&a=238&v=1) | `domestic_cancel` | POST /trade/v1/domestic/cancel | |
| [국내_주문가능수량](https://apidev.futures.co.kr/#/api?g=120&a=234&v=1) | `domestic_orderable-quantity` | POST /trade/v1/domestic/orderable-quantity | |
| [국내_잔고내역](https://apidev.futures.co.kr/#/api?g=120&a=227&v=1) | `domestic_open-interest` | POST /trade/v1/domestic/open-interest | |
| [국내_주문오류내역](https://apidev.futures.co.kr/#/api?g=120&a=235&v=1) | `domestic_order-error` | POST /trade/v1/domestic/order-error | |
| [국내_주문내역](https://apidev.futures.co.kr/#/api?g=120&a=216&v=1) | `domestic_order-history` | POST /trade/v1/domestic/order-history | |
| [국내_예수금조회](https://apidev.futures.co.kr/#/api?g=120&a=223&v=1) | `domestic_deposit` | POST /trade/v1/domestic/deposit | |
| [국내_미결제약정(영업일 기준)](https://apidev.futures.co.kr/#/api?g=120&a=278&v=1) | `domestic_open-position` | POST /trade/v1/domestic/open-position | |
| [국내_신규주문(야간)](https://apidev.futures.co.kr/#/api?g=121&a=222&v=1) | `domestic_order-night` | POST /trade/v1/domestic/order-night | |
| [국내_정정주문(야간)](https://apidev.futures.co.kr/#/api?g=121&a=231&v=1) | `domestic_modify-night` | POST /trade/v1/domestic/modify-night | |
| [국내_취소주문(야간)](https://apidev.futures.co.kr/#/api?g=121&a=239&v=1) | `domestic_cancel-night` | POST /trade/v1/domestic/cancel-night | |

## 확인할 것

- 조회(예수금·잔고·미결제약정·주문내역·주문가능수량)부터. 주문·정정·취소는 할 때마다 확인받기
- 새 API 국내_미결제약정(영업일 기준) 명세 확인

## 제보

- 아직 없음
