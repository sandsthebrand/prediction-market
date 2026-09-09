"""Fail-closed contract-equivalence guard for cross-platform arbitrage."""
from __future__ import annotations

import re
from datetime import datetime


def _date(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except (TypeError, ValueError):
        return None


def _numbers(text: str | None) -> set[str]:
    return set(re.findall(r"\b\d+(?:\.\d+)?\b", text or ""))


async def verify_contract_equivalence(db, poly_market_id: str, kalshi_market_id: str) -> tuple[bool, str]:
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
    p = by_platform.get("polymarket")
    k = by_platform.get("kalshi")
    if p is None or k is None:
        return False, "pair does not contain one Polymarket and one Kalshi market"

    if p[4] and k[4] and p[4].strip().lower() != k[4].strip().lower():
        return False, "resolution sources conflict"

    p_date = _date(p[7] or p[6])
    k_date = _date(k[7] or k[6])
    if p_date and k_date and p_date != k_date:
        return False, f"resolution dates conflict: {p_date} vs {k_date}"
    if not p[5] or not k[5]:
        return False, "resolution criteria missing on one side"

    # Different numeric thresholds in otherwise similar contracts are a hard
    # reject. This catches units/threshold drift that fuzzy title matching can miss.
    pn = _numbers(p[5] + " " + (p[1] or ""))
    kn = _numbers(k[5] + " " + (k[1] or ""))
    if pn and kn and pn != kn:
        return False, f"resolution thresholds/numbers conflict: {pn} vs {kn}"

    return True, "resolution metadata compatible"
