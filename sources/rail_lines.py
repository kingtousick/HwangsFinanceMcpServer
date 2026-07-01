"""철도/광역교통 노선 프리셋 → 검색 키워드 변환.

공사현황 Tool(입찰공고·예산·고시·공정률)은 모두 '사업명/노선명' 텍스트로 원본을
검색한다. 그런데 같은 노선도 출처마다 표기가 다르다(예: 'GTX-A' vs '수도권광역급행철도
A노선' vs '삼성동탄'). 노선 프리셋은 한 별칭에 여러 표기를 묶어 검색 누락을 줄인다.

resolve_line(query):
  - 프리셋 별칭(또는 그 별칭의 키워드 중 하나)과 매칭되면 등록된 키워드 세트를 돌려준다.
  - 매칭이 없으면 입력 문자열 자체를 단일 키워드로 passthrough(자유 키워드 검색 지원).

수록 범위는 수도권 부동산에 영향이 큰 주요 광역/도시철도 위주. 미수록 노선은 자유
키워드로 그대로 조회하면 된다. region_codes.py와 같은 '프리셋 + passthrough' 철학.
"""
from __future__ import annotations

import re

# 별칭 → 검색 키워드 후보. kric_m은 국가철도공단 주요사업현황 페이지 구분
# ('05010402'=광역철도, '05010302'=일반철도)으로 공정률 스크래핑 힌트.
# agencies(선택): 발주/수요기관 필터 힌트. 숫자 노선명("9호선")이 도로 노선번호
# (국도79호선·소로2-9호선 등)에 부분일치로 걸리는 노이즈를 기관으로 걸러낸다.
RAIL_LINES: dict[str, dict] = {
    "GTX-A": {
        "keywords": ["수도권광역급행철도 A", "GTX-A", "GTX A", "삼성동탄", "운정삼성"],
        "kric_m": "05010402",
    },
    "GTX-B": {
        "keywords": ["수도권광역급행철도 B", "GTX-B", "GTX B", "송도마석", "용산상봉"],
        "kric_m": "05010402",
    },
    "GTX-C": {
        "keywords": ["수도권광역급행철도 C", "GTX-C", "GTX C", "덕정수원"],
        "kric_m": "05010402",
    },
    "신안산선": {
        "keywords": ["신안산선", "안산선 복선전철"],
        "kric_m": "05010402",
    },
    "월곶판교": {
        "keywords": ["월곶판교", "월곶~판교", "월곶판교 복선전철"],
        "kric_m": "05010402",
    },
    "7호선 청라연장": {
        "keywords": ["도시철도 7호선 청라", "7호선 청라", "청라국제도시 연장"],
        "kric_m": "05010402",
    },
    "1호선 검단연장": {
        "keywords": ["인천도시철도 1호선 검단", "1호선 검단", "검단 연장"],
        "kric_m": "05010402",
    },
    "별내선": {
        "keywords": ["별내선", "8호선 별내", "암사별내"],
        "kric_m": "05010402",
    },
    "대곡소사": {
        "keywords": ["대곡소사", "대곡~소사", "서해선 대곡소사"],
        "kric_m": "05010402",
    },
    "서해선": {
        "keywords": ["서해선 복선전철", "서해선"],
        "kric_m": "05010302",
    },
    "동탄인덕원": {
        "keywords": ["인덕원동탄", "인덕원~동탄", "동탄인덕원 복선전철"],
        "kric_m": "05010402",
    },
    "위례신사선": {
        "keywords": ["위례신사선", "위례신사 도시철도"],
        "kric_m": "05010402",
    },
    "9호선 연장": {
        # 서울 9호선 4단계(강동~고덕강일·미사) 및 인천 검암 직결 연장. 숫자 노선명이라
        # 정밀 키워드 + 기관 필터 병행(운영구간 보수/도로 노선번호 노이즈 제거).
        "keywords": ["9호선 4단계", "9호선 연장", "고덕강일", "9호선 검암",
                     "9호선 공항철도", "강일지구 9호선"],
        "kric_m": "05010402",
        "agencies": ["서울교통공사", "서울특별시", "서울시", "국가철도공단",
                     "도시기반시설", "인천광역시", "인천교통공사"],
    },
}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip())


def _alias_index() -> dict[str, str]:
    """소문자/공백제거 키 → canonical 별칭. 별칭명과 키워드 모두 색인."""
    idx: dict[str, str] = {}
    for alias, info in RAIL_LINES.items():
        names = [alias] + info["keywords"]
        for n in names:
            idx[re.sub(r"[\s\-~]", "", n).lower()] = alias
    return idx


_INDEX = _alias_index()


def resolve_line(query: str) -> dict:
    """노선 프리셋이면 등록된 키워드 세트를, 아니면 입력 자체를 단일 키워드로 반환.

    반환: {"line": <별칭 또는 입력>, "keywords": [...], "kric_m": <구분 or None>,
           "agencies": <기관 힌트 리스트 or None>, "preset": bool}.
    """
    q = _norm(query)
    flat = re.sub(r"[\s\-~]", "", q).lower()
    alias = _INDEX.get(flat)
    if alias:
        info = RAIL_LINES[alias]
        return {
            "line": alias,
            "keywords": list(info["keywords"]),
            "kric_m": info.get("kric_m"),
            "agencies": info.get("agencies"),
            "preset": True,
        }
    return {"line": q, "keywords": [q], "kric_m": None, "agencies": None,
            "preset": False}
