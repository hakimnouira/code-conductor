import pytest

from service import Inventory


def test_add_normalizes_sku_and_accumulates_quantity():
    inventory = Inventory()
    item = inventory.add(" ab-12 ", 3)
    assert item.sku == "AB-12"
    assert item.quantity == 3
    updated = inventory.add("AB-12", 4)
    assert updated.sku == "AB-12"
    assert updated.quantity == 7
    assert inventory.available("ab-12") == 7


def test_remove_updates_quantity_and_unknown_sku_is_zero():
    inventory = Inventory()
    inventory.add("sku-1", 5)
    assert inventory.remove("SKU-1", 2).quantity == 3
    assert inventory.available("SKU-1") == 3
    assert inventory.available("missing") == 0


@pytest.mark.parametrize("quantity", [0, -1, True, 1.5])
def test_add_rejects_non_positive_integer_quantity(quantity):
    with pytest.raises(ValueError):
        Inventory().add("SKU-1", quantity)


def test_remove_rejects_invalid_quantity_and_overdraw():
    inventory = Inventory()
    inventory.add("SKU-1", 2)
    with pytest.raises(ValueError):
        inventory.remove("SKU-1", 0)
    with pytest.raises(ValueError):
        inventory.remove("SKU-1", 3)
    assert inventory.available("SKU-1") == 2


def test_sku_must_be_non_empty():
    inventory = Inventory()
    with pytest.raises(ValueError):
        inventory.add("   ", 1)
