"""Pre-trade executable order-book depth checks for Phase 1."""

from __future__ import annotations

import httpx
from execution.enums import Side
from execution.models import OrderLeg


def _level_values(level):
    if isinstance(level, dict):
        return float(level["price"]), float(level["size"])
    return float(level[0]), float(level[1])


async def _polymarket_depth(client, leg: OrderLeg) -> float | None:
    resolver = getattr(client, "_book_resolver", None)
    if resolver is None:
        return None
    resolved = await resolver.resolve(
        leg.market_id, leg.side, leg.size, leg.limit_price
    )
    if resolved is None:
        return None
    host = getattr(client, "host", "https://clob.polymarket.com").rstrip("/")
    async with httpx.AsyncClient(timeout=5.0) as http:
        response = await http.get(
            f"{host}/book", params={"token_id": resolved.token_id}
        )
        response.raise_for_status()
        book = response.json()
    total = 0.0
    for level in book.get("asks" if resolved.side is Side.BUY else "bids", []):
        try:
            price, size = _level_values(level)
        except (TypeError, ValueError, KeyError, IndexError):
            continue
        if (resolved.side is Side.BUY and price <= resolved.limit_price) or (
            resolved.side is Side.SELL and price >= resolved.limit_price
        ):
            total += size
    return total


async def _kalshi_depth(client, leg: OrderLeg) -> float | None:
    base = getattr(client, "api_base", None)
    if not base:
        return None
    limiter = getattr(client, "_limit", None) or getattr(
        client, "_acquire_rate_limit", None
    )
    signer = getattr(client, "_sign", None) or getattr(client, "_sign_request", None)
    if limiter is None or signer is None:
        return None
    await limiter()
    path = f"/markets/{leg.market_id}/orderbook"
    headers = signer("GET", f"/trade-api/v2{path}")
    response = await client.http_client.get(f"{base}{path}", headers=headers)
    if response.status_code != 200:
        return None
    data = response.json().get("orderbook_fp", {})
    levels = (
        data.get("yes_dollars", [])
        if leg.side is Side.SELL
        else data.get("no_dollars", [])
    )
    total = 0.0
    for level in levels:
        try:
            price, size = _level_values(level)
        except (TypeError, ValueError, IndexError):
            continue
        effective_yes_price = price if leg.side is Side.SELL else 1.0 - price
        if (leg.side is Side.BUY and effective_yes_price <= float(leg.limit_price)) or (
            leg.side is Side.SELL and effective_yes_price >= float(leg.limit_price)
        ):
            total += size
    return total


async def get_executable_depth(client, leg: OrderLeg) -> float | None:
    """Return quantity immediately executable within the leg's limit price."""
    custom = getattr(client, "get_executable_depth", None)
    if custom is not None:
        return await custom(leg)
    label = str(
        getattr(client, "platform", getattr(client, "platform_label", ""))
    ).lower()
    if label.startswith("paper") or client.__class__.__name__.lower().startswith(
        "paper"
    ):
        # Legacy paper clients are deliberately no longer treated as having
        # infinite/requested-size liquidity. They must implement live-book
        # depth before being used by the Phase 1 engine.
        return None
    if leg.platform == "polymarket":
        return await _polymarket_depth(client, leg)
    if leg.platform == "kalshi":
        return await _kalshi_depth(client, leg)
    return None


def executable_quantity(
    depth_a: float | None, depth_b: float | None, requested: float
) -> float | None:
    if requested <= 0 or depth_a is None or depth_b is None:
        return None
    depth = min(max(0.0, float(depth_a)), max(0.0, float(depth_b)))
    return min(requested, depth) if depth > 0 else None
