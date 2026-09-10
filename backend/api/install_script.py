"""``GET /install-worker.sh`` — the worker installer served for ``curl | sh``.

게이트 2. The founder connects a worker with a GitHub-Actions-runner-style
one-liner (``curl -fsSL https://api.bsvibe.dev/install-worker.sh | sh``); the
frontend's worker-connect card shows exactly that. The script is a static file
under ``backend/api/static``; this public route streams it with a shell
content-type. No auth: obtaining the installer is not privileged (it handles no
secret — registration authenticates interactively afterwards via ``bsvibe login``).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Response

router = APIRouter()

_SCRIPT_PATH = Path(__file__).parent / "static" / "install-worker.sh"


@router.get("/install-worker.sh")
async def install_worker_script() -> Response:
    """Serve the worker installer as ``text/x-shellscript`` for ``curl | sh``."""
    return Response(
        content=_SCRIPT_PATH.read_text(encoding="utf-8"),
        media_type="text/x-shellscript",
        headers={"Cache-Control": "public, max-age=300"},
    )
