"""#929 (B), stage 2 PR 2e — vault and skills code does no file I/O of its own.

With ``knowledge_store_backend = postgres`` the vault and the skills are ``vault_documents``
rows, and every machine sees the same ones. A direct ``Path`` read or write in that code would
reach this machine's disk instead — silently, and only once there is more than one machine.
So the code that handles them goes through a :class:`StorageBackend` (``Vault.read`` / ``write``
/ ``append`` / ``create`` / …), and this guard keeps new file I/O out.

The exceptions are listed, each with the reason it is not vault I/O.
"""

from __future__ import annotations

import ast
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]

#: Code that reads or writes the vault or the skills.
_SCOPE: tuple[str, ...] = (
    "backend/knowledge",
    "backend/extensions/skill",
    "backend/mcp/tools/knowledge_tools.py",
    "backend/mcp/tools/knowledge_retraction_tools.py",
    "backend/mcp/tools/graph_tools.py",
    "backend/mcp/tools/skills_tools.py",
    "backend/mcp/tools/decisions_tools.py",
    "backend/mcp/tools/reindex_tools.py",
    "backend/mcp/tools/workflow_tools.py",
    "backend/api/v1/skills.py",
    "backend/api/v1/inside",
    "backend/api/v1/decisions",
    "backend/api/v1/checkpoints.py",
    "backend/workflow/application/runtime/settle_runtime.py",
)

#: File-system calls (method or function names).
_FS_CALLS = {
    "write_text",
    "read_text",
    "write_bytes",
    "read_bytes",
    "unlink",
    "rglob",
    "glob",
    "iterdir",
    "mkdir",
    "rename",
    "rmtree",
    "is_file",
    "is_dir",
    "open",
}

#: Where file I/O is the point, with why it is not vault I/O.
_ALLOWED: dict[str, str] = {
    "backend/knowledge/graph/storage.py": "the filesystem store itself",
    "backend/knowledge/vault_store.py": "creates the directory of a filesystem store",
    "backend/knowledge/store.py": "the ops command that copies disk <-> rows",
    "backend/knowledge/graph/vault.py": (
        "on-disk only: ensure_dirs and the append fast path, both under FileSystemStorage"
    ),
    "backend/knowledge/code_graph/graph.py": (
        "save_graph/load_graph file helpers; the vault path uses dump_graph/parse_graph"
    ),
    "backend/knowledge/code_graph/pipeline.py": "reads the product repo being bootstrapped",
    "backend/knowledge/retrieval/ontology.py": "OntologyRegistry is not built in production",
    "backend/extensions/skill/loader.py": (
        "load_all reads a directory (tests); production loads from the store"
    ),
}


def _files() -> list[Path]:
    out: list[Path] = []
    for entry in _SCOPE:
        path = _REPO / entry
        out += sorted(path.rglob("*.py")) if path.is_dir() else [path]
    return out


def _fs_calls(tree: ast.AST) -> list[tuple[int, str]]:
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = (
            fn.attr if isinstance(fn, ast.Attribute) else fn.id if isinstance(fn, ast.Name) else ""
        )
        if name in _FS_CALLS:
            hits.append((node.lineno, name))
    return hits


def test_vault_and_skills_code_does_no_file_io_of_its_own() -> None:
    offenders = []
    for path in _files():
        rel = path.relative_to(_REPO).as_posix()
        if rel in _ALLOWED:
            continue
        offenders += [
            f"{rel}:{line} {name}()"
            for line, name in _fs_calls(ast.parse(path.read_text(encoding="utf-8")))
        ]

    assert offenders == [], (
        "go through the vault's StorageBackend (Vault.read/write/append/create/…):\n"
        + "\n".join(offenders)
    )


def test_every_exception_still_exists() -> None:
    """A stale exception is a hole someone could walk a new file through."""
    for rel in _ALLOWED:
        assert (_REPO / rel).is_file(), rel
