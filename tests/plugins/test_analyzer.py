"""Tests for backend.plugins.analyzer — AST-based + LLM fallback danger detection."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from backend.plugins.analyzer import DangerAnalyzer, StaticAnalyzer


class TestStaticAnalyzer:
    def test_detects_httpx_import(self):
        result = StaticAnalyzer().analyze("import httpx\n")
        assert result is not None
        is_dangerous, reason = result
        assert is_dangerous is True
        assert "httpx" in reason

    def test_detects_from_import_of_subprocess(self):
        result = StaticAnalyzer().analyze("from subprocess import run\n")
        assert result is not None
        assert result[0] is True

    def test_safe_code_passes(self):
        code = "def f():\n    return 1 + 2\n"
        result = StaticAnalyzer().analyze(code)
        assert result is not None
        assert result[0] is False

    def test_parse_error_returns_none(self):
        result = StaticAnalyzer().analyze("def broken(:")
        assert result is None

    def test_detects_socket_import(self):
        result = StaticAnalyzer().analyze("import socket\n")
        assert result is not None
        assert result[0] is True

    def test_dotted_import_detected(self):
        result = StaticAnalyzer().analyze("import urllib.request\n")
        assert result is not None
        assert result[0] is True


class TestDangerAnalyzerCache:
    async def test_static_result_cached(self, tmp_path: Path):
        analyzer = DangerAnalyzer(cache_path=tmp_path / "danger.json", llm_fn=None)
        code = "import httpx\n"

        r1 = await analyzer.analyze("p", code, "desc")
        r2 = await analyzer.analyze("p", code, "desc")
        assert r1 == r2 == (True, r1[1])

    async def test_cache_invalidated_on_content_change(self, tmp_path: Path):
        analyzer = DangerAnalyzer(cache_path=tmp_path / "danger.json", llm_fn=None)
        r1 = await analyzer.analyze("p", "import httpx\n", "")
        r2 = await analyzer.analyze("p", "x = 1\n", "")
        assert r1[0] is True
        assert r2[0] is False


class TestDangerAnalyzerLLMFallback:
    async def test_calls_llm_when_ast_parse_fails(self, tmp_path: Path):
        llm = AsyncMock(return_value='{"is_dangerous": false, "reason": "all good"}')

        analyzer = DangerAnalyzer(cache_path=tmp_path / "danger.json", llm_fn=llm)
        is_dangerous, reason = await analyzer.analyze("p", "def broken(:", "")

        assert is_dangerous is False
        assert reason == "all good"
        llm.assert_awaited_once()

    async def test_llm_default_dangerous_on_parse_failure(self, tmp_path: Path):
        llm = AsyncMock(return_value="not json at all")

        analyzer = DangerAnalyzer(cache_path=tmp_path / "danger.json", llm_fn=llm)
        is_dangerous, _ = await analyzer.analyze("p", "def broken(:", "")

        assert is_dangerous is True

    async def test_no_llm_means_default_dangerous_when_ast_fails(self, tmp_path: Path):
        analyzer = DangerAnalyzer(cache_path=tmp_path / "danger.json", llm_fn=None)
        is_dangerous, reason = await analyzer.analyze("p", "def broken(:", "")
        assert is_dangerous is True
        assert "no llm" in reason.lower()

    async def test_llm_not_called_when_static_succeeds(self, tmp_path: Path):
        llm = AsyncMock()
        analyzer = DangerAnalyzer(cache_path=tmp_path / "danger.json", llm_fn=llm)
        await analyzer.analyze("p", "x = 1\n", "")
        llm.assert_not_awaited()

    async def test_llm_strips_markdown_fence(self, tmp_path: Path):
        llm = AsyncMock(
            return_value='```json\n{"is_dangerous": true, "reason": "x"}\n```',
        )
        analyzer = DangerAnalyzer(cache_path=tmp_path / "danger.json", llm_fn=llm)
        is_dangerous, reason = await analyzer.analyze("p", "def broken(:", "")
        assert is_dangerous is True
        assert reason == "x"


@pytest.mark.parametrize(
    "mod",
    ["requests", "aiohttp", "smtplib", "boto3", "paramiko", "subprocess"],
)
def test_known_dangerous_modules(mod):
    result = StaticAnalyzer().analyze(f"import {mod}\n")
    assert result is not None
    assert result[0] is True
