"""Polymarket CLOB V2 execution client.

The legacy py-clob-client package was archived and is not used here. All V2
SDK calls are synchronous, so they are isolated behind asyncio.to_thread to
keep the trading event loop responsive.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

import aiosqlite

from core.secrets import get_secret
from execution.clients.base import BaseExecutionClient, OrderResult
from execution.clients.polymarket_book import BookResolver
from execution.enums import Side
from execution.models import OrderLeg

logger = logging.getLogger(__name__)


class PolymarketExecutionClientV2(BaseExecutionClient):
    def __init__(self, db_connection: aiosqlite.Connection, private_key: str | None = None,
                 funder: str | None = None, chain_id: int = 137) -> None:
        super().__init__(db_connection, platform_label="polymarket")
        self._book_resolver = BookResolver(db_connection)
        self.private_key = private_key or get_secret("POLYMARKET_PRIVATE_KEY", "") or ""
        self.funder = funder or get_secret("POLYMARKET_WALLET_ADDRESS", "") or ""
        self.chain_id = chain_id
        self.host = os.getenv("POLYMARKET_API_BASE", "https://clob.polymarket.com")
        self.signature_type = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "0"))
        self._client = None
        self._initialized = False

    def _ensure_client(self):
        if self._initialized:
            return
        from py_clob_client_v2 import ApiCreds, ClobClient, SignatureTypeV2
        if not self.private_key:
            raise ValueError("POLYMARKET_PRIVATE_KEY is required")
        kwargs = {"host": self.host, "chain_id": self.chain_id, "key": self.private_key}
        if self.funder:
            kwargs["funder"] = self.funder
            kwargs["signature_type"] = self.signature_type
        api_key = get_secret("POLYMARKET_API_KEY", "") or ""
        api_secret = get_secret("POLYMARKET_API_SECRET", "") or ""
        api_passphrase = get_secret("POLYMARKET_API_PASSPHRASE", "") or ""
        if api_key and api_secret and api_passphrase:
            kwargs["creds"] = ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=api_passphrase)
        self._client = ClobClient(**kwargs)
        if not kwargs.get("creds"):
            creds = self._client.create_or_derive_api_key()
            self._client.set_api_creds(creds)
        self._initialized = True

    async def _call(self, fn, *args, **kwargs):
        return await asyncio.to_thread(fn, *args, **kwargs)

    async def submit_order(self, leg: OrderLeg, signal_id: str | None = None, strategy: str | None = None) -> OrderResult:
        start = time.time()
        try:
            resolved = await self._book_resolver.resolve(leg.market_id, leg.side, leg.size, leg.limit_price)
            if resolved is None:
                raise ValueError("BookResolver rejected order")
            self._ensure_client()
            from py_clob_client_v2 import OrderArgs, OrderType, PartialCreateOrderOptions, Side as PolySide
            side = PolySide.BUY if resolved.side is Side.BUY else PolySide.SELL
            tick = await self._call(self._client.get_tick_size, resolved.token_id)
            response = await self._call(
                self._client.create_and_post_order,
                OrderArgs(token_id=resolved.token_id, price=resolved.limit_price, side=side, size=resolved.size),
                PartialCreateOrderOptions(tick_size=str(tick)),
                OrderType.GTC,
            )
            order_id = response.get("orderID") or response.get("order_id") or response.get("id")
            if not order_id:
                raise RuntimeError(f"Polymarket V2 returned no order id: {response}")
            await self.write_order(leg, OrderResult(order_id=order_id, platform="polymarket", status="pending",
                                                     submission_latency_ms=int((time.time() - start) * 1000)),
                                   signal_id=signal_id, strategy=strategy)
            result = await self._poll(order_id, leg, start, signal_id, strategy)
            return result
        except Exception as exc:
            result = OrderResult(order_id=f"FAILED-{leg.market_id}", platform="polymarket", status="failed",
                                  submission_latency_ms=int((time.time() - start) * 1000), error_message=str(exc))
            await self.write_order(leg, result, signal_id=signal_id, strategy=strategy)
            logger.exception("Polymarket V2 order failed")
            return result

    async def _poll(self, order_id, leg, start, signal_id, strategy, max_polls=30):
        for _ in range(max_polls):
            await asyncio.sleep(0.25)
            order = await self._call(self._client.get_order, order_id)
            status = str(order.get("status", "")).upper()
            matched = float(order.get("size_matched", order.get("sizeMatched", 0)) or 0)
            if status in {"MATCHED", "UNMATCHED", "CANCELED", "CANCELLED"}:
                if status != "MATCHED" and matched < float(leg.size):
                    await self._cancel_if_open(order_id)
                fill_price = float(order.get("price", leg.limit_price or 0)) if matched else None
                if matched > 0:
                    # For a resting limit order the order price is a safe
                    # conservative fill price. Trade-level reconciliation can
                    # replace it later with weighted actual trade prices.
                    fee = await self._estimate_fee(leg.market_id, fill_price, matched)
                    result = OrderResult(order_id=order_id, platform="polymarket",
                                         status="filled" if matched >= leg.size else "partially_filled",
                                         submission_latency_ms=int((time.time() - start) * 1000),
                                         fill_latency_ms=int((time.time() - start) * 1000),
                                         filled_price=fill_price, filled_size=matched, fee_paid=fee)
                    await self.update_order_fill(result)
                    await self.write_fill_event(result)
                    return result
                result = OrderResult(order_id=order_id, platform="polymarket", status="failed",
                                     submission_latency_ms=int((time.time() - start) * 1000),
                                     error_message=f"terminal status={status}")
                await self.update_order_fill(result)
                return result
        await self._cancel_if_open(order_id)
        return OrderResult(order_id=order_id, platform="polymarket", status="pending",
                           submission_latency_ms=int((time.time() - start) * 1000),
                           error_message="fill poll timeout; order was cancelled and requires reconciliation")

    async def _estimate_fee(self, condition_id, price, size):
        try:
            info = await self._call(self._client.get_clob_market_info, condition_id)
            fd = info.get("fd") or {}
            rate = float(fd.get("r", 0.0))
            exponent = float(fd.get("e", 2.0))
            return round(size * rate * (price ** exponent) * ((1.0 - price) ** exponent), 5)
        except Exception:
            return 0.0

    async def _cancel_if_open(self, order_id):
        try:
            from py_clob_client_v2 import OrderPayload
            await self._call(self._client.cancel_order, OrderPayload(orderID=order_id))
            return True
        except Exception:
            logger.exception("Failed to cancel Polymarket V2 order %s", order_id)
            return False

    async def cancel_order(self, order_id: str) -> bool:
        return await self._cancel_if_open(order_id)

    async def get_order_status(self, order_id: str) -> dict | None:
        try:
            self._ensure_client()
            return await self._call(self._client.get_order, order_id)
        except Exception:
            logger.exception("Polymarket V2 get_order failed")
            return None

    async def get_balance(self) -> float | None:
        try:
            self._ensure_client()
            from py_clob_client_v2 import BalanceAllowanceParams, AssetType
            result = await self._call(self._client.get_balance_allowance,
                                      BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
            raw = result.get("balance", result.get("balance_dollars", 0))
            return float(raw) / 1e6 if float(raw) > 1000 else float(raw)
        except Exception:
            logger.exception("Polymarket V2 balance lookup failed")
            return None

    async def close(self) -> None:
        return None
