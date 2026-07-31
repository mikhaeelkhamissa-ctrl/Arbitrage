# funding-scanner

Hourly logger of perp funding-rate spreads between **Hyperliquid** and US-accessible
CEXs (**OKX**, **Kraken Futures**). Stage 0 of a delta-neutral funding-rate arbitrage
research project: before any capital moves, log every spread for two weeks and see
what's actually left after fees.

**This tool reads public market data only.** No API keys, no accounts, no orders.

## Why OKX + Kraken and not Binance?

The original plan compared Hyperliquid against Binance. Empirically, as of July 2026,
`fapi.binance.com` returns **HTTP 451** and Bybit returns **HTTP 403** to US IP
addresses — including GitHub Actions runners. No VPNs / geo-evasion, period, so the
comparison venues are the ones a US person can actually reach: OKX (public data
accessible) and Kraken Futures (public data accessible, and one of the few venues
where a US person could legally trade the CEX leg later). That constraint is itself
a finding, and it makes the dataset more honest: these are the spreads available to
*this* trader, not to a hypothetical offshore one.

## How it works

`scanner.py` runs once per hour (GitHub Actions, `.github/workflows/scan.yml`):

1. **Hyperliquid** `POST /info {"type":"metaAndAssetCtxs"}` — funding is quoted
   **per 1 hour** (relative rate).
2. **OKX** `GET /api/v5/public/funding-rate` per instrument — funding is quoted
   **per funding interval** (8h for most instruments, 4h for some); normalized by
   dividing by the interval computed from `nextFundingTime - fundingTime`.
3. **Kraken Futures** `GET /derivatives/api/v3/tickers` — `fundingRate` is
   **absolute (quote currency per hour per unit of base)**; normalized by dividing
   by mark price.
4. Symbols matched across venues (HL's `kPEPE` 1000x-prefix stripped — funding
   rates are relative, so denomination is irrelevant; Kraken `XBT`→`BTC`).
5. One row per (symbol, cex) appended to `data/funding_log.csv`, sorted by net
   spread within each run.
6. HIP-3 / builder-dex perps (equities, RWAs) logged to
   `data/hip3_funding_log.csv`. **Log-only. Never traded** — tokenized-equity
   perps are geo-restricted for US persons and carry overnight-gap / oracle-lag /
   roll risks (WTI perp printed −531% annualized funding, April 2026).

## The numbers

All spread math uses relative rates. Definitions:

| Column | Meaning |
|---|---|
| `ts_utc` | Scan timestamp (ISO 8601, UTC) |
| `symbol` | Normalized base asset |
| `cex` | Comparison venue (`okx` or `kraken`) |
| `hl_funding_1h` / `cex_funding_1h` | Relative funding rate per hour (decimal; positive = longs pay shorts) |
| `hl_apr_pct` / `cex_apr_pct` | Above, annualized ×(24·365), in % |
| `gross_spread_8h_bps` | `(hl − cex) × 8 × 10⁴` — signed spread per 8h in bps |
| `fee_8h_bps` | Round-trip fees (open+close, both legs) amortized over the assumed hold, per 8h |
| `net_spread_8h_bps` | `abs(gross) − fee` — **the number that matters** |
| `net_apr_pct` | Net spread annualized, in % |
| `direction` | Which venue you'd short to collect the spread |
| `hl_mark` / `cex_mark` | Mark prices at scan time |
| `hl_oi_usd` / `hl_day_vlm_usd` | HL open interest / 24h volume (capacity sanity check) |

**Fee assumptions** (`config.py`): base-tier maker fees — HL 1.5 bps, OKX 2 bps,
Kraken 2 bps per fill; round trip = 2 fills per venue = 7 bps total, amortized over
a 7-day hold (21 × 8h periods) → **0.333 bps per 8h**. Slippage is NOT modeled here;
it's applied at analysis time (Stage 2 gate requires slippage-modeled results).

**Stage 0 gate:** two weeks of data, sustained net spreads > 2 bps/8h on ≥ 3 symbols.
Benchmark to beat before any bot trades: sUSDe (high-single-digit APY, one click).

## Running it

Local (Python 3.12+):

```bash
pip install -r requirements.txt
python scanner.py            # appends one scan to data/*.csv, prints top 10
```

GitHub (the intended mode — laptop can stay closed):

1. Create a **public** repo, push this folder to `main`.
2. Repo → Actions → enable workflows. The scan runs hourly at :07 UTC and commits
   new rows back to the repo.
3. Test immediately: Actions → *hourly-funding-scan* → **Run workflow**.

Note: GitHub disables scheduled workflows after ~60 days without repository
activity; any manual commit (analysis notebooks, README edits) resets the clock.

## Known limitations (deliberate, Stage 0 scope)

- Hourly snapshots of *current/predicted* funding, not settled-funding history.
  Good enough to find candidates; the gate analysis can pull historical endpoints
  for the shortlist.
- No slippage / depth modeling (that's the Stage 2 entry gate's job).
- Kraken's long-tail marks can be stale on thin symbols — treat single-venue
  outliers with suspicion (thin-market force-settlement is a known risk class).
- One venue failing drops out of that run instead of failing the scan; Hyperliquid
  failing fails the run loudly.
