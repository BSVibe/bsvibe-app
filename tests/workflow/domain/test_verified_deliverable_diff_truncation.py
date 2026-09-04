"""Diff truncation must never silently drop the founder's own artifact.

``write_verified_deliverable`` caps the stored diff at ``_MAX_DIFF_CHARS`` so a
runaway (vendored/generated) file cannot bloat the row. A blind head-cut is the
bug this exists to kill: whichever file happens to sort first in the diff wins
the budget, and the founder's own ``artifact_refs`` file can be cut to nothing
without a trace. ``_truncate_diff_preserving_artifacts`` is the pure function
that fixes this — tested here directly, with NO mocking, because it takes and
returns plain strings.
"""

from __future__ import annotations

from typing import Any

from backend.workflow.domain.verified_deliverable import (
    _truncate_diff_preserving_artifacts,
)


def _section(path: str, lines: list[str], *, deleted: bool = False) -> str:
    """A byte-real ``diff --git`` section for ``path`` with ``lines`` added."""
    a_path = path
    b_path = "/dev/null" if deleted else path
    header = f"diff --git a/{a_path} b/{b_path}\n"
    meta = f"index 1111111..2222222 100644\n--- a/{a_path}\n+++ b/{b_path}\n@@ -0,0 +1,{len(lines)} @@\n"
    body = "".join(f"+{line}\n" for line in lines)
    return header + meta + body


def _details_by_path(detail: list[dict[str, Any]] | None) -> dict[str | None, dict[str, Any]]:
    assert detail is not None
    return {entry["path"]: entry for entry in detail}


class TestNegativeControlFitsUnderBudget:
    """The most important guarantee: when nothing needs cutting, nothing is
    touched — not even reconstructed via joined slices."""

    def test_diff_under_budget_is_returned_byte_identical_and_unflagged(self) -> None:
        diff = _section("src/a.py", ["one", "two"])
        result, truncated, detail = _truncate_diff_preserving_artifacts(
            diff, artifact_refs=["src/a.py"], max_chars=len(diff) + 1000
        )
        assert result is diff, "must be the ORIGINAL object, not a reconstruction"
        assert truncated is False
        assert detail is None

    def test_the_boundary_max_chars_equal_to_len_still_does_not_truncate(self) -> None:
        diff = _section("src/a.py", ["x"])
        result, truncated, detail = _truncate_diff_preserving_artifacts(
            diff, artifact_refs=[], max_chars=len(diff)
        )
        assert result is diff
        assert truncated is False
        assert detail is None


class TestArtifactSurvivesOverTheVendoredFile:
    def test_a_huge_non_artifact_section_is_dropped_whole_before_the_artifact_shrinks(
        self,
    ) -> None:
        vendor = _section("vendor/blob.min.js", ["x" * 5000])
        art_a = _section("src/a.py", ["real work a"])
        art_b = _section("src/b.py", ["real work b"])
        diff = vendor + art_a + art_b
        max_chars = len(art_a) + len(art_b) + 5  # room for both artifacts, not vendor

        result, truncated, detail = _truncate_diff_preserving_artifacts(
            diff, artifact_refs=["src/a.py", "src/b.py"], max_chars=max_chars
        )

        assert truncated is True
        assert "real work a" in result
        assert "real work b" in result
        assert "vendor/blob.min.js" not in result
        # Whole-or-nothing on non-artifacts, no partial cut occurred -> no detail.
        assert detail is None


class TestPriorityExceedsBudgetRecordsEveryFile:
    """The regression this round exists to close: a ``break`` used to stop
    recording detail entries once the budget ran dry, so a priority file that
    got 0 bytes also got 0 mention — it vanished from the report exactly like
    the vendored-file bug it was supposed to prevent."""

    def test_every_priority_file_is_named_even_the_ones_that_got_nothing(self) -> None:
        a = _section("src/a.py", ["a" * 20])
        b = _section("src/b.py", ["b" * 20])
        c = _section("src/c.py", ["c" * 20])
        diff = a + b + c
        max_chars = len(a) + 10  # a fits, b gets a sliver, c must get zero

        result, truncated, detail = _truncate_diff_preserving_artifacts(
            diff, artifact_refs=["src/a.py", "src/b.py", "src/c.py"], max_chars=max_chars
        )

        assert truncated is True
        by_path = _details_by_path(detail)
        assert set(by_path) == {"src/a.py", "src/b.py", "src/c.py"}, (
            "src/c.py must not disappear from detail just because it got 0 chars"
        )
        assert by_path["src/a.py"]["kept_chars"] == len(a)
        assert 0 < by_path["src/b.py"]["kept_chars"] < len(b)
        assert by_path["src/c.py"]["kept_chars"] == 0
        assert by_path["src/c.py"]["total_chars"] == len(c)
        # The result text itself must not contain any of c's body.
        assert "c" * 20 not in result


