"""측정 전용 — GUC 가 빈 연결에서 강제 표를 언급하는 SQL 문장을 행 유무와 무관하게 기록한다.

연결별로 app.current_workspace_id 의 현재 값을 추적한다:
- set_config('app.current_workspace_id', v, true|false) 문장을 보면 값을 갱신
- 트랜잭션 종료(commit/rollback)에서 is_local 값은 사라진다 → 세션 값으로 되돌림
"""

from __future__ import annotations

import os
import re
import sys

from sqlalchemy import event
from sqlalchemy.engine import Engine

_ROOT = os.environ["BLINDSTMT_ROOT"].rstrip("/") + "/"  # the checkout under test
_OUT = os.environ["BLINDSTMT_OUT"]
_POLICIED = re.compile(
    r'\b(?:FROM|JOIN|INTO|UPDATE)\s+"?(workspaces|products|execution_runs|deliverables|execution_decisions|requests)"?\b',
    re.I,
)
_SET = re.compile(
    r"set_config\('app\.current_workspace_id',\s*(?:'([^']*)'|(%\(\w+\)s|\$\d+|:\w+)),\s*(true|false)\)",
    re.I,
)
_state: dict[int, dict[str, str]] = {}


def _frames():
    import greenlet  # noqa: PLC0415 — only when a statement is recorded

    f = sys._getframe(3)
    g = greenlet.getcurrent()
    while True:
        while f is not None:
            yield f
            f = f.f_back
        g = g.parent
        if g is None:
            return
        f = g.gr_frame


def _site() -> str:
    backend = test = entry = "-"
    for f in _frames():
        fn = f.f_code.co_filename
        if _ROOT in fn:
            rel = fn.split(_ROOT, 1)[1]
            if backend == "-" and rel.startswith(("backend/", "plugin/")) and "/data/" not in rel:
                backend = f"{rel}:{f.f_lineno}:{f.f_code.co_name}"
            if rel.startswith(("backend/", "plugin/")):
                entry = f"{rel}:{f.f_code.co_name}"
            if test == "-" and rel.startswith("tests/"):
                test = f"{rel}:{f.f_code.co_name}"
                break
    return f"{backend}|{entry}|{test}"


def _key(conn) -> int:
    return id(conn.connection.dbapi_connection)


@event.listens_for(Engine, "before_cursor_execute")
def _before(conn, cursor, statement, parameters, context, executemany):
    if conn.dialect.name != "postgresql":
        return
    st = _state.setdefault(_key(conn), {"local": None, "session": ""})
    m = _SET.search(statement)
    if m:
        lit, _param, local = m.groups()
        val = lit if lit is not None else _first_param(parameters)
        if local.lower() == "true":
            st["local"] = val
        else:
            st["session"] = val
            st["local"] = None
        return
    if not _POLICIED.search(statement):
        return
    guc = st["local"] if st["local"] is not None else st["session"]
    if (guc or "") == "":
        one = re.sub(r"\s+", " ", statement)[:400]
        with open(_OUT, "a") as fh:
            fh.write(_site() + "|" + one + "\n")


def _first_param(parameters):
    if isinstance(parameters, dict):
        return str(next(iter(parameters.values()), ""))
    if isinstance(parameters, (list, tuple)) and parameters:
        p = parameters[0]
        return str(
            p
            if not isinstance(p, (list, tuple, dict))
            else (list(p.values())[0] if isinstance(p, dict) else p[0])
        )
    return ""


def _end(conn):
    st = _state.get(_key(conn))
    if st:
        st["local"] = None


event.listen(Engine, "commit", _end)
event.listen(Engine, "rollback", _end)
