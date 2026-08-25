"""LLM-derived, repo-grounded verification gate — the pure domain layer.

Replaces the stack-hardcoded quality bar (`uv run ruff`/`mypy` on `.py`) and the
per-stack detector list with ONE mechanism: an LLM reads the repo's OWN
manifests + the changed files and derives the runnable verification commands,
stack-general. This module is the PURE half — the tolerant parser of the LLM's
output + the grounding prompt builder. The LLM call + sandbox execution live in
the service layer (mirrors outcome_demonstration).
"""

from backend.workflow.domain.gate_derivation import (
    DerivedCommand,
    derivation_planner_messages,
    parse_derived_gate,
)


class TestParseDerivedGate:
    def test_parses_commands_with_kind_and_rationale(self) -> None:
        gate = parse_derived_gate(
            {
                "applicable": True,
                "commands": [
                    {
                        "command": "uv run ruff check money.py",
                        "kind": "quality",
                        "rationale": "lint",
                    },
                    {
                        "command": "uv run pytest test_money.py",
                        "kind": "test",
                        "rationale": "suite",
                    },
                ],
            }
        )
        assert gate.applicable is True
        assert gate.commands == (
            DerivedCommand(command="uv run ruff check money.py", kind="quality", rationale="lint"),
            DerivedCommand(command="uv run pytest test_money.py", kind="test", rationale="suite"),
        )
        assert not gate.is_empty

    def test_drops_empty_commands_and_defaults_kind_to_quality(self) -> None:
        gate = parse_derived_gate(
            {"commands": [{"command": ""}, {"cmd": "cargo test"}, {"nope": 1}]}
        )
        # Empty command dropped; `cmd` alias accepted; kind defaults to quality.
        assert gate.commands == (DerivedCommand(command="cargo test", kind="quality"),)

    def test_coerces_unknown_kind_to_quality(self) -> None:
        gate = parse_derived_gate({"commands": [{"command": "go test ./...", "kind": "weird"}]})
        assert gate.commands[0].kind == "quality"

    def test_applicable_false_when_llm_says_non_code(self) -> None:
        # A pure-prose / design deliverable: no runnable gate applies.
        gate = parse_derived_gate({"applicable": False, "commands": []})
        assert gate.applicable is False
        assert gate.is_empty

    def test_applicable_defaults_true_but_empty_commands_stays_empty(self) -> None:
        gate = parse_derived_gate({"commands": []})
        assert gate.applicable is True
        assert gate.is_empty

    def test_tolerates_garbage_shapes(self) -> None:
        for raw in (None, [], "nonsense", 42, {"commands": "notalist"}):
            gate = parse_derived_gate(raw)
            assert gate.is_empty
            # A shape we cannot read at all is not-applicable (honest downgrade),
            # never a spurious runnable gate.
            assert gate.applicable is False

    def test_dedupes_identical_commands(self) -> None:
        gate = parse_derived_gate({"commands": [{"command": "npm test"}, {"command": "npm test"}]})
        assert gate.commands == (DerivedCommand(command="npm test", kind="quality"),)


