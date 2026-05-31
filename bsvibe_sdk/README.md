# bsvibe-sdk

Plugin SDK for BSVibe. **v0.1.0** (introduced by Lift S).

The SDK is the plugin-author-facing surface — Protocols, decorators,
and helper types that external plugin authors import to write a BSVibe
plugin without depending on backend internals.

Design source: `~/Docs/BSVibe_Class_Architecture_Design_2026-05-30.md`
(v8 §13 Lift S + D39 + D42).

## Status

Local-only. Not yet published to PyPI. The package layout + `pyproject.toml`
are the future-publishability artifact.

## Quickstart

```python
from bsvibe_sdk import Context, Result, plugin

p = plugin(name="github", credentials=[...], data_jurisdiction="us")

@p.action(name="open_pr", mcp_exposed=True)
async def open_pr(context: Context, *, branch: str, title: str) -> Result:
    ...
    return Result.ok({"pr_number": 42})
```

## Public surface

- `Plugin` — Protocol for a loaded plugin instance.
- `plugin(...)` — factory returning a `PluginBuilder` with capability decorators.
- `Action` — Protocol for a registered action.
- `action(...)` — standalone decorator alias (alternative to `@p.action`).
- `EventBusSubscriber` — Protocol for plugins that subscribe to engine events.
- `on_event(kind_prefix=...)` — decorator that marks a free function as a subscriber.
- `Event` — event envelope (`kind`, `payload`).
- `Context` — plugin capability call context (logger, config, credentials, input_data).
- `Result` — optional success/error envelope (`Result.ok(...)` / `Result.err(...)`).
- `__version__` — package version constant.

## Lift S scope (what is NOT yet here)

- Plugin implementations (discord, linear, notion, github, audit, ...) —
  they stay under `backend/extensions/implementations/`. Lift R relocates
  the connectors to ship alongside the SDK.
- Live event bus wiring — `on_event` attaches metadata; the engine
  doesn't route events to it until Lift N.
- PyPI publication — version constant tracks future external release.

## Design constraints

- Zero heavy dependencies (no FastAPI, SQLAlchemy, LiteLLM, structlog).
- Plugin-only per v8 §D42 — Skills are yaml + md data, not an SDK contract.
- `py.typed` per PEP 561.
