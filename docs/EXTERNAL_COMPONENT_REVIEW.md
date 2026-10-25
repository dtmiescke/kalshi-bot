# External component review and integration boundary

This project keeps one exchange client and one guarded order path. Third-party
MCP servers are reference implementations to study, not services to run beside
the bot with their own credentials or order permissions.

## Integrated in this change

- A read-only market-rule normalizer in `bot/market_rules.py` reuses the
  existing `KalshiClient.market(ticker)` call and produces compact rule text,
  explicit missing-data status, source attribution, and truncation metadata.
- The output is marked as untrusted external data. It must never be interpreted
  as instructions and must not override risk settings, settlement labels, or
  order gates.
- No duplicate Kalshi client, data collector, order endpoint, database table,
  UI, or new dependency is introduced.

## Reviewed ideas and decisions

- **9crusher/mcp-server-kalshi**: useful reference for fetching and consolidating
  primary/secondary rules and contract-term PDFs. Its trading tools are not
  imported. This project reads market metadata through its existing client;
  PDF extraction can be added only with a bounded, tested parser and official
  source verification.
- **BrainDAO/mcp-kalshi**: read-only market/order-book methods are conceptually
  useful; portfolio and order-write methods are deliberately not connected.
  Never let an MCP server bypass the bot's own risk, idempotency, persistence,
  and confirmation gates.
- **OctagonAI/Kalshi CLI and Half-Kelly**: risk concepts are candidates for
  offline comparison only. Do not replace the current fee-aware sizing, limits,
  loss stops, or circuit breakers without leakage-safe walk-forward evidence.
- **Avellaneda–Stoikov market making**: designed for inventory-aware two-sided
  quoting, not a drop-in replacement for the bot's current event/edge strategy.
  Evaluate only as a separate paper-only experiment with adverse-selection,
  queue-position, fees, inventory, and fill assumptions.
- **NWS/GFS and FRED**: useful only when the bot explicitly supports weather or
  macro contracts and has source-specific freshness plus settlement mapping.
  They are not wired into sports/crypto signals here.
- **FIX 4.4**: not enabled; first verify Kalshi account/API eligibility and
  protocol support. Do not invent a FIX route or parallel execution channel.

## Validation required before using rules in decisions

1. Fetch rules by the exact market ticker and retain source and timestamp.
2. Compare normalized text with the official market detail/contract PDF.
3. Treat absent, stale, truncated, or conflicting terms as uncertainty; never
   silently infer settlement.
4. Add scenario fixtures for sports postponements, ties, voids, and corrections.
5. Keep this feature read-only until an explicitly reviewed decision-flow change
   demonstrates it cannot bypass existing safety checks.
