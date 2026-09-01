"""``bsvibe products`` / ``runs`` / ``deliverables`` — 터미널에서 제품을 본다.

실측(2026-08-31): CLI 는 `login`/`logout`/`status`/`pat` 과 워커 등록뿐이었다 —
**auth + 워커 등록 전용**이고 제품 표면은 0 이었다. 형님이 SSH 로 붙은 호스트에서
*"지금 뭐가 돌고 있지"* 를 물으려면 브라우저를 열거나 MCP 클라이언트를 붙여야
했다.

REST 는 이미 다 있다(`GET /api/v1/products` · `runs` · `deliverables`). 없던 것은
서브시스템이 아니라 **명령 몇 개**다.

이 스위트가 고정하는 것:

* 목록이 **파이프 가능**하다 — `--quiet` 는 id 만 한 줄에 하나씩 낸다. 사람이
  읽는 장식은 stderr 로 가므로 `for id in $(bsvibe products list --quiet)` 가 안전하다.
* 미로그인은 스택트레이스가 아니라 **다음에 칠 명령**을 알려준다.
* HTTP 실패는 **서버의 말 그대로** 보고된다 — 삼키면 다음 한 시간을 태운다.
* 읽기 전용이다. 이 PR 은 **아무것도 변경하지 않는다** — 터미널에서 실수로 런을
  지우거나 취소하는 표면을 열지 않는다.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from backend.executors.worker import cli as cli_mod
from backend.executors.worker.credentials import CredentialsNotFound, HostCredentials

CREDS = HostCredentials(
    access_token="access-token-xyz",
    refresh_token="refresh-token-xyz",
    expires_at=99999999999,
    issuer="https://api.bsvibe.dev",
)

PRODUCTS = [
    {
        "id": "6c19a033-748d-48a6-8237-1ba83c49b5e8",
        "name": "BStockReport",
        "slug": "bstockreport",
        "repo_url": "https://github.com/blas1n/BStockReport",
        "bootstrap_status": "skipped:client_attach",
    },
    {
        "id": "2cad16bd-1258-4ab9-8f7d-74d403847354",
        "name": "BSVibe",
        "slug": "bsvibe",
        "repo_url": "https://github.com/BSVibe/BSvibe-app",
        "bootstrap_status": "complete",
    },
]

RUNS = [
    {
        "id": "b5c48946-1600-4de5-a3c0-eb3036b27c25",
        "product_id": PRODUCTS[1]["id"],
        "status": "review_ready",
        "intent": "라우팅 규칙이 몇 개인지 한 문장으로만 답해줘.\n조사만 하고 보고해라.",
        "created_at": "2026-08-31T02:36:20Z",
    }
]

DELIVERABLES = [
    {
        "id": "cc7d1bc6-8012-4d30-83dc-f1a6f45393da",
        "run_id": RUNS[0]["id"],
        "deliverable_type": "code",
        "verified": True,
        "created_at": "2026-08-31T02:44:17Z",
    }
]


@pytest.fixture
def signed_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_mod, "load_host_credentials", lambda: CREDS)


def _serve(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    monkeypatch.setattr(cli_mod, "_api_transport", lambda: httpx.MockTransport(handler))


def _json(payload: Any, *, seen: dict[str, Any] | None = None) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=payload)

    return handler


class TestTheCommandsExist:
    def test_the_parser_advertises_the_product_surface(self) -> None:
        help_text = cli_mod.build_bsvibe_parser().format_help()
        for command in ("products", "runs", "deliverables"):
            assert command in help_text, f"`bsvibe {command}` 가 도움말에 없다"


class TestProductsList:
    def test_it_reads_the_products_endpoint_with_the_bearer(
        self, signed_in: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, Any] = {}
        _serve(monkeypatch, _json(PRODUCTS, seen=seen))

        assert cli_mod.run_bsvibe_cli(["products", "list"]) == 0

        assert seen["url"] == "https://api.bsvibe.dev/api/v1/products"
        assert seen["auth"] == "Bearer access-token-xyz"

    def test_the_human_listing_names_the_product(
        self, signed_in: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _serve(monkeypatch, _json(PRODUCTS))

        assert cli_mod.run_bsvibe_cli(["products", "list"]) == 0

        out = capsys.readouterr().out
        assert "bstockreport" in out
        assert "BStockReport" in out
        # bootstrap 상태는 형님이 가장 자주 확인하는 축이다 — 분석이 끝났는지.
        assert "skipped:client_attach" in out

    def test_quiet_prints_only_ids_one_per_line(
        self, signed_in: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``for id in $(bsvibe products list --quiet)`` 가 안전해야 한다."""
        _serve(monkeypatch, _json(PRODUCTS))

        assert cli_mod.run_bsvibe_cli(["products", "list", "--quiet"]) == 0

        captured = capsys.readouterr()
        assert captured.out.split() == [p["id"] for p in PRODUCTS]

    def test_an_empty_workspace_says_so_without_failing(
        self, signed_in: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """비어 있는 것은 오류가 아니다 — rc 0 이고 다음에 뭘 할지 알려준다."""
        _serve(monkeypatch, _json([]))

        assert cli_mod.run_bsvibe_cli(["products", "list"]) == 0
        assert "No products" in capsys.readouterr().out

    def test_quiet_on_an_empty_workspace_prints_nothing(
        self, signed_in: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--quiet 는 사람용 문장도 내면 안 된다 — 스크립트가 그걸 id 로 읽는다."""
        _serve(monkeypatch, _json([]))

        assert cli_mod.run_bsvibe_cli(["products", "list", "--quiet"]) == 0
        assert capsys.readouterr().out == ""


class TestRunsList:
    def test_it_reads_the_runs_endpoint(
        self, signed_in: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, Any] = {}
        _serve(monkeypatch, _json(RUNS, seen=seen))

        assert cli_mod.run_bsvibe_cli(["runs", "list"]) == 0

        assert seen["url"].startswith("https://api.bsvibe.dev/api/v1/runs")

    def test_the_intent_is_shown_on_one_line(
        self, signed_in: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """지시문은 여러 줄이다. 목록이 줄바꿈으로 깨지면 한 화면에 안 들어온다."""
        _serve(monkeypatch, _json(RUNS))

        assert cli_mod.run_bsvibe_cli(["runs", "list"]) == 0

        out = capsys.readouterr().out
        assert len(out.strip().splitlines()) == 1, f"런 한 건은 한 줄이어야 한다:\n{out}"
        assert "review_ready" in out
        assert "라우팅 규칙" in out

    def test_it_sends_only_parameters_the_server_knows(
        self, signed_in: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """서버가 모르는 쿼리는 **에러가 아니라 무시**된다.

        처음엔 ``--product`` 를 붙였고 테스트는 green 이었다 — mock 이 아무 쿼리나
        받아줬기 때문이다. 실제 ``GET /api/v1/runs`` 는 ``limit`` 하나만 받으므로,
        prod 에서는 필터가 걸린 것처럼 보이면서 전체가 돌아왔을 것이다.

        그래서 판정을 뒤집는다: 보내는 쿼리가 **서버 시그니처의 부분집합**인지 본다.
        """
        seen: dict[str, Any] = {}
        _serve(monkeypatch, _json(RUNS, seen=seen))

        assert cli_mod.run_bsvibe_cli(["runs", "list", "--limit", "5"]) == 0

        sent = set(httpx.URL(seen["url"]).params.keys())
        assert sent <= {"limit"}, f"서버가 모르는 쿼리를 보냈다: {sent - {'limit'}}"


class TestDeliverablesList:
    def test_it_reads_the_deliverables_endpoint(
        self, signed_in: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        seen: dict[str, Any] = {}
        _serve(monkeypatch, _json(DELIVERABLES, seen=seen))

        assert cli_mod.run_bsvibe_cli(["deliverables", "list"]) == 0

        assert seen["url"].startswith("https://api.bsvibe.dev/api/v1/deliverables")
        assert DELIVERABLES[0]["id"] in capsys.readouterr().out


class TestItFailsLoudly:
    def test_not_signed_in_prints_the_next_command(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def _raise() -> HostCredentials:
            raise CredentialsNotFound("no credential file")

        monkeypatch.setattr(cli_mod, "load_host_credentials", _raise)

        assert cli_mod.run_bsvibe_cli(["products", "list"]) == 1

        err = capsys.readouterr().err
        assert "bsvibe login" in err, "다음에 칠 명령을 알려줘야 한다"

    def test_an_http_failure_reports_the_servers_own_words(
        self, signed_in: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """삼킨 이유는 다음 한 시간을 태운다."""

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, text="workspace not selected")

        _serve(monkeypatch, handler)

        assert cli_mod.run_bsvibe_cli(["products", "list"]) == 1

        err = capsys.readouterr().err
        assert "403" in err
        assert "workspace not selected" in err


class TestTheSurfaceStaysReadOnly:
    def test_no_mutating_verb_is_exposed(self) -> None:
        """이 PR 은 조회만 연다.

        터미널에서 실수로 런을 취소하거나 산출물을 회수하는 표면은 승인 흐름
        (Safe Mode·체크포인트)을 우회한다. 그건 별도 결정이지 이 PR 의 몫이 아니다.
        """
        help_text = cli_mod.build_bsvibe_parser().format_help()
        for verb in ("cancel", "discard", "retract", "delete"):
            assert verb not in help_text, f"변경 동사 '{verb}' 가 열렸다"


class TestTheFixturesAreNotImagined:
    """⚠️ 이 클래스가 이 파일에서 가장 값진 부분이다.

    처음 쓴 픽스처는 **MCP 응답 모양**을 보고 지은 것이었다. REST 는 다르다:

    ======================  ===========================  =========================
    필드                     MCP `bsvibe_products_list`   REST `ProductResponse`
    ======================  ===========================  =========================
    ``execution_target``     있음                          **없음**
    ``retracted_at``         (deliverables) 있음           **없음** (``verified``)
    ======================  ===========================  =========================

    mock 은 아무 dict 나 받아 주므로 그 차이가 **테스트에서는 영원히 green** 이고,
    prod 에서는 목록이 통째로 ``-`` 가 된다. 그래서 픽스처를 서버의 실제 스키마에
    대조한다 — 필드 이름을 나열하는 게 아니라 **모델에서 뽑아** 비교하므로,
    스키마가 바뀌면 여기가 먼저 빨개진다.
    """

    @pytest.mark.parametrize(
        ("fixture", "model_path"),
        [
            (PRODUCTS[0], "backend.api.v1.products._schemas:ProductResponse"),
            (RUNS[0], "backend.workflow.serialization.run_views:RunResponse"),
            (
                DELIVERABLES[0],
                "backend.workflow.serialization.deliverable_views:DeliverableResponse",
            ),
        ],
    )
    def test_every_fixture_key_exists_on_the_server_model(
        self, fixture: dict[str, Any], model_path: str
    ) -> None:
        import importlib

        module_name, _, attr = model_path.partition(":")
        model = getattr(importlib.import_module(module_name), attr)
        invented = set(fixture) - set(model.model_fields)
        assert not invented, (
            f"{attr} 에 없는 필드를 픽스처가 지어냈다: {sorted(invented)} — "
            "mock 은 이걸 받아 주지만 prod 응답에는 없다"
        )
