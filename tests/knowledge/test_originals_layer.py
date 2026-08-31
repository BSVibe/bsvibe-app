"""원본 레이어 — 런타임 원본(요청·피드백·회고)을 vault 에 불변으로 남긴다.

형님 요구(2026-08-31): *"실제 사용 과정에서 사용자의 요청, 피드백, 혹은 스스로
깨달은 회고 등이 원본 그대로 저장 되어야 한다. 상위 지식은 여러 원본이 합쳐진
것이기 때문에 변경 등이 될 수 있지만, 원본 지식은 히스토리성이기 때문에 보존된다."*

측정으로 확인된 격차(prod 2026-08-31): 요청 223건·피드백 41건·settle 147건이
DB 운영 테이블에만 있고 vault 에는 0건. 셋 다 ``execution_runs`` 에
``ON DELETE CASCADE`` 로 묶여 런 삭제 시 함께 사라진다.

이 스위트가 고정하는 불변식은 **불변성**이다 — 같은 키로 다시 쓰려 해도 처음
바이트가 이긴다. 상위 지식(concepts)은 바뀌어도 원본은 안 바뀐다.

``Vault`` 를 통째로 주입받는 이유: 워크스페이스 vault 위치의 정의는
:func:`~backend.knowledge.graph.vault_paths.workspace_vault_root` 하나뿐이어야
한다(``test_one_vault_root_definition``). ``vault_root`` 와 ``workspace_id`` 를
따로 받으면 이 모듈이 레이아웃을 두 번째로 조립하게 된다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.knowledge.graph.vault import Vault
from backend.knowledge.originals import ORIGINAL_KINDS, record_original


def _vault(tmp_path: Path) -> Vault:
    vault = Vault(tmp_path)
    vault.ensure_dirs()
    return vault


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class TestOriginalIsStoredVerbatim:
    """원본은 가공 없이 그대로 남는다."""

    @pytest.mark.asyncio
    async def test_request_body_is_the_intent_verbatim(self, tmp_path: Path) -> None:
        """요청 원본의 본문은 형님이 쓴 글자 그대로여야 한다.

        settle 이 만드는 관찰 노트는 요약·가공된 것이고, 원본 레이어는 그
        가공 이전을 보존하는 것이 존재 이유다.
        """
        intent = (
            "라우팅 규칙이 지금 이 워크스페이스에 몇 개 있는지 한 문장으로만 답해줘.\n"
            "조사만 하고 보고해라 — 파일은 하나도 쓰지 마라."
        )

        path = await record_original(
            vault=_vault(tmp_path),
            kind="request",
            key="9a8861ec-e76c-4c8d-afc6-296451f129de",
            title="라우팅 규칙 개수 확인",
            content=intent,
        )

        assert path is not None
        body = _read(path).split("---\n", 2)[2]
        assert body.strip() == intent

    @pytest.mark.asyncio
    async def test_non_ascii_survives_byte_exact(self, tmp_path: Path) -> None:
        """한국어 원본이 바이트 단위로 살아남아야 한다.

        형님 요청은 전부 한국어다. ASCII 픽스처로만 검사하면 인코딩·길이
        단위 혼동을 영원히 못 잡는다.
        """
        content = "형님 피드백: 가장 단순한 게 가장 낫다 — 다른 작업에도 동일 적용.\n이모지도 살아야 한다 🧠"

        path = await record_original(
            vault=_vault(tmp_path),
            kind="feedback",
            key="71c7a930-7731-4f59-999f-2bc9820ce95a",
            title="단순함 우선 원칙",
            content=content,
        )

        assert path is not None
        assert content.encode() in path.read_bytes()


class TestOriginalIsImmutable:
    """히스토리성 — 한 번 쓴 원본은 절대 바뀌지 않는다."""

    @pytest.mark.asyncio
    async def test_rewriting_the_same_key_does_not_change_the_stored_bytes(
        self, tmp_path: Path
    ) -> None:
        """같은 키로 다른 내용을 써도 처음 바이트가 이긴다.

        이것이 이 레이어의 존재 이유다. 백필과 실시간 기록이 같은 행을 두 번
        건드려도 과거가 다시 쓰이면 안 된다.
        """
        vault = _vault(tmp_path)
        first = await record_original(
            vault=vault,
            kind="retrospect",
            key="run-6b14b269",
            title="회고",
            content="처음 깨달은 것",
        )
        assert first is not None
        before = first.read_bytes()

        second = await record_original(
            vault=vault,
            kind="retrospect",
            key="run-6b14b269",
            title="회고",
            content="나중에 덮어쓰려는 것",
        )

        assert second is None, "이미 있는 원본은 다시 쓰지 않고 None 을 돌려줘야 한다"
        assert first.read_bytes() == before
        assert "나중에 덮어쓰려는 것".encode() not in before

    @pytest.mark.asyncio
    async def test_one_file_per_key(self, tmp_path: Path) -> None:
        """같은 키로 두 번 불러도 파일은 하나다 (타임스탬프 이름이면 둘이 된다)."""
        vault = _vault(tmp_path)
        for _ in range(2):
            await record_original(
                vault=vault, kind="request", key="req-abc", title="t", content="c"
            )

        written = list((tmp_path / "seeds" / "request").glob("*.md"))
        assert len(written) == 1


class TestOriginalIsAddressable:
    """저장 위치가 기존 열람·검색 표면에 그대로 걸려야 한다."""

    @pytest.mark.asyncio
    async def test_each_kind_lands_under_its_own_seeds_subdir(self, tmp_path: Path) -> None:
        """세 종류가 seeds/<kind>/ 로 갈린다.

        ``seeds`` 는 이미 note 열람 화이트리스트(``_NOTE_DIRS``)와 검색 색인
        카테고리 양쪽에 있다 — 여기 떨구면 GUI 열람과 검색이 인프라 추가 없이
        따라온다.
        """
        vault = _vault(tmp_path)
        for kind in ORIGINAL_KINDS:
            path = await record_original(
                vault=vault,
                kind=kind,
                key=f"k-{kind}",
                title=f"제목 {kind}",
                content=f"본문 {kind}",
            )
            assert path is not None
            assert path.parent == tmp_path / "seeds" / kind

    @pytest.mark.asyncio
    async def test_note_read_whitelist_admits_the_written_path(self, tmp_path: Path) -> None:
        """쓴 경로가 실제로 note 열람 API 가 허용하는 형태여야 한다.

        경로를 문자열로 짐작하지 않고 프로덕션 판별기를 그대로 불러 확인한다.
        """
        from backend.api.v1.inside.note import _is_note_path

        path = await record_original(
            vault=_vault(tmp_path), kind="request", key="req-1", title="t", content="c"
        )
        assert path is not None
        assert _is_note_path(str(path.relative_to(tmp_path)))

    @pytest.mark.asyncio
    async def test_provenance_rides_the_frontmatter(self, tmp_path: Path) -> None:
        """어느 런/결정에서 나왔는지가 원본에 박혀 있어야 파생 지식과 이어진다."""
        path = await record_original(
            vault=_vault(tmp_path),
            kind="retrospect",
            key="act-1",
            title="t",
            content="c",
            provenance={"run_id": "run-42", "activity_id": "act-1"},
        )
        assert path is not None
        head = _read(path).split("---\n", 2)[1]
        assert "run-42" in head
        assert "kind: retrospect" in head


class TestRecordingNeverBreaksTheCaller:
    """기록 실패가 런을 죽이면 안 된다 — 부수적 기록이지 본업이 아니다."""

    @pytest.mark.asyncio
    async def test_unwritable_vault_returns_none_instead_of_raising(self, tmp_path: Path) -> None:
        """디렉터리 대신 파일이 가로막고 있어도 예외가 호출자에게 새면 안 된다."""
        vault = Vault(tmp_path)
        (tmp_path / "seeds").write_text("나는 디렉터리가 아니다", encoding="utf-8")

        result = await record_original(
            vault=vault, kind="request", key="req-1", title="t", content="c"
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_empty_content_is_not_recorded(self, tmp_path: Path) -> None:
        """빈 원본은 원본이 아니다 — 원클릭 승인처럼 0자인 것은 기록하지 않는다.

        settle 싱크가 이미 같은 판단을 한다(형님이 글자를 쓴 것만 노트가 된다).
        원본 레이어도 같은 기준이어야 빈 파일이 쌓이지 않는다.
        """
        result = await record_original(
            vault=_vault(tmp_path), kind="feedback", key="dec-1", title="t", content="   \n  "
        )

        assert result is None
        assert not list((tmp_path / "seeds" / "feedback").glob("*.md"))
