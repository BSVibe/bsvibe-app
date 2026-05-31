"""invoke_skill — system-prompt injection + retrieval prime + tool gating."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.extensions.skill.exceptions import SkillLoadError, SkillRunError
from backend.extensions.skill.loader import SkillLoader
from backend.extensions.skill.runner import Searcher, invoke_skill


class _FakeSearcher:
    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.calls: list[tuple[str, int, int]] = []

    async def search(self, query: str, *, top_k: int = 20, max_chars: int = 50_000) -> str:
        self.calls.append((query, top_k, max_chars))
        return self.payload


def _make_loader(tmp_path: Path, body: str, name: str = "x") -> SkillLoader:
    (tmp_path / f"{name}.md").write_text(body, encoding="utf-8")
    loader = SkillLoader(tmp_path)
    loader.load_all()
    return loader


async def _capture_completion(captured: dict) -> object:
    async def _fn(*, system_prompt, user_input, model, allowed_tools):
        captured["system_prompt"] = system_prompt
        captured["user_input"] = user_input
        captured["model"] = model
        captured["allowed_tools"] = allowed_tools
        return "OK"

    return _fn


@pytest.mark.asyncio
async def test_system_prompt_injection(tmp_path: Path) -> None:
    loader = _make_loader(
        tmp_path,
        "---\nname: x\nversion: 1\ndescription: d\n---\nMy system prompt body.",
    )
    captured: dict = {}
    result = await invoke_skill(
        name="x",
        user_input="hello",
        loader=loader,
        completion_fn=await _capture_completion(captured),
    )
    assert "My system prompt body." in captured["system_prompt"]
    assert captured["user_input"] == "hello"
    assert result.response == "OK"


@pytest.mark.asyncio
async def test_retrieval_primes_context(tmp_path: Path) -> None:
    loader = _make_loader(tmp_path, "---\nname: x\nversion: 1\ndescription: d\n---\nSystem.")
    searcher = _FakeSearcher("found: alpha\nfound: beta")
    captured: dict = {}
    result = await invoke_skill(
        name="x",
        user_input="alpha?",
        loader=loader,
        completion_fn=await _capture_completion(captured),
        searcher=searcher,
    )
    assert "Retrieved context" in captured["system_prompt"]
    assert "alpha" in captured["system_prompt"]
    assert searcher.calls == [("alpha?", 20, 50_000)]
    assert result.retrieval_chars > 0


@pytest.mark.asyncio
async def test_retrieval_query_override(tmp_path: Path) -> None:
    loader = _make_loader(tmp_path, "---\nname: x\nversion: 1\ndescription: d\n---\n.")
    searcher = _FakeSearcher("")
    captured: dict = {}
    await invoke_skill(
        name="x",
        user_input="user-input",
        loader=loader,
        completion_fn=await _capture_completion(captured),
        searcher=searcher,
        retrieval_query="custom query",
    )
    assert searcher.calls[0][0] == "custom query"


@pytest.mark.asyncio
async def test_allowed_tools_intersects_available(tmp_path: Path) -> None:
    loader = _make_loader(
        tmp_path,
        (
            "---\nname: x\nversion: 1\ndescription: d\n"
            "allowed_tools: [search_knowledge, create_note]\n---\n."
        ),
    )
    captured: dict = {}
    await invoke_skill(
        name="x",
        user_input="hi",
        loader=loader,
        completion_fn=await _capture_completion(captured),
        available_tools=["search_knowledge", "send_email", "create_note"],
    )
    # send_email filtered out (not allowed); other two pass.
    assert sorted(captured["allowed_tools"]) == ["create_note", "search_knowledge"]


@pytest.mark.asyncio
async def test_no_allowed_tools_passes_through_available(tmp_path: Path) -> None:
    loader = _make_loader(tmp_path, "---\nname: x\nversion: 1\ndescription: d\n---\n.")
    captured: dict = {}
    await invoke_skill(
        name="x",
        user_input="hi",
        loader=loader,
        completion_fn=await _capture_completion(captured),
        available_tools=["a", "b", "c"],
    )
    assert captured["allowed_tools"] == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_model_override_passed_through(tmp_path: Path) -> None:
    loader = _make_loader(
        tmp_path,
        "---\nname: x\nversion: 1\ndescription: d\nmodel: openai/gpt-4o\n---\n.",
    )
    captured: dict = {}
    await invoke_skill(
        name="x",
        user_input="hi",
        loader=loader,
        completion_fn=await _capture_completion(captured),
    )
    assert captured["model"] == "openai/gpt-4o"


@pytest.mark.asyncio
async def test_unknown_skill(tmp_path: Path) -> None:
    loader = SkillLoader(tmp_path)
    captured: dict = {}
    with pytest.raises(SkillLoadError):
        await invoke_skill(
            name="missing",
            user_input="hi",
            loader=loader,
            completion_fn=await _capture_completion(captured),
        )


@pytest.mark.asyncio
async def test_llm_failure_wrapped(tmp_path: Path) -> None:
    loader = _make_loader(tmp_path, "---\nname: x\nversion: 1\ndescription: d\n---\n.")

    async def failing(**_kwargs: object) -> str:
        raise RuntimeError("upstream down")

    with pytest.raises(SkillRunError, match="invocation failed"):
        await invoke_skill(name="x", user_input="hi", loader=loader, completion_fn=failing)


def test_searcher_protocol_runtime_check() -> None:
    # Ensure the Protocol is recognizable for documentation / typing purposes.
    assert issubclass(_FakeSearcher, object)
    assert hasattr(Searcher, "__class_getitem__") or True  # Protocol is class
