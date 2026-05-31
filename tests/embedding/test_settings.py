"""EmbeddingSettings — parse from per-account JSON, default-disabled."""

from __future__ import annotations

from backend.embedding.settings import EmbeddingSettings


class TestFromAccountSettings:
    def test_none_when_missing(self):
        assert EmbeddingSettings.from_account_settings(None) is None
        assert EmbeddingSettings.from_account_settings({}) is None
        assert EmbeddingSettings.from_account_settings({"unrelated": 1}) is None

    def test_none_when_disabled(self):
        # ``embedding`` present but empty dict → still disabled.
        assert EmbeddingSettings.from_account_settings({"embedding": {}}) is None

    def test_minimal_config(self):
        s = EmbeddingSettings.from_account_settings(
            {"embedding": {"model": "ollama/nomic-embed-text"}}
        )
        assert s is not None
        assert s.model == "ollama/nomic-embed-text"
        assert s.api_base is None
        assert s.timeout == 10.0
        assert s.max_input_length == 8000

    def test_full_config(self):
        s = EmbeddingSettings.from_account_settings(
            {
                "embedding": {
                    "model": "text-embedding-3-small",
                    "api_base": "https://api.openai.com/v1",
                    "timeout": 30.0,
                    "max_input_length": 4096,
                }
            }
        )
        assert s is not None
        assert s.model == "text-embedding-3-small"
        assert s.api_base == "https://api.openai.com/v1"
        assert s.timeout == 30.0
        assert s.max_input_length == 4096

    def test_round_trip_via_to_dict(self):
        original = EmbeddingSettings(model="m", api_base="b", timeout=5.0, max_input_length=100)
        s = EmbeddingSettings.from_account_settings({"embedding": original.to_dict()})
        assert s == original

    def test_invalid_model_type_rejected(self):
        assert EmbeddingSettings.from_account_settings({"embedding": {"model": 42}}) is None
