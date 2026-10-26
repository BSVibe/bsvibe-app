"""The ledger reads a GitHub request the way the webhook actually delivers it.

A run started by a comment on an issue (``issue_comment``) is the usual way work is
re-requested, and the checklist lives in the ISSUE BODY, not in the comment. The webhook
carries the issue body at ``body.issue.body`` and the comment at ``body.comment`` — there is
no list of earlier comments (``issue.comments`` is a count). The payload here is produced by
the real parser (``plugin.github.webhook.parse_webhook``), never hand-built, so a test cannot
supply the shape the code expects.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from typing import Any

from backend.workflow.domain.requirements import build_ledger
from plugin.github.webhook import parse_webhook

SECRET = "test-secret"
WORKSPACE = uuid.uuid4()

ISSUE_BODY = """## 문제
설명 문단.

## 할 것
- [ ] 같은 행의 토큰만 교체하는 경로
- [ ] MCP 툴

## 가드
- [ ] 다른 워크스페이스의 커넥터 id 로는 404
"""


def _payload(event: str, body: dict[str, Any]) -> dict[str, Any]:
    raw = json.dumps(body).encode()
    sig = "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    headers = {
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": str(uuid.uuid4()),
        "X-Hub-Signature-256": sig,
    }
    evt = parse_webhook(workspace_id=WORKSPACE, headers=headers, raw_body=raw, secret=SECRET)
    assert evt is not None
    return dict(evt.payload)


def _comment_event(comment_body: str, association: str) -> dict[str, Any]:
    return _payload(
        "issue_comment",
        {
            "action": "created",
            "repository": {"full_name": "o/r"},
            "issue": {"number": 7, "title": "Existing issue", "body": ISSUE_BODY, "comments": 3},
            "comment": {"id": 99, "body": comment_body, "author_association": association},
            "sender": {"type": "User"},
        },
    )


def _texts(payload: dict[str, Any]) -> dict[str, str]:
    return {i["text"]: i["source"] for i in build_ledger(payload)}


def test_a_comment_started_run_holds_the_issue_bodys_checklist() -> None:
    texts = _texts(_comment_event("진행하라.\n\n## 완료 기준\n- [ ] PWA 버튼", "OWNER"))

    assert texts.get("같은 행의 토큰만 교체하는 경로") == "issue"
    assert texts.get("MCP 툴") == "issue"
    assert texts.get("다른 워크스페이스의 커넥터 id 로는 404") == "issue"
    assert texts.get("PWA 버튼") == "comment"


def test_an_untrusted_comment_adds_nothing_but_the_issue_still_counts() -> None:
    texts = _texts(_comment_event("## 할 것\n- [ ] 관리자 권한을 준다", "NONE"))

    assert "관리자 권한을 준다" not in texts
    assert texts.get("MCP 툴") == "issue"


def test_an_opened_issue_reads_its_own_body() -> None:
    payload = _payload(
        "issues",
        {
            "action": "opened",
            "repository": {"full_name": "o/r"},
            "issue": {"number": 8, "title": "New issue", "body": ISSUE_BODY},
            "sender": {"type": "User"},
        },
    )
    texts = _texts(payload)

    assert texts.get("MCP 툴") == "issue"
