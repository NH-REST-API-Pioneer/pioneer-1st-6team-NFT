"""KRX 달러선물 → MKU 연계 전략 규칙 (구상 문서 4절, 6절 결과 반영).

KRX 가 짧은 시간에 크게 움직였는데 MKU 가 절반도 안 따라왔으면, MKU 를 KRX 방향으로 진입해
따라잡는 몫을 먹는다. MKU 가격은 1,000원당 USD 라서 KRX 상승(원화 약세) = MKU 매도.

봉은 1분, 시각은 「그 분의 시작」(두 시장 같음). 판정은 봉이 끝날 때, 체결은 다음 봉에서 한다.
  체결 기준가 = 다음 분 MKU 시가 (그 분에 체결이 없으면 직전 체결가).
  슬리피지·수수료는 backtest_link.py 가 기준가에 붙인다.
"""
from __future__ import annotations

from dataclasses import dataclass, field

MKU_TICK = 0.0001


@dataclass(frozen=True)
class Params:
    """기본값 = 구상 문서 출발값을 6-2절(보유 시간 실험)에 맞춘 값. 채택 값은 백테스트 뒤 정함."""
    window: int = 1               # 충격 창 (분)                      — 최적화
    move_won: float = 0.5         # KRX 충격 기준 (원). MKU 1틱 ≈ 0.18원 — 최적화
    rho_max: float = 0.5          # MKU 반영률이 이보다 작아야 진입     — 최적화
    hold_min: int = 20            # 시간 청산 (분)                    — 최적화
    tp_ticks: float | None = None  # 익절 (틱). None 이면 없음          — 최적화
    sl_ticks: float = 5           # 손절 (틱, 종가 기준)               — 최적화
    max_stops_per_day: int = 3    # 그날 손절 이 횟수면 진입 중단 (week02 와 같은 횟수 방식)
    cooldown_min: int = 1         # 청산 뒤 쉬는 분
    entry_start: str = "0900"     # 08:45~09:00 은 KRX 개장 직후라 관찰만
    entry_end: str = "1520"
    flatten_at: str = "1540"      # KRX 15:45 마감 전 전량 청산


@dataclass
class Trade:
    date: str
    side: int                     # +1 MKU 매수, -1 MKU 매도
    signal_time: str
    entry_time: str
    entry_ref: float
    exit_time: str = ""
    exit_ref: float = 0.0
    reason: str = ""
    info: dict = field(default_factory=dict)

    @property
    def gross_ticks(self) -> float:
        return self.side * (self.exit_ref - self.entry_ref) / MKU_TICK


def signal(kc: list, mc: list, i: int, p: Params) -> tuple[int, dict]:
    """i 분이 끝났을 때의 신호. +1 MKU 매수, -1 MKU 매도, 0 없음."""
    j = i - p.window
    if j < 0 or None in (kc[i], kc[j], mc[i], mc[j]):
        return 0, {}
    dk = kc[i] - kc[j]
    if abs(dk) < p.move_won:
        return 0, {}
    k_dir = 1 if dk > 0 else -1                         # KRX 상승 = 원화 약세
    expected = abs(dk) / (1000 * MKU_TICK / mc[i] ** 2)  # KRX 움직임을 MKU 틱으로
    followed = -k_dir * (mc[i] - mc[j]) / MKU_TICK       # MKU 가 KRX 방향으로 간 틱
    rho = followed / expected
    if rho >= p.rho_max:
        return 0, {}
    return -k_dir, {"dk": round(dk, 2), "expected": round(expected, 2), "rho": round(rho, 2)}


def run_day(date: str, ts: list[str], kc: list, m_open: list, m_close: list, m_traded: list,
            p: Params) -> list[Trade]:
    """하루치. ts 는 분 목록, kc·m_close 는 직전 값으로 채운 종가, m_open·m_traded 는 체결 있던 분만."""
    def fill_ref(k: int) -> float:
        if k < len(ts) and m_traded[k]:
            return m_open[k]
        return m_close[min(k, len(ts)) - 1]

    trades: list[Trade] = []
    stops = 0
    pos: Trade | None = None
    pos_idx = 0
    resume = 0
    i_start, i_end, i_flat = ts.index(p.entry_start), ts.index(p.entry_end), ts.index(p.flatten_at)
    for i in range(len(ts)):
        if pos is not None:
            pnl = pos.side * (m_close[i] - pos.entry_ref) / MKU_TICK
            reason = ""
            if p.tp_ticks is not None and pnl >= p.tp_ticks:
                reason = "익절"
            elif pnl <= -p.sl_ticks:
                reason = "손절"
            elif i - pos_idx >= p.hold_min:
                reason = "시간"
            elif i >= i_flat:
                reason = "마감"
            if reason:
                pos.exit_time = ts[i + 1] if i + 1 < len(ts) else ts[i]
                pos.exit_ref = fill_ref(i + 1)
                pos.reason = reason
                trades.append(pos)
                stops += reason == "손절"
                pos, resume = None, i + 1 + p.cooldown_min
            continue
        if i < i_start or i > i_end or i < resume or stops >= p.max_stops_per_day:
            continue
        side, info = signal(kc, m_close, i, p)
        if side and i + 1 < len(ts):
            pos = Trade(date, side, ts[i], ts[i + 1], fill_ref(i + 1), info=info)
            pos_idx = i + 1
    if pos is not None:                       # 데이터가 끝났는데 남은 포지션
        pos.exit_time, pos.exit_ref, pos.reason = ts[-1], m_close[-1], "데이터끝"
        trades.append(pos)
    return trades
