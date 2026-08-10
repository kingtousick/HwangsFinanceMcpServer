"""SEC XBRL 파생 지표 조립기(composer).

sources/sec.py(IO, 실패 시 raise)와 core/xbrl.py(순수 계산)를 엮어 툴이 그대로
반환할 dict를 만든다. sources/portfolio.py와 같은 위치의 계층으로,
**예외를 던지지 않고 부분성공 dict를 반환**한다:
값을 모르면 null로 두고 errors[]에 사유를 남긴다. 추정·보간은 하지 않는다.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from core import xbrl
from core.schema import FILING, err_item, now_kst_iso
from sources import sec

logger = logging.getLogger("finance-mcp")

# --- 유형자산·감가상각 태그 -------------------------------------------------
_GROSS = "PropertyPlantAndEquipmentGross"
_NET = "PropertyPlantAndEquipmentNet"
_ACCUM = ("AccumulatedDepreciationDepletionAndAmortization"
          "PropertyPlantAndEquipment")
_LIFE = "PropertyPlantAndEquipmentUsefulLife"
# 회사마다 쓰는 태그가 다르다. 우선순위대로 첫 성공을 채택하고 실제 태그를 반환한다.
_DDA_TAGS = [
    "DepreciationDepletionAndAmortization",
    "DepreciationAmortizationAndAccretionNet",
    "DepreciationAndAmortization",
    "Depreciation",
]

# --- 현금흐름·매출 태그 -----------------------------------------------------
_CAPEX_TAGS = [
    "PaymentsToAcquirePropertyPlantAndEquipment",
    "PaymentsToAcquireProductiveAssets",
]
_OCF_TAGS = [
    "NetCashProvidedByUsedInOperatingActivities",
    "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
]
_REV_TAGS = [
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "SalesRevenueNet",
]
_JOIN_TOL_DAYS = 5   # 분기 종료일이 회사·항목마다 며칠 어긋나는 것을 흡수

_CAPEX_NOTE = (
    "미국 현금흐름표는 분기 단독이 아니라 누적(YTD) 공시가 일반적이라 분기 단독값은 "
    "누적 차분으로 산출한다(capex_derived/ocf_derived=true). 4분기는 연간−3분기누적. "
    "앞 분기 누적이 없으면 값을 만들지 않는다."
)
_FUNDAMENTALS_NOTE = (
    "fy/fp는 SEC 원본 라벨로, '그 사실의 기간'이 아니라 '그 사실이 실린 보고서'의 "
    "회계연도/분기다(전년 동기 비교치도 당해 보고서 라벨로 온다). 기간 판단은 반드시 "
    "start/end를 쓸 것."
)

_LIFE_THRESHOLD = 0.3   # |변화| 0.3년 이상이면 연장/단축으로 본다
_LIFE_NOTE = (
    "gross PP&E ÷ 연환산 감가상각비(D&A) 역산 프록시. 회계 각주 원문과 다를 수 있다. "
    "분모 D&A는 현금흐름표 항목이라 무형자산 상각이 섞여 내용연수가 과소 추정될 수 있다."
)


def _skeleton(ticker: str, **extra) -> dict:
    return {
        "ticker": (ticker or "").strip().upper(),
        "cik": None,
        "entity_name": None,
        "series": [],
        "timestamp": now_kst_iso(),
        "source": "SEC EDGAR XBRL",
        "data_kind": FILING,
        "errors": [],
        **extra,
    }


async def _resolve(out: dict, ticker: str) -> str | None:
    """CIK를 채우고 반환. 실패 시 out['errors']에 남기고 None."""
    try:
        cik, name = await sec.resolve_cik(ticker)
    except sec.SecConfigError as e:
        out["errors"].append(err_item("sec_user_agent", e, "SEC"))
        return None
    except Exception as e:  # noqa: BLE001
        out["errors"].append(err_item("cik", e, "SEC"))
        return None
    out["cik"], out["entity_name"] = cik, name
    return cik


async def _facts(cik: str, tag: str) -> tuple[str | None, list[dict], Exception | None]:
    """단일 태그 조회 → (단위, 사실목록, 예외). 실패해도 예외를 올리지 않는다."""
    try:
        unit, facts = xbrl.units_of(await sec.concept(cik, tag))
        return unit, facts, None
    except Exception as e:  # noqa: BLE001
        return None, [], e


async def _facts_any(cik: str, tags: list[str]):
    """태그 후보 중 첫 성공 → (태그, 단위, 사실목록, 예외)."""
    try:
        tag, cj = await sec.concept_any(cik, tags)
        unit, facts = xbrl.units_of(cj)
        return tag, unit, facts, None
    except Exception as e:  # noqa: BLE001
        return None, None, [], e


def _direct_useful_life(facts: list[dict], unit: str | None) -> dict | None:
    """회사가 직접 태깅한 내용연수(있는 회사만). 자산군별로 여러 값이 온다.

    companyconcept는 세그먼트 축을 주지 않아 어느 자산군인지 알 수 없으므로,
    최신 시점의 값들을 그대로 나열하고 여럿이면 ambiguous로 표시한다.
    """
    rows = [(xbrl.parse_dt(f.get("end")), f.get("val")) for f in facts or []]
    rows = [(e, v) for e, v in rows if e and isinstance(v, (int, float))]
    if not rows:
        return None
    latest = max(e for e, _ in rows)
    vals = sorted({float(v) for e, v in rows if e == latest})
    return {"end": latest.isoformat(), "unit": unit, "values": vals,
            "ambiguous": len(vals) > 1}


async def implied_useful_life(ticker: str, years: int = 3) -> dict:
    """감가상각 내용연수 역산 프록시 시계열.

    implied_life = 총 유형자산(gross PP&E) ÷ 연환산 감가상각비(D&A)
    Gross 미공시 기업은 Net + 감가상각누계액으로 복원하고, 그것도 없으면
    그 연도를 건너뛴다(추정 금지).
    """
    out = _skeleton(ticker, latest_life=None, prior_life=None, delta_years=None,
                    flag="insufficient_data", dda_tag=None,
                    direct_useful_life=None, note=_LIFE_NOTE)
    cik = await _resolve(out, ticker)
    if cik is None:
        return out

    (dda_tag, _dda_unit, dda_facts, dda_err), gross, net, accum, life = \
        await asyncio.gather(
            _facts_any(cik, _DDA_TAGS),
            _facts(cik, _GROSS),
            _facts(cik, _NET),
            _facts(cik, _ACCUM),
            _facts(cik, _LIFE),
        )
    out["dda_tag"] = dda_tag
    out["direct_useful_life"] = _direct_useful_life(life[1], life[0])

    if dda_err is not None:
        out["errors"].append(err_item("dda", dda_err, "SEC"))
        return out
    if not gross[1] and not (net[1] and accum[1]):
        out["errors"].append(err_item(
            "gross_ppe",
            f"{_GROSS} 미공시이고 {_NET}+감가상각누계액으로도 복원할 수 없음",
            "SEC"))
        return out

    dda_rows = xbrl.dedupe(dda_facts)
    fys = xbrl.fy_intervals(dda_rows, extrapolate=False)
    if not fys:
        out["errors"].append(err_item("fiscal_year", "연간 D&A 공시가 없어 "
                                      "회계연도 구간을 확정할 수 없음", "SEC"))
        return out

    for fy_start, fy_end in fys[-max(1, years):]:
        g = xbrl.pick_instant(gross[1], fy_end)
        gross_val = g["val"] if g else None
        gross_source = "reported"
        if gross_val is None:
            n = xbrl.pick_instant(net[1], fy_end)
            a = xbrl.pick_instant(accum[1], fy_end)
            if n and a:
                # 감가상각누계액은 관례상 양수로 태깅되지만 음수 태깅 filer 방어.
                gross_val = n["val"] + abs(a["val"])
                gross_source = "restored(net+accum)"
        if gross_val is None:
            continue

        dda_ann, basis = xbrl.ttm(dda_facts, at=fy_end)
        if not dda_ann:
            continue

        out["series"].append({
            "fy": xbrl.fy_label(fy_end),
            "fp": "FY",
            "end": fy_end.isoformat(),
            "gross_ppe": gross_val,
            "gross_source": gross_source,
            "dda_annualized": dda_ann,
            "dda_basis": basis,
            "implied_life_years": round(gross_val / dda_ann, 2),
            "filed": (g or {}).get("filed").isoformat() if g and g.get("filed") else None,
            "restated": bool((g or {}).get("restated")),
        })

    if len(out["series"]) >= 2:
        out["latest_life"] = out["series"][-1]["implied_life_years"]
        out["prior_life"] = out["series"][-2]["implied_life_years"]
        delta = round(out["latest_life"] - out["prior_life"], 2)
        out["delta_years"] = delta
        out["flag"] = ("extended" if delta >= _LIFE_THRESHOLD
                       else "shortened" if delta <= -_LIFE_THRESHOLD
                       else "stable")
    else:
        if out["series"]:
            out["latest_life"] = out["series"][-1]["implied_life_years"]
        out["errors"].append(err_item(
            "delta_years",
            f"비교 가능한 회계연도가 {len(out['series'])}개뿐이라 변화를 판단할 수 없음",
            "SEC"))
    return out


# ------------------------------------------------------------- CAPEX 시계열

def _nearest(rows: list[dict], target, tol: int = _JOIN_TOL_DAYS) -> dict | None:
    """end가 target에 가장 가까운 행(허용 오차 안). 없으면 None."""
    best, gap = None, None
    for r in rows:
        e = xbrl.parse_dt(r["end"])
        if e is None:
            continue
        g = abs((e - target).days)
        if g <= tol and (gap is None or g < gap):
            best, gap = r, g
    return best


async def capex_series(ticker: str, quarters: int = 8) -> dict:
    """분기별 CAPEX 실제 집행액·영업현금흐름·FCF·매출대비 비중.

    가이던스가 아니라 현금흐름표에 찍힌 실제 집행액이다.
    """
    out = _skeleton(ticker, yoy_capex_pct=None, ttm_capex=None, ttm_basis=None,
                    tags={}, note=_CAPEX_NOTE)
    cik = await _resolve(out, ticker)
    if cik is None:
        return out

    capex, ocf, rev = await asyncio.gather(
        _facts_any(cik, _CAPEX_TAGS),
        _facts_any(cik, _OCF_TAGS),
        _facts_any(cik, _REV_TAGS),
    )
    out["tags"] = {"capex": capex[0], "ocf": ocf[0], "revenue": rev[0]}
    for label, res in (("capex", capex), ("ocf", ocf), ("revenue", rev)):
        if res[3] is not None:
            out["errors"].append(err_item(label, res[3], "SEC"))
    if capex[3] is not None:
        return out   # capex 없이는 이 툴의 의미가 없다

    capex_q = xbrl.quarterize(capex[2])
    ocf_q = xbrl.quarterize(ocf[2])
    rev_q = xbrl.quarterize(rev[2])

    rows = []
    for c in capex_q:
        end = xbrl.parse_dt(c["end"])
        o = _nearest(ocf_q, end)
        r = _nearest(rev_q, end)
        # 태그 표준 부호는 현금유출(양수)이지만 음수로 태깅하는 filer가 있다.
        capex_val = abs(c["val"])
        ocf_val = o["val"] if o else None
        rev_val = r["val"] if r else None
        rows.append({
            "fy": c["fy"], "fp": c["fp"], "start": c["start"], "end": c["end"],
            "capex": capex_val,
            "ocf": ocf_val,
            "fcf": (ocf_val - capex_val) if ocf_val is not None else None,
            "revenue": rev_val,
            "capex_to_revenue_pct": (round(capex_val / rev_val * 100, 2)
                                     if rev_val else None),
            "capex_derived": c["derived"],
            "ocf_derived": o["derived"] if o else None,
            "revenue_derived": r["derived"] if r else None,
            "filed": c["filed"],
            "restated": c["restated"],
        })

    out["series"] = rows[-max(1, quarters):]
    if out["series"]:
        latest = out["series"][-1]
        prior = next((x for x in rows
                      if x["fy"] == latest["fy"] - 1 and x["fp"] == latest["fp"]), None)
        if prior and prior["capex"]:
            out["yoy_capex_pct"] = round(
                (latest["capex"] / prior["capex"] - 1) * 100, 2)
        else:
            out["errors"].append(err_item(
                "yoy_capex_pct", "전년 동일 분기 값이 없어 YoY를 계산할 수 없음", "SEC"))
    ttm_val, basis = xbrl.ttm(capex[2])
    if ttm_val is not None:
        out["ttm_capex"], out["ttm_basis"] = abs(ttm_val), basis
    else:
        out["errors"].append(err_item("ttm_capex", "TTM 산출에 필요한 누적/분기 "
                                      "공시가 부족함", "SEC"))
    return out


# ------------------------------------------------------- 원자료(태그 직접 조회)

def _raw_series(facts: list[dict], years: int) -> list[dict]:
    """dedupe한 사실을 최근 years 회계연도치만 남겨 반환. SEC 원본 라벨 유지."""
    rows = xbrl.dedupe(facts)
    if not rows:
        return []
    latest = max(r["end"] for r in rows)
    cutoff = latest - timedelta(days=366 * max(1, years))
    return [{
        "fy": r.get("fy"), "fp": r.get("fp"), "form": r.get("form"),
        "start": r["start"].isoformat() if r.get("start") else None,
        "end": r["end"].isoformat(),
        "val": r["val"],
        "filed": r["filed"].isoformat() if r.get("filed") else None,
        "accn": r.get("accn"),
        "restated": r["restated"],
    } for r in rows if r["end"] >= cutoff]


async def fundamentals(ticker: str, concepts: list[str], years: int = 3) -> dict:
    """지정한 us-gaap 태그들의 보고 시계열 원자료."""
    tags = [str(c).strip() for c in (concepts or []) if str(c).strip()]
    out = _skeleton(ticker, results=[], note=_FUNDAMENTALS_NOTE)
    out.pop("series", None)
    if not tags:
        out["errors"].append(err_item("concepts", "조회할 us-gaap 태그를 "
                                      "하나 이상 지정하세요", "input"))
        return out
    cik = await _resolve(out, ticker)
    if cik is None:
        return out

    fetched = await asyncio.gather(*[_facts(cik, t) for t in tags])
    for tag, (unit, facts, err) in zip(tags, fetched):
        if err is not None:
            out["errors"].append(err_item(tag, err, "SEC"))
            out["results"].append({"concept": tag, "unit": None, "series": []})
            continue
        out["results"].append({"concept": tag, "unit": unit,
                               "series": _raw_series(facts, years)})

    # 단일 태그 조회는 최상위에도 펼쳐 준다(호출부 편의).
    if len(out["results"]) == 1:
        r = out["results"][0]
        out["concept"], out["unit"], out["series"] = r["concept"], r["unit"], r["series"]
    return out


# ------------------------------------------------------------- RPO 수주잔고

_RPO = "RevenueRemainingPerformanceObligation"
_RPO_PCT = "RevenueRemainingPerformanceObligationPercentage"
_RPO_NOTE = (
    "RPO는 세그먼트 축(axis)이 붙은 다중 사실로 공시되는 일이 잦은데 "
    "companyconcept는 축 정보를 주지 않는다. 같은 시점에 값이 여러 개면 합산하거나 "
    "최대값을 고르지 않고 filed 최신 1건만 채택하며 ambiguous=true로 표시한다."
)


def _instant_series(facts: list[dict], limit: int) -> list[dict]:
    """instant 사실 시계열(최근 limit개). 같은 시점 다중 사실은 ambiguous 표시."""
    rows = xbrl.dedupe(facts, instant=True)
    counts: dict = {}
    for f in facts or []:
        e = xbrl.parse_dt(f.get("end"))
        if e is not None and isinstance(f.get("val"), (int, float)):
            counts.setdefault(e, set()).add(round(float(f["val"]), 6))
    return [{
        "end": r["end"].isoformat(),
        "val": r["val"],
        "form": r.get("form"),
        "filed": r["filed"].isoformat() if r.get("filed") else None,
        "restated": r["restated"],
        "ambiguous": len(counts.get(r["end"], ())) > 1,
    } for r in rows[-max(1, limit):]]


async def rpo_backlog(ticker: str, quarters: int = 8) -> dict:
    """잔여 이행의무(RPO) 잔고 시계열. 미공시 기업은 disclosed=false."""
    out = _skeleton(ticker, disclosed=False, unit=None, percentage=None,
                    qoq_pct=None, yoy_pct=None, note=_RPO_NOTE)
    cik = await _resolve(out, ticker)
    if cik is None:
        return out

    (unit, facts, err), (pct_unit, pct_facts, _pct_err) = await asyncio.gather(
        _facts(cik, _RPO), _facts(cik, _RPO_PCT))
    if err is not None:
        out["errors"].append(err_item(
            "rpo", f"us-gaap:{_RPO} 미공시 기업입니다 ({err})", "SEC"))
        return out

    out["disclosed"] = True
    out["unit"] = unit
    out["series"] = _instant_series(facts, quarters)
    if pct_facts:
        out["percentage"] = _instant_series(pct_facts, quarters)

    vals = [s["val"] for s in out["series"]]
    if len(vals) >= 2 and vals[-2]:
        out["qoq_pct"] = round((vals[-1] / vals[-2] - 1) * 100, 2)
    if len(vals) >= 5 and vals[-5]:
        out["yoy_pct"] = round((vals[-1] / vals[-5] - 1) * 100, 2)
    return out
