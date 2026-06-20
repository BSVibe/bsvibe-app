"""Tests for backend.common.clamp utility."""

import pytest

from backend.common.clamp import clamp


class TestClamp:
    """Test cases for clamp function."""

    def test_clamp_value_within_range(self) -> None:
        """Value within [low, high] should return unchanged."""
        assert clamp(5.0, 0.0, 10.0) == 5.0
        assert clamp(3.14, 0.0, 10.0) == 3.14

    def test_clamp_value_below_low(self) -> None:
        """Value below low should return low."""
        assert clamp(-5.0, 0.0, 10.0) == 0.0
        assert clamp(0.0, 1.0, 10.0) == 1.0

    def test_clamp_value_above_high(self) -> None:
        """Value above high should return high."""
        assert clamp(15.0, 0.0, 10.0) == 10.0
        assert clamp(100.0, 0.0, 10.0) == 10.0

    def test_clamp_value_equal_to_low(self) -> None:
        """Value equal to low should return low."""
        assert clamp(0.0, 0.0, 10.0) == 0.0

    def test_clamp_value_equal_to_high(self) -> None:
        """Value equal to high should return high."""
        assert clamp(10.0, 0.0, 10.0) == 10.0

    def test_clamp_low_greater_than_high_raises_error(self) -> None:
        """If low > high, should raise ValueError."""
        with pytest.raises(ValueError):
            clamp(5.0, 10.0, 0.0)

    def test_clamp_equal_bounds(self) -> None:
        """If low == high, should return that value."""
        assert clamp(5.0, 3.0, 3.0) == 3.0
        assert clamp(10.0, 5.0, 5.0) == 5.0
