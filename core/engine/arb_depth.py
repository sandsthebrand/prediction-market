"""Pre-trade executable order-book depth checks for Phase 1."""
from __future__ import annotations

import asyncio
import math

import httpx

from execution.enums import Side
from execution.models import OrderLeg


async def _polymarket_depth(client, leg: OrderLeg) -> float | None:
    resolver = getattr(client, "_book_resolver", None)
    if resolver is None:
        return None
    resolved = await resolver.resolve(leg.market_id, leg.side, leg.size, leg.limit_price)
    if resolved is None:
        return None
    host = getattr(client, "host", "https://clob.polymarket.com").rstrip("/")
    async with httpx.AsyncClient(timeout=5.0) as http:
        response = await http.get(f"{host}/book", params={"token_id": resolved.token_id})
        response.raise_for_status()
        book = response.json()
    levels = book.get("asks" if resolved.side is Side.BUY else "bids", [])
    total = 0.0
    for level in levels:
        try:
            price = float(level.get("price", level[0]))
            size = float(level.get("size", level[1]))
        except (TypeError, ValueError, KeyError, IndexError):
            continue
        if resolved.side is Side.BUY and price <= resolved.limit_price:
            total += size
        elif resolved.side is Side.SELL and price >= resolved.limit_price:
            total += size
    return total


async def _kalshi_depth(client, leg: OrderLeg) -> float | None:
    base = getattr(client, "api_base", None)
    if not base:
        return None
    await client._acquire_rate_limit()
    path = f"/markets/{leg.market_id}/orderbook"
    headers = client._sign_request("GET", f"/trade-api/v2{path}")
    response = await client.http_client.get(f"{base}{path}", headers=headers)
    if response.status_code != 200:
        return None
    data = response.json().get("orderbook_fp", {})
    yes = data.get("yes_dollars", [])
    no = data.get("no_dollars", [])
    total = 0.0
    for level in yes if leg.side is Side.SELL else no:
        try:
            price = float(level[0])
            size = float(level[1])
        except (TypeError, ValueError, IndexError):
            continue
        # A no bid at q is a yes ask at 1-q.
        effective_yes_price = price if leg.side is Side.SELL else 1.0 - price
        if leg.side is Side.BUY and effective_yes_price <= float(leg.limit_price):
            total += size
        elif leg.side is Side.SELL and effective_yes_price >= float(leg.limit_price):
            total += size
    return total


async def get_executable_depth(client, leg: OrderLeg) -> float | None:
    """Return quantity immediately executable within the leg's limit price.

    ``None`` means depth could not be verified and must be treated as a
    fail-closed condition by live Phase 1. Paper execution is intentionally
    exempt so the paper engine can model fills without a live venue.
    """
    platform = str(getattr(client, "platform", getattr(client, "platform_label", ""))).lower()
    if platform == "paper":
        return float(leg.size)
    if leg.platform == "polymarket":
        return await _polymarket_depth(client, leg)
    if leg.platform == "kalshi":
        return await _kalshi_depth(client, leg)
    return None


def executable_quantity(depth_a: float | None, depth_b: float | None, requested: float) -> float | None:
    if requested <= 0 or depth_a is None or depth_b is None:
        return None
    depth = min(max(0.0, float(depth_a)), max(0.0, float(depth_b)))
    if depth <= 0:
        return None
    return min(requested, depth)
