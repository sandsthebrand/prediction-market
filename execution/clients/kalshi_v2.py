"""Kalshi current event-order API client."""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import time
import uuid
from pathlib import Path

import aiosqlite
import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from core.secrets import get_secret
from execution.clients.base import BaseExecutionClient, OrderResult

logger = logging.getLogger(__name__)


class KalshiExecutionClientV2(BaseExecutionClient):
    def __init__(
        self,
        db_connection: aiosqlite.Connection,
        api_key=None,
        rsa_key_path=None,
        api_base=None,
    ):
        super().__init__(db_connection, platform_label="kalshi")
        self.api_key = api_key or get_secret("KALSHI_API_KEY", "") or ""
        self.api_base = (
            api_base
            or os.getenv("KALSHI_API_BASE")
            or "https://external-api.kalshi.com/trade-api/v2"
        ).rstrip("/")
        key_path = rsa_key_path or get_secret("KALSHI_RSA_KEY_PATH", "") or ""
        self._private_key: RSAPrivateKey | None = None
        if key_path:
            expanded = Path(key_path).expanduser()
            if not expanded.exists():
                raise FileNotFoundError(f"Kalshi RSA key file not found: {expanded}")
            self._private_key = serialization.load_pem_private_key(
                expanded.read_bytes(), password=None
            )
        self.http_client = httpx.AsyncClient(timeout=15)
        self._tokens = 20.0
        self._last_refill = time.monotonic()
        self._fee_cache: dict[str, float] = {}

    async def _limit(self):
        now = time.monotonic()
        self._tokens = min(20.0, self._tokens + (now - self._last_refill) * 10.0)
        self._last_refill = now
        while self._tokens < 1:
            await asyncio.sleep(0.1)
            now = time.monotonic()
            self._tokens = min(20.0, self._tokens + (now - self._last_refill) * 10.0)
            self._last_refill = now
        self._tokens -= 1

    def _sign(self, method, path):
        if not self.api_key or not self._private_key:
            raise ValueError("Kalshi API key and RSA key are required")
        ts = str(int(time.time() * 1000))
        sig = self._private_key.sign(
            (ts + method.upper() + path).encode(),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.api_key,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "Content-Type": "application/json",
        }

    async def _get_json(self, path):
        await self._limit()
        response = await self.http_client.get(
            self.api_base + path,
            headers=self._sign("GET", path),
        )
        if response.status_code != 200:
            raise RuntimeError(f"GET {path} returned HTTP {response.status_code}")
        return response.json()

    async def get_pretrade_fee_rate(self, leg):
        """Resolve the current Kalshi taker fee coefficient for this market."""
        ticker = str(leg.market_id)
        cached = self._fee_cache.get(ticker)
        if cached is not None:
            return cached

        market_payload = await self._get_json(f"/markets/{ticker}")
        market = market_payload.get("market") or {}
        event_ticker = market.get("event_ticker")
        if not event_ticker:
            raise ValueError(f"Kalshi market {ticker} has no event ticker")

        event_payload = await self._get_json(f"/events/{event_ticker}")
        event = event_payload.get("event") or {}
        series_ticker = event.get("series_ticker")
        if not series_ticker:
            raise ValueError(f"Kalshi event {event_ticker} has no series ticker")

        series_payload = await self._get_json(f"/series/{series_ticker}")
        series = series_payload.get("series") or {}
        fee_type = event.get("fee_type_override") or series.get("fee_type")
        multiplier = event.get("fee_multiplier_override")
        if multiplier is None:
            multiplier = series.get("fee_multiplier")
        if multiplier is None:
            raise ValueError(f"Kalshi fee multiplier missing for {ticker}")
        if fee_type not in {"quadratic", "quadratic_with_maker_fees"}:
            raise ValueError(
                f"unsupported Kalshi fee type for Phase 1: {fee_type!r}"
            )

        base_rate = float(os.getenv("KALSHI_QUADRATIC_BASE_RATE", "0.07"))
        rate = base_rate * float(multiplier)
        if rate < 0 or rate > 1:
            raise ValueError(f"invalid Kalshi fee rate for {ticker}: {rate}")
        self._fee_cache[ticker] = rate
        return rate

    async def submit_order(self, leg, signal_id=None, strategy=None):
        start = time.time()
        cid = str(uuid.uuid4())
        try:
            await self._limit()
            path = "/trade-api/v2/portfolio/events/orders"
            body = {
                "ticker": leg.market_id,
                "client_order_id": cid,
                "side": "bid" if leg.side.value == "BUY" else "ask",
                "count": f"{leg.size:.2f}",
                "price": f"{leg.limit_price:.4f}",
                "time_in_force": "immediate_or_cancel",
                "self_trade_prevention_type": "taker_at_cross",
                "cancel_order_on_pause": True,
            }
            response = await self.http_client.post(
                self.api_base + "/portfolio/events/orders",
                json=body,
                headers=self._sign("POST", path),
            )
            if response.status_code != 201:
                raise RuntimeError(f"HTTP {response.status_code}: {response.text}")
            oid = response.json()["order_id"]
            await self.write_order(
                leg,
                OrderResult(
                    order_id=oid,
                    platform="kalshi",
                    status="pending",
                    submission_latency_ms=int((time.time() - start) * 1000),
                    fee_verified=False,
                ),
                signal_id=signal_id,
                strategy=strategy,
            )
            return await self._poll(oid, leg, start)
        except Exception as exc:
            result = OrderResult(
                order_id=f"FAILED-{leg.market_id}",
                platform="kalshi",
                status="failed",
                submission_latency_ms=int((time.time() - start) * 1000),
                error_message=str(exc),
            )
            await self.write_order(
                result=result, leg=leg, signal_id=signal_id, strategy=strategy
            )
            logger.exception("Kalshi V2 order failed")
            return result

    async def _poll(self, oid, leg, start, max_polls=30):
        for _ in range(max_polls):
            await asyncio.sleep(0.25)
            await self._limit()
            path = f"/trade-api/v2/portfolio/orders/{oid}"
            response = await self.http_client.get(
                self.api_base + f"/portfolio/orders/{oid}",
                headers=self._sign("GET", path),
            )
            if response.status_code != 200:
                continue
            order = response.json().get("order", response.json())
            status = str(order.get("status", "")).lower()
            matched = float(
                order.get("fill_count_fp", order.get("fill_count", 0)) or 0
            )
            if status in {"executed", "filled", "canceled", "cancelled"}:
                if matched > 0:
                    price = float(order.get("yes_price_dollars", leg.limit_price))
                    taker_fee = float(order.get("taker_fees_dollars", 0) or 0)
                    maker_fee = float(order.get("maker_fees_dollars", 0) or 0)
                    fee = taker_fee + maker_fee
                    result = OrderResult(
                        order_id=oid,
                        platform="kalshi",
                        status="filled" if matched >= leg.size else "partially_filled",
                        submission_latency_ms=int((time.time() - start) * 1000),
                        fill_latency_ms=int((time.time() - start) * 1000),
                        filled_price=price,
                        filled_size=matched,
                        fee_paid=fee,
                        fee_verified=True,
                    )
                    await self.update_order_fill(result)
                    await self.write_fill_event(result)
                    return result
                result = OrderResult(
                    order_id=oid,
                    platform="kalshi",
                    status="failed",
                    submission_latency_ms=int((time.time() - start) * 1000),
                    error_message="order cancelled without fill",
                    fee_verified=True,
                )
                await self.update_order_fill(result)
                return result
        await self.cancel_order(oid)
        return OrderResult(
            order_id=oid,
            platform="kalshi",
            status="pending",
            submission_latency_ms=int((time.time() - start) * 1000),
            error_message=(
                "fill poll timeout; order cancelled and requires reconciliation"
            ),
            fee_verified=False,
        )

    async def list_open_orders(self) -> list[dict]:
        """Return all currently open Kalshi orders."""
        orders: list[dict] = []
        cursor = None
        while True:
            query = "?limit=200"
            if cursor:
                query += f"&cursor={cursor}"
            payload = await self._get_json(f"/portfolio/orders{query}")
            page = payload.get("orders") or []
            orders.extend(dict(order) for order in page)
            cursor = payload.get("cursor")
            if not cursor or not page:
                break
        return orders

    async def list_recent_fills(self, since: int | None = None) -> list[dict]:
        """Return authenticated Kalshi fills, optionally bounded by timestamp."""
        fills: list[dict] = []
        cursor = None
        while True:
            query = "?limit=200"
            if cursor:
                query += f"&cursor={cursor}"
            payload = await self._get_json(f"/portfolio/fills{query}")
            page = payload.get("fills") or []
            for fill in page:
                if since is None:
                    fills.append(dict(fill))
                    continue
                ts = fill.get("created_time") or fill.get("timestamp")
                try:
                    if ts is None or float(ts) >= float(since):
                        fills.append(dict(fill))
                except (TypeError, ValueError):
                    fills.append(dict(fill))
            cursor = payload.get("cursor")
            if not cursor or not page:
                break
        return fills

    async def get_exchange_positions(self) -> list[dict]:
        """Return current Kalshi positions."""
        payload = await self._get_json("/portfolio/positions?limit=200")
        positions = payload.get("market_positions")
        if positions is None:
            positions = payload.get("positions")
        if positions is None:
            raise RuntimeError("Kalshi positions response missing positions field")
        return [dict(position) for position in positions]

    async def cancel_order(self, oid):
        await self._limit()
        path = f"/trade-api/v2/portfolio/events/orders/{oid}"
        response = await self.http_client.delete(
            self.api_base + f"/portfolio/events/orders/{oid}",
            headers=self._sign("DELETE", path),
        )
        return response.status_code in (200, 204)

    async def get_order_status(self, oid):
        try:
            await self._limit()
            path = f"/trade-api/v2/portfolio/orders/{oid}"
            response = await self.http_client.get(
                self.api_base + f"/portfolio/orders/{oid}",
                headers=self._sign("GET", path),
            )
            return (
                response.json().get("order", response.json())
                if response.status_code == 200
                else None
            )
        except Exception:
            logger.exception("Kalshi get order failed")
            return None

    async def get_balance(self):
        try:
            await self._limit()
            path = "/trade-api/v2/portfolio/balance"
            response = await self.http_client.get(
                self.api_base + "/portfolio/balance", headers=self._sign("GET", path)
            )
            if response.status_code != 200:
                return None
            raw = response.json().get("balance")
            if raw is None:
                raise ValueError("Kalshi balance missing")
            scale = float(os.getenv("KALSHI_BALANCE_CENTS_PER_DOLLAR", "100"))
            if scale != 100:
                raise ValueError("KALSHI_BALANCE_CENTS_PER_DOLLAR must remain 100")
            return float(raw) / scale
        except Exception:
            logger.exception("Kalshi balance lookup failed")
            return None

    async def close(self):
        await self.http_client.aclose()
