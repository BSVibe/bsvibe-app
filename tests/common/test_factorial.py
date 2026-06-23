"""Tests for backend.common.factorial utility."""

import pytest

from backend.common.factorial import factorial


class TestFactorial:
    """Test cases for factorial function."""

    def test_factorial_zero(self) -> None:
        assert factorial(0) == 1

    def test_factorial_one(self) -> None:
        assert factorial(1) == 1

    def test_factorial_small_positive(self) -> None:
        assert factorial(5) == 120

    def test_factorial_larger_positive(self) -> None:
        assert factorial(10) == 3628800

    def test_factorial_two(self) -> None:
        assert factorial(2) == 2

    def test_factorial_negative_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            factorial(-1)

    def test_factorial_large_negative_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            factorial(-100)
