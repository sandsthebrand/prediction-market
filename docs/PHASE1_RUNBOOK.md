# Phase 1 — Cross-platform arbitrage runbook

## Scope

Phase 1 trades only matched Polymarket ↔ Kalshi contracts. Directional P2–P5 strategies are disabled by default.

The system does **not** use a fixed minimum dollar profit. An opportunity is eligible when executable net profit remains positive after venue fees, modeled slippage, and the configurable `PHASE1_MIN_NET_EDGE` gate. The default gate is `0.005` per contract.

## Safety defaults

- Starting capital: configured by `STARTING_CAPITAL`; use `$100` for the first live test.
- Per-trade capital: bounded by `MAX_POSITION_PCT`.
- Total portfolio exposure: bounded by `MAX_PORTFOLIO_EXPOSURE_PCT`.
- P1 sizing does not use Kelly.
- Both legs are submitted concurrently.
- Requested size is never treated as filled size.
- Resting remainder is cancelled after a fill/partial fill.
- Any imbalance is immediately flattened with an aggressive limit order.
- Any unknown submission state, imbalance, or flatten failure persists a global execution halt.
- A halt survives process restart and requires explicit operator re-arm.
- Missing executable order-book depth fails closed.
- Contract-equivalence metadata failures fail closed.

## Paper validation

Run in paper mode first. Keep the system in paper/shadow mode for at least 1–2 weeks of live market data. Review:

1. realized P&L versus theoretical P&L;
2. fees and slippage;
3. opportunity count and rejected-opportunity reasons;
4. partial-fill frequency;
5. imbalance/flatten frequency;
6. maximum drawdown;
7. stale-price and depth failures;
8. contract-match false positives.

Do not set the live preflight flags during paper validation.

## Live preflight

Live execution is intentionally blocked unless both are explicitly set:

```text
PHASE1_FEES_VERIFIED=true
PHASE1_API_V2_VERIFIED=true
```

Before setting them, verify the current venue fee schedule, order minimums, tick/price increments, account balances, API permissions, and rate limits against the live venue documentation.

Polymarket production is CLOB V2 at `https://clob.polymarket.com`; the legacy V1 SDK is not supported in production.

Kalshi live orders use the current event-order V2 endpoint under `https://external-api.kalshi.com/trade-api/v2/portfolio/events/orders`.

## First live test

Use a dedicated account/wallet with approximately `$100`. Do not increase risk limits automatically. After each live session, verify exchange balances and database reconciliation before continuing.

Suggested scale gates:

`$100 → $250 → $500 → $1,000 → $2,500 → $5,000 → $10,000+`

Advance only after realized results and execution reliability justify the next level.

## Emergency halt

If the system reports an execution halt:

1. Do not restart it to clear the halt.
2. Confirm all open orders are cancelled on both venues.
3. Reconcile exchange orders, fills, positions, and balances against the database.
4. Resolve any residual exposure.
5. Only then explicitly clear the persistent halt.

## Important

A profitable theoretical spread is not a realized profit. Every performance report must separate theoretical opportunity P&L from realized P&L after actual fills, fees, slippage, and flattening costs.