class TestDerivationPlannerMessages:
    def test_grounds_the_prompt_in_the_repos_real_manifests(self) -> None:
        msgs = derivation_planner_messages(
            manifests={
                "pyproject.toml": "[tool.ruff]\nline-length = 100\n",
                "Makefile": "test:\n\tuv run pytest\n",
            },
            changed_files=["money.py", "test_money.py"],
            intent="Add money utilities",
        )
        assert msgs[0]["role"] == "system"
        joined = "\n".join(m["content"] for m in msgs)
        # The repo's OWN manifest content is in the prompt (grounding) …
        assert "[tool.ruff]" in joined
        assert "Makefile" in joined
        # … along with the changed files it must scope to.
        assert "money.py" in joined
        # The system prompt forbids inventing tools/flags/extras the repo doesn't define.
        sys_lower = msgs[0]["content"].lower()
        assert "invent" in sys_lower or "only" in sys_lower

    def test_no_manifests_still_produces_a_valid_message_pair(self) -> None:
        msgs = derivation_planner_messages(
            manifests={}, changed_files=[], intent="Write a design doc"
        )
        assert [m["role"] for m in msgs] == ["system", "user"]

    def test_system_prompt_hardcodes_no_stack_specific_tool_or_runner(self) -> None:
        # The deriver must generalise across stacks: the LLM maps the repo's
        # manifests to commands, so OUR prompt must not steer toward one stack's
        # tools/runners (that is exactly the coupling this whole redesign removes).
        sys = derivation_planner_messages(manifests={}, changed_files=[], intent="x")[0][
            "content"
        ].lower()
        for tool in ("ruff", "pytest", "mypy", "cargo", "go test", "npm", "pnpm", "yarn", "uv run"):
            assert tool not in sys, f"prompt is stack-biased: mentions {tool!r}"

    def test_system_prompt_prefers_a_real_check_over_a_trivial_compile(self) -> None:
        # The live gap: the deriver returned a syntax/compile-only check instead
        # of the repo's real lint/test. The prompt must steer to STRONG checks
        # (the repo's own test run + lint/type) over a trivial parse-only one —
        # phrased generically, not by naming a stack's tools.
        sys = derivation_planner_messages(manifests={}, changed_files=[], intent="x")[0][
            "content"
        ].lower()
        assert "test" in sys
        assert "compile" in sys or "syntax" in sys
        assert "weak" in sys or "trivial" in sys


class TestSurfaceChecks:
    """A repo's own USER-SURFACE checks are a third kind, and they must survive
    the scoping rule that keeps quality checks off untouched files.

    The gate's claim has been "this repo passed its own checks". The claim we
    need is "this change works where the user receives it". Some repos already
    declare checks of that second kind — BStockReport declares a marked suite
    that drives its report through a stubbed delivery API and asserts the text
    that ARRIVES. Those are exactly the checks that would have caught the two
    live defects unit tests slept through (a silent truncation fabricating
    `$922,010` → `$922,0`, and a dispatch that delivered nothing at all).
    """

    def test_a_declared_surface_check_keeps_its_kind(self) -> None:
        gate = parse_derived_gate(
            {
                "commands": [
                    {
                        "command": "make e2e",
                        "kind": "surface",
                        "rationale": "the repo declares an end-to-end target",
                    }
                ]
            }
        )
        assert gate.commands == (
            DerivedCommand(
                command="make e2e",
                kind="surface",
                rationale="the repo declares an end-to-end target",
            ),
        )

    def test_the_prompt_asks_for_checks_that_exercise_the_delivered_behaviour(self) -> None:
        sys = derivation_planner_messages(manifests={}, changed_files=[], intent="x")[0][
            "content"
        ].lower()
        assert "surface" in sys, "the deriver cannot emit a kind it was never told about"
        assert "end-to-end" in sys or "delivered" in sys

    def test_the_prompt_forbids_inventing_a_surface_suite(self) -> None:
        """Without a harness the LLM invents one — the failure mode the whole
        derivation design exists to prevent. A surface check is emitted only
        when the repo DECLARES it."""
        sys = derivation_planner_messages(manifests={}, changed_files=[], intent="x")[0]["content"]
        assert "declare" in sys.lower()

    def test_the_changed_file_scoping_rule_exempts_surface_checks(self) -> None:
        """The live suppressor. "Scope checks to the CHANGED files" is right for
        lint, and it silently guarantees a declared surface suite is NEVER run —
        the change is in ``src/``, the surface check is in its own directory."""
        sys = derivation_planner_messages(manifests={}, changed_files=[], intent="x")[0][
            "content"
        ].lower()
        scope_line = next(line for line in sys.splitlines() if "changed files" in line)
        assert "surface" in scope_line, (
            f"the scoping instruction must say it does not apply to surface checks: {scope_line!r}"
        )


