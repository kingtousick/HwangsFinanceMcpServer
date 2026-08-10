"""core/xbrl.py 단위 테스트 — 네트워크 접근 없음.

결산월이 다른 회사(12월/1월=NVDA/6월=MSFT/53주=AAPL)와 수정신고·비교표시
사실이 섞인 상황에서 분기 단독값이 정확히 나오는지 검증한다.
"""
from __future__ import annotations

from datetime import date

from core import xbrl


def _d(start, end, val, *, filed="2026-01-01", accn="a-1", form="10-Q",
       fy=None, fp=None) -> dict:
    """duration 사실. fy/fp는 일부러 틀리게 넣어도 결과가 같아야 한다."""
    return {"start": start, "end": end, "val": val, "filed": filed,
            "accn": accn, "form": form, "fy": fy, "fp": fp}


def _i(end, val, *, filed="2026-01-01", accn="a-1", form="10-K") -> dict:
    """instant 사실(재무상태표 항목)."""
    return {"end": end, "val": val, "filed": filed, "accn": accn, "form": form}


def _by_fp(rows) -> dict:
    return {(r["fy"], r["fp"]): r for r in rows}


# ------------------------------------------------------------------ 기본 헬퍼

def test_parse_dt_and_span():
    assert xbrl.parse_dt("2025-03-31") == date(2025, 3, 31)
    assert xbrl.parse_dt("2025-03-31T00:00:00") == date(2025, 3, 31)
    assert xbrl.parse_dt("") is None
    assert xbrl.parse_dt("nope") is None
    # start/end 모두 포함이므로 1월 한 달은 31일
    assert xbrl.span_days(date(2025, 1, 1), date(2025, 1, 31)) == 31


def test_fy_label_uses_end_year_with_january_correction():
    assert xbrl.fy_label(date(2025, 1, 26)) == 2025    # NVDA FY2025
    assert xbrl.fy_label(date(2025, 6, 30)) == 2025    # MSFT FY2025
    assert xbrl.fy_label(date(2024, 9, 28)) == 2024    # AAPL FY2024
    assert xbrl.fy_label(date(2025, 12, 31)) == 2025
    assert xbrl.fy_label(date(2025, 1, 3)) == 2024     # 12월말 기준 52/53주 결산


def test_annualize():
    assert xbrl.annualize(100.0, 365) == 100.0
    assert round(xbrl.annualize(100.0, 90), 2) == 405.56
    assert xbrl.annualize(None, 365) is None
    assert xbrl.annualize(100.0, 0) is None


def test_units_of_picks_unit_with_most_facts():
    cj = {"units": {"USD": [{"end": "2025-01-01", "val": 1}] * 3,
                    "shares": [{"end": "2025-01-01", "val": 1}]}}
    unit, facts = xbrl.units_of(cj)
    assert unit == "USD" and len(facts) == 3
    assert xbrl.units_of({}) == (None, [])


# ------------------------------------------------------------------ dedupe

def test_dedupe_takes_latest_filed():
    facts = [
        _d("2025-01-01", "2025-03-31", 100, filed="2025-05-01", accn="old"),
        _d("2025-01-01", "2025-03-31", 105, filed="2025-08-01", accn="new"),
    ]
    rows = xbrl.dedupe(facts)
    assert len(rows) == 1
    assert rows[0]["val"] == 105 and rows[0]["accn"] == "new"


def test_dedupe_flags_restated_when_value_changed():
    facts = [
        _d("2025-01-01", "2025-03-31", 100, filed="2025-05-01", accn="old"),
        _d("2025-01-01", "2025-03-31", 105, filed="2025-08-01", accn="new"),
    ]
    assert xbrl.dedupe(facts)[0]["restated"] is True


def test_dedupe_same_value_repeated_is_not_restated():
    """같은 기간이 여러 보고서에 반복 등장하는 건 비교표시라 정상이다."""
    facts = [
        _d("2025-01-01", "2025-03-31", 100, filed="2025-05-01", accn="a"),
        _d("2025-01-01", "2025-03-31", 100, filed="2026-05-01", accn="b"),
    ]
    assert xbrl.dedupe(facts)[0]["restated"] is False


