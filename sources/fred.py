"""FRED(세인트루이스 연은) CSV 시계열 — 신용스프레드·금리. 인증키 불필요.

  GET https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10&cosd=2021-08-10
  observation_date,DGS10
  2021-08-10,1.34
  2021-08-11,.          ← 휴장일 결측은 마침표 한 글자

헤더 첫 열 이름은 버전에 따라 DATE / observation_date로 달라지므로 이름이 아니라
위치로 파싱한다.

주의: FRED 앞단 Akamai는 **브라우저 User-Agent를 붙잡아 둔다**(응답 없이 타임아웃).
core/http.py의 기본 UA는 국내 비공식 엔드포인트용 Chrome UA라 여기서는 반드시
덮어써야 한다. _HEADERS 주석 참고.

절대 레벨만으로는 판단이 안 되므로 1년·5년 백분위를 함께 낸다. 백분위 계산에
5년치가 필요해 요청 기간과 무관하게 항상 5년을 받아오고, period는 반환할
points를 자르는 데만 쓴다.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta

from core import http
from core.schema import PREV_CLOSE, err_item, now_kst_iso, to_float

logger = logging.getLogger("finance-mcp")

_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"

# ★ FRED 앞단의 Akamai는 브라우저 User-Agent로 오는 요청을 응답 없이 붙잡아 둔다
#   (실측 2026-08-10: core/http.py 기본 Chrome UA → 100% ReadTimeout,
#    Accept/Accept-Language를 붙여도 동일. curl/python-httpx/임의 도구명 UA → 0.2초에 200).
#   국내 비공식 엔드포인트와 정반대라 여기서만 UA를 덮어쓴다.
_HEADERS = {"User-Agent": "finance-mcp (python-httpx)",
            "Accept": "text/csv,text/plain,*/*"}

DEFAULT_IDS = ["BAMLC0A0CM", "BAMLH0A0HYM2", "BAMLC0A4CBBB", "DGS10", "T10Y2Y"]

# id → (한글 이름, 단위)
META = {
    "BAMLC0A0CM": ("미국 투자등급(IG) 회사채 OAS", "%p"),
    "BAMLH0A0HYM2": ("미국 하이일드(HY) 회사채 OAS", "%p"),
    "BAMLC0A4CBBB": ("미국 BBB 등급 회사채 OAS", "%p"),
    "DGS10": ("미국 국채 10년 금리", "%"),
    "T10Y2Y": ("미국 국채 10년-2년 스프레드", "%p"),
}

PERIOD_DAYS = {"1mo": 31, "3mo": 92, "6mo": 183, "ytd": 366,
               "1y": 366, "2y": 731, "5y": 1827}
_HISTORY_DAYS = 1827          # 5년 — percentile_5y 계산용
_MAX_POINTS = 120             # MCP 응답 폭발 방지(주간·월간으로 축약)


def parse_csv(text: str) -> list[dict]:
    """fredgraph.csv → [{date, value}]. 결측('.'/''/'NA') 행은 제거한다."""
    out: list[dict] = []
    for i, line in enumerate((text or "").splitlines()):
        line = line.strip()
        if not line or i == 0:      # 헤더 1줄 스킵
            continue
        parts = line.split(",")
        if len(parts) < 2:
            continue
        d, raw = parts[0].strip(), parts[1].strip()
        if raw in (".", "", "NA", "NaN"):
            continue
        v = to_float(raw)
        if v is None or len(d) < 10:
            continue
        out.append({"date": d[:10], "value": v})
    out.sort(key=lambda p: p["date"])
    return out


def _downsample(points: list[dict]) -> tuple[list[dict], str]:
    """관측이 많으면 주간(ISO주 마지막)→월간 순으로 축약한다."""
    if len(points) <= _MAX_POINTS:
        return points, "daily"
    reduced = points
    for keyfn, label in ((lambda p: _iso_week(p["date"]), "weekly"),
                         (lambda p: p["date"][:7], "monthly")):
        buckets: dict = {}
        for p in points:
            buckets[keyfn(p)] = p      # 같은 버킷의 마지막 관측이 남는다
        reduced = sorted(buckets.values(), key=lambda p: p["date"])
        if len(reduced) <= _MAX_POINTS:
            return reduced, label
    return reduced, "monthly"


def _iso_week(d: str) -> str:
    y, m, dd = (int(x) for x in d.split("-"))
    iso = date(y, m, dd).isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _change_since(points: list[dict], days: int) -> float | None:
    """days일 전 이하의 마지막 관측 대비 절대차. 관측 개수가 아니라 날짜 기준."""
    if len(points) < 2:
        return None
    latest = points[-1]
    cutoff = (date.fromisoformat(latest["date"]) - timedelta(days=days)).isoformat()
    base = None
    for p in points:
        if p["date"] <= cutoff:
            base = p
        else:
            break
    if base is None:
        return None
    return round(latest["value"] - base["value"], 4)


def _percentile(points: list[dict], days: int, latest: float) -> float | None:
    """최근 days일 관측 중 latest 이하의 비율(%). 절대 레벨 해석의 기준."""
    if not points:
        return None
    cutoff = (date.fromisoformat(points[-1]["date"]) - timedelta(days=days)).isoformat()
    window = [p["value"] for p in points if p["date"] >= cutoff]
    if len(window) < 2:
        return None
    return round(sum(1 for v in window if v <= latest) / len(window) * 100, 1)


async def series(fred_id: str, period: str = "1y", today: date | None = None) -> dict:
    """단일 FRED 시리즈. 실패 시 raise(상위에서 행 단위 errors로 흡수)."""
    sid = (fred_id or "").strip().upper()
    start = (today or date.today()) - timedelta(days=_HISTORY_DAYS)
    text = await http.get_text(_URL, params={"id": sid, "cosd": start.isoformat()},
                               headers=_HEADERS, retries=2, backoff=0.5,
                               timeout=10.0)
    if text.lstrip().startswith("<"):
        raise RuntimeError(f"FRED 응답이 CSV가 아님(시리즈 ID 확인 필요): {sid}")
    full = parse_csv(text)
    if not full:
        raise RuntimeError(f"FRED 관측치 없음: {sid}")

    latest = full[-1]
    name, unit = META.get(sid, (sid, None))
    window_days = PERIOD_DAYS.get(period, PERIOD_DAYS["1y"])
    cutoff = (date.fromisoformat(latest["date"])
              - timedelta(days=window_days)).isoformat()
    points, interval = _downsample([p for p in full if p["date"] >= cutoff])

    return {
        "id": sid,
        "name": name,
        "unit": unit,
        "latest": latest["value"],
        "latest_date": latest["date"],
        "change_1m": _change_since(full, 30),
        "change_3m": _change_since(full, 91),
        "percentile_1y": _percentile(full, 366, latest["value"]),
        "percentile_5y": _percentile(full, _HISTORY_DAYS, latest["value"]),
        "interval": interval,
        "count": len(points),
        "points": points,
    }


async def spreads(ids: list[str] | None = None, period: str = "1y") -> dict:
    """여러 시리즈를 병렬 조회. 개별 실패는 그 행의 error로만 남긴다(부분 성공)."""
    wanted = [str(i).strip().upper() for i in (ids or DEFAULT_IDS) if str(i).strip()]
    results = await asyncio.gather(*[series(i, period) for i in wanted],
                                   return_exceptions=True)
    rows, errors = [], []
    for sid, res in zip(wanted, results):
        if isinstance(res, Exception):
            name, unit = META.get(sid, (sid, None))
            item = err_item(sid, res, "FRED")
            rows.append({"id": sid, "name": name, "unit": unit, "latest": None,
                         "points": [], "error": item["reason"]})
            errors.append(item)
        else:
            rows.append(res)

    as_of = max((r.get("latest_date") for r in rows if r.get("latest_date")),
                default=None)
    return {
        "as_of": as_of,
        "period": period,
        "count": len(rows),
        "series": rows,
        "note": ("절대 레벨만으로는 판단할 수 없다. percentile_1y/5y와 함께 읽을 것. "
                 "OAS 확대는 위험선호 후퇴 신호로 AI capex 사이클에 선행한다."),
        "timestamp": now_kst_iso(),
        "source": "FRED",
        "data_kind": PREV_CLOSE,
        "errors": errors,
    }