class TestStatedConstraints:
    """An intent does not only say what to BUILD — it often says what NOT to do
    ("don't touch the tests", "no new dependencies", "don't write any files").

    Those are verifiable, and deterministically so: a constraint is a command
    whose exit code says whether it held. The deriver already RECEIVES the
    intent, so nothing needed building — it was simply never told to read the
    intent for constraints. Hardcoding them backend-side is not an option:
    constraints are natural language and therefore unbounded, which is exactly
    why the general mechanism (an LLM that already has the intent) is the right
    place. 형님: "이건 수많은 예시 케이스 중 하나니."
    """

    def test_the_prompt_asks_for_constraints_to_become_checks(self) -> None:
        sys = derivation_planner_messages(manifests={}, changed_files=[], intent="x")[0][
            "content"
        ].lower()
        assert "constraint" in sys, "the deriver was never told constraints are checkable"

    def test_the_prompt_names_no_specific_constraint(self) -> None:
        """Constraints are unbounded — the prompt teaches the SHAPE, never a
        list. A named constraint would be the hardcoding this exists to avoid,
        and would steer the model to look only for that one."""
        sys = derivation_planner_messages(manifests={}, changed_files=[], intent="x")[0][
            "content"
        ].lower()
        for hardcoded in ("don't touch the tests", "no new dependencies", "pyproject.toml"):
            assert hardcoded not in sys, f"prompt hardcodes a specific constraint: {hardcoded!r}"

    def test_the_baseline_reaches_the_deriver_when_known(self) -> None:
        """A constraint check has to compare against where the tree STOOD. The
        agent commits as it works, so a check that only sees the working tree
        misses everything already committed — and ``shell_exec`` writes never
        reach ``written_paths`` at all (run fae09a47: 62 shell_exec calls, every
        recorded ``writes`` empty, +108/-2 committed). The baseline is the only
        thing that catches those, and it already exists on the run."""
        user = derivation_planner_messages(
            manifests={}, changed_files=[], intent="x", baseline="abc1234"
        )[1]["content"]
        assert "abc1234" in user

    def test_no_baseline_is_stated_as_absent_not_invented(self) -> None:
        """A fabricated baseline would make every constraint check compare
        against a ref that does not exist — failing for the wrong reason."""
        user = derivation_planner_messages(manifests={}, changed_files=[], intent="x")[1]["content"]
        assert "baseline" not in user.lower() or "unknown" in user.lower()


class TestTheConstraintSurvivesTheEscapeHatch:
    """prod 실증 `30e36b62` (2026-08-20): 제약을 명시한 지시문 + 쓰기 0 인 런에서
    deriver 가 ``{"applicable": false, "commands": []}`` 를 냈다. 제약 검사는
    하나도 나오지 않았다.

    원인은 CONSTRAINTS 문단이 아니라 그 **바로 뒤** 문장이다 — *"명령으로 검증할
    수 없는 변경이면 applicable=false"* 가 정확히 이 경우에 해당해서 이긴다.
    #779 가 이미 가르친 실패 모드다: **나중 문장이 앞 문장을 이긴다.** 그래서
    앞에 강조를 더 붙이는 게 아니라 **이기는 문장 자체**를 고친다.

    제약은 산출물이 없어도 검사 가능하다 — "아무것도 쓰지 마라" 가 지켜졌다는 것은
    트리를 기준선과 대조하면 결정적으로 증명된다. 산출물이 없다는 것이 증명할 것이
    없다는 뜻은 아니다.
    """

    def test_the_escape_hatch_itself_knows_about_constraints(self) -> None:
        """The sentence that WINS must carry the exception. A CONSTRAINTS
        paragraph placed before it is overridden — measured, not guessed."""
        sys = derivation_planner_messages(manifests={}, changed_files=[], intent="x")[0]["content"]
        head, _, tail = sys.partition('set "applicable" to false')
        assert tail, "escape-hatch sentence not found — this pin needs updating"
        assert "constraint" in tail.lower(), (
            "the applicable=false escape must state that a stated constraint keeps "
            "the gate applicable; otherwise it silently swallows every constraint check"
        )

    def test_the_constraint_paragraph_still_teaches_only_the_shape(self) -> None:
        sys = derivation_planner_messages(manifests={}, changed_files=[], intent="x")[0][
            "content"
        ].lower()
        assert "constraint" in sys
        for hardcoded in ("don't touch the tests", "no new dependencies", "pyproject.toml"):
            assert hardcoded not in sys


