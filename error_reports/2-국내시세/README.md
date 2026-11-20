# 2 · 국내 시세

## 범위

| API | 문서명 | 경로 | 확인 |
| --- | --- | --- | --- |
| [국내_현재가](https://apidev.futures.co.kr/#/api?g=118&a=242&v=1) | `domestic_price` | POST /trade/v1/domestic/price | |
| [국내_호가](https://apidev.futures.co.kr/#/api?g=118&a=244&v=1) | `domestic_price-bid` | POST /trade/v1/domestic/price-bid | |
| [국내_차트(일)](https://apidev.futures.co.kr/#/api?g=118&a=237&v=1) | `domestic_chart-daily` | POST /trade/v1/domestic/chart-daily | |
| [국내_차트(분)](https://apidev.futures.co.kr/#/api?g=118&a=236&v=1) | `domestic_chart-minute` | POST /trade/v1/domestic/chart-minute | |
| [국내_선물전체시세](https://apidev.futures.co.kr/#/api?g=118&a=219&v=1) | `domestic_price-all-futures` | POST /trade/v1/domestic/price-all-futures | |
| [국내_옵션전체시세](https://apidev.futures.co.kr/#/api?g=118&a=225&v=1) | `domestic_price-all-option` | POST /trade/v1/domestic/price-all-option | |
| [국내_투자자직전매매동향](https://apidev.futures.co.kr/#/api?g=118&a=240&v=1) | `domestic_trend-investor` | POST /trade/v1/domestic/trend-investor | |
| [국내_종목예상체결가](https://apidev.futures.co.kr/#/api?g=118&a=232&v=1) | `domestic_price-expect-fill` | POST /trade/v1/domestic/price-expect-fill | |
| [국내_품목별세부정보](https://apidev.futures.co.kr/#/api?g=118&a=241&v=1) | `domestic_product-detail` | POST /trade/v1/domestic/product-detail | |
| [국내_현재가(야간)](https://apidev.futures.co.kr/#/api?g=119&a=243&v=1) | `domestic_price-night` | POST /trade/v1/domestic/price-night | |
| [국내_옵션전체시세(야간)](https://apidev.futures.co.kr/#/api?g=119&a=226&v=1) | `domestic_price-all-option-night` | POST /trade/v1/domestic/price-all-option-night | |

## 확인할 것

- 응답 필드가 명세 표와 같은지, 필수 필드를 빼면 거절되는지, 선택 필드 기본값대로 동작하는지
- 차트 `nxt_key` 흐름·상한은 다른 팀이 많이 제보함 (team-04·07) → 중복 확인 후 진행

## 제보

- 아직 없음
