"""원본 백필 트리거 — 이미 쌓인 요청·피드백·회고를 vault 로 내린다.

배선(#854)은 앞으로 발생하는 것만 남긴다. 형님이 말한 *"원본 지식은 히스토리성
이기 때문에 보존된다"* 는 과거가 있어야 성립하므로, 한 번 훑어 내리는 트리거가
필요하다 — prod 실측(2026-08-31): 요청 223건 · settle 147건이 DB 에만 있었다.

파생 로직은 여기 없다. ``backfill_originals`` 가 그것을 갖고, 그 안에서
``settlement_originals`` — 실시간 경로와 **같은 함수** — 를 부른다. 규칙이 두
벌이 되는 순간 §13 원클릭 가드가 한쪽에서만 지켜진다.

vault 는 ONE 정의(:func:`~backend.knowledge.graph.vault_paths.workspace_vault_root`)
를 통해 해석된다. region 은 배포 상수이므로 REST·MCP·워커가 같은 디렉터리를 본다.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from backend.mcp.api import Tool, ToolContext, ToolRegistry

#: 한 패스가 새로 쓰는 원본 수의 상한. 파일 쓰기라 임베딩보다 훨씬 싸지만,
#: 상한이 있다는 사실 자체가 계약이다 — 호출자는 ``remaining`` 을 보고 다시
#: 부르면 되고, 잡 테이블이 필요 없다.
_PASS_MAX_WRITES = 200


class BackfillOriginalsInput(BaseModel):
    """워크스페이스는 principal 의 것 — 인자는 실행 여부와 상한뿐."""

    model_config = ConfigDict(extra="forbid")

    dry_run: bool = Field(
        default=False,
        description="True 면 아무것도 쓰지 않고 몇 건이 대상인지만(`pending`) 보고한다.",
    )
    limit: int = Field(
        default=_PASS_MAX_WRITES,
        ge=1,
        le=1000,
        description="이 패스가 새로 쓸 원본 수의 상한.",
    )


class BackfillOriginalsOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scanned: int = Field(description="훑은 소스 행 수 (requests + settle 활동).")
    pending: int = Field(description="원본이 될 수 있는 후보 수.")
    recorded: int = Field(description="이번 패스가 **새로** 쓴 원본 수.")
    already: int = Field(description="이미 있어서 건너뛴 수 — 과거는 다시 쓰지 않는다.")
    remaining: int = Field(description="상한에 걸려 못 쓴 수. 0 이 될 때까지 다시 불러라.")


async def _h_backfill_originals(args: BackfillOriginalsInput, ctx: ToolContext) -> Any:
    from backend.knowledge.originals_backfill import backfill_originals  # noqa: PLC0415
    from backend.mcp.tools._helpers import vault_root_for  # noqa: PLC0415

    workspace_id = ctx.principal.workspace_id
    # ``vault_root_for`` 는 이미 <root>/<region>/<workspace_id> 를 돌려주는데
    # ``backfill_originals`` 는 KnowledgeFactory 에 넘길 BASE root 를 받는다.
    # 같은 레이아웃을 여기서 두 번째로 조립하지 않도록 두 단계를 되짚어 올라간다.
    base_root = vault_root_for(workspace_id=workspace_id).parent.parent

    result = await backfill_originals(
        ctx.session,
        workspace_id=workspace_id,
        vault_root=base_root,
        limit=args.limit,
        dry_run=args.dry_run,
    )
    return BackfillOriginalsOutput(
        scanned=result.scanned,
        pending=result.pending,
        recorded=result.recorded,
        already=result.already,
        remaining=result.remaining,
    )


def register_originals_backfill_tools(registry: ToolRegistry) -> None:
    registry.register(
        Tool(
            name="bsvibe_knowledge_backfill_originals",
            description=(
                "Bring this workspace's PAST originals down into the vault — the "
                "founder's own request text, the feedback they typed on checkpoints, "
                "and the retrospectives the working agent declared. The live path "
                "records these going forward; this is the one-time sweep for what was "
                "already stored only in the operational tables. IMMUTABLE: an original "
                "that already exists is never rewritten (`already` counts those), so "
                "calling this twice is safe. ONE pass is bounded by `limit` writes; "
                "`remaining` is what it did not reach — CALL AGAIN UNTIL `remaining` "
                "IS 0. Pass `dry_run: true` first to see the scale without writing."
            ),
            input_schema=BackfillOriginalsInput,
            output_schema=BackfillOriginalsOutput,
            handler=_h_backfill_originals,
            required_scopes=("mcp:write",),
            audit_event="bsvibe.mcp.backfill_originals.invoked",
        )
    )


__all__ = ["register_originals_backfill_tools"]
