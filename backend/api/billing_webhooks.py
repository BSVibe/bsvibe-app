"""``POST /api/webhooks/paddle`` — Paddle subscription events (#928 step 2).

Public; a delivery is Paddle's because its ``Paddle-Signature`` verifies against the configured
secret. All of the meaning lives in :mod:`backend.identity.billing_paddle`.

* 404 — no secret configured: this deployment sells nothing
* 401 — signature missing, wrong or stale
* 200 — applied, or acknowledged without effect (duplicate, stale, not a subscription event,
  unknown workspace). Paddle retries every non-2xx, and a retry cannot fix any of those.
"""

from __future__ import annotations

import json
import time
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_db_session
from backend.config import get_settings
from backend.identity.billing_paddle import (
    handle_subscription_event,
    parse_subscription_event,
    verify_signature,
)

logger = structlog.get_logger(__name__)

router = APIRouter()


@router.post("/webhooks/paddle")
async def receive_paddle_webhook(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> Any:
    settings = get_settings()
    if not settings.paddle_webhook_secret:
        return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"detail": "Not Found"})
    body = await request.body()
    if not verify_signature(
        request.headers.get("paddle-signature"),
        body,
        secret=settings.paddle_webhook_secret,
        now=time.time(),
        tolerance_s=settings.paddle_signature_tolerance_s,
    ):
        logger.info("paddle_webhook_signature_rejected")
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"detail": "signature verification failed"},
        )
    event = parse_subscription_event(json.loads(body))
    if event is None:
        return {"outcome": "ignored"}
    outcome = await handle_subscription_event(
        session, event, pro_price_ids=frozenset(settings.paddle_pro_price_ids)
    )
    await session.commit()
    return {"outcome": outcome}


__all__ = ["router"]