def test_dedupe_flags_amended_form():
    facts = [_d("2025-01-01", "2025-12-31", 400, form="10-K/A")]
    assert xbrl.dedupe(facts)[0]["restated"] is True


def test_dedupe_drops_unusable_facts():
    facts = [
        _d("2025-01-01", "2025-03-31", None),        # val 없음
        _d(None, "2025-03-31", 100),                 # duration인데 start 없음
        {"start": "2025-01-01", "end": "bad", "val": 1},
        _d("2025-01-01", "2025-03-31", 100),
    ]
    assert len(xbrl.dedupe(facts)) == 1


# ------------------------------------------------------------------ 회계연도 구간

def test_fy_intervals_calendar_year():
    rows = xbrl.dedupe([
        _d("2024-01-01", "2024-12-31", 400, form="10-K"),
        _d("2025-01-01", "2025-12-31", 440, form="10-K"),
        _d("2025-01-01", "2025-03-31", 100),
    ])
    assert xbrl.fy_intervals(rows) == [
        (date(2024, 1, 1), date(2024, 12, 31)),
        (date(2025, 1, 1), date(2025, 12, 31)),
    ]


def test_fy_intervals_extrapolates_in_progress_year():
    """진행 중 회계연도는 10-K가 없어 연간 사실이 없다 → 직전 FY 길이로 연장."""
    rows = xbrl.dedupe([
        _d("2025-01-01", "2025-12-31", 440, form="10-K"),
        _d("2026-01-01", "2026-03-31", 120),   # FY2026 Q1
    ])
    fys = xbrl.fy_intervals(rows)
    assert fys[-1] == (date(2026, 1, 1), date(2026, 12, 31))


def test_fy_intervals_merges_overlapping_candidates():
    """1~3일 어긋난 중복 연간 구간은 하나로 합친다(52/53주 결산 오차)."""
    rows = xbrl.dedupe([
        _d("2025-01-01", "2025-12-31", 440, form="10-K", accn="x"),
        _d("2025-01-02", "2025-12-30", 441, form="10-K", accn="y"),
    ])
    assert len(xbrl.fy_intervals(rows, extrapolate=False)) == 1


# ------------------------------------------------------------------ YTD 차분 ★

def test_quarterize_ytd_diff_calendar_year():
    """현금흐름표 전형: 누적만 공시 → Q2~Q4는 차분, Q4는 연간−9개월누적."""
    facts = [
        _d("2025-01-01", "2025-03-31", 100),   # 3M 누적
        _d("2025-01-01", "2025-06-30", 220),   # 6M 누적
        _d("2025-01-01", "2025-09-30", 300),   # 9M 누적
        _d("2025-01-01", "2025-12-31", 420, form="10-K"),  # 연간
    ]
    rows = _by_fp(xbrl.quarterize(facts))
    assert rows[(2025, "Q1")]["val"] == 100
    assert rows[(2025, "Q2")]["val"] == 120
    assert rows[(2025, "Q3")]["val"] == 80
    assert rows[(2025, "Q4")]["val"] == 120        # ★ 420 − 300
    assert rows[(2025, "Q1")]["derived"] is False  # Q1 누적 = Q1 단독
    assert rows[(2025, "Q4")]["derived"] is True
    # 차분으로 만든 기간은 앞 누적 종료 다음 날부터다
    assert rows[(2025, "Q4")]["start"] == "2025-10-01"
    assert rows[(2025, "Q4")]["end"] == "2025-12-31"


def test_quarterize_prefers_reported_solo_over_diff():
    """3개월 단독 공시(손익계산서 전형)가 있으면 차분하지 않는다."""
    facts = [
        _d("2025-01-01", "2025-03-31", 100),
        _d("2025-04-01", "2025-06-30", 130),   # 단독 공시
        _d("2025-01-01", "2025-06-30", 220),   # 누적(차분하면 120)
        _d("2025-01-01", "2025-12-31", 420, form="10-K"),
    ]
    q2 = _by_fp(xbrl.quarterize(facts))[(2025, "Q2")]
    assert q2["val"] == 130 and q2["derived"] is False


