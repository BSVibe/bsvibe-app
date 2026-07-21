"""Notification push-copy catalog — localized KO/EN strings per event.

The five notification producers (``needs_you`` / ``triggered`` / ``shipped`` /
``failed`` / ``daily_brief``) render their push ``{title, body}`` HERE, keyed by
the workspace's ``workspaces.language``, instead of hard-coding English. The app
+ founder are KO-localized (PR #514/#526 made model-written prose follow
``workspaces.language``); this closes the same gap for the push channel's fixed
chrome — a KO founder no longer gets English push notifications.

These are SHORT, FIXED strings, NOT model prose, so they are a static message
catalog keyed by language — NOT the LLM ``language_directive`` generation path
(that localizes what the model writes; this localizes the app's own chrome). The
founder's OWN verbatim text (a Decision question, a deliverable's title line, a
failure reason, a trigger source) rides through UNCHANGED — only the framing
around it is localized. An unknown / missing language falls back to English.

Pure leaf module — no DB, no bounded-context imports — so every producer (worker
or write-path) can import it without an import-linter contract edge. The static
deep links live here too, so this is the single home for notification chrome;
``shipped`` / ``failed`` link to a per-row id and build their link at the
producer.
"""

from __future__ import annotations

from dataclasses import dataclass

_DEFAULT_LANGUAGE = "en"
_SUPPORTED: frozenset[str] = frozenset({"en", "ko"})

#: Static deep-link targets. ``shipped`` (``/deliverables/<id>``) and ``failed``
#: (``/runs/<id>``) link to a per-row id, so those are built at the producer.
#: ``needs_you`` points at the Brief (``/brief``) — the standalone decisions tab
#: was removed and folded into the Brief, so a bare ``/decisions`` is now a dead
#: target.
NEEDS_YOU_LINK = "/brief"
TRIGGERED_LINK = "/brief"
DAILY_BRIEF_LINK = "/brief"


@dataclass(frozen=True, slots=True)
class NotificationCopy:
    """One notification's rendered push ``title`` + ``body`` (already localized)."""

    title: str
    body: str


#: Localized TITLE (the framing) per event. Every event has one for each
#: supported language; an unknown language resolves to ``en``.
_TITLES: dict[str, dict[str, str]] = {
    "needs_you": {"en": "A run needs your decision", "ko": "결정이 필요한 작업이 있어요"},
    "triggered": {"en": "New work came in", "ko": "새 작업이 들어왔어요"},
    "shipped": {"en": "A verified deliverable shipped", "ko": "검증된 산출물이 배포됐어요"},
    "failed": {"en": "A run failed", "ko": "작업이 실패했어요"},
    "daily_brief": {"en": "Your daily brief", "ko": "오늘의 요약"},
}

#: Localized fallback BODY for the "detail-bearing" events — used only when the
#: founder's verbatim detail (question / deliverable title / failure reason) is
#: empty, so the notification still says something meaningful.
_FALLBACK_BODY: dict[str, dict[str, str]] = {
    "needs_you": {
        "en": "A run has paused and needs your input.",
        "ko": "작업이 멈췄고 결정을 기다리고 있어요.",
    },
    "shipped": {
        "en": "A verified deliverable is ready.",
        "ko": "검증된 산출물이 준비됐어요.",
    },
    "failed": {
        "en": "A run reached its failed terminal.",
        "ko": "작업이 실패 상태로 종료됐어요.",
    },
}


#: Friendly, founder-facing BODY for a SYSTEM-minted ``needs_you`` Decision that
#: carries NO founder question (verify-gate / ``human_review_required``). Keyed
#: by the Decision ``payload["reason"]`` → localized copy, so the raw English
#: honesty-gate ``decision.rationale`` ("verified but the target declares no gate
#: to run — weak evidence (grade D)") never rides out to a KO founder. An unknown
#: reason resolves to the generic ``needs_you`` fallback body above.
_NEEDS_YOU_REASON_BODY: dict[str, dict[str, str]] = {
    "weak_evidence_no_gate": {
        "en": (
            "The work is done, but I couldn't strongly verify the result — "
            "please check whether it's OK to ship as-is."
        ),
        "ko": (
            "작업을 마쳤는데, 결과가 제대로 검증됐다고 확신하기 어려워요. "
            "그대로 내보내도 될지 봐주세요."
        ),
    },
}

