"""Domain models for the inventory benchmark."""
from dataclasses import dataclass


@dataclass(frozen=True)
class InventoryItem:
    sku: str
    quantity: int
