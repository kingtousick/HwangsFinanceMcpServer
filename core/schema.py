"""정규화 응답 빌더 (설계서 §5).

모든 Tool은 성공 시 ok(), 실패 시 fail() 형태의 dict를 반환한다.
- 숫자 필드는 문자열이 아닌 float (쉼표 제거).
- timestamp는 ISO8601 + KST 오프셋.
- 부분 실패 허용: 채울 수 없는 필드는 None.
"""
from __future__ import annotations

import re

from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))

# data_kind: 값이 어느 시점의 것인지. 미국 밸류에이션/SEC/FRED 툴이 사용한다.
# (기존 국내 툴은 ok() 빌더를 쓰며 이 필드를 넣지 않는다 — 응답 크기 유지)
REALTIME = "realtime"        # 실시간 체결가
INTRADAY = "intraday"        # 장중 지연 시세
PREV_CLOSE = "prev_close"    # 전일 종가 기준
FILING = "filing"            # 공시 원문 기준

# 에러 메시지에 섞여 나오는 API 키를 마스킹.
# data.go.kr(serviceKey), 수출입은행(authkey), 열린재정(Key/apiKey), DART(crtfc_key) 등
# 파라미터명 변형 포괄.
_SECRET_RE = re.compile(
    r"(serviceKey|service_key|authkey|auth_key|apiKey|api_key|Key)=[^&\s'\"]+",
    re.IGNORECASE,
)

# 한국은행 ECOS는 인증키가 쿼리가 아니라 URL 경로에 들어간다
# (ecos.bok.or.kr/api/KeyStatisticList/{키}/json/...). httpx 예외 메시지에 URL이
# 그대로 실리므로 경로 세그먼트도 마스킹한다.
_PATH_KEY_RE = re.compile(
    r"(ecos\.bok\.or\.kr/api/\w+/)[^/\s'\"]+",
    re.IGNORECASE,
)


def _scrub(s: str) -> str:
    return _PATH_KEY_RE.sub(r"\1***", _SECRET_RE.sub(r"\1=***", s))


def _reason(err) -> str:
    """예외/문자열을 사람이 읽을 수 있는 사유 문자열로.

    httpx.ReadTimeout·ReadError 등 네트워크 예외는 str()이 빈 문자열이라
    그대로 쓰면 "reason": "" 가 되어 아무 정보도 주지 못한다. 이럴 때는
    예외 클래스명이라도 남긴다.
    """
    s = str(err).strip()
    if not s and isinstance(err, BaseException):
        s = type(err).__name__
    return _scrub(s)


def to_float(v) -> float | None:
    """문자열/숫자를 float로. 쉼표 제거. 변환 불가 시 None."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "")
    if s == "":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def epoch_to_kst_iso(epoch: int | float | None) -> str | None:
    """유닉스 epoch(초)를 KST ISO8601 문자열로."""
    if epoch is None:
        return None
    try:
        return datetime.fromtimestamp(float(epoch), tz=KST).isoformat()
    except (ValueError, OSError, OverflowError):
        return None


def now_kst_iso() -> str:
    return datetime.now(tz=KST).isoformat()


def ok(
    name: str,
    value,
    *,
    change=None,
    change_pct=None,
    timestamp: str | None = None,
    currency: str | None = None,
    source: str | None = None,
) -> dict:
    """성공 응답. 숫자 필드는 float로 정규화."""
    def _round(v, nd):
        return round(v, nd) if isinstance(v, float) else v

    return {
        "name": name,
        "value": to_float(value),
        "change": _round(to_float(change), 4),
        "change_pct": _round(to_float(change_pct), 4),
        "timestamp": timestamp,
        "currency": currency,
        "source": source,
    }


def err_item(field: str, reason, source: str) -> dict:
    """부분성공 응답의 errors[] 원소.

    값을 못 채운 필드마다 왜/어디서 실패했는지 남긴다. 값은 null로 두고
    추정치로 채우지 않는다. reason에 섞인 API 키는 마스킹한다.
    """
    return {"field": field, "reason": _reason(reason), "source": source}


def fail(name: str, err, source: str = "fallback") -> dict:
    """실패 응답. Claude가 WebSearch로 폴백하도록 error 필드를 채운다.

    error 문자열에 섞인 API 키(serviceKey/authkey)는 마스킹한다.
    """
    return {"name": name, "error": _reason(err), "source": source}