#: Localized CTA framing for the trailing deep-link line of a push. ``needs_you``
#: asks the founder to ANSWER; every other event asks them to REVIEW. The absolute
#: URL is appended after " → " so chat channels render a tappable link.
_CTA_PREFIX: dict[str, dict[str, str]] = {
    "answer": {"en": "Answer it in your Brief", "ko": "요약에서 답해주세요"},
    "review": {"en": "Review it in your Brief", "ko": "요약에서 확인해주세요"},
}


def _resolve_language(language: str | None) -> str:
    """Normalize to a supported tag; anything unknown / missing → ``en``."""
    lang = (language or "").strip() or _DEFAULT_LANGUAGE
    return lang if lang in _SUPPORTED else _DEFAULT_LANGUAGE


def _as_int(value: object) -> int:
    """A count param coerced to ``int`` (0 for anything non-numeric)."""
    return value if isinstance(value, int) else 0


def notification_copy(event: str, language: str | None, **params: object) -> NotificationCopy:
    """Render the localized push ``title`` + ``body`` for ``event`` in ``language``.

    Parameters per event:

    * ``needs_you`` / ``shipped`` / ``failed`` — ``detail`` (the founder's
      verbatim text: a Decision question, the deliverable's title line, or the
      failure reason). Empty ``detail`` uses the localized fallback body.
    * ``triggered`` — ``source`` (the trigger's origin, e.g. ``"sentry"``), kept
      verbatim inside the localized sentence.
    * ``daily_brief`` — ``shipped`` / ``failed`` / ``pending`` integer counts.

    An unknown / missing ``language`` falls back to English.
    """
    lang = _resolve_language(language)
    return NotificationCopy(title=_TITLES[event][lang], body=_render_body(event, lang, params))


def _render_body(event: str, lang: str, params: dict[str, object]) -> str:
    if event == "triggered":
        source = str(params.get("source") or "").strip() or (
            "외부" if lang == "ko" else "an external"
        )
        if lang == "ko":
            return f"{source} 트리거로 새 작업이 시작됐어요."
        return f"A {source} trigger started new work."

    if event == "daily_brief":
        shipped = _as_int(params.get("shipped"))
        failed = _as_int(params.get("failed"))
        pending = _as_int(params.get("pending"))
        if lang == "ko":
            return f"배포 {shipped} · 실패 {failed} · 대기 결정 {pending}"
        return f"{shipped} shipped · {failed} failed · {pending} decisions awaiting you"

    # Detail-bearing events (needs_you / shipped / failed): the founder's own
    # verbatim text when present, else the localized fallback.
    detail = str(params.get("detail") or "").strip()
    return detail or _FALLBACK_BODY[event][lang]


def needs_you_reason_body(reason: str, language: str | None) -> str:
    """Localized ``needs_you`` body for a SYSTEM-minted Decision (no founder question).

    A ``human_review_required`` / verify-gate Decision has no founder question, so
    its ``needs_you`` body is derived from the machine ``reason`` (not the English
    ``decision.rationale``). A KNOWN reason maps to warm, founder-facing localized
    copy; any UNKNOWN reason resolves to the generic ``needs_you`` fallback body —
    so no raw English honesty-gate jargon ever reaches the founder. An unknown /
    missing ``language`` falls back to English.
    """
    lang = _resolve_language(language)
    mapping = _NEEDS_YOU_REASON_BODY.get(reason or "")
    if mapping is not None:
        return mapping[lang]
    return _FALLBACK_BODY["needs_you"][lang]


def notification_cta(event: str, language: str | None, base_url: str, path: str) -> str:
    """Render the trailing deep-link line as a localized, tappable CTA.

    Turns a bare relative deep-link ``path`` (``/brief``, ``/deliverables/<id>``,
    …) into ``"<localized call-to-action> → <base_url><path>"`` so Telegram / Slack
    render an absolute clickable URL instead of a bare ``/decisions`` fragment.
    ``needs_you`` asks the founder to ANSWER; every other event asks them to
    REVIEW. An unknown / missing ``language`` falls back to English.
    """
    lang = _resolve_language(language)
    kind = "answer" if event == "needs_you" else "review"
    url = f"{base_url.rstrip('/')}{path}"
    return f"{_CTA_PREFIX[kind][lang]} → {url}"


__all__ = [
    "DAILY_BRIEF_LINK",
    "NEEDS_YOU_LINK",
    "TRIGGERED_LINK",
    "NotificationCopy",
    "needs_you_reason_body",
    "notification_copy",
    "notification_cta",
]
