"""SEC XBRL 파생 지표 조립기(composer).

sources/sec.py(IO, 실패 시 raise)와 core/xbrl.py(순수 계산)를 엮어 툴이 그대로
반환할 dict를 만든다. sources/portfolio.py와 같은 위치의 계층으로,
**예외를 던지지 않고 부분성공 dict를 반환**한다:
값을 모르면 null로 두고 errors[]에 사유를 남긴다. 추정·보간은 하지 않는다.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta

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

    # 단일 태그도 최상위에 펼치지 않는다. 예전에는 호출부 편의로 series를 최상위에
    # 중복 수록했는데, 같은 시계열이 두 번 실려 응답이 정확히 2배가 됐다(실측:
    # ADBE Revenues 단독 7,680자 = 2태그 요청과 동일). 항상 results[]에서 읽는다.
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


# ---------------------------------------------- 연간 핵심지표(논리명 → 태그 폴백)

# 같은 항목이라도 회사마다 us-gaap 태그가 다르다. 호출부가 태그를 찍어 보내면
# 태그가 어긋난 회사에서 통째로 빈손이 되므로, 여기서 논리명으로 받아 우선순위
# 폴백까지 서버가 처리한다. 실제 채택된 태그는 tags{}로 되돌려 준다.
_DUR_ITEMS: dict[str, list[str]] = {
    "revenue": _REV_TAGS,
    "operating_income": ["OperatingIncomeLoss"],
    "net_income": ["NetIncomeLoss", "ProfitLoss"],
    "dda": _DDA_TAGS,
    "ocf": _OCF_TAGS,
    "capex": _CAPEX_TAGS,
    "dividends": ["PaymentsOfDividendsCommonStock",
                  "PaymentsOfDividends",
                  "PaymentsOfDividendsCommonStockIncludingSpinoff"],
    "buybacks": ["PaymentsForRepurchaseOfCommonStock",
                 "PaymentsForRepurchaseOfEquity"],
    # 희석주식수는 duration(기간 가중평균)이다. 시점 주식수는 아래 instant 쪽.
    "shares_diluted": ["WeightedAverageNumberOfDilutedSharesOutstanding",
                       "WeightedAverageNumberOfSharesOutstandingBasic",
                       "WeightedAverageNumberOfShareOutstandingBasicAndDiluted"],
    # ADBE는 ...SoftwareExcludingAcquiredInProcessCost를 쓴다(실측). R&D를 아예
    # 공시하지 않는 업종(소매 등)도 있으므로 없는 것 자체가 정상일 수 있다.
    "rnd": ["ResearchAndDevelopmentExpense",
            "ResearchAndDevelopmentExpenseSoftwareExcludingAcquiredInProcessCost",
            "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost"],
    "pretax_income": [
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItems"
        "NoncontrollingInterest",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAnd"
        "IncomeLossFromEquityMethodInvestments",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesDomestic"],
    "income_tax": ["IncomeTaxExpenseBenefit"],
}
_INST_ITEMS: dict[str, list[str]] = {
    "assets": ["Assets"],
    "liabilities": ["Liabilities"],
    "equity": ["StockholdersEquity",
               "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    "cash": ["CashAndCashEquivalentsAtCarryingValue",
             "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"],
    "debt_lt": ["LongTermDebtNoncurrent", "LongTermDebt"],
    "debt_st": ["LongTermDebtCurrent", "DebtCurrent", "ShortTermBorrowings"],
    # CommonStockSharesOutstanding은 instant다(start가 없다). duration 쪽에 두면
    # 연간 사실 판정에서 영원히 걸러져 죽은 후보가 된다 — 실측으로 확인.
    "shares_outstanding": ["CommonStockSharesOutstanding", "CommonStockSharesIssued"],
}
# 재무상태표 시점을 회계연도 종료일에 맞출 때 허용할 오차(일). 52/53주 결산 편차.
_INSTANT_TOL = 8

_ANNUAL_NOTE = (
    "연간값은 기간 길이 350~380일인 사실만 채택한다(form 라벨이 아니라 실제 기간 "
    "기준). 6개월 누적·3개월 단독이 같은 배열에 섞여 오는 문제를 서버에서 걸러낸 "
    "결과이므로 그대로 쓰면 된다. dividends가 null이고 dividend_status가 "
    "'not_tagged'면 배당 태그 후보가 전부 미공시라는 뜻으로, 무배당 기업일 "
    "가능성이 높다(그 경우 retained는 배당 0으로 계산하고 "
    "retained_assumes_no_dividend=true로 표시한다). "
    "★ shares_diluted/shares_outstanding은 unit이 아니라 **주식 수**다(금액 아님). "
    "shares_diluted는 기간 가중평균, shares_outstanding은 회계연도 종료 시점 값. "
    "roic_pct는 영업이익x(1-실효세율)/(net_debt+equity)이며 투하자본이 0 이하면 "
    "만들지 않는다. 추정·보간은 하지 않는다."
)


def _annual_duration(facts: list[dict]) -> dict[int, dict]:
    """duration 사실 → {회계연도: 연간 사실}. 연간 = 기간 350~380일.

    같은 회계연도가 여러 제출본에 나오면 filed 최신본을 채택한다. 기간 길이로
    거르므로 6개월 누적·3개월 단독이 섞여 들어와도 배제된다.
    """
    out: dict[int, dict] = {}
    for r in xbrl.dedupe(facts):
        start = r.get("start")
        if start is None:
            continue
        if not (xbrl.FY_MIN_DAYS <= xbrl.span_days(start, r["end"])
                <= xbrl.FY_MAX_DAYS):
            continue
        fy = xbrl.fy_label(r["end"])
        prev = out.get(fy)
        if prev is None or (r.get("filed") or date.min) >= (prev.get("filed") or date.min):
            out[fy] = r
    return out


async def _facts_all(cik: str, tags: list[str]) -> list[tuple]:
    """후보 태그를 **전부** 조회 → [(태그, 단위, 사실, 예외)] (우선순위 순).

    첫 성공에서 멈추지 않는 이유: 태그가 404는 아닌데 옛 기간만 남아 있는 경우가
    있다. 실측 예 — AAPL의 PaymentsOfDividendsCommonStock은 FY2016~2017 2개뿐이고
    실제 배당은 PaymentsOfDividends(FY2013~2025)에 있다. 첫 성공을 채택하면
    배당이 통째로 비어 누적유보가 계산되지 않는다.
    """
    res = await asyncio.gather(*[_facts(cik, t) for t in tags])
    return [(t, u, f, e) for t, (u, f, e) in zip(tags, res)]


def _pick_duration(cands: list[tuple], years: int):
    """duration 후보 중 **최근 회계연도 커버리지가 가장 넓은** 태그를 채택한다.

    반환 (태그, 단위, {fy: 사실}, 보충태그[]). 채택 태그에 빠진 회계연도는 다음
    순위 후보로 메우고 어떤 태그를 썼는지 보충태그에 남긴다(같은 항목이라도
    회사가 중간에 태그를 갈아타는 일이 있다).
    """
    scored = [(t, u, _annual_duration(f)) for t, u, f, e in cands if e is None]
    scored = [x for x in scored if x[2]]
    if not scored:
        return None, None, {}, []
    all_fy = sorted({fy for _, _, a in scored for fy in a})
    recent = set(all_fy[-max(1, years):])
    # max는 첫 최대값을 돌려주므로 동점이면 우선순위가 앞선 후보가 이긴다.
    tag, unit, ann = max(scored, key=lambda x: len(recent & set(x[2])))
    merged, extra = dict(ann), []
    for t2, _u2, a2 in scored:
        if t2 == tag:
            continue
        filled = [fy for fy in recent - set(merged) if fy in a2]
        if filled:
            merged.update({fy: a2[fy] for fy in filled})
            extra.append(t2)
    return tag, unit, merged, extra


def _pick_instant(cands: list[tuple], years: int):
    """instant 후보 중 최근 기간 사실이 가장 많은 태그를 채택한다. (태그, 사실목록)."""
    best, best_tag, best_n = [], None, -1
    for t, _u, f, e in cands:
        if e is not None:
            continue
        rows = xbrl.dedupe(f, instant=True)
        if not rows:
            continue
        cutoff = max(r["end"] for r in rows) - timedelta(days=366 * max(1, years))
        n = sum(1 for r in rows if r["end"] >= cutoff)
        if n > best_n:
            best, best_tag, best_n = f, t, n
    return best_tag, best


# 분할 보정: 정수배(또는 그 역수)와 이만큼 이내로 맞아떨어져야 분할로 인정한다.
# 단순 정정(수치가 몇 % 바뀌는 것)을 분할로 오인하지 않기 위한 문턱이다.
_SPLIT_TOL = 0.005


def _is_split_ratio(r: float) -> bool:
    """주식분할 비율로 인정할 수 있는 값인지. 2:1, 15:1, 1:10(병합) 등."""
    if not r or r <= 0:
        return False
    for cand in (r, 1.0 / r):
        n = round(cand)
        if n >= 2 and abs(cand / n - 1.0) <= _SPLIT_TOL:
            return True
    return False


def _variants(rows_by_fy: dict[int, list[tuple]]) -> dict[int, float]:
    """회계연도별 주식수 보정 계수. 같은 기준(최신 제출본)으로 맞춘다.

    주식분할이 있으면 SEC 원자료에 분할 전/후 값이 **둘 다** 남는다. 최신 10-K가
    소급 조정한 연도는 분할 후 값이 함께 실리지만, 그보다 오래된 연도는 옛 기준
    값만 있어 그대로 이으면 시계열이 끊긴다(실측 ORLY: FY2022 64,962,000 →
    FY2023 914,976,000). 같은 연도에 공존하는 두 값의 비율이 곧 관측된 분할
    비율이므로(ORLY는 정확히 15.0), 그것을 옛 연도에 적용한다. 추정이 아니다.

    rows_by_fy: {회계연도: [(제출일, 값), ...]}  →  {회계연도: 곱할 계수}
    """
    filed_all = [f for vs in rows_by_fy.values() for f, _v in vs if f]
    if not filed_all:
        return {fy: 1.0 for fy in rows_by_fy}
    newest = max(filed_all)
    scales: dict[int, float] = {}
    scale, carry = 1.0, 1.0
    for fy in sorted(rows_by_fy, reverse=True):
        vs = [(f, v) for f, v in rows_by_fy[fy] if v]
        if not vs:
            scales[fy] = carry
            continue
        latest_filed, chosen = max(vs)
        on_newest_basis = latest_filed == newest
        scales[fy] = scale if on_newest_basis else carry
        distinct = {v for _f, v in vs}
        if len(distinct) > 1:
            old = min(distinct)
            r = chosen / old
            if _is_split_ratio(r):
                carry = scales[fy] * r
    return scales


def _dur_variants(facts: list[dict]) -> dict[int, list[tuple]]:
    """연간 duration 사실을 제출본별로 모은다(분할 보정용). {fy: [(filed, val)]}."""
    out: dict[int, list[tuple]] = {}
    for f in facts or []:
        start, end = xbrl.parse_dt(f.get("start")), xbrl.parse_dt(f.get("end"))
        val = f.get("val")
        if not start or not end or not isinstance(val, (int, float)):
            continue
        if not (xbrl.FY_MIN_DAYS <= xbrl.span_days(start, end) <= xbrl.FY_MAX_DAYS):
            continue
        out.setdefault(xbrl.fy_label(end), []).append(
            (xbrl.parse_dt(f.get("filed")) or date.min, float(val)))
    return out


def _inst_variants(facts: list[dict], cal: dict[int, date]) -> dict[int, list[tuple]]:
    """회계연도 종료 시점 instant 사실을 제출본별로 모은다. {fy: [(filed, val)]}."""
    out: dict[int, list[tuple]] = {}
    for f in facts or []:
        end, val = xbrl.parse_dt(f.get("end")), f.get("val")
        if not end or not isinstance(val, (int, float)):
            continue
        for fy, fy_end in cal.items():
            if abs((end - fy_end).days) <= _INSTANT_TOL:
                out.setdefault(fy, []).append(
                    (xbrl.parse_dt(f.get("filed")) or date.min, float(val)))
                break
    return out


def _fy_calendar(annuals: dict[str, dict[int, dict]]) -> dict[int, date]:
    """{회계연도: 종료일}. 매출→순이익→영업이익 순으로 먼저 잡히는 것을 채택한다."""
    cal: dict[int, date] = {}
    for key in ("revenue", "net_income", "operating_income", "ocf"):
        for fy, r in (annuals.get(key) or {}).items():
            cal.setdefault(fy, r["end"])
    return cal


def _ratio(num, den, pct: bool = False, nd: int = 2):
    """분모가 0·None이면 None. 추정하지 않는다."""
    if num is None or not den:
        return None
    return round(num / den * (100.0 if pct else 1.0), nd)


async def annual_metrics(ticker: str, years: int = 5) -> dict:
    """연간 핵심지표 한 판 — 수익력·재무구조·주주환원을 회계연도별로 조립한다.

    태그 폴백·연간값 추출·유도 필드를 서버에서 끝내 호출부가 us-gaap 태그를 알
    필요가 없게 한다. 값을 모르면 null로 두고 errors[]에 사유를 남긴다.
    """
    out = _skeleton(ticker, tags={}, tag_fallbacks_used={}, unit=None,
                    dividend_status=None, roe_avg_pct=None, roic_avg_pct=None,
                    shares_growth_3y_pct=None, note=_ANNUAL_NOTE)
    cik = await _resolve(out, ticker)
    if cik is None:
        return out

    dur_keys, inst_keys = list(_DUR_ITEMS), list(_INST_ITEMS)
    fetched = await asyncio.gather(
        *[_facts_all(cik, _DUR_ITEMS[k]) for k in dur_keys],
        *[_facts_all(cik, _INST_ITEMS[k]) for k in inst_keys],
    )
    dur_c = dict(zip(dur_keys, fetched[:len(dur_keys)]))
    inst_c = dict(zip(inst_keys, fetched[len(dur_keys):]))

    annuals: dict[str, dict[int, dict]] = {}
    units: dict[str, str | None] = {}
    dur_facts: dict[str, list[dict]] = {}
    for k in dur_keys:
        tag, unit, ann, extra = _pick_duration(dur_c[k], years)
        out["tags"][k], annuals[k], units[k] = tag, ann, unit
        dur_facts[k] = next((f for t, _u, f, e in dur_c[k]
                             if t == tag and e is None), [])
        if extra:
            out["tag_fallbacks_used"][k] = extra
        if tag is None:
            out["errors"].append(err_item(
                k, f"태그 후보 전부 미공시 또는 연간 사실 없음: {_DUR_ITEMS[k]}", "SEC"))

    inst_facts: dict[str, list[dict]] = {}
    for k in inst_keys:
        tag, facts = _pick_instant(inst_c[k], years)
        out["tags"][k], inst_facts[k] = tag, facts
        if tag is None:
            out["errors"].append(err_item(
                k, f"태그 후보 전부 미공시: {_INST_ITEMS[k]}", "SEC"))

    out["unit"] = units.get("revenue") or units.get("net_income")
    cal = _fy_calendar(annuals)
    if not cal:
        out["errors"].append(err_item(
            "series", "연간 사실(350~380일)이 없어 회계연도를 구성할 수 없음", "SEC"))
        return out

    # 주식수는 분할이 있으면 분할 전/후 기준이 섞인다. 같은 연도에 공존하는 두
    # 값의 비율로 옛 연도를 최신 기준에 맞춘다(_variants 참고).
    shares_scale = {
        "shares_diluted": _variants(_dur_variants(dur_facts.get("shares_diluted", []))),
        "shares_outstanding": _variants(
            _inst_variants(inst_facts.get("shares_outstanding", []), cal)),
    }

    # 배당 태그 후보가 전부 비었다 = 보통주 배당을 안 하는 기업일 가능성이 높다.
    no_div_tag = out["tags"]["dividends"] is None
    out["dividend_status"] = "not_tagged" if no_div_tag else "disclosed"

    rows = []
    for fy in sorted(cal)[-max(1, years):]:
        end = cal[fy]
        row: dict = {"fy": fy, "end": end.isoformat()}
        restated = False
        for k in dur_keys:
            r = (annuals.get(k) or {}).get(fy)
            row[k] = r["val"] if r else None
            restated = restated or bool(r and r.get("restated"))
        for k in inst_keys:
            r = xbrl.pick_instant(inst_facts[k], end, tol_days=_INSTANT_TOL)
            row[k] = r["val"] if r else None
            restated = restated or bool(r and r.get("restated"))

        # 현금유출 태그는 표준 부호가 양수지만 음수로 태깅하는 filer가 있다.
        for k in ("capex", "dividends", "buybacks"):
            if row[k] is not None:
                row[k] = abs(row[k])

        # 주식분할 소급 보정. 계수가 1이 아니면 옛 기준 값을 최신 기준으로 옮긴 것이다.
        row["shares_split_adjusted"] = False
        for k in ("shares_diluted", "shares_outstanding"):
            sc = shares_scale[k].get(fy, 1.0)
            if row[k] is not None and sc != 1.0:
                row[k] = row[k] * sc
                row["shares_split_adjusted"] = True

        # Liabilities 미공시 기업(ORLY 등)은 자산−자본으로 복원한다.
        row["liabilities_derived"] = False
        if row["liabilities"] is None and None not in (row["assets"], row["equity"]):
            row["liabilities"] = row["assets"] - row["equity"]
            row["liabilities_derived"] = True

        debt = [v for v in (row.pop("debt_lt"), row.pop("debt_st")) if v is not None]
        row["debt_total"] = sum(debt) if debt else None
        row["net_debt"] = (row["debt_total"] - row["cash"]
                           if row["debt_total"] is not None and row["cash"] is not None
                           else None)
        row["ebitda"] = (row["operating_income"] + row["dda"]
                         if None not in (row["operating_income"], row["dda"]) else None)
        row["fcf"] = (row["ocf"] - row["capex"]
                      if None not in (row["ocf"], row["capex"]) else None)
        row["operating_margin_pct"] = _ratio(row["operating_income"], row["revenue"],
                                             pct=True)
        row["net_debt_to_ebitda"] = _ratio(row["net_debt"], row["ebitda"])

        row["rnd_to_revenue_pct"] = _ratio(row["rnd"], row["revenue"], pct=True)

        # 자기자본이 음수면(자사주 매입 누적 등) ROE는 부호가 뒤집혀 의미가 없다.
        # 큰 음수 퍼센트를 내보내면 스크리닝에서 오판하므로 값을 만들지 않는다.
        row["equity_negative"] = bool(row["equity"] is not None and row["equity"] <= 0)
        row["roe_pct"] = None if row["equity_negative"] else _ratio(
            row["net_income"], row["equity"], pct=True)

        # 실효세율은 세전이익이 양수일 때만 의미가 있다(적자연도는 부호가 뒤집힌다).
        eff = (_ratio(row["income_tax"], row["pretax_income"])
               if (row["pretax_income"] or 0) > 0 else None)
        row["effective_tax_rate_pct"] = None if eff is None else round(eff * 100, 2)

        # ROIC = 영업이익x(1-실효세율) / 투하자본(net_debt + equity).
        # 자기자본이 음수여도 투하자본이 양수면 성립하므로 ROE 대신 쓸 수 있다.
        invested = (row["net_debt"] + row["equity"]
                    if None not in (row["net_debt"], row["equity"]) else None)
        row["invested_capital"] = invested
        if eff is not None and row["operating_income"] is not None and (invested or 0) > 0:
            row["nopat"] = row["operating_income"] * (1.0 - eff)
            row["roic_pct"] = round(row["nopat"] / invested * 100, 2)
        else:
            row["nopat"] = None
            row["roic_pct"] = None

        # 누적유보 = 순이익 − 배당 − 자사주. 배당 태그가 아예 없으면 0으로 두되
        # 가정했다는 사실을 플래그로 남긴다(조용히 0으로 채우지 않는다).
        div = 0.0 if (no_div_tag and row["dividends"] is None) else row["dividends"]
        row["retained_assumes_no_dividend"] = bool(no_div_tag and row["dividends"] is None)
        if None not in (row["net_income"], div, row["buybacks"]):
            row["shareholder_returns"] = div + row["buybacks"]
            row["retained"] = row["net_income"] - row["shareholder_returns"]
        else:
            row["shareholder_returns"] = None
            row["retained"] = None
        row["restated"] = restated
        rows.append(row)

    out["series"] = rows

    # 피셔 13(주주 희석) 판정 근거. 3개 회계연도 전 대비 희석주식수 증가율.
    if len(rows) >= 4 and rows[-4]["shares_diluted"] and rows[-1]["shares_diluted"]:
        out["shares_growth_3y_pct"] = round(
            (rows[-1]["shares_diluted"] / rows[-4]["shares_diluted"] - 1) * 100, 2)
    else:
        out["errors"].append(err_item(
            "shares_growth_3y_pct",
            "희석주식수가 4개 회계연도치 모이지 않아 3년 증가율을 낼 수 없음", "SEC"))

    roics = [r["roic_pct"] for r in rows if r["roic_pct"] is not None][-3:]
    if roics:
        out["roic_avg_pct"] = round(sum(roics) / len(roics), 2)

    roes = [r["roe_pct"] for r in rows if r["roe_pct"] is not None][-3:]
    if roes:
        out["roe_avg_pct"] = round(sum(roes) / len(roes), 2)
    elif any(r["equity_negative"] for r in rows):
        out["errors"].append(err_item(
            "roe_avg_pct", "자기자본이 음수라 ROE가 의미를 갖지 못함 "
            "(ROIC 등 대체 지표를 쓸 것)", "SEC"))
    else:
        out["errors"].append(err_item("roe_avg_pct",
                                      "순이익·자기자본 공시가 부족해 ROE 평균을 "
                                      "계산할 수 없음", "SEC"))
    return out