class TestCiDeclarationsGrounding:
    """§10 실측 (prod, 2026-08-25): BSVibe 게이트 94건 중 `ruff check` 96% ·
    `mypy` 77% 인데 `ruff format --check` 45% · `lint-imports` 16%. 갈리는 지점이
    정확히 "명령 이름이 `.github/workflows/ci.yml` 에만 있는 것"이다.

    시스템 프롬프트는 이미 "manifests / build config / CI 에 근거하라",
    "레포가 VERBATIM 으로 선언한 명령(… a CI step)을 선호하라"고 지시한다 —
    그런데 CI 파일은 deriver 에게 **한 번도 보여준 적이 없다**. 따를 수 없는
    지시였다. 여기서는 CI 선언을 별도의 라벨 붙은 블록으로 준다.
    """

    _CI = (
        "jobs:\n  lint:\n    steps:\n"
        "      - run: uv run ruff format --check backend/\n"
        "      - run: uv run lint-imports\n"
    )

    def test_ci_declarations_reach_the_prompt_verbatim(self) -> None:
        msgs = derivation_planner_messages(
            manifests={"pyproject.toml": "[tool.ruff]\n"},
            changed_files=["money.py"],
            intent="Add money utilities",
            ci_declarations={".github/workflows/ci.yml": self._CI},
        )
        user = msgs[1]["content"]
        # The exact command names the repo declares — the two that were being
        # guessed at 45% / 16% because they exist ONLY here.
        assert "ruff format --check backend/" in user
        assert "lint-imports" in user
        assert ".github/workflows/ci.yml" in user

    def test_ci_is_its_own_block_distinct_from_the_manifests(self) -> None:
        """ "What the repo RUNS to check itself" and "what the repo DEPENDS on"
        are different questions; a model that cannot tell them apart cannot
        prefer a verbatim CI step over an inferred conventional invocation."""
        msgs = derivation_planner_messages(
            manifests={"pyproject.toml": "MANIFEST_MARKER"},
            changed_files=[],
            intent="x",
            ci_declarations={".github/workflows/ci.yml": "CI_MARKER"},
        )
        user = msgs[1]["content"]
        assert "MANIFEST_MARKER" in user and "CI_MARKER" in user
        # A header of its own, and the CI content lives under it — not merged
        # into the manifest listing.
        header = next(
            line
            for line in user.splitlines()
            if "CI" in line and line.rstrip().endswith(":") and "MARKER" not in line
        )
        head, _, tail = user.partition(header)
        assert "MANIFEST_MARKER" in head, "the manifest block must precede the CI block"
        assert "CI_MARKER" in tail, "the CI content must live under the CI header"
        assert "CI_MARKER" not in head, "CI content leaked into the manifest block"

    def test_no_ci_declarations_emits_no_empty_header(self) -> None:
        """A repo with no CI must not be told about an empty CI section — that
        is noise the model can only mis-ground on."""
        for ci in (None, {}):
            user = derivation_planner_messages(
                manifests={"pyproject.toml": "x"},
                changed_files=[],
                intent="x",
                ci_declarations=ci,
            )[1]["content"]
            assert "ci declaration" not in user.lower()
            assert "workflow" not in user.lower()

    def test_the_prompt_tells_the_model_what_a_ci_declaration_IS(self) -> None:
        """Handing over the bytes is half of it — the deriver also has to know
        that a check named there is one the repo REQUIRES of itself."""
        sys = derivation_planner_messages(manifests={}, changed_files=[], intent="x")[0][
            "content"
        ].lower()
        assert "ci declaration" in sys

    def test_the_ci_rule_keeps_the_prompt_stack_agnostic(self) -> None:
        """The whole point is that the REPO names its checks, not us. A tool
        name in our prompt would put the coupling straight back."""
        sys = derivation_planner_messages(manifests={}, changed_files=[], intent="x")[0][
            "content"
        ].lower()
        for tool in ("ruff", "pytest", "mypy", "cargo", "go test", "npm", "pnpm", "yarn", "uv run"):
            assert tool not in sys, f"prompt is stack-biased: mentions {tool!r}"

    def test_the_ci_rule_does_not_exempt_ci_steps_from_the_scope_rule(self) -> None:
        """A CI step lints the WHOLE repo; a gate that copies it verbatim fails
        the change on pre-existing debt in untouched files."""
        sys = derivation_planner_messages(manifests={}, changed_files=[], intent="x")[0][
            "content"
        ].lower()
        ci_line = next(line for line in sys.splitlines() if "ci declaration" in line)
        assert "scope" in ci_line, (
            f"the CI rule must restate that the scoping rule applies: {ci_line!r}"
        )
