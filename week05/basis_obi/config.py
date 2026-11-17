"""KRX–MKU 베이시스 과제 설정 — 상품·시간·비용을 한곳에. 근거는 README.

종목코드 확인 (2026-11-17, NH API):
  KRX 미국달러선물 근월 A756C000 (2026년 12월물, 최종거래일 2026-12-21)  — 국내 선물전체시세 prd=75
  SGX 미니 원달러   근월 MKUZ26   (만기 2026-12-21)                     — 해외 만기일조회
  11월물(A756B000, MKUX26)은 2026-11-16 만료.
웹소켓 TR (개발자센터 명세): 국내 체결 KA·호가 KB, 해외 체결 FA·호가 FB. 국내 야간(KC·KD)은 모의 미지원.
동시 구독 최대 20건 (호가·체결·주문체결 합산).
"""
from __future__ import annotations

KRX_SYM = "A756C000"
MKU_SYM = "MKUZ26"
SUBS = {
    "dom": [("KA", KRX_SYM), ("KB", KRX_SYM)],      # 국내 키
    "ovs": [("FA", MKU_SYM), ("FB", MKU_SYM)],      # 해외 키
}

# 수집 시간 (KST, 평일). KRX 주간장 08:45~15:45 앞뒤로 조금씩
COLLECT_START = "08:30"
COLLECT_END = "15:50"
KRX_OPEN = ("08:45", "15:45")

# 1쌍 = KRX 2계약 + MKU 1계약 (명세)
PAIR_KRX_CONTRACTS = 2
PAIR_MKU_CONTRACTS = 1
FEE_ROUNDTRIP_KRW_PER_USD = 0.435      # 왕복 수수료 (8,120원)
FEE_ROUNDTRIP_KRW = 8120
MKT_ROUNDTRIP_KRW_PER_USD = 0.79       # 두 다리 시장가 왕복 (호가로 체결할 때는 중복 차감 안 함)
OVS_LATENCY_S = 1.3                    # 해외 주문 지연 실측 (n=4, 3주차)