def test_quarterize_skips_quarter_when_prior_cumulative_missing():
    """앞 분기 누적이 없으면 값을 만들지 않는다(추정 금지)."""
    facts = [
        _d("2025-01-01", "2025-03-31", 100),
        # 6M 누적 없음
        _d("2025-01-01", "2025-09-30", 300),
        _d("2025-01-01", "2025-12-31", 420, form="10-K"),
    ]
    rows = _by_fp(xbrl.quarterize(facts))
    assert (2025, "Q2") not in rows       # 만들지 않음
    assert (2025, "Q3") not in rows       # 6M이 없어 차분 불가
    assert rows[(2025, "Q4")]["val"] == 120


def test_quarterize_ignores_wrong_fy_fp_labels():
    """★ fact의 fy/fp는 '실린 보고서'의 것이라 믿으면 안 된다.

    전년 동기 비교치가 fy=2025,fp=Q2로 실려 와도 날짜 기준으로 FY2024에 배정돼야 한다.
    """
    facts = [
        # FY2024 (fy/fp 라벨은 전부 2025년 보고서 것으로 오염돼 있다)
        _d("2024-01-01", "2024-03-31", 90, fy=2025, fp="Q1"),
        _d("2024-01-01", "2024-06-30", 200, fy=2025, fp="Q2"),
        _d("2024-01-01", "2024-12-31", 400, form="10-K", fy=2025, fp="FY"),
        # FY2025
        _d("2025-01-01", "2025-03-31", 100, fy=2025, fp="Q1"),
        _d("2025-01-01", "2025-06-30", 220, fy=2025, fp="Q2"),
    ]
    rows = _by_fp(xbrl.quarterize(facts))
    assert rows[(2024, "Q1")]["val"] == 90
    assert rows[(2024, "Q2")]["val"] == 110     # 200 − 90
    assert rows[(2025, "Q1")]["val"] == 100
    assert rows[(2025, "Q2")]["val"] == 120     # 220 − 100


def test_quarterize_nvidia_january_fiscal_year():
    """1월 결산(52/53주). FY2025 = 2024-01-29 ~ 2025-01-26."""
    facts = [
        _d("2024-01-29", "2024-04-28", 100),
        _d("2024-01-29", "2024-07-28", 230),
        _d("2024-01-29", "2024-10-27", 380),
        _d("2024-01-29", "2025-01-26", 560, form="10-K"),
    ]
    rows = _by_fp(xbrl.quarterize(facts))
    assert rows[(2025, "Q1")]["val"] == 100
    assert rows[(2025, "Q2")]["val"] == 130
    assert rows[(2025, "Q3")]["val"] == 150
    assert rows[(2025, "Q4")]["val"] == 180


def test_quarterize_microsoft_june_fiscal_year():
    """6월 결산. FY2025 = 2024-07-01 ~ 2025-06-30."""
    facts = [
        _d("2024-07-01", "2024-09-30", 50),
        _d("2024-07-01", "2024-12-31", 110),
        _d("2024-07-01", "2025-03-31", 175),
        _d("2024-07-01", "2025-06-30", 250, form="10-K"),
    ]
    rows = _by_fp(xbrl.quarterize(facts))
    assert [rows[(2025, f"Q{i}")]["val"] for i in (1, 2, 3, 4)] == [50, 60, 65, 75]


def test_quarterize_53_week_fiscal_year():
    """53주 결산(371일). AAPL FY2023 = 2022-09-25 ~ 2023-09-30."""
    facts = [
        _d("2022-09-25", "2022-12-31", 40),
        _d("2022-09-25", "2023-04-01", 75),
        _d("2022-09-25", "2023-07-01", 100),
        _d("2022-09-25", "2023-09-30", 130, form="10-K"),
    ]
    rows = _by_fp(xbrl.quarterize(facts))
    assert [rows[(2023, f"Q{i}")]["val"] for i in (1, 2, 3, 4)] == [40, 35, 25, 30]


