"""The output-language directive follows the USER's language — always.

Two defects this pins, both observed in prod (2026-08-18):

1. **English was a silent exception.** ``language_directive`` returned ``""``
   for the default language, so an English workspace's model got NO language
   instruction at all. The workspace language is a USER SETTING; English is one
   of its values, not an absence of one.

2. **The directive named a language but never forbade leaving it.** 12 August
   runs produced turns mixing the workspace language with Japanese — one leaked
   all the way to a founder-visible deliverable summary ("stream_consumers.py
   を新規作成し、…"). Every such turn ALSO contained the workspace language, so
   the directive was reaching the model; it just said "write in X" and said
   nothing about not switching mid-response.
"""

from __future__ import annotations

import pytest

from backend.identity.output_language import (
    current_output_language,
    language_directive,
    set_output_language,
)


@pytest.fixture(autouse=True)
def _reset_language():  # noqa: ANN202
    yield
    set_output_language("en")


@pytest.mark.parametrize("lang", ["en", "ko", "ja", "pt-BR"])
def test_every_user_language_gets_a_directive(lang: str) -> None:
    """No language is an exception — including the default.

    English used to return "" ("zero prompt overhead"), which is not zero
    instruction: it left the model free to answer an English workspace in any
    language at all."""
    directive = language_directive(lang)
    assert directive.strip(), f"{lang} got no directive"


def test_directive_names_the_users_language_not_a_hardcoded_one() -> None:
    """The instruction is parameterised by the workspace setting. A language the
    name table does not know still gets a usable instruction from its tag."""
    assert "English" in language_directive("en")
    assert "Korean" in language_directive("ko")
    # Unknown tag → the tag itself is still a usable instruction, and it must not
    # smuggle in some other language's name.
    unknown = language_directive("pt-BR")
    assert "pt-BR" in unknown
    assert "Korean" not in unknown and "English" not in unknown


@pytest.mark.parametrize("lang", ["en", "ko"])
def test_directive_forbids_switching_language_mid_response(lang: str) -> None:
    """Naming a language is not enough — prod drifted INTO Japanese while still
    writing the workspace language in the same response. The instruction has to
    say "do not switch", or a long tool-heavy generation wanders."""
    directive = language_directive(lang).lower()
    assert "only" in directive or "do not" in directive, directive
    assert "switch" in directive or "other language" in directive, directive


def test_code_and_identifiers_stay_verbatim() -> None:
    """Scope guard — the directive must keep exempting code so it never asks the
    model to translate identifiers or shell commands."""
    for lang in ("en", "ko"):
        directive = language_directive(lang)
        assert "code" in directive.lower()


def test_contextvar_drives_the_directive_when_no_argument_given() -> None:
    set_output_language("ko")
    assert current_output_language() == "ko"
    assert "Korean" in language_directive()


def test_missing_or_blank_language_falls_back_to_the_default() -> None:
    set_output_language(None)
    assert current_output_language() == "en"
    set_output_language("   ")
    assert current_output_language() == "en"
    # …and the fallback is still a real instruction, not an empty string.
    assert language_directive().strip()
