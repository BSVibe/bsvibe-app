"""DangerAnalyzer — auto-detect dangerous plugin code via static AST + LLM fallback.

Lifted from ``bsage/core/danger_analyzer.py`` with only the import path
rewritten; the surface (``async analyze(name, code, description)``) is
preserved verbatim per Workflow §6 #4 "Reused unchanged".
"""

from __future__ import annotations

import ast
import hashlib
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

_DANGEROUS_MODULES = frozenset(
    {
        # HTTP clients
        "requests",
        "httpx",
        "aiohttp",
        "urllib",
        "urllib2",
        "urllib3",
        # Network / low-level
        "socket",
        "ftplib",
        "imaplib",
        "poplib",
        # Subprocess / OS execution
        "subprocess",
        # Messaging SDKs
        "telegram",
        "slack_sdk",
        "twilio",
        "sendgrid",
        "smtplib",
        "paramiko",
        # Cloud SDKs
        "boto3",
        "botocore",
    }
)

_DANGEROUS_TOP_MODULES = frozenset(m.split(".")[0] for m in _DANGEROUS_MODULES)

_LLMFn = Callable[[str], Awaitable[str]]

_LLM_PROMPT = """\
Analyze the following Python plugin code for dangerous external side effects.

DANGEROUS: makes HTTP calls, sends messages/emails, runs subprocesses, or communicates \
with external services outside the local vault.
SAFE: only reads/writes vault data via context, calls context.llm, \
or performs purely local computation.

Reply ONLY with valid JSON (no markdown): {{"is_dangerous": true/false, "reason": "one sentence"}}

Plugin name: {name}
Plugin description: {description}
Code:
```python
{code}
```"""


class StaticAnalyzer:
    """AST-based detection of dangerous import patterns in plugin Python code."""

    def analyze(self, code: str) -> tuple[bool, str] | None:
        try:
            tree = ast.parse(code)
        except SyntaxError as exc:
            logger.warning("static_analysis_parse_failed", error=str(exc))
            return None

        found: list[str] = []

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    if top in _DANGEROUS_TOP_MODULES:
                        found.append(f"imports '{alias.name}'")
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                top = module.split(".")[0]
                if top in _DANGEROUS_TOP_MODULES:
                    found.append(f"imports from '{module}'")

        if found:
            return True, "External communication detected: " + "; ".join(found)
        return False, "No dangerous import patterns detected"


class DangerCache:
    """Persistent JSON cache of danger analysis results, keyed by content hash."""

    def __init__(self, cache_path: Path) -> None:
        self._path = cache_path
        self._data: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self._data = {}

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(self._data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("danger_cache_save_failed", error=str(exc))

    @staticmethod
    def _hash(content: str) -> str:
        return "sha256:" + hashlib.sha256(content.encode()).hexdigest()[:16]

    def get(self, name: str, content: str) -> tuple[bool, str] | None:
        entry = self._data.get(name)
        if entry and entry.get("hash") == self._hash(content):
            return bool(entry["is_dangerous"]), str(entry["reason"])
        return None

    def set(self, name: str, content: str, result: tuple[bool, str]) -> None:
        is_dangerous, reason = result
        self._data[name] = {
            "hash": self._hash(content),
            "is_dangerous": is_dangerous,
            "reason": reason,
        }
        self._save()


class DangerAnalyzer:
    """Determines whether a plugin is dangerous: cache → static AST → LLM fallback."""

    def __init__(
        self,
        cache_path: Path,
        llm_fn: _LLMFn | None = None,
    ) -> None:
        self._cache = DangerCache(cache_path)
        self._static = StaticAnalyzer()
        self._llm_fn = llm_fn

    async def analyze(self, name: str, code: str, description: str) -> tuple[bool, str]:
        cached = self._cache.get(name, code)
        if cached is not None:
            logger.debug("danger_cache_hit", plugin=name, is_dangerous=cached[0])
            return cached

        static_result = self._static.analyze(code)
        if static_result is not None:
            logger.info(
                "danger_static_result",
                plugin=name,
                is_dangerous=static_result[0],
                reason=static_result[1],
            )
            self._cache.set(name, code, static_result)
            return static_result

        if self._llm_fn is not None:
            llm_result = await self._llm_analyze(name, code, description)
            logger.info(
                "danger_llm_result",
                plugin=name,
                is_dangerous=llm_result[0],
                reason=llm_result[1],
            )
            self._cache.set(name, code, llm_result)
            return llm_result

        fallback: tuple[bool, str] = (
            True,
            "Static analysis failed and no LLM available — defaulting to dangerous",
        )
        logger.warning("danger_analysis_fallback", plugin=name)
        self._cache.set(name, code, fallback)
        return fallback

    async def _llm_analyze(self, name: str, code: str, description: str) -> tuple[bool, str]:
        assert self._llm_fn is not None
        prompt = _LLM_PROMPT.format(name=name, description=description, code=code[:4000])
        try:
            response = (await self._llm_fn(prompt)).strip()
            if response.startswith("```"):
                response = response.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            parsed = json.loads(response)
            return bool(parsed["is_dangerous"]), str(parsed.get("reason", "LLM analysis"))
        except Exception as exc:
            logger.warning("danger_llm_parse_failed", plugin=name, error=str(exc))
            return True, f"LLM analysis failed ({exc}) — defaulting to dangerous"
