"""LiteLLM ``async_pre_call_hook`` + ChatService dispatcher (Bundle 1.5c).

Skeleton — concrete lift of BSGateway's ``routing/hook.py`` + ``chat/service.py``
lands after Bundle G provides the workers + AsyncSession threading required.
The two surfaces are pure dispatch logic; what's pending is the connective
tissue (request-scoped session, account resolution, audit emit context).
"""

from __future__ import annotations

from backend.api.litellm_hook.chat_service import ChatService
from backend.api.litellm_hook.hook import LiteLLMHook

__all__ = ["ChatService", "LiteLLMHook"]
