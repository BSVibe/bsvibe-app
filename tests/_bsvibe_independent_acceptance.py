import pytest

from backend.common.factorial import factorial


def test_factorial_zero():
    assert factorial(0) == 1


def test_factorial_one():
    assert factorial(1) == 1


def test_factorial_small_positive():
    assert factorial(5) == 120


def test_factorial_larger_positive():
    assert factorial(10) == 3628800


def test_factorial_negative_raises_value_error():
    with pytest.raises(ValueError):
        factorial(-1)


def test_factorial_negative_large_raises_value_error():
    with pytest.raises(ValueError):
        factorial(-100)


def test_factorial_negative_error_message_contains_input():
    with pytest.raises(ValueError, match="-3"):
        factorial(-3)