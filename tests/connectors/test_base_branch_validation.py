"""base_branch 저장 시점 검증 — ``-`` 로 시작하는 값(git 옵션 주입)을 거부한다."""

import pytest

from backend.connectors.github_access import (
    is_valid_base_branch,
    reject_server_owned_installation,
)

_EVIL = "--upload-pack=touch /tmp/x;git-upload-pack"


@pytest.mark.parametrize("name", [_EVIL, "-x", "a..b", "a b", "x.lock", "", "a:b", "/a"])
def test_bad_base_branch_rejected_on_save(name: str) -> None:
    assert not is_valid_base_branch(name)
    with pytest.raises(ValueError, match="base_branch"):
        reject_server_owned_installation(
            "github", external_ref=None, delivery_config={"base_branch": name}
        )


@pytest.mark.parametrize("name", ["main", "dev", "release/1.0", "feat/x_y-z"])
def test_good_base_branch_accepted(name: str) -> None:
    reject_server_owned_installation(
        "github", external_ref=None, delivery_config={"base_branch": name}
    )