class TestSectionOrderIsPreserved:
    """Decision: truncation preserves the ORIGINAL document order rather than
    promoting artifact sections to the front. A reviewer reads a diff
    top-to-bottom expecting it to mirror the real patch; reordering an
    already-truncated diff would make it harder, not easier, to reconstruct
    what actually happened. Pinned here so a future change cannot silently
    flip this without a red test."""

    def test_a_kept_earlier_non_priority_section_still_precedes_the_priority_one(
        self,
    ) -> None:
        aaa = _section("aaa/small.py", ["s"])
        mmm = _section("mmm/mine.py", ["m"])  # the artifact
        zzz = _section("zzz/huge.bin", ["z" * 5000])
        diff = aaa + mmm + zzz
        max_chars = len(aaa) + len(mmm)  # room for aaa + mmm, zzz must be dropped

        result, truncated, detail = _truncate_diff_preserving_artifacts(
            diff, artifact_refs=["mmm/mine.py"], max_chars=max_chars
        )

        assert truncated is True
        assert "zzz/huge.bin" not in result
        assert result.index("aaa/small.py") < result.index("mmm/mine.py"), (
            "the artifact must stay in its original position, not jump to the front"
        )


class TestEmptyResultNeverHappens:
    def test_header_less_diff_past_budget_keeps_a_leading_slice_not_nothing(self) -> None:
        diff = "not a real git diff, just prose\n" * 50
        result, truncated, detail = _truncate_diff_preserving_artifacts(
            diff, artifact_refs=[], max_chars=10
        )
        assert result != ""
        assert truncated is True
        assert detail == [{"path": None, "kept_chars": 10, "total_chars": len(diff)}]

    def test_a_single_non_priority_section_too_big_to_fit_still_yields_a_sliver(self) -> None:
        """The 'else' branch's whole-or-nothing rule can legitimately keep
        NOTHING (the one section does not fit the remaining budget at all) —
        the empty-result guard must catch that too, not just the header-less
        case."""
        diff = _section("only/file.py", ["y" * 5000])
        result, truncated, detail = _truncate_diff_preserving_artifacts(
            diff, artifact_refs=[], max_chars=1
        )
        assert result != ""
        assert truncated is True
        assert detail is not None
        assert detail[0]["kept_chars"] == 1


class TestNonAsciiContentAndPaths:
    """Byte vs. character confusion is exactly the kind of bug that hides
    behind ASCII-only fixtures."""

    def test_korean_path_is_matched_and_survives_truncation(self) -> None:
        vendor = _section("vendor/blob.min.js", ["x" * 5000])
        art = _section("docs/설명.md", ["한글 콘텐츠가 잘려서는 안 된다"])
        diff = vendor + art
        max_chars = len(art) + 5

        result, truncated, detail = _truncate_diff_preserving_artifacts(
            diff, artifact_refs=["docs/설명.md"], max_chars=max_chars
        )

        assert truncated is True
        assert "한글 콘텐츠가 잘려서는 안 된다" in result
        assert "vendor/blob.min.js" not in result

    def test_non_ascii_body_length_is_measured_in_characters_not_bytes(self) -> None:
        a = _section("src/a.py", ["café résumé 커피"])
        b = _section("src/b.py", ["café résumé 커피"])
        diff = a + b
        assert len(a) == len(b)
        max_chars = len(a) + 3

        result, truncated, detail = _truncate_diff_preserving_artifacts(
            diff, artifact_refs=["src/a.py", "src/b.py"], max_chars=max_chars
        )

        by_path = _details_by_path(detail)
        assert by_path["src/a.py"]["kept_chars"] == len(a)
        assert by_path["src/b.py"]["kept_chars"] == 3
        assert len(result) == max_chars
