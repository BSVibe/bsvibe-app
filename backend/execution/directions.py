from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# TODO(bundle-x-integration): out-of-scope source dep -- backend.src.models
# from backend.src.models import Direction, Project, Request
# TODO(bundle-x-integration): out-of-scope source dep -- backend.src.schemas
# from backend.src.schemas import DirectionCreate


@dataclass(frozen=True)
class DirectionIngestionResult:
    direction: Direction
    request: Request | None
    routing_options: list[Project]

    @property
    def routing_required(self) -> bool:
        return self.request is None


async def create_direction(
    *,
    payload: DirectionCreate,
    tenant_id: uuid.UUID,
    actor_id: str,
    session: AsyncSession,
) -> Direction:
    direction = Direction(
        tenant_id=tenant_id,
        project_id=payload.project_id,
        source=payload.source,
        actor_id=actor_id,
        body=payload.body,
        target_hint=payload.target_hint,
    )
    session.add(direction)
    await session.commit()
    await session.refresh(direction)
    return direction


def _normalise_hint(value: str | None) -> str:
    return (value or "").strip().casefold()


def _project_matches_hint(project: Project, hint: str) -> bool:
    if not hint:
        return False
    if str(project.id).casefold() == hint:
        return True
    name = project.name.casefold()
    return name == hint or hint in name


async def _tenant_projects(session: AsyncSession, tenant_id: uuid.UUID) -> list[Project]:
    stmt = select(Project).where(Project.tenant_id == tenant_id).order_by(Project.created_at.desc())
    return list((await session.execute(stmt)).scalars())


_PROJECT_NAME_MAX = 80
_PROJECT_DESC_MAX = 1000


def _project_name_from_body(body: str) -> str:
    """Derive a project name from the Direction body's first line."""
    stripped = (body or "").strip()
    first_line = stripped.splitlines()[0].strip() if stripped else ""
    return first_line[:_PROJECT_NAME_MAX].strip() or "New Project"


async def ingest_direction(
    *,
    payload: DirectionCreate,
    tenant_id: uuid.UUID,
    actor_id: str,
    session: AsyncSession,
) -> DirectionIngestionResult:
    projects = await _tenant_projects(session, tenant_id)
    selected_project: Project | None = None
    routing_options: list[Project] = []

    if payload.project_id is not None:
        selected_project = next(
            (project for project in projects if project.id == payload.project_id), None
        )
        if selected_project is None:
            raise LookupError("Project not found")
    elif not projects:
        # Greenfield: the founder's first Direction has no project to
        # land in. A project is mandatory for any work — every Request,
        # WorkPlan and Deliverable is project-scoped — so bootstrap one
        # from the Direction itself rather than dead-ending the founder
        # on a "which project?" prompt with zero options to pick.
        selected_project = Project(
            tenant_id=tenant_id,
            name=_project_name_from_body(payload.body),
            description=payload.body.strip()[:_PROJECT_DESC_MAX],
        )
        session.add(selected_project)
        await session.flush()
    else:
        hint = _normalise_hint(payload.target_hint)
        if hint:
            matches = [project for project in projects if _project_matches_hint(project, hint)]
            if len(matches) == 1:
                selected_project = matches[0]
            else:
                routing_options = matches or projects
        elif len(projects) == 1:
            selected_project = projects[0]
        else:
            routing_options = projects

    direction = Direction(
        tenant_id=tenant_id,
        project_id=selected_project.id if selected_project is not None else None,
        source=payload.source,
        actor_id=actor_id,
        body=payload.body,
        target_hint=payload.target_hint,
    )
    session.add(direction)
    await session.flush()

    request: Request | None = None
    if selected_project is not None:
        request = Request(
            tenant_id=tenant_id,
            project_id=selected_project.id,
            origin_direction_id=direction.id,
            intent=payload.body.strip(),
        )
        session.add(request)
        await session.flush()

    await session.commit()
    await session.refresh(direction)
    if request is not None:
        await session.refresh(request)

    return DirectionIngestionResult(
        direction=direction,
        request=request,
        routing_options=routing_options,
    )
