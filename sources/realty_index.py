"""주택 매매·전세 가격지수 시계열 (한국은행 ECOS가 중계하는 원기관 통계).

실거래가(molit.py)는 개별 단지·평형·층 편차가 커서 '시장이 오르는지'를 보기
어렵다. 지수 계열을 함께 봐야 실거래 해석이 된다.

두 출처를 지원한다(ECOS StatisticItemList 실측, 2026-08-07):
  - 부동산원(기본): 901Y113 매매 / 901Y114 전세. 시도 단위 24개 지역 ×
    주택유형 4종의 **2차원** 통계표(ITEM1=유형, ITEM2=지역 순서 — 뒤집으면
    INFO-200). 기준월 2025.03=100. 공표 지연이 있어 ECOS 반영은 몇 달 늦다.
  - KB(source='kb'): 901Y062 매매 / 901Y063 전세. 1차원이라 전국·서울만
    가능하지만 최신월까지 반영이 빠르다. 기준월 2026.01=100.

두 지수는 기준월·표본이 달라 **수치를 직접 비교하지 말고** 각각의 변화율로 본다.
시군구(강남구 등) 단위는 ECOS에 없다 — get_apt_trade_summary(실거래)로 본다.
"""
from __future__ import annotations

from sources import ecos

# 부동산원 지역 항목(Group2 계정항목)
_REB_REGIONS = {
    "전국": "R70A", "수도권": "R70B", "지방": "R70C", "5대광역시": "R70D",
    "8개도": "R70E", "서울": "R70F", "경기": "R70G", "인천": "R70H",
    "부산": "R70I", "대구": "R70J", "광주": "R70K", "대전": "R70L",
    "울산": "R70M", "세종": "R70N", "강원": "R70O", "충북": "R70P",
    "충남": "R70Q", "전북": "R70R", "전남": "R70S", "경북": "R70T",
    "경남": "R70U", "제주": "R70V", "6대광역시": "R70W", "9개도": "R70X",
}
# 부동산원 주택유형 항목(Group1 구분코드)
_REB_TYPES = {"종합": "H69A", "아파트": "H69B", "연립다세대": "H69C", "단독주택": "H69D"}
_REB_STATS = {"매매": "901Y113", "전세": "901Y114"}

# KB 항목(1차원) — (유형, 지역) → 항목코드 접미어
_KB_STATS = {"매매": ("901Y062", "P63"), "전세": ("901Y063", "P64")}
_KB_SUFFIX = {
    ("종합", "전국"): "A", ("단독주택", "전국"): "AA", ("연립다세대", "전국"): "AB",
    ("아파트", "전국"): "AC", ("종합", "서울"): "AD", ("아파트", "서울"): "ACA",
}

# 입력 표기 흡수(시도명 변형). 시군구는 미지원이라 안내로 유도한다.
_REGION_ALIASES = {
    "서울특별시": "서울", "서울시": "서울", "경기도": "경기", "인천광역시": "인천",
    "부산광역시": "부산", "대구광역시": "대구", "광주광역시": "광주",
    "대전광역시": "대전", "울산광역시": "울산", "세종특별자치시": "세종",
    "강원도": "강원", "충청북도": "충북", "충청남도": "충남",
    "전라북도": "전북", "전라남도": "전남", "경상북도": "경북",
    "경상남도": "경남", "제주도": "제주", "제주특별자치도": "제주",
}
_TYPE_ALIASES = {"전체": "종합", "주택종합": "종합", "빌라": "연립다세대",
                 "연립": "연립다세대", "다세대": "연립다세대", "단독": "단독주택"}


def _norm_region(region: str) -> str:
    r = (region or "전국").strip()
    return _REGION_ALIASES.get(r, r)


def _norm_type(house_type: str) -> str:
    t = (house_type or "아파트").strip()
    return _TYPE_ALIASES.get(t, t)


async def price_index(region: str = "전국", kind: str = "매매",
                      house_type: str = "아파트", months: int = 36,
                      source: str = "부동산원") -> dict:
    """주택 가격지수 시계열. kind는 '매매'/'전세', source는 '부동산원'/'kb'."""
    k = (kind or "매매").strip()
    if k not in _REB_STATS:
        raise ValueError(f"kind는 '매매' 또는 '전세'만 가능합니다(입력: '{kind}')")
    reg, typ = _norm_region(region), _norm_type(house_type)
    src = (source or "부동산원").strip().lower()

    if src in ("kb", "국민은행"):
        stat, prefix = _KB_STATS[k]
        suffix = _KB_SUFFIX.get((typ, reg))
        if suffix is None:
            raise ValueError(
                f"KB 지수는 (유형,지역) 조합이 제한적입니다: "
                f"{', '.join(f'{t}/{g}' for t, g in _KB_SUFFIX)} "
                f"(입력: {typ}/{reg}). 지역을 세분하려면 source='부동산원'을 쓰세요")
        item = prefix + suffix
        label = f"주택{k}가격지수(KB) {reg} {typ}"
        org = "KB국민은행"
    else:
        if reg not in _REB_REGIONS:
            raise ValueError(
                f"'{region}'은 지원 지역이 아닙니다. 지수는 시도 단위까지만 제공됩니다"
                f"({', '.join(list(_REB_REGIONS)[:8])} 등). 시군구 단위 시세는 "
                "get_apt_trade_summary(실거래)를 사용하세요")
        if typ not in _REB_TYPES:
            raise ValueError(
                f"'{house_type}'은 지원 유형이 아닙니다: {', '.join(_REB_TYPES)}")
        stat = _REB_STATS[k]
        # 2차원 통계표는 반드시 유형 → 지역 순서(뒤집으면 데이터 없음)
        item = f"{_REB_TYPES[typ]}/{_REB_REGIONS[reg]}"
        label = f"주택{k}가격지수(한국부동산원) {reg} {typ}"
        org = "한국부동산원"

    rows = await ecos.search_series(stat, item, "M", months)
    return ecos.build_series(rows, label, stat, item, "M", extra={
        "region": reg,
        "kind": k,
        "house_type": typ,
        "org": org,
        "note": "지수는 기준월=100의 상대값이라 출처가 다르면 수치를 직접 비교하지 "
                "말고 변화율로 볼 것. 공표 지연으로 최근 몇 개월은 비어 있을 수 있음",
    })
