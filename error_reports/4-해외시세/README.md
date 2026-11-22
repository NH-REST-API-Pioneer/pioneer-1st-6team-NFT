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
| 응답 키 (최상위·Occurs1) | 7개 모두 명세 표와 같음, 값은 모두 문자열, `Occurs1` 내 항상 빈 필드 없음. 단 `해외_차트(일)·(분)` 최상위 `sym` 은 `cidx_yn` 0·생략·9 에서 빈 문자열 `""`, `cidx_yn=1` 에서만 `"6AZ26"` 반환 (23:56 추가 확인) | 키 목록은 같음 (`sym` 빈 값은 설명서에) |
| 필수 필드 빼기 | `ETR023`. 단 `해외_호가·차트(일)·차트(분)` 빈 `sym` 은 `ETR029` (국내는 `ETR023`), `해외_옵션전체시세` `sym` 은 명세 `필수: N` 이나 누락·빈값 모두 `EMG_001` `[]종목을 찾을수 없습니다.` (23:56 추가 확인) | 대체로 같음 → R17 참고 |
| 선택 필드 기본값 | 차트 일·분 생략 = 기본값 명시, 분봉 `dcnt` 생략 = 5분봉 | 같음 |
| 분봉 `dcnt` 0·문자·-1 | `0·문자`(23:06)·`-1`(23:56 추가 확인) 모두 1분봉 (국내는 `0·문자` 30초 봉) | 국내·해외 다름 → R13 에 덧붙임 (`0·문자`) |
| `cidx_yn` | 분봉·일봉(단기 5봉 기준) 모두 `Occurs1` 데이터는 같으나, `cidx_yn=1` 에서만 최상위 `sym` 이 `""` → `"6AZ26"` 으로 채워짐 (`9` 도 오류 없이 받아들임, 23:56 추가 확인) | 분봉은 명세 「연결 없음」과 데이터상 일치 |
| 틀린 거래소·종목·품목·건수 | `EMG_001` 8가지 메시지(정의서 일치 1건·불일치 7건), `해외_호가` 거래소 오류를 `[]종목 없음`으로 오표시, 없는 종목은 `해외_호가` `ETR029` vs `해외_옵션전체시세` `EMG_001` | **정의서 뜻·원인과 다름 → R17** |
| 품목별세부정보·장운영정보 검색 | `장운영정보` `prd_cd` 1·2 포함 검색, `prd_nm` 비우면 전체; `품목별세부정보` 필터 5종 정상 동작; 두 API 모두 범위 밖 값은 0건 | 설명서에 적음 |
| 페이지 넘김 | 품목별세부정보 100+50, 장운영정보 100+45, 겹침 없음, 마지막 쪽 `nxt_key` 빈 값 | 정상 (차트의 9 채운 키와 방식 다름 → 설명서에) |
| 명세 표기 오류 | `품목별세부정보` `occurs1_exch_cd` 「거래소V코드」·`prd_nm` 「2:공백」, `옵션전체시세` `occurs1_c/p_exec_cd` 한글명 「콜/풋종목코드」(실제는 거래소코드 `"CME"`) | **→ R07 표기 모음에 추가** |

## 제보

- [R17](R17-EMG001-여러뜻.md) EMG_001 이 정의서 뜻과 달리 8가지 메시지(일치 1건·불일치 7건)로 쓰이고, 해외_호가 거래소 오류를 종목 없음으로 오표시
- [R07](../0-명세대조/R07-표기-모음.md) 해외 시세 명세 표기 오류 3건 추가 (`거래소V코드`·`prd_nm 2:공백`·`occurs1_c/p_exec_cd` 한글명)
