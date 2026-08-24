"""벤더 빌트인 denylist 가 썩었다 — prod 에서 에이전트 런 전부가 죽었다.

``claude_code._NATIVE_TOOLS`` 주석이 이 상황을 예고하고 있었다:

    *"An enumerated denylist over a vendor's built-ins **ROTS** … A tool added in
    the next CLI release would silently hand the agent back the user's filesystem.
    So this list is BEST EFFORT, and the guarantee is elsewhere:
    ``_exposed_tools_are_ours`` … aborts the task."*

**실측 (워커 로그, 2026-08-24 14:16:28 / 14:16:34 KST, 런 ``cb37cbcd``):**

    claude_code_unsanctioned_tools problem='unsanctioned: ListAgents, ReportFindings, SendMessage'

가드는 설계대로 정확히 작동했다 — 조용히 파운더 파일시스템을 내주는 대신 두 번의
시도 모두 큰 소리로 멈췄고, 런은 정직하게 실패했다. 하지만 **denylist 가 썩은 채로는
어떤 에이전트 런도 성공할 수 없다.** client_attach 만의 문제가 아니다: ``server_sandbox``
런도 같은 분기·같은 목록을 쓰므로 같은 CLI 에서 똑같이 죽는다.

⚠️ init 이벤트가 3개만 지목한 것은 **그 환경이 그 3개만 노출했기 때문**이지 목록의
구멍이 3개라는 뜻이 아니다. 현행 CLI 빌트인 전수와 대조하면 **9개**가 빠져 있었다.
노출되지 않은 이름을 미리 거부하는 것은 무해하고, 다음 런 모양에서 새는 것을 막는다.
"""

from __future__ import annotations

#: prod 런 ``cb37cbcd`` 의 ``system/init`` 이 실제로 노출해 abort 를 일으킨 셋.
_ABORTED_A_PROD_RUN = ("ListAgents", "ReportFindings", "SendMessage")

#: 같은 세대 CLI 의 빌트인인데 목록에 없던 나머지. 이 런에서는 노출되지 않았을 뿐이다.
_ALSO_MISSING = (
    "Agent",
    "Artifact",
    "ListMcpResourcesTool",
    "ReadMcpResourceDirTool",
    "ReadMcpResourceTool",
    "SendUserFile",
)


def test_the_builtins_that_aborted_a_prod_run_are_denied() -> None:
    from backend.executors.worker.claude_code import _NATIVE_TOOLS

    denied = set(_NATIVE_TOOLS.split())
    missing = [name for name in _ABORTED_A_PROD_RUN if name not in denied]
    assert not missing, f"prod 에서 런을 죽인 빌트인이 아직 거부되지 않는다: {missing}"


def test_the_rest_of_todays_builtin_surface_is_denied() -> None:
    from backend.executors.worker.claude_code import _NATIVE_TOOLS

    denied = set(_NATIVE_TOOLS.split())
    missing = [name for name in _ALSO_MISSING if name not in denied]
    assert not missing, f"현행 CLI 빌트인이 아직 거부되지 않는다: {missing}"


def test_the_guard_is_still_the_guarantee() -> None:
    """양성 대조군 — 목록을 채우는 것은 편의이고, **보증은 노출 검사**다.

    목록은 또 썩는다. 다음에 썩었을 때도 조용히 새는 게 아니라 멈춰야 한다."""
    from backend.executors.worker.claude_code import _exposed_tools_are_ours

    event = {
        "type": "system",
        "subtype": "init",
        "tools": ["mcp__bsvibe__bsvibe_work_file_read", "SomeToolInventedNextRelease"],
    }
    problem = _exposed_tools_are_ours(event, ["mcp__bsvibe__bsvibe_work_file_read"])
    assert problem is not None
    assert "SomeToolInventedNextRelease" in problem
