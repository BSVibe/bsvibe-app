"""A cited test whose signature wraps is read past its signature.

Prod #1290 (run 4d0fe9c9, 2026-11-03): R1/R2 came back ``unclear`` — "the test shows only its
signature, the body is cut". ruff wraps a long test name's parameters, so the definition is

    async def test_list_reads_every_runs_rounds_in_one_query(
        client, db, workspace_id, monkeypatch
    ) -> None:
        ...

and the column-0 ``) -> None:`` line was taken for the bracket that closes the test — so the
body stopped at the signature. A closer that ends the signature (``):`` / ``) -> T:`` /
``) {``) opens the body; only one that does not (``});``) ends it.
"""

from __future__ import annotations

from backend.workflow.domain.requirements import cited_test_body

_PY = """async def test_list_reads_every_runs_rounds_in_one_query(
    client, db, workspace_id, monkeypatch
) -> None:
    listed = (await client.get("/api/v1/runs")).json()
    assert sum("verification_results" in s for s in statements) == 1


async def test_next():
    pass
"""

_TS = """it("shows the line", async () => {
  render(<RunDetail />);
  expect(screen.getByText("x")).toBeInTheDocument();
});

it("next", () => {});
"""

_GO = """func TestWrapped(
	t *testing.T,
) {
	assert(t, true)
}

func TestNext(t *testing.T) {}
"""


def test_a_wrapped_python_signature_keeps_its_body() -> None:
    body = cited_test_body(_PY, "test_list_reads_every_runs_rounds_in_one_query")
    assert body is not None
    assert 'listed = (await client.get("/api/v1/runs")).json()' in body
    assert "verification_results" in body
    assert "test_next" not in body


def test_a_js_test_still_ends_at_its_closing_bracket() -> None:
    body = cited_test_body(_TS, "shows the line")
    assert body is not None and body.endswith("});")
    assert "next" not in body


def test_a_wrapped_brace_signature_keeps_its_body() -> None:
    body = cited_test_body(_GO, "TestWrapped")
    assert body is not None
    assert "assert(t, true)" in body
    assert "TestNext" not in body


def test_a_wrapped_helper_signature_keeps_its_body_too() -> None:
    from backend.workflow.domain.requirements import cited_test_helpers

    text = (
        "async def _seed_run(\n"
        "    db, workspace_id, outcomes\n"
        ") -> uuid.UUID:\n"
        "    store_real_rows(db, outcomes)\n"
        "\n\n"
        "def test_x(db):\n"
        "    _seed_run(db, 1, [])\n"
    )
    shown = cited_test_helpers(text, "def test_x(db):\n    _seed_run(db, 1, [])\n")
    assert "store_real_rows" in shown
