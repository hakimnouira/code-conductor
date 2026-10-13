import pytest

from discount import calculate_discount


def test_discount_applies_percentage():
    assert calculate_discount(100, 25) == 75
    assert calculate_discount(80, 10) == 72


def test_zero_and_full_discount():
    assert calculate_discount(12, 0) == 12
    assert calculate_discount(12, 100) == 0


@pytest.mark.parametrize("price,percent", [(-1, 10), (10, -1), (10, 101)])
def test_invalid_values_raise_value_error(price, percent):
    with pytest.raises(ValueError):
        calculate_discount(price, percent)
