"""정규화 응답 빌더 (설계서 §5).

모든 Tool은 성공 시 ok(), 실패 시 fail() 형태의 dict를 반환한다.
- 숫자 필드는 문자열이 아닌 float (쉼표 제거).
- timestamp는 ISO8601 + KST 오프셋.
- 부분 실패 허용: 채울 수 없는 필드는 None.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))


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


def fail(name: str, err, source: str = "fallback") -> dict:
    """실패 응답. Claude가 WebSearch로 폴백하도록 error 필드를 채운다."""
    return {"name": name, "error": str(err), "source": source}
