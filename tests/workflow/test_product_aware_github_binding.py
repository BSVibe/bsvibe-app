"""``resolve_github_binding`` must answer for the RUN'S PRODUCT, not the workspace.

Regression cover for #681. ``resolve_github_binding`` was workspace-scoped and
returned the FIRST active github row — so in a workspace holding two products
(BSVibe → ``BSVibe/bsvibe-app``, BStockReport → ``blas1n/BStockReport``) EVERY
run resolved the same single binding. The run-setup provisioner then cloned that
repo into the run workspace, and the agent was told to implement BStockReport
while standing inside a ``bsvibe-app`` checkout — and would have pushed a branch
+ opened a PR there. That is data corruption in someone else's repo, not a
routing nit.

The contract this file pins:

* the product's ``repo_url`` SELECTS the binding — a non-matching binding is not
  used even when it sorts first
* no binding matches the product's repo → ``None``. Deliberate: the caller falls
  back to the product-workspace provisioner and skips github delivery. NOT
  pushing beats pushing to a stranger's repo.
* a product WITHOUT ``repo_url`` (substrate-only) and a call with NO
  ``product_id`` keep the previous workspace-scoped behaviour — single-product
  and non-product callers are untouched
* repo matching is form-insensitive (``https://…/o/n``, ``…/o/n.git``, ``o/n``)
* several qualifying candidates resolve DETERMINISTICALLY (``.first()`` used to
  make bootstrap and delivery pick different accounts for the same workspace)
* the provisioner call site actually THREADS ``run.product_id`` through — a
  resolver fixed in isolation would still clone the wrong repo
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.connectors.db import ConnectorAccountRow
from backend.identity.workspaces_db import ProductRow, WorkspaceRow
from backend.router.accounts.crypto import CredentialCipher
from backend.workflow.application.delivery.connector_dispatch import (
    build_github_workspace_provisioner,
    resolve_github_binding,
)
from backend.workflow.infrastructure.db import ExecutionRun, RunStatus

from .._support import db_engine

# No module-level ``pytest.mark.asyncio``: ``asyncio_mode = "auto"`` already
# marks the async tests, and the normaliser drift-guard at the bottom is sync.
TEST_KEY = b"0123456789abcdef0123456789abcdef"


@pytest_asyncio.fixture
async def sf():
    # Connector rows, products and runs live in three declarative modules; the
    # Single Base unification means one create_all covers all of them.
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def cipher() -> CredentialCipher:
    return CredentialCipher(TEST_KEY)


async def _seed_workspace(session: AsyncSession, workspace_id: uuid.UUID) -> None:
    if await session.get(WorkspaceRow, workspace_id) is None:
        session.add(WorkspaceRow(id=workspace_id, name="ws-681", safe_mode=False))
        await session.flush()


async def _seed_product(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    repo_url: str | None,
    slug: str = "p",
    metadata: dict[str, object] | None = None,
) -> uuid.UUID:
    await _seed_workspace(session, workspace_id)
    product_id = uuid.uuid4()
    session.add(
        ProductRow(
            id=product_id,
            workspace_id=workspace_id,
            name=slug,
            slug=slug,
            repo_url=repo_url,
            product_metadata=metadata or {},
        )
    )
    await session.commit()
    return product_id


async def _seed_binding(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    repo: str | None,
    created_at: datetime | None = None,
    is_active: bool = True,
    cipher: CredentialCipher | None = None,
    base_branch: str = "main",
) -> uuid.UUID:
    """Seed a github connector.

    ``repo=None`` writes an EMPTY ``delivery_config`` — what a fresh "Connect
    with GitHub" actually produces (the OAuth flow binds a credential; nobody
    types a repo). #684 makes that the normal shape rather than a dead row.
    """
    await _seed_workspace(session, workspace_id)
    account_id = uuid.uuid4()
    secret = (cipher or CredentialCipher(TEST_KEY)).encrypt("ghp_test_token")
    row = ConnectorAccountRow(
        id=account_id,
        workspace_id=workspace_id,
        connector="github",
        webhook_token=uuid.uuid4().hex,
        signing_secret_ciphertext=secret,
        delivery_config=({} if repo is None else {"repo": repo, "base_branch": base_branch}),
        is_active=is_active,
    )
    if created_at is not None:
        row.created_at = created_at
    session.add(row)
    await session.commit()
    return account_id


class TestProductSelectsTheBinding:
    async def test_matching_binding_wins_over_the_other_product_repo(
        self, sf: async_sessionmaker[AsyncSession]
    ) -> None:
        """Two products, two bindings: the run's product picks its OWN repo.

        The non-matching binding is seeded FIRST (and with an earlier
        ``created_at``) so a first-row-wins resolver fails this test — that is
        exactly the #681 production shape.
        """
        ws = uuid.uuid4()
        early = datetime(2026, 1, 1, tzinfo=UTC)
        late = datetime(2026, 6, 1, tzinfo=UTC)
        async with sf() as s:
            other = await _seed_binding(
                s, workspace_id=ws, repo="BSVibe/bsvibe-app", created_at=early
            )
            mine = await _seed_binding(
                s, workspace_id=ws, repo="blas1n/BStockReport", created_at=late
            )
            product_id = await _seed_product(
                s,
                workspace_id=ws,
                repo_url="https://github.com/blas1n/BStockReport",
                slug="bstockreport",
            )
            binding = await resolve_github_binding(s, workspace_id=ws, product_id=product_id)

        assert binding is not None
        assert binding.repo == "blas1n/BStockReport"
        assert binding.account.id == mine
        assert binding.account.id != other

    async def test_unpinned_product_repo_never_resolves_the_other_repo(
        self, sf: async_sessionmaker[AsyncSession]
    ) -> None:
        """Only the sibling's repo is pinned → the answer is never that repo.

        #681 returned ``None`` here. #684 keeps the SAFETY property (a run never
        targets a repo its product does not own) while dropping the collateral
        damage: the sibling's connector is a valid CREDENTIAL, so the binding
        comes back aimed at the product's OWN repo instead of nothing.
        """
        ws = uuid.uuid4()
        async with sf() as s:
            await _seed_binding(s, workspace_id=ws, repo="BSVibe/bsvibe-app")
            product_id = await _seed_product(
                s,
                workspace_id=ws,
                repo_url="https://github.com/blas1n/BStockReport",
                slug="bstockreport",
            )
            binding = await resolve_github_binding(s, workspace_id=ws, product_id=product_id)

        assert binding is not None
        assert binding.repo == "blas1n/BStockReport"
        assert binding.repo != "BSVibe/bsvibe-app"

    async def test_inactive_matching_binding_is_still_skipped(
        self, sf: async_sessionmaker[AsyncSession]
    ) -> None:
        """A revoked connector is not resurrected just because its repo matches."""
        ws = uuid.uuid4()
        async with sf() as s:
            await _seed_binding(s, workspace_id=ws, repo="blas1n/BStockReport", is_active=False)
            product_id = await _seed_product(
                s, workspace_id=ws, repo_url="blas1n/BStockReport", slug="bstockreport"
            )
            binding = await resolve_github_binding(s, workspace_id=ws, product_id=product_id)

        assert binding is None


class TestBackwardsCompatibleFallback:
    async def test_product_without_repo_url_falls_back_to_workspace(
        self, sf: async_sessionmaker[AsyncSession]
    ) -> None:
        """A substrate-only product has no repo of its own — the workspace's
        github target is still the right answer (pre-#681 behaviour)."""
        ws = uuid.uuid4()
        async with sf() as s:
            await _seed_binding(s, workspace_id=ws, repo="BSVibe/bsvibe-app")
            product_id = await _seed_product(s, workspace_id=ws, repo_url=None, slug="substrate")
            binding = await resolve_github_binding(s, workspace_id=ws, product_id=product_id)

        assert binding is not None and binding.repo == "BSVibe/bsvibe-app"

    async def test_blank_repo_url_falls_back_to_workspace(
        self, sf: async_sessionmaker[AsyncSession]
    ) -> None:
        """An empty-string ``repo_url`` is "no repo", not "match nothing"."""
        ws = uuid.uuid4()
        async with sf() as s:
            await _seed_binding(s, workspace_id=ws, repo="BSVibe/bsvibe-app")
            product_id = await _seed_product(s, workspace_id=ws, repo_url="   ", slug="blank")
            binding = await resolve_github_binding(s, workspace_id=ws, product_id=product_id)

        assert binding is not None and binding.repo == "BSVibe/bsvibe-app"

    async def test_no_product_id_keeps_workspace_behaviour(
        self, sf: async_sessionmaker[AsyncSession]
    ) -> None:
        """Callers that legitimately know no product (a workspace-only seam)
        must keep resolving exactly as before — the parameter is optional."""
        ws = uuid.uuid4()
        async with sf() as s:
            await _seed_binding(s, workspace_id=ws, repo="BSVibe/bsvibe-app")
            binding = await resolve_github_binding(s, workspace_id=ws)

        assert binding is not None and binding.repo == "BSVibe/bsvibe-app"

    async def test_unknown_product_id_falls_back_to_workspace(
        self, sf: async_sessionmaker[AsyncSession]
    ) -> None:
        """A deleted / unknown product id must not blank out delivery."""
        ws = uuid.uuid4()
        async with sf() as s:
            await _seed_binding(s, workspace_id=ws, repo="BSVibe/bsvibe-app")
            binding = await resolve_github_binding(s, workspace_id=ws, product_id=uuid.uuid4())

        assert binding is not None and binding.repo == "BSVibe/bsvibe-app"


class TestRepoFormRobustness:
    @pytest.mark.parametrize(
        "repo_url",
        [
            "https://github.com/owner/name",
            "https://github.com/owner/name.git",
            "https://github.com/owner/name/",
            "http://github.com/Owner/Name",
            "git@github.com:owner/name.git",
            "owner/name",
        ],
    )
    async def test_repo_url_forms_match_owner_name_binding(
        self, sf: async_sessionmaker[AsyncSession], repo_url: str
    ) -> None:
        """The founder types a URL; the connector config holds ``owner/name``.
        They must still be recognised as the SAME repo."""
        ws = uuid.uuid4()
        async with sf() as s:
            await _seed_binding(s, workspace_id=ws, repo="owner/name")
            product_id = await _seed_product(s, workspace_id=ws, repo_url=repo_url, slug="p")
            binding = await resolve_github_binding(s, workspace_id=ws, product_id=product_id)

        assert binding is not None, f"{repo_url} must match the owner/name binding"
        assert binding.repo == "owner/name"

    async def test_binding_configured_as_a_url_also_matches(
        self, sf: async_sessionmaker[AsyncSession]
    ) -> None:
        """Normalisation applies to BOTH sides — a URL-shaped ``delivery_config``
        repo matches an ``owner/name`` product."""
        ws = uuid.uuid4()
        async with sf() as s:
            await _seed_binding(s, workspace_id=ws, repo="https://github.com/owner/name.git")
            product_id = await _seed_product(s, workspace_id=ws, repo_url="owner/name", slug="p")
            binding = await resolve_github_binding(s, workspace_id=ws, product_id=product_id)

        assert binding is not None

    async def test_a_different_owner_is_not_a_match(
        self, sf: async_sessionmaker[AsyncSession]
    ) -> None:
        """Same repo NAME under a different owner is a DIFFERENT repo — the
        fork/namesake case must not silently cross-push.

        The namesake pin does not match, so it cannot become the target; under
        #684 the row still serves as the credential and the product's own
        ``owner/name`` is what the binding aims at.
        """
        ws = uuid.uuid4()
        async with sf() as s:
            await _seed_binding(s, workspace_id=ws, repo="someone-else/name")
            product_id = await _seed_product(s, workspace_id=ws, repo_url="owner/name", slug="p")
            binding = await resolve_github_binding(s, workspace_id=ws, product_id=product_id)

        assert binding is not None
        assert binding.repo == "owner/name"
        assert binding.repo != "someone-else/name"


class TestDeterministicPick:
    async def test_identical_repo_candidates_resolve_stably(
        self, sf: async_sessionmaker[AsyncSession]
    ) -> None:
        """Three active bindings on the SAME repo → the oldest always wins.

        In production bootstrap and delivery picked DIFFERENT accounts for the
        same workspace because ``.first()`` inherited whatever order the DB felt
        like. Every caller must land on one account.
        """
        ws = uuid.uuid4()
        async with sf() as s:
            oldest = await _seed_binding(
                s,
                workspace_id=ws,
                repo="owner/name",
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
            await _seed_binding(
                s,
                workspace_id=ws,
                repo="owner/name",
                created_at=datetime(2026, 3, 1, tzinfo=UTC),
            )
            await _seed_binding(
                s,
                workspace_id=ws,
                repo="owner/name",
                created_at=datetime(2026, 5, 1, tzinfo=UTC),
            )
            product_id = await _seed_product(s, workspace_id=ws, repo_url="owner/name", slug="p")
            picks = {
                (await resolve_github_binding(s, workspace_id=ws, product_id=product_id)).account.id  # type: ignore[union-attr]
                for _ in range(5)
            }

        assert picks == {oldest}

    async def test_workspace_fallback_is_also_deterministic(
        self, sf: async_sessionmaker[AsyncSession]
    ) -> None:
        """The no-product path gets the same stable ordering (it fed the
        bootstrap-vs-delivery split just as much)."""
        ws = uuid.uuid4()
        async with sf() as s:
            oldest = await _seed_binding(
                s, workspace_id=ws, repo="a/one", created_at=datetime(2026, 1, 1, tzinfo=UTC)
            )
            await _seed_binding(
                s, workspace_id=ws, repo="b/two", created_at=datetime(2026, 2, 1, tzinfo=UTC)
            )
            picks = {
                (await resolve_github_binding(s, workspace_id=ws)).account.id  # type: ignore[union-attr]
                for _ in range(5)
            }

        assert picks == {oldest}


class _CountingGitOps:
    """Fake ``GitOps`` that records what the provisioner tried to clone."""

    def __init__(self) -> None:
        self.cloned: list[str] = []

    async def clone(self, repo_url: str, dest: Path, *, token: str | None, depth: int = 1) -> None:
        self.cloned.append(repo_url)
        dest.mkdir(parents=True, exist_ok=True)
        (dest / ".git").mkdir()

    async def checkout_new_branch(self, dest: Path, branch: str) -> None:
        return None


class TestProvisionerThreadsTheProduct:
    """A resolver fixed in isolation still clones the wrong repo unless the
    provisioner passes ``run.product_id`` — pin the WIRING, not just the query."""

    def _run(self, workspace_id: uuid.UUID, product_id: uuid.UUID | None) -> ExecutionRun:
        return ExecutionRun(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            product_id=product_id,
            request_id=None,
            status=RunStatus.OPEN,
            payload={},
        )

    async def test_provision_clones_the_products_own_repo(
        self, sf: async_sessionmaker[AsyncSession], cipher: CredentialCipher, tmp_path: Path
    ) -> None:
        ws = uuid.uuid4()
        async with sf() as s:
            await _seed_binding(
                s,
                workspace_id=ws,
                repo="BSVibe/bsvibe-app",
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
                cipher=cipher,
            )
            await _seed_binding(
                s,
                workspace_id=ws,
                repo="blas1n/BStockReport",
                created_at=datetime(2026, 6, 1, tzinfo=UTC),
                cipher=cipher,
            )
            product_id = await _seed_product(
                s,
                workspace_id=ws,
                repo_url="https://github.com/blas1n/BStockReport",
                slug="bstockreport",
            )

        ops = _CountingGitOps()
        provision = build_github_workspace_provisioner(cipher=cipher, git_ops=ops)
        run = self._run(ws, product_id)
        workspace_dir = tmp_path / str(run.id)
        workspace_dir.mkdir(parents=True)

        async with sf() as s:
            await provision(s, run, workspace_dir)

        assert ops.cloned == ["https://github.com/blas1n/BStockReport.git"]

    async def test_provision_never_clones_the_sibling_repo(
        self, sf: async_sessionmaker[AsyncSession], cipher: CredentialCipher, tmp_path: Path
    ) -> None:
        """Only the sibling's repo is pinned → the clone is still the product's.

        #681 asserted "no clone at all" here; #684 supplies the credential from
        that sibling connector and clones the product's OWN repo. What must
        never happen — cloning ``BSVibe/bsvibe-app`` for a BStockReport run —
        is what this now pins.
        """
        ws = uuid.uuid4()
        async with sf() as s:
            await _seed_binding(s, workspace_id=ws, repo="BSVibe/bsvibe-app", cipher=cipher)
            product_id = await _seed_product(
                s,
                workspace_id=ws,
                repo_url="https://github.com/blas1n/BStockReport",
                slug="bstockreport",
            )

        ops = _CountingGitOps()
        provision = build_github_workspace_provisioner(cipher=cipher, git_ops=ops)
        run = self._run(ws, product_id)
        workspace_dir = tmp_path / str(run.id)
        workspace_dir.mkdir(parents=True)

        async with sf() as s:
            await provision(s, run, workspace_dir)

        assert len(ops.cloned) == 1
        assert "blas1n/BStockReport" in ops.cloned[0]
        assert "bsvibe-app" not in ops.cloned[0]

    async def test_provision_skips_without_any_github_credential(
        self, sf: async_sessionmaker[AsyncSession], cipher: CredentialCipher, tmp_path: Path
    ) -> None:
        """No active github connector → no clone, so the product-workspace
        provisioner downstream materialises the checkout instead."""
        ws = uuid.uuid4()
        async with sf() as s:
            product_id = await _seed_product(
                s,
                workspace_id=ws,
                repo_url="https://github.com/blas1n/BStockReport",
                slug="bstockreport",
            )

        ops = _CountingGitOps()
        provision = build_github_workspace_provisioner(cipher=cipher, git_ops=ops)
        run = self._run(ws, product_id)
        workspace_dir = tmp_path / str(run.id)
        workspace_dir.mkdir(parents=True)

        async with sf() as s:
            await provision(s, run, workspace_dir)

        assert ops.cloned == []


class TestRepoSlugAgreesWithInboundNormaliser:
    """The inbound webhook route normalises repo strings the same way
    (``backend.api.webhooks._repo_slug``) to bind an issue to its product. The
    two live in different layers (an import-linter contract keeps the inbound
    layer off this package), so pin that they cannot drift apart."""

    @pytest.mark.parametrize(
        "raw",
        [
            "owner/name",
            "https://github.com/owner/name",
            "https://github.com/owner/name.git",
            "http://github.com/Owner/Name/",
            "git@github.com:owner/name.git",
            "",
        ],
    )
    def test_same_slug_as_webhooks(self, raw: str) -> None:
        from backend.api.webhooks import _repo_slug  # noqa: PLC0415
        from backend.workflow.application.delivery.connector_dispatch._resolver import (  # noqa: PLC0415
            normalize_repo_slug,
        )

        assert normalize_repo_slug(raw) == _repo_slug(raw)


class TestConnectorIsCredentialNotRepo:
    """#684 — the connector supplies the CREDENTIAL; the product names the repo.

    A GitHub App is installed account-wide, so pinning one repo per connector
    row forced a duplicate connector per product. Worse, a fresh "Connect with
    GitHub" writes an EMPTY ``delivery_config``, so under #683 alone every
    product in the workspace silently lost its binding: connected, credential
    valid, nothing delivered.
    """

    async def test_repo_comes_from_the_product_when_connector_pins_none(
        self, sf: async_sessionmaker[AsyncSession]
    ) -> None:
        """A repo-less credential still serves the product's own repo."""
        ws = uuid.uuid4()
        async with sf() as s:
            account_id = await _seed_binding(s, workspace_id=ws, repo=None)
            product_id = await _seed_product(
                s,
                workspace_id=ws,
                repo_url="https://github.com/blas1n/BStockReport",
                slug="bstockreport",
            )
            binding = await resolve_github_binding(s, workspace_id=ws, product_id=product_id)
        assert binding is not None
        assert binding.account.id == account_id
        # The PRODUCT decides the repo — and its casing survives.
        assert binding.repo == "blas1n/BStockReport"
        assert binding.base_branch == "main"

    async def test_explicit_pin_still_wins_over_the_generic_credential(
        self, sf: async_sessionmaker[AsyncSession]
    ) -> None:
        """A connector that names the product's repo is preferred (base_branch)."""
        ws = uuid.uuid4()
        early = datetime(2026, 1, 1, tzinfo=UTC)
        async with sf() as s:
            await _seed_binding(s, workspace_id=ws, repo=None, created_at=early)
            pinned = await _seed_binding(
                s,
                workspace_id=ws,
                repo="blas1n/BStockReport",
                base_branch="develop",
            )
            product_id = await _seed_product(
                s,
                workspace_id=ws,
                repo_url="https://github.com/blas1n/BStockReport",
                slug="bstockreport",
            )
            binding = await resolve_github_binding(s, workspace_id=ws, product_id=product_id)
        assert binding is not None
        assert binding.account.id == pinned
        # The explicit pin is what carries a non-default base_branch.
        assert binding.base_branch == "develop"

    async def test_two_products_share_one_credential(
        self, sf: async_sessionmaker[AsyncSession]
    ) -> None:
        """The duplicate-connector-per-product requirement is gone (#684)."""
        ws = uuid.uuid4()
        async with sf() as s:
            account_id = await _seed_binding(s, workspace_id=ws, repo=None)
            first = await _seed_product(
                s, workspace_id=ws, repo_url="https://github.com/blas1n/BStockReport", slug="a"
            )
            second = await _seed_product(
                s, workspace_id=ws, repo_url="https://github.com/BSVibe/bsvibe-app", slug="b"
            )
            one = await resolve_github_binding(s, workspace_id=ws, product_id=first)
            two = await resolve_github_binding(s, workspace_id=ws, product_id=second)
        assert one is not None and two is not None
        assert one.account.id == account_id == two.account.id
        assert one.repo == "blas1n/BStockReport"
        assert two.repo == "BSVibe/bsvibe-app"

    async def test_no_active_credential_means_no_binding(
        self, sf: async_sessionmaker[AsyncSession]
    ) -> None:
        """A product repo with nothing to authenticate as still resolves None."""
        ws = uuid.uuid4()
        async with sf() as s:
            await _seed_binding(s, workspace_id=ws, repo=None, is_active=False)
            product_id = await _seed_product(
                s, workspace_id=ws, repo_url="https://github.com/blas1n/BStockReport"
            )
            binding = await resolve_github_binding(s, workspace_id=ws, product_id=product_id)
        assert binding is None

    async def test_substrate_only_product_keeps_the_workspace_target(
        self, sf: async_sessionmaker[AsyncSession]
    ) -> None:
        """No repo_url → the connector's own pin is still the answer (legacy)."""
        ws = uuid.uuid4()
        async with sf() as s:
            await _seed_binding(s, workspace_id=ws, repo="BSVibe/bsvibe-app")
            product_id = await _seed_product(s, workspace_id=ws, repo_url=None)
            binding = await resolve_github_binding(s, workspace_id=ws, product_id=product_id)
        assert binding is not None
        assert binding.repo == "BSVibe/bsvibe-app"

    async def test_repo_less_credential_is_not_used_without_a_product(
        self, sf: async_sessionmaker[AsyncSession]
    ) -> None:
        """A workspace-only caller has no repo to infer — must not guess."""
        ws = uuid.uuid4()
        async with sf() as s:
            await _seed_binding(s, workspace_id=ws, repo=None)
            binding = await resolve_github_binding(s, workspace_id=ws)
        assert binding is None


# ── client_attach: the server may TALK to github, never hold the source ─────
# Live 2026-08-10: approving a client_attach run's deliverable resolved a github
# binding (the product owns a repo and the workspace has a credential, #684) and
# ``deliver_github`` tried to commit the run's checkout — which does not exist,
# because that model never clones to the server. It died with
#     GitError: git add -A failed: fatal: not a git repository
# and, since the github call sits OUTSIDE the per-binding try, the exception took
# the whole dispatch down with it: the telegram binding was never even reached.
#
# #723's fix was to resolve ``None`` here. It stopped the crash and cost the rest:
# NO client_attach run could ever get a PR, because a binding is how delivery
# finds github at all. The guard was in the wrong place — the thing that must
# never happen is the SERVER OBTAINING THE SOURCE, and that is the provisioner's
# and the freshness merge's business, both of which now refuse it themselves.
# Opening a PR needs no checkout, and since #735 the branch is already pushed.


async def test_client_attach_product_resolves_its_github_target(sf) -> None:
    """A binding is the credential + the repo, not a claim about a checkout.

    Guarding here removed the whole delivery surface for this execution model;
    the danger (a server-side clone) is refused where it would actually happen.
    """
    async with sf() as session:
        workspace_id = uuid.uuid4()
        product_id = await _seed_product(
            session,
            workspace_id=workspace_id,
            repo_url="https://github.com/blas1n/BStockReport",
            metadata={"execution_target": "client_attach"},
        )
        await _seed_binding(session, workspace_id=workspace_id, repo=None)

        got = await resolve_github_binding(
            session, workspace_id=workspace_id, product_id=product_id
        )

        assert got is not None, (
            "no binding means no PR for every client_attach run — the guard belongs "
            "at the clone, not at the credential"
        )
        assert got.repo == "blas1n/BStockReport"


async def test_server_sandbox_product_still_resolves_its_repo(sf) -> None:
    """The default model is untouched — it DOES have a checkout to deliver from."""
    async with sf() as session:
        workspace_id = uuid.uuid4()
        product_id = await _seed_product(
            session,
            workspace_id=workspace_id,
            repo_url="https://github.com/blas1n/BStockReport",
            metadata={"execution_target": "server_sandbox"},
        )
        await _seed_binding(session, workspace_id=workspace_id, repo=None)

        got = await resolve_github_binding(
            session, workspace_id=workspace_id, product_id=product_id
        )

        assert got is not None
        assert got.repo == "blas1n/BStockReport"
