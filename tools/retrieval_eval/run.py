"""Run the retrieval eval set against a REAL workspace and report hit@k / recall@k.

    python -m tools.retrieval_eval.run <workspace_id> [--sweep]

Drives the same retriever the answer paths use, so the numbers describe what the
founder actually gets — not a lab reconstruction. ``--sweep`` re-runs the set across
candidate (top_k, min_similarity) pairs so a later tuning lift argues from measured
deltas instead of taste.

Broad questions are printed with whatever they retrieved but never scored: no note
answers "현 프로젝트 상황 설명해줘", so scoring them would push us to tune retrieval
into a shape that flatters a question retrieval is the wrong tool for.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

import yaml

from backend.config import get_settings
from backend.data.session import session_scope
from backend.knowledge.factory import KnowledgeFactory
from backend.knowledge.retrieval.embedder_resolution import resolve_knowledge_embedder
from backend.knowledge.retrieval.semantic_note_retriever import SemanticNoteRetriever
from backend.knowledge.retrieval.hybrid_search import hybrid_search
from backend.knowledge.retrieval.storage.pg import PgNoteVectorBackend
from tools.retrieval_eval.scoring import QuestionResult, summarize

_EVAL_SET = Path(__file__).with_name("eval_set.yaml")

#: What the answer paths use TODAY — inherited from the verify path, which only ever
#: needed a couple of judge criteria. Every sweep is measured against this.
_CURRENT = (3, 0.5)
_SWEEP: tuple[tuple[int, float], ...] = ((3, 0.5), (8, 0.5), (8, 0.35), (12, 0.35), (12, 0.25))


async def _retrieve(
    session: Any, workspace_id: uuid.UUID, query: str, *, top_k: int, min_similarity: float
) -> list[str]:
    settings = get_settings()
    embedder = resolve_knowledge_embedder(settings)
    if not embedder.enabled or embedder.model is None:
        raise SystemExit("no knowledge embedder configured — nothing to evaluate")
    semantic = SemanticNoteRetriever(
        embedder,
        PgNoteVectorBackend(session, workspace_id=workspace_id, embedding_model=embedder.model),
        top_k=top_k,
        min_similarity=min_similarity,
    )
    items = await semantic.retrieve_structured(query)
    return [item.ref or "" for item in items]


async def _retrieve_hybrid(workspace_id: uuid.UUID, query: str, *, limit: int) -> list[str]:
    """The BUILT-BUT-UNWIRED path: BM25 + graph traversal + vector, fused with RRF
    (``hybrid_search``). It searches graph ENTITIES, not notes — but each entity
    carries the ``source_path`` of the note that produced it, so a hit maps back to
    the unit the answer paths actually ground in."""
    from backend.knowledge.graph.storage import FileSystemStorage  # noqa: PLC0415
    from backend.knowledge.graph.vault_backend import VaultBackend  # noqa: PLC0415

    settings = get_settings()
    factory = KnowledgeFactory(
        region=settings.knowledge_default_region,
        workspace_id=str(workspace_id),
        vault_root=Path(settings.knowledge_vault_root),
    )
    backend = VaultBackend(FileSystemStorage(factory.vault_path))
    await backend.initialize()

    embedder = resolve_knowledge_embedder(settings)

    async def _embed(text: str) -> list[float]:
        return list(await embedder.embed(text))

    hits = await hybrid_search(
        backend, query, limit=limit, embed_fn=_embed if embedder.enabled else None
    )
    refs: list[str] = []
    for hit in hits:
        path = getattr(hit.entity, "source_path", "") or ""
        if path and path not in refs:
            refs.append(path)
    return refs


async def _run_once(
    workspace_id: uuid.UUID, questions: list[dict[str, Any]], *, top_k: int, min_similarity: float
) -> list[QuestionResult]:
    results: list[QuestionResult] = []
    async with session_scope() as session:
        for q in questions:
            refs = await _retrieve(
                session,
                workspace_id,
                str(q["question"]),
                top_k=top_k,
                min_similarity=min_similarity,
            )
            results.append(
                QuestionResult(
                    question=str(q["question"]),
                    kind=str(q.get("kind") or "specific"),
                    expected=[str(e) for e in (q.get("expected") or [])],
                    retrieved=refs,
                )
            )
    return results


def _report(results: list[QuestionResult], *, top_k: int, min_similarity: float) -> dict[str, Any]:
    summary = summarize(results)
    print(f"\n=== top_k={top_k} min_similarity={min_similarity} ===")
    for r in results:
        if r.expected:
            hit = (
                "HIT " if any(any(s in ref for ref in r.retrieved) for s in r.expected) else "MISS"
            )
            print(f"  {hit} {r.question[:44]:<46} → {len(r.retrieved)} notes")
            if hit == "MISS":
                for ref in r.retrieved:
                    print(f"        got: {ref}")
        else:
            print(f"  ---- {r.question[:44]:<46} → {len(r.retrieved)} notes (broad, unscored)")
            for ref in r.retrieved:
                print(f"        got: {ref}")
    hr = summary.hit_rate
    mr = summary.mean_recall
    print(
        f"  hit@k={hr:.0%}  mean recall={mr:.0%}  "
        f"({summary.specific_count} specific, {summary.broad_count} broad unscored)"
        if hr is not None and mr is not None
        else "  (no specific questions)"
    )
    return {
        "top_k": top_k,
        "min_similarity": min_similarity,
        "hit_rate": hr,
        "mean_recall": mr,
        "misses": summary.misses,
        "results": [
            {"q": r.question, "kind": r.kind, "expected": r.expected, "retrieved": r.retrieved}
            for r in results
        ],
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("workspace_id")
    parser.add_argument("--sweep", action="store_true", help="also try candidate knobs")
    parser.add_argument(
        "--hybrid", action="store_true", help="also score the unwired hybrid_search path"
    )
    parser.add_argument("--out", default="", help="write the baseline JSON here")
    args = parser.parse_args()

    questions = yaml.safe_load(_EVAL_SET.read_text(encoding="utf-8"))["questions"]
    ws = uuid.UUID(args.workspace_id)

    runs = _SWEEP if args.sweep else (_CURRENT,)
    baseline: list[dict[str, Any]] = []
    for top_k, min_sim in runs:
        results = await _run_once(ws, questions, top_k=top_k, min_similarity=min_sim)
        baseline.append(_report(results, top_k=top_k, min_similarity=min_sim))

    if args.hybrid:
        results = []
        async with session_scope():
            for q in questions:
                refs = await _retrieve_hybrid(ws, str(q["question"]), limit=8)
                results.append(
                    QuestionResult(
                        question=str(q["question"]),
                        kind=str(q.get("kind") or "specific"),
                        expected=[str(e) for e in (q.get("expected") or [])],
                        retrieved=refs,
                    )
                )
        baseline.append(_report(results, top_k=8, min_similarity=-1.0))

    if args.out:
        Path(args.out).write_text(json.dumps(baseline, ensure_ascii=False, indent=2), "utf-8")
        print(f"\nbaseline → {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