def test_quarterize_propagates_restated():
    facts = [
        _d("2025-01-01", "2025-09-30", 300),
        _d("2025-01-01", "2025-12-31", 420, form="10-K/A"),
    ]
    q4 = _by_fp(xbrl.quarterize(facts))[(2025, "Q4")]
    assert q4["restated"] is True


def test_quarterize_empty_input():
    assert xbrl.quarterize([]) == []


# ------------------------------------------------------------------ TTM

def test_ttm_at_fiscal_year_end_uses_annual():
    facts = [
        _d("2025-01-01", "2025-09-30", 300),
        _d("2025-01-01", "2025-12-31", 420, form="10-K"),
    ]
    assert xbrl.ttm(facts, at=date(2025, 12, 31)) == (420.0, "fy")


def test_ttm_mid_year_uses_prior_annual_plus_ytd_diff():
    """진행 중 회계연도: 직전 연간(400) + 당해 6M(220) − 전년 6M(200) = 420."""
    facts = [
        _d("2024-01-01", "2024-06-30", 200),
        _d("2024-01-01", "2024-12-31", 400, form="10-K"),
        _d("2025-01-01", "2025-03-31", 100),
        _d("2025-01-01", "2025-06-30", 220),
    ]
    assert xbrl.ttm(facts, at=date(2025, 6, 30)) == (420.0, "ttm")


def test_ttm_falls_back_to_quarter_sum():
    """누적 공시가 전혀 없고 3개월 단독만 있는 경우."""
    facts = [
        _d("2024-07-01", "2024-09-30", 10),
        _d("2024-10-01", "2024-12-31", 20),
        _d("2025-01-01", "2025-03-31", 30),
        _d("2025-04-01", "2025-06-30", 40),
        # 회계연도 구간을 잡으려면 연간 사실이 하나는 필요하다
        _d("2024-01-01", "2024-12-31", 60, form="10-K"),
    ]
    val, basis = xbrl.ttm(facts, at=date(2025, 6, 30))
    assert basis == "quarters_sum" and val == 100.0


def test_ttm_returns_none_when_insufficient():
    assert xbrl.ttm([]) == (None, None)
    assert xbrl.ttm([_d("2025-01-01", "2025-03-31", 100)]) == (None, None)


def test_ttm_defaults_to_latest_end():
    facts = [
        _d("2025-01-01", "2025-09-30", 300),
        _d("2025-01-01", "2025-12-31", 420, form="10-K"),
    ]
    assert xbrl.ttm(facts) == (420.0, "fy")


# ------------------------------------------------------------------ instant

def test_pick_instant_exact_and_tolerance():
    facts = [_i("2025-12-31", 1000), _i("2024-12-31", 900)]
    assert xbrl.pick_instant(facts, date(2025, 12, 31))["val"] == 1000
    assert xbrl.pick_instant(facts, date(2025, 12, 28))["val"] == 1000   # 3일 오차
    assert xbrl.pick_instant(facts, date(2025, 11, 30)) is None          # 허용 밖


def test_pick_instant_takes_latest_filed():
    facts = [
        _i("2025-12-31", 1000, filed="2026-02-01", accn="old"),
        _i("2025-12-31", 1010, filed="2026-05-01", accn="new"),
    ]
    r = xbrl.pick_instant(facts, date(2025, 12, 31))
    assert r["val"] == 1010 and r["restated"] is True


def test_cumulative_returns_ytd_only():
    facts = [
        _d("2025-01-01", "2025-06-30", 220),
        _d("2025-04-01", "2025-06-30", 120),   # 단독 공시는 제외
        _d("2025-01-01", "2025-12-31", 420, form="10-K"),
    ]
    rows = xbrl.cumulative(facts)
    assert [r["val"] for r in rows] == [220.0, 420.0]
