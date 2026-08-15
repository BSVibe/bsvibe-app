"""신호 토큰화 — 정밀 검색기 세 개가 공유하는 하나의 문법 (트랙 A-0).

세 검색기(``canon`` · ``resolved_decisions`` · ``negative_pattern``)가 각자
``[a-z0-9]+`` 를 들고 있었고, 문서는 서로 "같은 문법" 이라고 말하지만 실제로는
이미 갈라져 있었다(``len > 1`` vs ``len >= 3``, stopword 유무). 여기로 모은다.

**왜 이 모듈이 생겼나.** 형님은 한국어로 가르치는데 토크나이저가 ASCII 전용이라,
prod 에서 실제로 포착된 교정을 넣으면 양쪽 토큰이 ``set()`` 이 나온다. 겹침이
비면 검색기가 ``continue`` 하므로 **그 교정은 영원히 표면화되지 않는다** — ratchet(§5)
이 형님 언어로는 존재하지 않았다는 뜻이다.

**설계 판단**

* **문자 bigram.** 형태소 분석기(mecab/konlpy)는 시스템 패키지 의존이라 컨테이너·CI 에
  무게를 얹는다. 공백 분절은 조사 때문에 실패한다 — ``명세는`` / ``명세를`` / ``명세가``
  가 전부 다른 토큰이 된다. bigram 은 ``명세`` 가 셋 다에서 나오므로 그 문제를 자연히 푼다.
* **ASCII 회귀 0.** 각 검색기의 기존 파라미터(``min_len`` / ``stopwords``)를 인자로 받는다.
  CJK 문법만 공유하고 ASCII 동작은 호출자별로 그대로다.
* **CJK 는 겹침 하나로 통과하지 못한다.** bigram 은 우연 겹침이 늘어나므로 ASCII 의
  "하나라도 겹치면 통과" 를 그대로 옮기면 노이즈가 쏟아진다 — 그건 `df66a253`
  (무관한 기준이 정상 작업을 죽인 사건)를 한국어로 재현하는 것이다.
"""

from __future__ import annotations

import re

#: ASCII 토큰 — 기존 세 검색기의 문법 그대로.
_ASCII_RE = re.compile(r"[a-z0-9]+")

#: 한글 음절 + 가나 + 한자. 공백/구두점으로 끊긴 연속 구간을 뽑는다.
_CJK_RE = re.compile(r"[가-힣぀-ヿ一-鿿]+")

#: CJK 토큰임을 표시하는 접두. 겹침 판정이 ASCII 와 CJK 를 구분해야 하는데,
#: 토큰 내용으로 되짚는 것은 (문서가 다른 곳에서 "structural smell" 이라 부른)
#: 문자열 역파싱이다. 표식을 붙여 구분을 1급으로 만든다.
_CJK_MARK = "\x00"

#: CJK 전용 겹침일 때 요구하는 최소 공유 토큰 수.
#: 1 이면 ``추가`` 같은 범용어 하나로 무관한 노트가 딸려온다.
_MIN_CJK_OVERLAP = 2


def _cjk_bigrams(text: str) -> set[str]:
    """연속 CJK 구간의 문자 bigram. 한 글자짜리 구간(조사 ``의``·``에``)은 버린다."""
    out: set[str] = set()
    for run in _CJK_RE.findall(text):
        for i in range(len(run) - 1):
            out.add(_CJK_MARK + run[i : i + 2])
    return out


def is_cjk_token(token: str) -> bool:
    """이 토큰이 CJK bigram 인가."""
    return token.startswith(_CJK_MARK)


def tokenize(
    text: str,
    *,
    min_len: int = 3,
    stopwords: frozenset[str] = frozenset(),
) -> set[str]:
    """``text`` 의 salient 토큰 — ASCII 단어 + CJK bigram.

    ``min_len`` / ``stopwords`` 는 **ASCII 에만** 적용된다(호출자별 기존 동작 보존).
    CJK 는 bigram 길이가 고정이고, 범용어 억제는 stopword 가 아니라 겹침 하한
    (:func:`overlaps`)이 맡는다 — 한국어 불용어 목록을 손으로 유지하는 것보다
    덜 깨지고, deny-list 를 늘리지 않는다.
    """
    ascii_tokens = {
        t for t in _ASCII_RE.findall(text.casefold()) if len(t) >= min_len and t not in stopwords
    }
    return ascii_tokens | _cjk_bigrams(text)


def overlaps_tokens(left: set[str], right: set[str]) -> bool:
    """이미 토큰화된 두 집합의 겹침 판정 — 검색기가 신호를 한 번만 토큰화하도록.

    * **ASCII 가 하나라도 겹치면 통과** — 기존 동작 그대로(회귀 0).
    * **CJK 만 겹칠 때는** :data:`_MIN_CJK_OVERLAP` 개 이상이어야 한다.
    """
    shared = left & right
    if not shared:
        return False
    if any(not is_cjk_token(t) for t in shared):
        return True
    return len(shared) >= _MIN_CJK_OVERLAP


def overlaps(
    left: str,
    right: str,
    *,
    min_len: int = 3,
    stopwords: frozenset[str] = frozenset(),
) -> bool:
    """두 텍스트가 검색을 트리거할 만큼 겹치는가.

    * **ASCII 가 하나라도 겹치면 통과** — 기존 동작 그대로(회귀 0).
    * **CJK 만 겹칠 때는** :data:`_MIN_CJK_OVERLAP` 개 이상이어야 한다.
    """
    return overlaps_tokens(
        tokenize(left, min_len=min_len, stopwords=stopwords),
        tokenize(right, min_len=min_len, stopwords=stopwords),
    )


__all__ = ["is_cjk_token", "overlaps", "overlaps_tokens", "tokenize"]
