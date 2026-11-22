# 2 · 국내 시세

11/22 점검 끝 (모의, 일요일이라 11/20 주간·11/20\~21 야간 데이터). 다른 팀이 이미 제보한 차트 `nxt_key`·`req_qty` 상한·기간 조회는 다시 보지 않았습니다.

## 범위

| API | 문서명 | 경로 | 확인 |
| --- | --- | --- | --- |
| [국내_현재가](https://apidev.futures.co.kr/#/api?g=118&a=242&v=1) | `domestic_price` | POST /trade/v1/domestic/price | 끝 |
| [국내_호가](https://apidev.futures.co.kr/#/api?g=118&a=244&v=1) | `domestic_price-bid` | POST /trade/v1/domestic/price-bid | 끝 |
| [국내_차트(일)](https://apidev.futures.co.kr/#/api?g=118&a=237&v=1) | `domestic_chart-daily` | POST /trade/v1/domestic/chart-daily | 끝 |
| [국내_차트(분)](https://apidev.futures.co.kr/#/api?g=118&a=236&v=1) | `domestic_chart-minute` | POST /trade/v1/domestic/chart-minute | 끝 |
| [국내_선물전체시세](https://apidev.futures.co.kr/#/api?g=118&a=219&v=1) | `domestic_price-all-futures` | POST /trade/v1/domestic/price-all-futures | 끝 |
| [국내_옵션전체시세](https://apidev.futures.co.kr/#/api?g=118&a=225&v=1) | `domestic_price-all-option` | POST /trade/v1/domestic/price-all-option | 끝 |
| [국내_투자자직전매매동향](https://apidev.futures.co.kr/#/api?g=118&a=240&v=1) | `domestic_trend-investor` | POST /trade/v1/domestic/trend-investor | 끝 |
| [국내_종목예상체결가](https://apidev.futures.co.kr/#/api?g=118&a=232&v=1) | `domestic_price-expect-fill` | POST /trade/v1/domestic/price-expect-fill | 끝 |
| [국내_품목별세부정보](https://apidev.futures.co.kr/#/api?g=118&a=241&v=1) | `domestic_product-detail` | POST /trade/v1/domestic/product-detail | 끝 |
| [국내_현재가(야간)](https://apidev.futures.co.kr/#/api?g=119&a=243&v=1) | `domestic_price-night` | POST /trade/v1/domestic/price-night | 끝 |
| [국내_옵션전체시세(야간)](https://apidev.futures.co.kr/#/api?g=119&a=226&v=1) | `domestic_price-all-option-night` | POST /trade/v1/domestic/price-all-option-night | 끝 |

## 확인한 것

스크립트: `check_market.py`(응답 키·필수 필드·범위 밖 값·기본값) → `logs/market_20261122_225342.json`, `check_market_values.py`(받아들여진 값이 결과를 바꾸는지) → `logs/market_values_20261122_225432.json`, 재현 기록 `logs/market_repro_20261122_225559.json`

| 확인 | 결과 | 명세와 |
| --- | --- | --- |
| 응답 키 (최상위·Occurs1) | 호출되는 9개 모두 명세 표와 정확히 같음, 값은 모두 문자열 | 같음 |
| 필수 필드 빼기·빈 값 | 모두 `ETR023` (날짜·만기는 빈 값이면 `ETR018`) | 같음 |
| 선택 필드 기본값 | 빼고 보낸 것과 명세 기본값을 적은 것 결과 같음 (차트 일·분, 옵션전체시세, 투자자동향) | 같음 |
| 야간 데이터 (모의) | 차트 `nght_tp` 2·호가 `exch_cd` NDV 가 야간 데이터를 줌 | **설명은 「모의 미지원」 → R12** |
| 범위 밖 코드값 | `nght_tp` 9·A → 야간, `exch_cd` XXX → 주간, `inq_tp` 9 → 금액, `cidx_yn`·`fut_cidx_exp_mod` 9 → 0, `dcnt` 0·abc → 30초 봉 | **오류 없음 → R13** |
| 숫자 필드에 문자 | 차트(일) `req_qty` abc → `ETR019`(문서에 없음), 차트(분) `dcnt` abc → 받아들임 | **→ R13** |
| 투자자동향 2020년 날짜 | 400, `errorCode`·`errorMessage` 뒤바뀜, 내부 `ETR045` | **→ R14** |
| 항상 빈 필드 | 차트(분) `occurs1_rt` 300봉, 옵션전체시세 `occurs1_p_theo_pric_chg` 261행 | **→ R15** |
| 현재가(야간)·옵션전체시세(야간) | `EGW053` | 설명 「모의 환경 미지원」과 같음 |
| 휴장일·미래 날짜 (투자자동향) | 모든 값 0 인 정상 응답 | 예수금 휴장일과 같은 동작 (운영진은 정상이라 답함) → 제보 안 함, 설명서에만 |
| 없는 prd·opt_prd | `EMG_001`, 메시지에 내부 TR 코드 | R13 참고에 넣음 |

## 제보

- [R12](R12-야간-모의미지원-설명.md) 「야간 모의 미지원」인 차트·호가가 모의에서 야간 데이터를 줌
- [R13](R13-코드값-검증없음.md) 코드값 필드가 범위 밖 값을 오류 없이 다른 값으로 처리 (+ ETR019 문서 누락)
- [R14](R14-투자자동향-오류필드-뒤바뀜.md) 투자자동향 옛 날짜 → errorCode·errorMessage 뒤바뀐 오류
- [R15](R15-항상-빈필드.md) 차트(분) 등락률·옵션 풋 이론가대비 항상 빈 값
