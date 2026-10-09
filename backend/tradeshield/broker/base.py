"""The only interface the rest of the platform uses to talk to the exchange.

To use the real 021 sandbox, implement these methods in broker/o21.py - nothing else changes.
"""
from abc import ABC, abstractmethod
from ..models import OrderRequest, BrokerOrder


class BrokerRejected(Exception):
    """The broker explicitly refused the order (definitive - never retry)."""


class Broker(ABC):
    @abstractmethod
    async def start(self): ...

    @abstractmethod
    async def stop(self): ...

    @abstractmethod
    async def place_order(self, req: OrderRequest) -> BrokerOrder:
        """MUST be idempotent on req.client_order_id (resending returns the same order)."""

    @abstractmethod
    async def cancel_order(self, client_order_id: str): ...

    @abstractmethod
    async def get_order(self, client_order_id: str):
        """Return BrokerOrder (with .fills) or None if the broker has never seen this id."""

    @abstractmethod
    async def get_open_orders(self) -> list: ...

    @abstractmethod
    async def get_positions(self) -> dict:
        """symbol -> net quantity for the whole account."""

    @abstractmethod
    def subscribe_ticks(self, cb): ...

    @abstractmethod
    def unsubscribe_ticks(self, cb): ...

    @abstractmethod
    def subscribe_fills(self, cb): ...

    @abstractmethod
    def unsubscribe_fills(self, cb): ...
