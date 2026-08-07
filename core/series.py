"""시계열 파생지표 계산 (주가·거시지표·부동산지수 공용).

시세 tool들이 "지금 값" 하나만 주던 것을 시계열로 확장하면서, 값 배열에서
투자 판단에 쓰는 요약치를 만든다. 소스(야후/네이버/ECOS)와 무관하게
(레이블, 값) 쌍의 리스트만 받는다.

- 결측(휴장·미발표)은 None으로 들어오므로 계산 전 제거한다.
- 변동성은 로그수익률이 아닌 단순수익률의 표준편차 × sqrt(연간 관측수).
  periods_per_year가 None이면 변동성·MDD를 생략한다(레벨 지표용).
"""
from __future__ import annotations

import math

# 관측 주기별 연간 환산 계수(변동성 annualize용)
PERIODS_PER_YEAR = {"1d": 252, "1wk": 52, "1mo": 12, "M": 12, "Q": 4, "D": 252, "A": 1}


def _clean(points: list[dict], value_key: str, label_key: str) -> list[tuple[str, float]]:
    out = []
    for p in points:
        v = p.get(value_key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out.append((str(p.get(label_key)), float(v)))
    return out


def max_drawdown(values: list[float]) -> float | None:
    """고점 대비 최대 낙폭(%). 음수로 반환(-23.4 = 23.4% 하락)."""
    if len(values) < 2:
        return None
    peak = values[0]
    worst = 0.0
    for v in values:
        if v > peak:
            peak = v
        if peak:
            dd = (v / peak - 1) * 100
            if dd < worst:
                worst = dd
    return round(worst, 2)


def volatility_pct(values: list[float], periods_per_year: int) -> float | None:
    """단순수익률 표준편차의 연율화(%). 관측 3개 미만이면 None."""
    if len(values) < 3:
        return None
    rets = [(values[i] / values[i - 1] - 1)
            for i in range(1, len(values)) if values[i - 1]]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return round(math.sqrt(var) * math.sqrt(periods_per_year) * 100, 2)


def summarize(points: list[dict], *, value_key: str = "close",
              label_key: str = "date",
              periods_per_year: int | None = None) -> dict:
    """시계열 요약: 시작/종료값, 기간수익률, 최고/최저, MDD, 변동성.

    periods_per_year를 주면 변동성과 MDD를 함께 계산한다(가격 계열용).
    """
    clean = _clean(points, value_key, label_key)
    if not clean:
        return {"count": 0}
    labels = [c[0] for c in clean]
    values = [c[1] for c in clean]
    first, last = values[0], values[-1]
    hi_i = max(range(len(values)), key=lambda i: values[i])
    lo_i = min(range(len(values)), key=lambda i: values[i])

    stats: dict = {
        "count": len(values),
        "start": labels[0],
        "end": labels[-1],
        "start_value": round(first, 4),
        "end_value": round(last, 4),
        "change": round(last - first, 4),
        "change_pct": round((last / first - 1) * 100, 2) if first else None,
        "high": round(values[hi_i], 4),
        "high_at": labels[hi_i],
        "low": round(values[lo_i], 4),
        "low_at": labels[lo_i],
        "pct_from_high": round((last / values[hi_i] - 1) * 100, 2) if values[hi_i] else None,
    }
    if periods_per_year:
        stats["max_drawdown_pct"] = max_drawdown(values)
        stats["volatility_pct"] = volatility_pct(values, periods_per_year)
    return stats


def recent_changes(points: list[dict], lookbacks: dict[str, int], *,
                   value_key: str = "value", label_key: str = "time",
                   pct: bool = True) -> dict:
    """N관측 전 대비 변화. 거시·부동산지수처럼 '3개월 전 대비'가 중요한 계열용.

    lookbacks 예: {"3개월": 3, "6개월": 6, "12개월": 12} (월 계열 기준).
    pct=True면 변화율(%), False면 절대 변화량을 반환한다. 금리처럼 단위가 %인
    지표는 절대 변화(%p)가 변화율보다 의미 있다(2.50→2.75는 "+10%"가 아니라
    "+0.25%p"로 읽어야 한다). 관측이 모자라면 해당 항목을 생략한다.
    """
    clean = _clean(points, value_key, label_key)
    if len(clean) < 2:
        return {}
    values = [c[1] for c in clean]
    last = values[-1]
    out: dict = {}
    for label, n in lookbacks.items():
        if len(values) <= n:
            continue
        base = values[-1 - n]
        if pct:
            if base:
                out[label] = round((last / base - 1) * 100, 2)
        else:
            out[label] = round(last - base, 4)
    return out
