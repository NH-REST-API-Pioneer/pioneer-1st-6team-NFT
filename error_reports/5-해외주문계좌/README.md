# 5 · 해외 주문·계좌

11/22(일) 조회 8개 끝, **11/23(월) CME 장중 14:08 `해외_주문`(R06) 미체결 지정가 1계약 검증 완료** (접수 2건 모두 즉시 취소, 미체결 0건). 다른 팀이 이미 제보한 예수금 `biz_dt` 빈 문자열(team-04)·장 밖 주문 5016(team-04)은 다시 보지 않았습니다.

## 범위

| API | 문서명 | 경로 | 확인 |
| --- | --- | --- | --- |
| [해외_주문](https://apidev.futures.co.kr/#/api?g=125&a=270&v=1) | `overseas_order` | POST /trade/v1/overseas/order | 끝 (11/23 평일 CME 장중, R06 확정) |
| [해외_주문가능수량](https://apidev.futures.co.kr/#/api?g=125&a=271&v=1) | `overseas_orderable-quantity` | POST /trade/v1/overseas/orderable-quantity | 끝 |
| [해외_주문오류내역](https://apidev.futures.co.kr/#/api?g=125&a=272&v=1) | `overseas_order-error` | POST /trade/v1/overseas/order-error | 끝 |
| [해외_주문내역](https://apidev.futures.co.kr/#/api?g=125&a=263&v=1) | `overseas_order-history` | POST /trade/v1/overseas/order-history | 끝 |
| [해외_잔고내역](https://apidev.futures.co.kr/#/api?g=125&a=267&v=1) | `overseas_open-interest` | POST /trade/v1/overseas/open-interest | 끝 |
| [해외_예수금조회](https://apidev.futures.co.kr/#/api?g=125&a=265&v=1) | `overseas_deposit` | POST /trade/v1/overseas/deposit | 끝 |
| [해외_만기일조회](https://apidev.futures.co.kr/#/api?g=125&a=262&v=1) | `overseas_product-expiry` | POST /trade/v1/overseas/product-expiry | 끝 |
| [해외_보유포지션만기조회](https://apidev.futures.co.kr/#/api?g=125&a=264&v=1) | `overseas_open-interest-expiry` | POST /trade/v1/overseas/open-interest-expiry | 끝 |
| [해외_누적손익현황](https://apidev.futures.co.kr/#/api?g=125&a=261&v=1) | `overseas_cumulative-pl` | POST /trade/v1/overseas/cumulative-pl | 끝 |

## 확인한 것

스크립트: `check_overseas_account.py` → `logs/overseas_account_20261122_231106.json`, 재현 `logs/overseas_account_repro_20261122_231157.json` (계좌 값 없이 구조·「0인지」만), `check_overseas_order.py` → `logs/overseas_order_20261123_140842.json`

| 확인 | 결과 | 명세와 |
| --- | --- | --- |
| 응답 키 | 9개 모두 명세와 같음. 11/23 주문으로 `해외_주문` 11키·`해외_주문내역` `Occurs1` 52키 일치 확인 (잔고·보유포지션만기는 0건이라 미비교) | 같음 |
| `해외_주문` 필수 모순 (11/23) | `sym` 생략·빈값 `5009` 거절(필수 N 틀림), `org_ord_no` 생략·빈값 200 정상 접수(필수 Y 틀림), 취소 `ord_qty` 생략·빈값 `ETR023` 거절(설명 신규·정정만 틀림) | **3개 모두 모순 → R06 확정** |
| 취소 후 `해외_주문내역` | 취소 완료된 주문은 `inq_div` 0(전체)~4 모두 0건으로 조회됨 | 설명서에 적음 |
| 필수 필드 빼기 | `ETR023` (주문가능수량 빈 `sym` 은 `ETR029`, 만기일조회 빈 `sym` 은 `ETR023`, 날짜 빈 값은 `ETR018`) | 같음 |
| 숫자 필드에 문자 | 주문가능수량·만기일조회·보유포지션만기 모두 뒤바뀐 `ETR045` | **→ R14 (5개 API 로 넓힘)** |
| 누적손익 환산통화 | USD·KRW 외 EUR 동작, 없는 XXX 는 「USD의 XXX환산합계」 모두 0 | **→ R18** |
| 예수금 잘못된 통화·환산구분 | `7332`, `5009`(「필수항목 입력하세요」) — 정의서에 없음 | **→ R18** |
| 주문내역 범위 밖 `inq_div`·`trd_div_cd` | 오류 없음 (국내는 5011) | **→ R18** |
| 주문가능수량 STOP 인데 `stop_pric` 없음 | 정상 응답 | **명세 「필수」와 다름 → R18** |
| 주문가능수량 가격·주문유형 | 바꿔도 결과 같음 | 설명서에만 (국내와 같은 동작) |
| 보유포지션만기 `nxt_key` "" vs "000" | 같음 | 명세 샘플·설명 차이는 문제 없음 |

## 제보

- [R06](R06-해외주문-필수모순.md) 해외_주문 필수 여부와 설명 모순 (11/23 CME 장중 실측 확정)
- [R18](R18-해외계좌-입력검증.md) 없는 환산통화가 「0」 손익 합계를 만들고, 해외 계좌 입력 검증이 제각각, 5009·7332 정의서에 없음
- (다른 섹터 제보에 근거 추가) [R14](../2-국내시세/R14-투자자동향-오류필드-뒤바뀜.md) 해외 3개 API 에서도 필드 뒤바뀜
