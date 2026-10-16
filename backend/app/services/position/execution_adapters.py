from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any


class ExecutionBackend(str, Enum):
    PAPER = "paper"
    BITUNIX_LIVE = "bitunix_live"


@dataclass
class OrderRequest:
    symbol: str
    direction: str
    margin_usdt: float
    leverage: int
    order_type: str
    entry_price: float | None
    stop_loss: float
    take_profit: float
    ai_setup_id: str | None = None
    order_extra: dict[str, Any] | None = None


@dataclass
class ExecutionResult:
    success: bool
    position_id: str | None
    message: str
    payload: dict[str, Any]


@dataclass
class PositionSnapshot:
    position_id: str
    status: str
    symbol: str
    direction: str
    margin_usdt: float
    leverage: int
    entry_price: float | None
    mark_price: float | None
    unrealized_pnl: float
    extra: dict[str, Any]


class ExecutionAdapter(ABC):
    backend: ExecutionBackend

    @abstractmethod
    async def submit_order(self, req: OrderRequest) -> ExecutionResult:
        raise NotImplementedError

    @abstractmethod
    async def cancel_pending(self, position_id: str) -> ExecutionResult:
        raise NotImplementedError

    @abstractmethod
    async def close_position(self, position_id: str) -> ExecutionResult:
        raise NotImplementedError


class PaperExecutionAdapter(ExecutionAdapter):
    backend = ExecutionBackend.PAPER

    def __init__(self, service) -> None:
        self._service = service

    async def submit_order(self, req: OrderRequest) -> ExecutionResult:
        return await self._service.create_order(req)

    async def cancel_pending(self, position_id: str) -> ExecutionResult:
        return await self._service.cancel_pending(position_id)

    async def close_position(self, position_id: str) -> ExecutionResult:
        return await self._service.manual_close(position_id)


class BitunixLiveExecutionAdapter(ExecutionAdapter):
    """Future live trading — not enabled in this release."""

    backend = ExecutionBackend.BITUNIX_LIVE

    async def submit_order(self, req: OrderRequest) -> ExecutionResult:
        return ExecutionResult(
            success=False,
            position_id=None,
            message="Live Bitunix execution is not enabled",
            payload={"backend": self.backend.value},
        )

    async def cancel_pending(self, position_id: str) -> ExecutionResult:
        return ExecutionResult(
            success=False,
            position_id=position_id,
            message="Live Bitunix execution is not enabled",
            payload={},
        )

    async def close_position(self, position_id: str) -> ExecutionResult:
        return ExecutionResult(
            success=False,
            position_id=position_id,
            message="Live Bitunix execution is not enabled",
            payload={},
        )
