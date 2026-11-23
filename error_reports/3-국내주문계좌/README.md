# 3 · 국내 주문·계좌

11/22(일) 조회 6개·주말 거절 시험 끝, **11/23(월) 장중 14:05 체결 안 되는 매수 지정가 1계약으로 신규→정정→취소 흐름 및 `국내_주문내역` `Occurs1` 51개 키 대조 완료** (남은 미체결 0건). 체결이 날 수 있는 시험(매매구분·주문유형 범위 밖 값)은 하지 않았습니다.

## 범위

| API | 문서명 | 경로 | 확인 |
| --- | --- | --- | --- |
| [국내_신규주문](https://apidev.futures.co.kr/#/api?g=120&a=221&v=1) | `domestic_order` | POST /trade/v1/domestic/order | 끝 (11/23 평일 접수·거절) |
| [국내_정정주문](https://apidev.futures.co.kr/#/api?g=120&a=230&v=1) | `domestic_modify` | POST /trade/v1/domestic/modify | 끝 (11/23 평일 접수·거절) |
| [국내_취소주문](https://apidev.futures.co.kr/#/api?g=120&a=238&v=1) | `domestic_cancel` | POST /trade/v1/domestic/cancel | 끝 (11/23 평일 접수·거절) |
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

스크립트: `check_account.py`(조회 6개) → `logs/account_20261122_225940.json`, 재현 `logs/account_repro_20261122_230041.json`, `check_orders.py`(미체결 주문) → `logs/orders_20261122_230253.json` (일요일), `logs/orders_20261123_140522.json` (평일 장중)

| 확인 | 결과 | 명세와 |
| --- | --- | --- |
| 조회 6개 응답 키 | 최상위 모두 명세와 같음. `국내_주문내역` `Occurs1` 51개 키 11/23 평일 주문으로 100% 일치 확인 (잔고·미결제는 체결 없이 0건이라 미비교) | 같음 |
| 필수 필드 빼기 | 모두 `ETR023` (예수금·주문오류내역 날짜 빈 값은 `ETR018`, 미결제약정은 `ETR023`, 정정 가격 생략은 `ETR023`) | **날짜 빈 값 불일치 → R16** |
| 범위 밖 코드값 | 주문내역(`trd_div`·`exec_div`·`trd_tm_div_cd`)·주문오류내역 `5011` 거절, 주문가능수량 `ord_sts`·`trd_div` 9 와 주문내역 `ord_div_cd` 9 는 200 정상 | **제각각 → R16** |
| 날짜 형식 틀림 (`2026112`) | 예수금·주문오류내역 `ETR018`, 미결제약정 `5040` 「자료 없음」 | **→ R16** |
| 주문가능수량 가격에 문자 | `ETR045`, `errorCode`·`errorMessage` 뒤바뀜 | **→ R14 (국내 2·해외 3개 API)** |
| 공용 API 야간 구분값 (모의) | 주문가능수량 `ord_sts=3`, 주문내역 `trd_tm_div_cd=2` 모두 200 정상 (`EGW053` 아님) | 설명 「야간 모의 미지원」 → R12 참고 |
| 평일 신규→정정→취소 흐름 (11/23) | `0000000601`(신규) → `0000000602`(정정) → `0000000603`(취소) 모두 200 정상, 미체결 0건. 단 정정 주문 `602`는 `occurs1_org_ord_no` 빈 값이고 `603` 취소 뒤에도 `occurs1_ord_sts_nm` 이 `정정완료` | 응답 키 일치 (상태명·원주문번호 특이점은 설명서에) |
| 5039 거부 6종 (11/22·11/23) | 일요일 개장전 · 가격 생략(하한가 위반) · 체결조건 9 · 없는 원주문 취소 · 동일 가격 정정 · 초과 수량 취소 모두 `5039` | **5039 포괄 코드 → R11** |
| 주문 거부 메시지의 하한가 | 1275.5 (금요일 종가 기준 다음 영업일), 일요일 현재가 API `lo` 는 1269.2 (월요일엔 둘 다 1275.5 일치) | 주말 시차, 설명서에만 |
| 거부 건의 주문오류내역 조회 | 일요일(`20261122`)뿐 아니라 **평일 장중(`20261123`) 5039 거부 5건 직후에도 0건** | **정의서 `[예시]`와 불일치 → R11 보강** |
| 야간 신규주문 | `EGW053` | 설명 「모의 미지원」과 같음 |

## 제보

- [R16](R16-계좌조회-입력검증-제각각.md) 계좌 조회 입력 검증이 API·필드마다 다름, 5011·5040 정의서에 없음
- (다른 섹터 제보에 근거 추가) [R14](../2-국내시세/R14-투자자동향-오류필드-뒤바뀜.md) 주문가능수량에서도 필드 뒤바뀜, [R11](../0-명세대조/R11-오류코드정의서-5039.md) 5039 실측
