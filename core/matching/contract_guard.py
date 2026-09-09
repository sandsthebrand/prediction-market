"""Fail-closed contract-equivalence guard for cross-platform arbitrage."""

from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urlparse


def _date(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except (TypeError, ValueError):
        return None


def _numbers(text: str | None) -> set[str]:
    return set(re.findall(r"\b\d+(?:\.\d+)?\b", text or ""))


def _source_url_domain(value: str | None) -> str | None:
    """Return a normalized hostname only when metadata contains a URL."""
    if not value:
        return None
    text = value.strip()
    if "://" not in text:
        return None
    try:
        host = (urlparse(text).hostname or "").lower().rstrip(".")
    except ValueError:
        return None
    return host or None


async def verify_contract_equivalence(
    db, poly_market_id: str, kalshi_market_id: str
) -> tuple[bool, str]:
    """Verify material resolution metadata before a P1 trade.

    Similarity alone is not enough. A pair is rejected when authoritative
    metadata directly conflicts or when dates/rules cannot be reconciled.
    """
    cur = await db.execute(
        """SELECT platform, title, description, category, resolution_source,
                  resolution_criteria, close_time, resolve_time
           FROM markets WHERE id IN (?, ?)""",
        (poly_market_id, kalshi_market_id),
    )
    rows = await cur.fetchall()
    if len(rows) != 2:
        return False, "missing market metadata"

    by_platform = {str(row[0]).lower(): row for row in rows}
    poly = by_platform.get("polymarket")
    kalshi = by_platform.get("kalshi")
    if poly is None or kalshi is None:
        return False, "pair does not contain one Polymarket and one Kalshi market"

    poly_source = _source_url_domain(poly[4])
    kalshi_source = _source_url_domain(kalshi[4])
    if poly_source and kalshi_source and poly_source != kalshi_source:
        return (
            False,
            f"resolution source URLs conflict: {poly_source} vs {kalshi_source}",
        )

    poly_date = _date(poly[7] or poly[6])
    kalshi_date = _date(kalshi[7] or kalshi[6])
    if poly_date and kalshi_date and poly_date != kalshi_date:
        return False, f"resolution dates conflict: {poly_date} vs {kalshi_date}"
    if not poly[5] or not kalshi[5]:
        return False, "resolution criteria missing on one side"

    # Different numeric thresholds in otherwise similar contracts are a hard
    # reject. This catches units/threshold drift that fuzzy title matching can miss.
    poly_numbers = _numbers((poly[5] or "") + " " + (poly[1] or ""))
    kalshi_numbers = _numbers((kalshi[5] or "") + " " + (kalshi[1] or ""))
    if poly_numbers and kalshi_numbers and poly_numbers != kalshi_numbers:
        return False, (
            "resolution thresholds/numbers conflict: "
            f"{poly_numbers} vs {kalshi_numbers}"
        )

    return True, "resolution metadata compatible"
