"""Kalshi current event-order API client."""
from __future__ import annotations

import asyncio
import base64
import json
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
from execution.models import OrderLeg

logger = logging.getLogger(__name__)


class KalshiExecutionClientV2(BaseExecutionClient):
    def __init__(self, db_connection: aiosqlite.Connection, api_key=None, rsa_key_path=None, api_base=None):
        super().__init__(db_connection, platform_label="kalshi")
        self.api_key = api_key or get_secret("KALSHI_API_KEY", "") or ""
        self.api_base = (api_base or os.getenv("KALSHI_API_BASE") or
                         "https://external-api.kalshi.com/trade-api/v2").rstrip("/")
        key_path = rsa_key_path or get_secret("KALSHI_RSA_KEY_PATH", "") or ""
        self._private_key: RSAPrivateKey | None = None
        if key_path:
            expanded = Path(key_path).expanduser()
            if not expanded.exists():
                raise FileNotFoundError(f"Kalshi RSA key file not found: {expanded}")
            self._private_key = serialization.load_pem_private_key(expanded.read_bytes(), password=None)
        self.http_client = httpx.AsyncClient(timeout=15)
        self._tokens = 20.0
        self._last_refill = time.monotonic()

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

    def _sign(self, method: str, path: str):
        if not self.api_key or not self._private_key:
            raise ValueError("Kalshi API key and RSA key are required")
        ts = str(int(time.time() * 1000))
        sig = self._private_key.sign(
            (ts + method.upper() + path).encode(),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.api_key,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "Content-Type": "application/json",
        }

    async def submit_order(self, leg: OrderLeg, signal_id=None, strategy=None) -> OrderResult:
        start = time.time()
        client_order_id = str(uuid.uuid4())
        try:
            await self._limit()
            path = "/trade-api/v2/portfolio/events/orders"
            body = {
                "ticker": leg.market_id,
                "client_order_id": client_order_id,
                "side": "bid" if leg.side.value == "BUY" else "ask",
                "count": f"{leg.size:.4f}",
                "price": f"{leg.limit_price:.4f}",
                "time_in_force": "good_till_canceled",
                "self_trade_prevention_type": "taker_at_cross",
                "cancel_order_on_pause": True,
            }
            response = await self.http_client.post(self.api_base + "/portfolio/events/orders",
                                                    json=body, headers=self._sign("POST", path))
            if response.status_code != 201:
                raise RuntimeError(f"HTTP {response.status_code}: {response.text}")
            data = response.json()
            order_id = data["order_id"]
            await self.write_order(leg, OrderResult(order_id=order_id, platform="kalshi", status="pending",
                                                     submission_latency_ms=int((time.time() - start) * 1000)),
                                   signal_id=signal_id, strategy=strategy)
            return await self._poll(order_id, leg, start, signal_id, strategy)
        except Exception as exc:
            result = OrderResult(order_id=f"FAILED-{leg.market_id}", platform="kalshi", status="failed",
                                  submission_latency_ms=int((time.time() - start) * 1000), error_message=str(exc))
            await self.write_order(leg, result, signal_id=signal_id, strategy=strategy)
            logger.exception("Kalshi V2 order failed")
            return result

    async def _poll(self, order_id, leg, start, signal_id, strategy, max_polls=30):
        for _ in range(max_polls):
            await asyncio.sleep(0.25)
            await self._limit()
            path = f"/trade-api/v2/portfolio/orders/{order_id}"
            response = await self.http_client.get(self.api_base + f"/portfolio/orders/{order_id}",
                                                  headers=self._sign("GET", path))
            if response.status_code != 200:
                continue
            order = response.json().get("order", response.json())
            status = str(order.get("status", "")).lower()
            matched = float(order.get("fill_count_fp", order.get("fill_count", 0)) or 0)
            if status in {"executed", "filled", "canceled", "cancelled", "resting"}:
                if status == "resting" and matched < leg.size:
                    continue
                if matched > 0:
                    price = float(order.get("average_fill_price", order.get("yes_price_dollars", leg.limit_price)))
                    fee = float(order.get("taker_fees_dollars", 0) or order.get("maker_fees_dollars", 0) or 0)
                    result = OrderResult(order_id=order_id, platform="kalshi",
                                         status="filled" if matched >= leg.size else "partially_filled",
                                         submission_latency_ms=int((time.time() - start) * 1000),
                                         fill_latency_ms=int((time.time() - start) * 1000),
                                         filled_price=price, filled_size=matched, fee_paid=fee)
                    await self.update_order_fill(result)
                    await self.write_fill_event(result)
                    return result
                if status in {"canceled", "cancelled"}:
                    result = OrderResult(order_id=order_id, platform="kalshi", status="failed",
                                         submission_latency_ms=int((time.time() - start) * 1000),
                                         error_message="order cancelled")
                    await self.update_order_fill(result)
                    return result
        await self.cancel_order(order_id)
        return OrderResult(order_id=order_id, platform="kalshi", status="pending",
                           submission_latency_ms=int((time.time() - start) * 1000),
                           error_message="fill poll timeout; order cancelled and requires reconciliation")

    async def cancel_order(self, order_id: str) -> bool:
        await self._limit()
        path = f"/trade-api/v2/portfolio/events/orders/{order_id}"
        response = await self.http_client.delete(self.api_base + f"/portfolio/events/orders/{order_id}",
                                                 headers=self._sign("DELETE", path))
        return response.status_code in (200, 204)

    async def get_order_status(self, order_id: str) -> dict | None:
        try:
            await self._limit()
            path = f"/trade-api/v2/portfolio/orders/{order_id}"
            response = await self.http_client.get(self.api_base + f"/portfolio/orders/{order_id}",
                                                  headers=self._sign("GET", path))
            return response.json().get("order", response.json()) if response.status_code == 200 else None
        except Exception:
            logger.exception("Kalshi get order failed")
            return None

    async def get_balance(self) -> float | None:
        try:
            await self._limit()
            path = "/trade-api/v2/portfolio/balance"
            response = await self.http_client.get(self.api_base + "/portfolio/balance",
                                                  headers=self._sign("GET", path))
            if response.status_code == 200:
                raw = response.json().get("balance", 0)
                return float(raw) / 100.0 if float(raw) > 1000 else float(raw)
        except Exception:
            logger.exception("Kalshi balance lookup failed")
        return None

    async def close(self):
        await self.http_client.aclose()
