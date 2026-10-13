"""Inventory service starter. Complete the TODOs without changing the public API."""
from models import InventoryItem


class Inventory:
    def __init__(self):
        self._items: dict[str, InventoryItem] = {}

    def add(self, sku: str, quantity: int) -> InventoryItem:
        raise NotImplementedError("Implement inventory addition")

    def remove(self, sku: str, quantity: int) -> InventoryItem:
        raise NotImplementedError("Implement inventory removal")

    def available(self, sku: str) -> int:
        raise NotImplementedError("Implement inventory lookup")
