"""LLMClassifier — Ollama-backed gray-band tie-breaker.

We mock the LLM client (no live Ollama). The classifier slots into
:class:`LocalVsCloudClassifier`'s ``secondary`` parameter.
"""

from __future__ import annotations

from backend.router.classifier.base import ClassificationFeatures
from backend.router.classifier.local_vs_cloud import LocalVsCloudClassifier
from backend.router.classifier.static import StaticClassifier
from backend.router.routing.classifiers.llm import LLMClassifier


class _FakeLlm:
    """Returns whatever single-token verdict it's configured for. Records prompt."""

    def __init__(self, *, verdict: str = "local", fail: bool = False) -> None:
        self.verdict = verdict
        self.fail = fail
        self.last_prompt: str | None = None

    async def complete(self, *, prompt: str, max_tokens: int, temperature: float) -> str:
        self.last_prompt = prompt
        if self.fail:
            raise RuntimeError("model down")
        return self.verdict


def _features(**overrides) -> ClassificationFeatures:
    base = {
        "token_count": 100,
        "system_prompt_chars": 0,
        "conversation_turns": 1,
        "code_block_count": 0,
        "tool_count": 0,
        "user_text": "do this thing",
        "system_prompt": "",
    }
    base.update(overrides)
    return ClassificationFeatures(**base)


class TestVerdictParsing:
    async def test_local_verdict(self):
        clf = LLMClassifier(llm=_FakeLlm(verdict="local"), model="x", api_base=None)
        result = await clf.classify(_features())
        assert result.tier == "local"
        assert result.strategy == "llm"

    async def test_cloud_verdict(self):
        clf = LLMClassifier(llm=_FakeLlm(verdict="cloud"), model="x", api_base=None)
        result = await clf.classify(_features())
        assert result.tier == "cloud"

    async def test_unparseable_defaults_to_cloud(self):
        # Unknown verdict → default cloud (safer for unsure requests).
        clf = LLMClassifier(llm=_FakeLlm(verdict="hippopotamus"), model="x", api_base=None)
        result = await clf.classify(_features())
        assert result.tier == "cloud"
        assert result.reason == "unparseable"

    async def test_case_insensitive(self):
        clf = LLMClassifier(llm=_FakeLlm(verdict="  LOCAL  "), model="x", api_base=None)
        assert (await clf.classify(_features())).tier == "local"


class TestFailureFallback:
    async def test_failure_returns_cloud_with_reason(self):
        clf = LLMClassifier(llm=_FakeLlm(fail=True), model="x", api_base=None)
        result = await clf.classify(_features())
        # Safer default on transient LLM failure.
        assert result.tier == "cloud"
        assert result.reason == "llm_unavailable"


class TestPromptBuild:
    async def test_includes_user_text(self):
        llm = _FakeLlm(verdict="local")
        clf = LLMClassifier(llm=llm, model="x", api_base=None)
        await clf.classify(_features(user_text="please refactor the auth module"))
        assert "please refactor the auth module" in (llm.last_prompt or "")

    async def test_includes_system_prompt_when_present(self):
        llm = _FakeLlm(verdict="local")
        clf = LLMClassifier(llm=llm, model="x", api_base=None)
        await clf.classify(_features(system_prompt="You are a Rust expert."))
        assert "Rust expert" in (llm.last_prompt or "")

    async def test_truncates_long_text(self):
        # The hard caps should clamp prompt size.
        llm = _FakeLlm(verdict="local")
        clf = LLMClassifier(
            llm=llm,
            model="x",
            api_base=None,
            user_text_max=10,
            system_prompt_max=5,
        )
        await clf.classify(
            _features(
                user_text="a" * 1000,
                system_prompt="b" * 1000,
            )
        )
        # Truncated input doesn't blow up the prompt length significantly.
        assert len(llm.last_prompt or "") < 500


class TestIntegrationWithLocalVsCloud:
    async def test_gray_band_uses_llm_secondary(self):
        # Static gives a gray-band score; LLM tie-breaks to "local".
        llm_clf = LLMClassifier(llm=_FakeLlm(verdict="local"), model="x", api_base=None)
        wrapper = LocalVsCloudClassifier(
            local_score_max=30,
            cloud_score_min=70,
            static=StaticClassifier(local_score_max=30, cloud_score_min=70),
            secondary=llm_clf,
        )
        # Craft features that score in the gray band (~50).
        # StaticClassifier scoring is deterministic — token_count = 1000 + 2 code blocks tends to land mid-band.
        result = await wrapper.classify(
            _features(token_count=2000, code_block_count=3, tool_count=2)
        )
        assert result.strategy == "two_tier"
        assert "llm" in (result.reason or "")
