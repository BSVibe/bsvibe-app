"""Tests for backend.util.textcase."""

import pytest

from backend.util.textcase import to_kebab


@pytest.mark.parametrize(
    "input_value,expected",
    [
        ("hello world", "hello-world"),
        ("HelloWorld", "helloworld"),
        ("hello---world", "hello-world"),
        ("---hello---world---", "hello-world"),
        ("", ""),
        ("!!!", ""),
        ("  ", ""),
        ("Hello World!", "hello-world"),
        ("camelCaseInput", "camelcaseinput"),
        ("snake_case_value", "snake-case-value"),
        ("Mixed--case__with...dots", "mixed-case-with-dots"),
    ],
)
def test_to_kebab(input_value: str, expected: str) -> None:
    assert to_kebab(input_value) == expected
