"""공공데이터포털(data.go.kr) 인증키 일원화.

data.go.kr 계정의 '일반 인증키(Decoding)'는 활용신청한 모든 서비스에 공용으로 쓰인다.
실거래가(molit)·나라장터(g2b)·관보고시(kr_notice)가 같은 키를 공유하므로 한 곳에서 읽는다.

우선순위: DATA_GO_KR_API_KEY > MOLIT_API_KEY (기존 호환).
어느 서비스를 호출하든 그 서비스를 data.go.kr에서 **활용신청**해야 200이 떨어진다
(미신청 시 게이트웨이 에러/403). 키 자체는 계정 단위라 동일하다.
"""
from __future__ import annotations

import os


def data_go_key() -> str:
    """data.go.kr Decoding 일반 인증키. 미설정 시 RuntimeError(상위에서 fallback)."""
    k = os.environ.get("DATA_GO_KR_API_KEY") or os.environ.get("MOLIT_API_KEY")
    if not k:
        raise RuntimeError("DATA_GO_KR_API_KEY(또는 MOLIT_API_KEY) not set")
    return k
