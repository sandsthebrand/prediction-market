"""Phase 1 paper client using live public books and fee metadata."""

from __future__ import annotations

import math
import os

import httpx

from execution.clients.base import OrderResult
from execution.clients.paper import PaperExecutionClient
from execution.enums import Side


class Phase1PaperExecutionClient(PaperExecutionClient):
    """Paper execution that prices against current public exchange state.

    No authenticated order endpoint is called. Public order books and market
    fee metadata are used so paper sizing/fees do not silently rely on the
    old fixed requested-size/fixed-fee proxy.
    """

    async def get_pretrade_fee_rate(self, leg):
        if leg.platform == "polymarket":
            market_id = leg.market_id
            host = os.getenv(
                "POLYMARKET_API_BASE", "https://clob.polymarket.com"
            ).rstrip("/")
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(f"{host}/clob-markets/{market_id}")
                response.raise_for_status()
                info = response.json()
            fd = info.get("fd") or {}
            rate = fd.get("r")
            if rate is None:
                raise ValueError(f"Polymarket fee metadata missing for {market_id}")
            rate = float(rate)
            if not 0 <= rate <= 1:
                raise ValueError(f"invalid Polymarket fee rate: {rate}")
            return rate

        if leg.platform == "kalshi":
            base = os.getenv(
                "KALSHI_API_BASE", "https://external-api.kalshi.com/trade-api/v2"
            ).rstrip("/")
            async with httpx.AsyncClient(timeout=5.0) as client:
                market_response = await client.get(
                    f"{base}/markets/{leg.market_id}"
                )
                market_response.raise_for_status()
                market = market_response.json().get("market") or {}
                event_ticker = market.get("event_ticker")
                if not event_ticker:
                    raise ValueError("Kalshi market has no event ticker")
                event_response = await client.get(f"{base}/events/{event_ticker}")
                event_response.raise_for_status()
                event = event_response.json().get("event") or {}
                series_ticker = event.get("series_ticker")
                if not series_ticker:
                    raise ValueError("Kalshi event has no series ticker")
                series_response = await client.get(f"{base}/series/{series_ticker}")
                series_response.raise_for_status()
                series = series_response.json().get("series") or {}
            fee_type = event.get("fee_type_override") or series.get("fee_type")
            multiplier = event.get("fee_multiplier_override")
            if multiplier is None:
                multiplier = series.get("fee_multiplier")
            if fee_type not in {"quadratic", "quadratic_with_maker_fees"}:
                raise ValueError(f"unsupported Kalshi paper fee type: {fee_type!r}")
            if multiplier is None:
                raise ValueError("Kalshi fee multiplier missing")
            base_rate = float(os.getenv("KALSHI_QUADRATIC_BASE_RATE", "0.07"))
            rate = base_rate * float(multiplier)
            if not 0 <= rate <= 1:
                raise ValueError(f"invalid Kalshi fee rate: {rate}")
            return rate

        raise ValueError(f"unsupported Phase 1 paper platform: {leg.platform}")

    async def get_executable_depth(self, leg):
        """Return current immediately executable depth at the limit."""
        if leg.platform == "polymarket":
            resolver = self._book_resolver
            if resolver is None:
                return None
            resolved = await resolver.resolve(
                leg.market_id, leg.side, leg.size, leg.limit_price
            )
            if resolved is None:
                return None
            host = os.getenv(
                "POLYMARKET_API_BASE", "https://clob.polymarket.com"
            ).rstrip("/")
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(
                    f"{host}/book", params={"token_id": resolved.token_id}
                )
                response.raise_for_status()
                book = response.json()
            levels = book.get("asks" if resolved.side is Side.BUY else "bids", [])
            total = 0.0
            for level in levels:
                price = float(level["price"])
                size = float(level["size"])
                if (resolved.side is Side.BUY and price <= resolved.limit_price) or (
                    resolved.side is Side.SELL and price >= resolved.limit_price
                ):
                    total += size
            return total

        if leg.platform == "kalshi":
            base = os.getenv(
                "KALSHI_API_BASE", "https://external-api.kalshi.com/trade-api/v2"
            ).rstrip("/")
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(
                    f"{base}/markets/{leg.market_id}/orderbook"
                )
                response.raise_for_status()
                data = response.json().get("orderbook_fp", {})
            levels = (
                data.get("no_dollars", [])
                if leg.side is Side.BUY
                else data.get("yes_dollars", [])
            )
            total = 0.0
            for level in levels:
                price = float(level[0])
                size = float(level[1])
                effective_yes_price = price if leg.side is Side.SELL else 1.0 - price
                if (
                    leg.side is Side.BUY
                    and effective_yes_price <= float(leg.limit_price)
                ) or (
                    leg.side is Side.SELL
                    and effective_yes_price >= float(leg.limit_price)
                ):
                    total += size
            return total

        return None

    async def submit_order(self, leg, signal_id=None, strategy=None):
        # Re-check live depth at the simulated submission point. The main
        # engine also performs a pre-trade depth check, so a changed book can
        # naturally produce a partial paper fill rather than assuming all
        # requested quantity remains executable.
        depth = await self.get_executable_depth(leg)
        if depth is None or depth <= 0:
            result = OrderResult(
                order_id=f"PAPER-REJECT-{leg.platform}-{leg.market_id}",
                platform=self.platform_label,
                status="failed",
                submission_latency_ms=0,
                error_message="no executable live-book depth",
            )
            await self.write_order(leg, result, signal_id=signal_id, strategy=strategy)
            return result

        executable_size = min(float(leg.size), float(depth))
        result = await super().submit_order(
            leg.__class__(
                market_id=leg.market_id,
                platform=leg.platform,
                side=leg.side,
                size=executable_size,
                limit_price=leg.limit_price,
                order_type=leg.order_type,
            ),
            signal_id=signal_id,
            strategy=strategy,
        )
        if result.status != "filled" or result.filled_price is None:
            return result

        fee_rate = await self.get_pretrade_fee_rate(leg)
        price = float(result.filled_price)
        qty = float(result.filled_size or 0)
        raw_fee = qty * fee_rate * price * (1.0 - price)
        fee = math.ceil(raw_fee * 10000.0) / 10000.0
        result = OrderResult(
            order_id=result.order_id,
            platform=result.platform,
            status=("filled" if qty + 1e-9 >= leg.size else "partially_filled"),
            submission_latency_ms=result.submission_latency_ms,
            fill_latency_ms=result.fill_latency_ms,
            filled_price=result.filled_price,
            filled_size=qty,
            fee_paid=fee,
            fee_verified=True,
            slippage=result.slippage,
            error_message=result.error_message,
        )
        await self.update_order_fill(result)
        return result
