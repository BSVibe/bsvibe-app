"""Tests for backend.common.bytesize.format_bytes."""

from __future__ import annotations

import pytest

from backend.common.bytesize import format_bytes


class TestFormatBytesExactBoundaries:
    def test_zero(self) -> None:
        assert format_bytes(0) == "0 B"

    def test_one_byte(self) -> None:
        assert format_bytes(1) == "1 B"

    def test_1023_bytes(self) -> None:
        assert format_bytes(1023) == "1023 B"

    def test_one_kib(self) -> None:
        assert format_bytes(1024) == "1.0 KiB"

    def test_one_mib(self) -> None:
        assert format_bytes(1024**2) == "1.0 MiB"

    def test_one_gib(self) -> None:
        assert format_bytes(1024**3) == "1.0 GiB"

    def test_one_tib(self) -> None:
        assert format_bytes(1024**4) == "1.0 TiB"

    def test_one_pib(self) -> None:
        assert format_bytes(1024**5) == "1.0 PiB"


class TestFormatBytesMidValues:
    def test_1536_bytes_is_1_5_kib(self) -> None:
        assert format_bytes(1536) == "1.5 KiB"

    def test_half_mib(self) -> None:
        assert format_bytes(512 * 1024) == "512.0 KiB"

    def test_1_5_mib(self) -> None:
        assert format_bytes(int(1.5 * 1024**2)) == "1.5 MiB"

    def test_large_pib_value(self) -> None:
        result = format_bytes(2 * 1024**5)
        assert result == "2.0 PiB"


class TestFormatBytesPrecision:
    def test_default_precision_is_1(self) -> None:
        assert format_bytes(1536) == "1.5 KiB"

    def test_precision_zero(self) -> None:
        assert format_bytes(1536, precision=0) == "2 KiB"

    def test_precision_two(self) -> None:
        assert format_bytes(1536, precision=2) == "1.50 KiB"

    def test_precision_zero_bytes_unit(self) -> None:
        assert format_bytes(0, precision=0) == "0 B"


class TestFormatBytesNegativeInput:
    def test_negative_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            format_bytes(-1)

    def test_large_negative_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            format_bytes(-1024)
