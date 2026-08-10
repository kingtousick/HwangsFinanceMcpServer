"""SEC XBRL 사실(fact) 목록을 다루는 순수 함수 모음. 네트워크 접근 없음.

companyconcept API 응답 스키마(실측):
  {"cik":320193, "taxonomy":"us-gaap", "tag":"Revenues", "units":{"USD":[
     {"start":"2024-01-01","end":"2024-03-31","val":90753000000,
      "accn":"0000320193-24-000081","fy":2024,"fp":"Q2","form":"10-Q",
      "filed":"2024-05-03","frame":"CY2024Q1"}, ...]}}
instant 개념(재무상태표 항목)은 start가 없고 end만 있다.

★ 가장 중요한 함정: fact의 fy/fp는 **그 사실의 기간이 아니라 그 사실이 실린
  보고서의 회계연도/분기**다. 2025년 3분기 10-Q에 실린 전년 동기 비교치도
  fy=2025, fp=Q3로 온다. (fy, fp)로 그룹핑하면 값이 섞여 틀린 수치가 나오므로
  이 모듈은 전부 start/end 날짜를 기준으로 판단한다.

★ 두 번째 함정: 미국 현금흐름표는 분기 단독이 아니라 **누적(YTD)** 공시가
  일반적이다. 분기 단독값은 누적 차분으로 만들고 derived=True로 표시한다.
  앞 분기 누적이 없으면 값을 만들지 않는다(추정 금지).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

_DAY = timedelta(days=1)

# 회계연도로 인정할 기간 길이(일). 52주=364, 53주=371, 달력연도=365/366.
FY_MIN_DAYS, FY_MAX_DAYS = 350, 380
# 3개월 단독 공시로 인정할 기간 길이(일).
Q_MIN_DAYS, Q_MAX_DAYS = 75, 105
# 회계연도 시작일과 같다고 볼 오차(일). 52/53주 결산의 미세 편차를 흡수한다.
_START_TOL = 3
# 회계연도 구간이 실질적으로 겹친다고 볼 기준(일).
_FY_OVERLAP_TOL = 15


def parse_dt(s) -> date | None:
    """'YYYY-MM-DD' → date. 파싱 불가 시 None."""
    if isinstance(s, date):
        return s
    if not s:
        return None
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def span_days(start: date, end: date) -> int:
    """XBRL 기간 길이(일). start/end 모두 포함이므로 +1."""
    return (end - start).days + 1


def annualize(val: float, days: int) -> float | None:
    """days일치 값을 1년치로 환산."""
    if val is None or not days:
        return None
    return val * 365.0 / days


def units_of(concept_json: dict) -> tuple[str | None, list[dict]]:
    """companyconcept JSON에서 (단위명, 사실 목록). 단위가 여럿이면 사실이 가장 많은 것."""
    units = (concept_json or {}).get("units") or {}
    if not units:
        return None, []
    unit = max(units, key=lambda k: len(units[k] or []))
    return unit, list(units[unit] or [])


def _norm(f: dict, *, instant: bool) -> dict | None:
    end = parse_dt(f.get("end"))
    if end is None:
        return None
    start = None if instant else parse_dt(f.get("start"))
    if not instant and start is None:
        return None
    val = f.get("val")
    if not isinstance(val, (int, float)):
        return None
    return {
        "start": start,
        "end": end,
        "val": float(val),
        "fy": f.get("fy"),
        "fp": f.get("fp"),
        "form": f.get("form"),
        "filed": parse_dt(f.get("filed")),
        "accn": f.get("accn"),
    }


def dedupe(facts: list[dict], *, instant: bool = False) -> list[dict]:
    """같은 기간의 중복 사실을 filed 최신본으로 정리하고 restated를 표시한다.

    같은 기간이 여러 제출본에 반복 등장하는 것은 정상이다(비교표시). 값까지
    달라졌거나 채택본의 form이 '/A'(수정신고)로 끝날 때만 restated=True.
    """
    groups: dict[tuple, list[dict]] = {}
    for f in facts or []:
        r = _norm(f, instant=instant)
        if r is None:
            continue
        groups.setdefault((r["start"], r["end"]), []).append(r)

    out: list[dict] = []
    for key in sorted(groups, key=lambda k: (k[1], k[0] or date.min)):
        members = groups[key]
        members.sort(key=lambda m: (m["filed"] or date.min, m["accn"] or ""))
        chosen = dict(members[-1])
        values = {round(m["val"], 6) for m in members}
        chosen["restated"] = (
            len(values) > 1 or str(chosen.get("form") or "").endswith("/A")
        )
        out.append(chosen)
    return out


def fy_intervals(rows: list[dict], *, extrapolate: bool = True) -> list[tuple[date, date]]:
    """회계연도 구간 목록. 연간 사실(350~380일)의 (start, end)에서 뽑는다.

    실질적으로 겹치는 후보는 더 늦게 끝나는 쪽으로 합친다(52/53주 결산 오차).
    extrapolate=True면 진행 중인 회계연도(아직 10-K가 없어 연간 사실이 없는
    구간)를 직전 회계연도 길이로 연장해 만든다.
    """
    cands = sorted({
        (r["start"], r["end"]) for r in rows
        if r.get("start") and FY_MIN_DAYS <= span_days(r["start"], r["end"]) <= FY_MAX_DAYS
    })
    fys: list[tuple[date, date]] = []
    for s, e in cands:
        if fys and s <= fys[-1][1] - timedelta(days=_FY_OVERLAP_TOL):
            if e > fys[-1][1]:
                fys[-1] = (s, e)
            continue
        fys.append((s, e))

    if extrapolate and fys and rows:
        max_end = max(r["end"] for r in rows)
        for _ in range(4):  # 폭주 방지 상한
            prev_s, prev_e = fys[-1]
            if max_end <= prev_e + timedelta(days=30):
                break
            length = (prev_e - prev_s).days
            start = prev_e + _DAY
            fys.append((start, start + timedelta(days=length)))
    return fys


def fy_label(fy_end: date) -> int:
    """회계연도 라벨 = 종료일의 연도.

    NVDA(2025-01-26 종료)→2025, MSFT(2025-06-30)→2025, AAPL(2024-09-28)→2024로
    회사 자체 표기와 일치한다. 다만 12월 말 기준 52/53주 결산사가 1월 초에
    끝나는 경우는 직전 연도가 맞으므로 보정한다.
    """
    if fy_end.month == 1 and fy_end.day <= 5:
        return fy_end.year - 1
    return fy_end.year


def _quarter_index(end: date, fy_start: date, fy_len: float) -> int:
    """분기 번호를 '몇 번째 사실인지'가 아니라 '기간 길이'로 계산한다(결측 분기 내성)."""
    n = round(((end - fy_start).days + 1) / (fy_len / 4.0))
    return min(4, max(1, int(n)))


def fy_buckets(rows: list[dict]) -> list[dict]:
    """회계연도별로 누적(YTD) 사실과 3개월 단독 사실을 분기 번호에 배정한다.

    반환 원소: {fy, start, end, cum: {qi: row}, solo: {qi: row}}
      cum  — start가 회계연도 시작일과 같은 사실(=YTD 누적). qi=4는 연간값.
      solo — 75~105일짜리로 회계연도 중간에서 시작하는 사실(=3개월 단독 공시).
    """
    fys = fy_intervals(rows)
    buckets: list[dict] = []
    for fs, fe in fys:
        fy_len = float(span_days(fs, fe))
        cum: dict[int, dict] = {}
        solo: dict[int, dict] = {}
        for r in rows:
            if r.get("start") is None:
                continue
            if r["start"] < fs - timedelta(days=_START_TOL):
                continue
            if r["end"] > fe + timedelta(days=_START_TOL):
                continue
            qi = _quarter_index(r["end"], fs, fy_len)
            if abs((r["start"] - fs).days) <= _START_TOL:
                cum[qi] = r
            elif Q_MIN_DAYS <= span_days(r["start"], r["end"]) <= Q_MAX_DAYS:
                solo[qi] = r
        buckets.append({"fy": fy_label(fe), "start": fs, "end": fe,
                        "cum": cum, "solo": solo})
    return buckets


def _emit(r: dict, fy: int, qi: int, *, derived: bool) -> dict:
    return {
        "fy": fy, "fp": f"Q{qi}",
        "start": r["start"].isoformat() if r.get("start") else None,
        "end": r["end"].isoformat(),
        "val": r["val"],
        "form": r.get("form"),
        "filed": r["filed"].isoformat() if r.get("filed") else None,
        "accn": r.get("accn"),
        "restated": bool(r.get("restated")),
        "derived": derived,
    }


def quarterize(facts: list[dict]) -> list[dict]:
    """duration 개념(현금흐름·손익) 사실 → 분기 단독값 시계열(기간 오름차순).

    우선순위: 3개월 단독 공시 > Q1 누적(=Q1 단독) > 앞 분기 누적과의 차분.
    셋 다 불가하면 그 분기는 만들지 않는다(추정 금지).
    derived=True면 누적 차분으로 산출한 값이라는 뜻이다.
    """
    rows = dedupe(facts)
    out: list[dict] = []
    for b in fy_buckets(rows):
        cum, solo = b["cum"], b["solo"]
        for q in (1, 2, 3, 4):
            if q in solo:
                out.append(_emit(solo[q], b["fy"], q, derived=False))
            elif q == 1 and 1 in cum:
                # 1분기 누적은 곧 1분기 단독값이다.
                out.append(_emit(cum[1], b["fy"], 1, derived=False))
            elif q in cum and (q - 1) in cum:
                a, c = cum[q - 1], cum[q]
                out.append(_emit({
                    "start": a["end"] + _DAY,
                    "end": c["end"],
                    "val": c["val"] - a["val"],
                    "form": c.get("form"),
                    "filed": max(x for x in (a.get("filed"), c.get("filed")) if x)
                             if (a.get("filed") or c.get("filed")) else None,
                    "accn": c.get("accn"),
                    "restated": bool(a.get("restated") or c.get("restated")),
                }, b["fy"], q, derived=True))
            # else: 앞 분기 누적이 없다 → 값을 만들지 않는다
    out.sort(key=lambda r: r["end"])
    return out


def cumulative(facts: list[dict]) -> list[dict]:
    """회계연도 누적(YTD) 사실만 분기 번호와 함께 반환. TTM 계산용."""
    rows = dedupe(facts)
    out = []
    for b in fy_buckets(rows):
        for qi, r in sorted(b["cum"].items()):
            out.append(_emit(r, b["fy"], qi, derived=False))
    out.sort(key=lambda r: r["end"])
    return out


def ttm(facts: list[dict], *, at: date | None = None,
        tol_days: int = 5) -> tuple[float | None, str | None]:
    """최근 12개월 값과 그 산출 근거(basis).

    basis:
      'fy'            — at이 회계연도 종료일이라 연간 공시값을 그대로 사용
      'ttm'           — 직전 연간 + 당해 누적 − 전년 동기 누적
      'quarters_sum'  — 분기 단독값 4개 합(위 둘이 불가할 때)
    산출 불가 시 (None, None). 추정·보간은 하지 않는다.
    """
    rows = dedupe(facts)
    if not rows:
        return None, None
    if at is None:
        at = max(r["end"] for r in rows)

    buckets = fy_buckets(rows)
    tol = timedelta(days=tol_days)

    # 1) at이 어떤 회계연도의 종료일이면 그 해 연간값이 곧 TTM이다.
    for b in buckets:
        if abs((b["end"] - at).days) <= tol_days and 4 in b["cum"]:
            return b["cum"][4]["val"], "fy"

    # 2) 진행 중 회계연도: 직전 연간 + 당해 누적 − 전년 동일 분기 누적
    for i, b in enumerate(buckets):
        if not (b["start"] - tol <= at <= b["end"] + tol) or i == 0:
            continue
        prev = buckets[i - 1]
        cur_qi = next((q for q, r in b["cum"].items()
                       if abs((r["end"] - at).days) <= tol_days), None)
        if cur_qi is None or 4 not in prev["cum"] or cur_qi not in prev["cum"]:
            break
        return (prev["cum"][4]["val"] - prev["cum"][cur_qi]["val"]
                + b["cum"][cur_qi]["val"]), "ttm"

    # 3) 분기 단독값 4개 합 — 실제로 연속 12개월을 덮을 때만.
    qs = [r for r in quarterize(facts) if parse_dt(r["end"]) <= at]
    if len(qs) >= 4:
        last4 = qs[-4:]
        s, e = parse_dt(last4[0]["start"]), parse_dt(last4[-1]["end"])
        if s and e and FY_MIN_DAYS <= span_days(s, e) <= FY_MAX_DAYS:
            return sum(r["val"] for r in last4), "quarters_sum"
    return None, None


def pick_instant(facts: list[dict], end: date,
                 tol_days: int = 5) -> dict | None:
    """instant 개념(재무상태표 항목)에서 지정 시점에 가장 가까운 사실.

    tol_days를 벗어나면 None(추정하지 않는다).
    """
    rows = dedupe(facts, instant=True)
    best, best_gap = None, None
    for r in rows:
        gap = abs((r["end"] - end).days)
        if gap <= tol_days and (best_gap is None or gap < best_gap):
            best, best_gap = r, gap
    return best
