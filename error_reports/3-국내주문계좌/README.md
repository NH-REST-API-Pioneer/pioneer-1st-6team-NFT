# 3 · 국내 주문·계좌

11/22(일) 조회 6개 끝. 주문은 사용자 확인(「미체결 주문만」) 뒤 시험했는데, **일요일이라 신규 주문이 `5039` 「거래소 개장전」으로 거절**돼 신규→정정→취소 흐름과 주문내역 목록 필드 대조는 **평일에 다시** 합니다. 체결이 날 수 있는 시험(매매구분·주문유형 범위 밖 값)은 하지 않습니다.

## 범위

| API | 문서명 | 경로 | 확인 |
| --- | --- | --- | --- |
| [국내_신규주문](https://apidev.futures.co.kr/#/api?g=120&a=221&v=1) | `domestic_order` | POST /trade/v1/domestic/order | 거절 시험만, 접수 흐름은 평일에 |
| [국내_정정주문](https://apidev.futures.co.kr/#/api?g=120&a=230&v=1) | `domestic_modify` | POST /trade/v1/domestic/modify | 거절 시험만, 접수 흐름은 평일에 |
| [국내_취소주문](https://apidev.futures.co.kr/#/api?g=120&a=238&v=1) | `domestic_cancel` | POST /trade/v1/domestic/cancel | 거절 시험만, 접수 흐름은 평일에 |
| [국내_주문가능수량](https://apidev.futures.co.kr/#/api?g=120&a=234&v=1) | `domestic_orderable-quantity` | POST /trade/v1/domestic/orderable-quantity | 끝 |
| [국내_잔고내역](https://apidev.futures.co.kr/#/api?g=120&a=227&v=1) | `domestic_open-interest` | POST /trade/v1/domestic/open-interest | 끝 |
| [국내_주문오류내역](https://apidev.futures.co.kr/#/api?g=120&a=235&v=1) | `domestic_order-error` | POST /trade/v1/domestic/order-error | 끝 |
| [국내_주문내역](https://apidev.futures.co.kr/#/api?g=120&a=216&v=1) | `domestic_order-history` | POST /trade/v1/domestic/order-history | 끝 |
| [국내_예수금조회](https://apidev.futures.co.kr/#/api?g=120&a=223&v=1) | `domestic_deposit` | POST /trade/v1/domestic/deposit | 끝 |
| [국내_미결제약정(영업일 기준)](https://apidev.futures.co.kr/#/api?g=120&a=278&v=1) | `domestic_open-position` | POST /trade/v1/domestic/open-position | 끝 |
| [국내_신규주문(야간)](https://apidev.futures.co.kr/#/api?g=121&a=222&v=1) | `domestic_order-night` | POST /trade/v1/domestic/order-night | 끝 |
| [국내_정정주문(야간)](https://apidev.futures.co.kr/#/api?g=121&a=231&v=1) | `domestic_modify-night` | POST /trade/v1/domestic/modify-night | 모의 미지원 (order-night 로 확인) |
| [국내_취소주문(야간)](https://apidev.futures.co.kr/#/api?g=121&a=239&v=1) | `domestic_cancel-night` | POST /trade/v1/domestic/cancel-night | 모의 미지원 (order-night 로 확인) |

## 확인한 것

스크립트: `check_account.py`(조회 6개) → `logs/account_20261122_225940.json`, 재현 `logs/account_repro_20261122_230041.json`, `check_orders.py`(미체결 주문) → `logs/orders_20261122_230253.json`

| 확인 | 결과 | 명세와 |
| --- | --- | --- |
| 조회 6개 응답 키 (최상위) | 모두 명세와 같음. 계좌가 비어 있어 `Occurs1` 안쪽은 비교 못 함 | 같음 (일부 미확인) |
| 필수 필드 빼기 | 모두 `ETR023` (예수금·주문오류내역 날짜 빈 값은 `ETR018`, 미결제약정은 `ETR023`) | **날짜 빈 값 불일치 → R16** |
| 범위 밖 코드값 | 주문내역(`trd_div`·`exec_div`·`trd_tm_div_cd`)·주문오류내역 `5011` 거절, 주문가능수량 `ord_sts`·`trd_div` 9 와 주문내역 `ord_div_cd` 9 는 200 정상 | **제각각 → R16** |
| 날짜 형식 틀림 (`2026112`) | 예수금·주문오류내역 `ETR018`, 미결제약정 `5040` 「자료 없음」 | **→ R16** |
| 주문가능수량 가격에 문자 | `ETR045`, `errorCode`·`errorMessage` 뒤바뀜 | **→ R14 (국내 2·해외 3개 API)** |
| 공용 API 야간 구분값 (모의) | 주문가능수량 `ord_sts=3`, 주문내역 `trd_tm_div_cd=2` 모두 200 정상 (`EGW053` 아님) | 설명 「야간 모의 미지원」 → R12 참고 |
| 신규 주문 (일요일) | `5039` 「처리불가 [거래소 개장전입니다. ]」 | 정의서 5039 No 1 과 유사 → **R11 실측 근거** |
| 지정가 가격 생략 / 체결조건 9 / 없는 원주문 취소 | 모두 `5039` (하한가 위반 / 잘못된 체결조건 / 주문내역조회 에러) | **5039 포괄 코드 → R11** |
| 주문 거부 메시지의 하한가 | 1275.5 (금요일 종가 기준 다음 영업일), 현재가 API `lo` 는 1269.2 | 주말 시차, 설명서에만 |
| 일요일 거부 건의 당일 주문오류내역 (`20261122`) | 0건 (다음 영업일은 미조회) | 평일 재확인 필요 |
| 야간 신규주문 | `EGW053` | 설명 「모의 미지원」과 같음 |

## 평일에 할 것 (확인받고)

- 체결 안 되는 지정가 1계약으로 신규 → 주문내역(목록 필드 51개·전체 54개 대조) → 정정 → 취소 → 주문내역
- 원주문보다 큰 수량으로 정정·취소, 정정에서 가격 생략(명세 필수), 거부 건의 평일 주문오류내역 기록 여부 확인

## 제보

- [R16](R16-계좌조회-입력검증-제각각.md) 계좌 조회 입력 검증이 API·필드마다 다름, 5011·5040 정의서에 없음
- (다른 섹터 제보에 근거 추가) [R14](../2-국내시세/R14-투자자동향-오류필드-뒤바뀜.md) 주문가능수량에서도 필드 뒤바뀜, [R11](../0-명세대조/R11-오류코드정의서-5039.md) 5039 실측
