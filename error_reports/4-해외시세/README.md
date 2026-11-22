# 4 · 해외 시세

11/22(일) 점검 끝 (해외 모의 키, 11/20\~21 데이터). 다른 팀이 이미 제보한 현재가 거래소 코드(team-02)·빈 배열(team-03)·2종목(team-04), 차트 `nxt_key`·건수 상한·연결 일봉(team-04)·종료일(team-07)·봉 시각(team-08)은 다시 보지 않았습니다.

## 범위

| API | 문서명 | 경로 | 확인 |
| --- | --- | --- | --- |
| [해외_현재가](https://apidev.futures.co.kr/#/api?g=124&a=276&v=1) | `overseas_price` | POST /trade/v1/overseas/price | 끝 |
| [해외_호가](https://apidev.futures.co.kr/#/api?g=124&a=277&v=1) | `overseas_bid-price` | POST /trade/v1/overseas/bid-price | 끝 |
| [해외_차트(일)](https://apidev.futures.co.kr/#/api?g=124&a=274&v=1) | `overseas_chart-daily` | POST /trade/v1/overseas/chart-daily | 끝 |
| [해외_차트(분)](https://apidev.futures.co.kr/#/api?g=124&a=273&v=1) | `overseas_chart-minute` | POST /trade/v1/overseas/chart-minute | 끝 |
| [해외_옵션전체시세](https://apidev.futures.co.kr/#/api?g=124&a=266&v=1) | `overseas_price-all-option` | POST /trade/v1/overseas/price-all-option | 끝 |
| [해외_품목별세부정보](https://apidev.futures.co.kr/#/api?g=124&a=275&v=1) | `overseas_product-detail` | POST /trade/v1/overseas/product-detail | 끝 |
| [해외_장운영정보](https://apidev.futures.co.kr/#/api?g=124&a=268&v=1) | `overseas_market-operations` | POST /trade/v1/overseas/market-operations | 끝 |

## 확인한 것

스크립트: `check_overseas_market.py` → `logs/overseas_market_20261122_230716.json`, EMG_001 사례 `logs/emg001_20261122_230815.json`

| 확인 | 결과 | 명세와 |
| --- | --- | --- |
| 응답 키 (최상위·Occurs1) | 7개 모두 명세 표와 같음, 값은 모두 문자열, 항상 빈 필드 없음 | 같음 |
| 필수 필드 빼기 | `ETR023`. 단 빈 `sym` 은 `ETR029` (국내는 `ETR023`) | 대체로 같음 → R17 참고 |
| 선택 필드 기본값 | 차트 일·분 생략 = 기본값 명시, 분봉 `dcnt` 생략 = 5분봉 | 같음 |
| 분봉 `dcnt` 0·문자·-1 | 1분봉 (국내는 30초 봉) | 국내·해외 다름 → R13 에 덧붙임 |
| `cidx_yn` | 분봉·일봉 모두 결과 같음 (9 도 받아들임) | 분봉은 명세 「연결 없음」과 같음 |
| 틀린 거래소·종목·품목·건수 | 대부분 `EMG_001`, 메시지 8가지·내부 TR 코드 노출 | **정의서 뜻과 다름 → R17** |
| 품목별세부정보·장운영정보 검색 | 포함 검색("6A" → 6A, M6A), `prd_cd` 만 넣고 `prd_nm` 비우면 전체, 범위 밖 값은 0건 | 설명서에 적음 |
| 페이지 넘김 | 품목별세부정보 100+50, 장운영정보 100+45, 겹침 없음, 마지막 쪽 `nxt_key` 빈 값 | 정상 (차트의 9 채운 키와 방식 다름 → 설명서에) |

## 제보

- [R17](R17-EMG001-여러뜻.md) EMG_001 이 정의서 뜻과 달리 8가지 상황에 쓰이고 메시지에 내부 TR 코드
