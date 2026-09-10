"""Polymarket CLOB V2 execution client."""

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

logger = logging.getLogger(__name__)


class PolymarketExecutionClientV2(BaseExecutionClient):
    def __init__(
        self,
        db_connection: aiosqlite.Connection,
        private_key=None,
        funder=None,
        chain_id=137,
    ):
        super().__init__(db_connection, platform_label="polymarket")
        self._book_resolver = BookResolver(db_connection)
        self.private_key = private_key or get_secret("POLYMARKET_PRIVATE_KEY", "") or ""
        self.funder = funder or get_secret("POLYMARKET_WALLET_ADDRESS", "") or ""
        self.chain_id = chain_id
        self.host = os.getenv("POLYMARKET_API_BASE", "https://clob.polymarket.com")
        self.signature_type = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "0"))
        self._client = None
        self._initialized = False
        self._translated_orders: dict[str, bool] = {}

    def _ensure_client(self):
        if self._initialized:
            return
        from py_clob_client_v2 import ApiCreds, ClobClient

        if not self.private_key:
            raise ValueError("POLYMARKET_PRIVATE_KEY is required")
        kwargs = {
            "host": self.host,
            "chain_id": self.chain_id,
            "key": self.private_key,
        }
        if self.funder:
            kwargs.update(funder=self.funder, signature_type=self.signature_type)
        ak = get_secret("POLYMARKET_API_KEY", "") or ""
        sec = get_secret("POLYMARKET_API_SECRET", "") or ""
        pp = get_secret("POLYMARKET_API_PASSPHRASE", "") or ""
        if ak and sec and pp:
            kwargs["creds"] = ApiCreds(
                api_key=ak,
                api_secret=sec,
                api_passphrase=pp,
            )
        self._client = ClobClient(**kwargs)
        if "creds" not in kwargs:
            self._client.set_api_creds(self._client.create_or_derive_api_key())
        self._initialized = True

    async def _call(self, fn, *args, **kwargs):
        return await asyncio.to_thread(fn, *args, **kwargs)

    async def get_pretrade_fee_rate(self, leg):
        """Return the authoritative per-market taker fee rate for a leg."""
        resolved = await self._book_resolver.resolve(
            leg.market_id, leg.side, leg.size, leg.limit_price
        )
        if resolved is None:
            raise ValueError("BookResolver rejected fee lookup")
        self._ensure_client()
        info = await self._call(self._client.get_clob_market_info, leg.market_id)
        fd = info.get("fd")
        if not fd or fd.get("r") is None:
            raise ValueError(
                f"Polymarket fee metadata missing for market {leg.market_id}"
            )
        rate = float(fd["r"])
        if rate < 0 or rate > 1:
            raise ValueError(f"invalid Polymarket fee rate: {rate}")
        return rate

    async def submit_order(self, leg, signal_id=None, strategy=None):
        start = time.time()
        try:
            resolved = await self._book_resolver.resolve(
                leg.market_id, leg.side, leg.size, leg.limit_price
            )
            if resolved is None:
                raise ValueError("BookResolver rejected order")
            self._ensure_client()
            from py_clob_client_v2 import (
                OrderArgs,
                OrderType,
                PartialCreateOrderOptions,
                Side as PolySide,
            )

            side = PolySide.BUY if resolved.side is Side.BUY else PolySide.SELL
            tick = await self._call(self._client.get_tick_size, resolved.token_id)
            response = await self._call(
                self._client.create_and_post_order,
                OrderArgs(
                    token_id=resolved.token_id,
                    price=resolved.limit_price,
                    side=side,
                    size=resolved.size,
                ),
                PartialCreateOrderOptions(tick_size=str(tick)),
                OrderType.FAK,
            )
            oid = (
                response.get("orderID")
                or response.get("order_id")
                or response.get("id")
            )
            if not oid:
                raise RuntimeError(f"Polymarket V2 returned no order id: {response}")
            self._translated_orders[str(oid)] = resolved.translated
            await self.write_order(
                leg,
                OrderResult(
                    order_id=oid,
                    platform="polymarket",
                    status="pending",
                    submission_latency_ms=int((time.time() - start) * 1000),
                    fee_verified=False,
                ),
                signal_id=signal_id,
                strategy=strategy,
                resolved=resolved,
            )
            return await self._poll(oid, leg, start)
        except Exception as exc:
            result = OrderResult(
                order_id=f"FAILED-{leg.market_id}",
                platform="polymarket",
                status="failed",
                submission_latency_ms=int((time.time() - start) * 1000),
                error_message=str(exc),
            )
            await self.write_order(leg, result, signal_id=signal_id, strategy=strategy)
            logger.exception("Polymarket V2 order failed")
            return result

    async def _poll(self, oid, leg, start, max_polls=40):
        for _ in range(max_polls):
            await asyncio.sleep(0.25)
            order = await self._call(self._client.get_order, oid)
            status = str(order.get("status", "")).upper()
            matched = float(
                order.get("size_matched", order.get("sizeMatched", 0)) or 0
            )
            if matched > 0 and status in {"LIVE", "DELAYED"}:
                await self.cancel_order(oid)
                status = "CANCELLED"
            if status in {"MATCHED", "UNMATCHED", "CANCELED", "CANCELLED"}:
                if matched > 0:
                    price = float(order.get("price", leg.limit_price or 0))
                    fee = None
                    fee_verified = False
                    fee_error = None
                    try:
                        fee = await self._estimate_fee(leg.market_id, price, matched)
                        fee_verified = True
                    except Exception as exc:
                        fee_error = str(exc)
                        logger.error(
                            "Polymarket fill %s confirmed but fee is unverified: %s",
                            oid,
                            fee_error,
                        )
                    result = OrderResult(
                        order_id=oid,
                        platform="polymarket",
                        status="filled" if matched >= leg.size else "partially_filled",
                        submission_latency_ms=int((time.time() - start) * 1000),
                        fill_latency_ms=int((time.time() - start) * 1000),
                        filled_price=price,
                        filled_size=matched,
                        fee_paid=fee,
                        fee_verified=fee_verified,
                        error_message=(
                            f"fee unverified: {fee_error}" if fee_error else None
                        ),
                    )
                    await self.update_order_fill(result)
                    await self.write_fill_event(result)
                    return result
                result = OrderResult(
                    order_id=oid,
                    platform="polymarket",
                    status="failed",
                    submission_latency_ms=int((time.time() - start) * 1000),
                    error_message=f"terminal status={status}",
                    fee_verified=False,
                )
                await self.update_order_fill(result)
                return result
        await self.cancel_order(oid)
        return OrderResult(
            order_id=oid,
            platform="polymarket",
            status="pending",
            submission_latency_ms=int((time.time() - start) * 1000),
            error_message="fill poll timeout; cancelled and requires reconciliation",
            fee_verified=False,
        )

    async def _estimate_fee(self, condition_id, price, size):
        info = await self._call(self._client.get_clob_market_info, condition_id)
        fd = info.get("fd")
        if not fd or fd.get("r") is None:
            raise ValueError("fee metadata missing")
        rate = float(fd["r"])
        exponent = int(fd.get("e", 2) or 2)
        raw = size * rate * price * (1.0 - price)
        scale = 10**exponent
        return -(-raw * scale // 1) / scale

    async def list_open_orders(self) -> list[dict]:
        """Return the complete authenticated Polymarket open-order set."""
        self._ensure_client()
        orders = await self._call(self._client.get_open_orders)
        if not isinstance(orders, list):
            raise RuntimeError("Polymarket open-orders response was not a list")
        return [dict(order) for order in orders]

    async def list_recent_fills(self, since: int | None = None) -> list[dict]:
        """Return authenticated Polymarket user trades.

        The SDK paginates by cursor and accepts an ``after`` timestamp.  We
        deliberately request the full available window when ``since`` is not
        supplied; reconciliation callers should provide their last-known
        timestamp to bound the query during normal operation.
        """
        self._ensure_client()
        from py_clob_client_v2 import TradeParams

        params = TradeParams(after=int(since)) if since is not None else None
        trades = await self._call(self._client.get_trades, params)
        if not isinstance(trades, list):
            raise RuntimeError("Polymarket trades response was not a list")
        return [dict(trade) for trade in trades]

    async def get_exchange_positions(self) -> list[dict]:
        """Positions are reconciled from the CLOB fills in Phase 1.

        Polymarket's CLOB client exposes orders/trades but not a canonical
        position endpoint. Returning an explicit unsupported error prevents
        an unavailable position feed from being mistaken for zero exposure.
        """
        raise NotImplementedError("Polymarket position reconciliation requires Data API")

    def economic_fill_price(self, order_id, price):
        return (
            1.0 - float(price)
            if self._translated_orders.get(str(order_id), False)
            else float(price)
        )

    async def cancel_order(self, oid):
        try:
            from py_clob_client_v2 import OrderPayload

            await self._call(self._client.cancel_order, OrderPayload(orderID=oid))
            return True
        except Exception:
            logger.exception("Failed to cancel Polymarket V2 order %s", oid)
            return False

    async def get_order_status(self, oid):
        try:
            self._ensure_client()
            return await self._call(self._client.get_order, oid)
        except Exception:
            return None

    async def get_balance(self):
        try:
            self._ensure_client()
            from py_clob_client_v2 import BalanceAllowanceParams, AssetType

            result = await self._call(
                self._client.get_balance_allowance,
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL),
            )
            raw = result.get("balance")
            if raw is None:
                raise ValueError("Polymarket collateral balance missing")
            scale = float(os.getenv("POLYMARKET_BALANCE_SCALE", "1000000"))
            if scale <= 0:
                raise ValueError("POLYMARKET_BALANCE_SCALE must be positive")
            return float(raw) / scale
        except Exception:
            logger.exception("Polymarket V2 balance lookup failed")
            return None

    async def close(self):
        return None
