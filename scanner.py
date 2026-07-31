"""Stage 0 funding-rate scanner.

Once per run (intended: hourly via cron / GitHub Actions):

  1. Pull funding for every perp on Hyperliquid (public Info API).
  2. Pull funding from OKX and Kraken Futures (public endpoints).
     (Binance & Bybit block US IPs with HTTP 451/403 -- see README.)
  3. Normalize everything to a per-hour relative rate:
       - Hyperliquid quotes per 1h            -> use as-is
       - OKX quotes per funding interval      -> divide by interval hours
       - Kraken quotes ABSOLUTE $/hr          -> divide by mark price
  4. For each symbol listed on HL + a CEX, compute the gross funding spread,
     subtract round-trip fees amortized over the configured hold period,
     and append one row per (symbol, cex) to data/funding_log.csv.
  5. Log HIP-3 / builder-dex perp funding to a separate CSV. LOG ONLY --
     tokenized-equity trading is off-limits (hard rule #6).

Reads public data only. No keys. No accounts. No orders. Ever (Stage 0).
"""

from __future__ import annotations

import csv
import datetime as dt
import sys
import time
from pathlib import Path

import requests

import config

HL_INFO = "https://api.hyperliquid.xyz/info"
OKX_TICKERS = "https://www.okx.com/api/v5/market/tickers?instType=SWAP"
OKX_FUNDING = "https://www.okx.com/api/v5/public/funding-rate"
KRAKEN_TICKERS = "https://futures.kraken.com/derivatives/api/v3/tickers"

HOURS_PER_YEAR = 24 * 365


def log(msg: str) -> None:
    print(f"[scanner] {msg}", flush=True)


def utcnow_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Fetchers -- each returns {normalized_symbol: {...}} and never raises;
# a venue that fails just drops out of this run.
# ---------------------------------------------------------------------------

def fetch_hl(dex: str | None = None) -> dict[str, dict]:
    """Hyperliquid perps. `funding` is a relative rate per 1 hour."""
    payload: dict = {"type": "metaAndAssetCtxs"}
    if dex:
        payload["dex"] = dex
    r = requests.post(HL_INFO, json=payload, timeout=config.HTTP_TIMEOUT)
    r.raise_for_status()
    meta, ctxs = r.json()
    out: dict[str, dict] = {}
    for m, c in zip(meta["universe"], ctxs):
        if m.get("isDelisted"):
            continue
        name = m["name"]
        sym = name
        if sym.startswith(config.HL_K_PREFIX) and sym[1:].isupper():
            sym = sym[1:]  # kPEPE -> PEPE (rates are relative; safe)
        try:
            mark = float(c["markPx"])
            out[sym] = {
                "hl_name": name,
                "funding_1h": float(c["funding"]),
                "mark": mark,
                "oi_usd": float(c["openInterest"]) * mark,
                "day_vlm_usd": float(c["dayNtlVlm"]),
            }
        except (KeyError, TypeError, ValueError):
            continue
    return out


def fetch_okx(wanted: set[str]) -> dict[str, dict]:
    """OKX USDT linear swaps. fundingRate is per funding interval
    (8h for most, 4h for some) -- normalize using fundingTime deltas."""
    r = requests.get(OKX_TICKERS, timeout=config.HTTP_TIMEOUT)
    r.raise_for_status()
    marks: dict[str, float] = {}
    for t in r.json()["data"]:
        inst = t["instId"]  # e.g. BTC-USDT-SWAP
        parts = inst.split("-")
        if len(parts) == 3 and parts[1] == "USDT" and parts[0] in wanted:
            try:
                marks[parts[0]] = float(t["last"])
            except (TypeError, ValueError):
                pass

    out: dict[str, dict] = {}
    for sym, mark in marks.items():
        try:
            fr = requests.get(
                OKX_FUNDING,
                params={"instId": f"{sym}-USDT-SWAP"},
                timeout=config.HTTP_TIMEOUT,
            )
            fr.raise_for_status()
            data = fr.json()["data"]
            if not data:
                continue
            d = data[0]
            interval_h = (int(d["nextFundingTime"]) - int(d["fundingTime"])) / 3.6e6
            if interval_h <= 0:
                interval_h = 8.0
            out[sym] = {
                "funding_1h": float(d["fundingRate"]) / interval_h,
                "mark": mark,
                "interval_h": interval_h,
            }
        except Exception as e:  # one bad instrument shouldn't kill the venue
            log(f"okx {sym}: {e}")
        time.sleep(config.OKX_THROTTLE_S)
    return out


def fetch_kraken() -> dict[str, dict]:
    """Kraken Futures PF_ perps. tickers.fundingRate is ABSOLUTE quote-ccy
    per hour per 1 unit of base -> relative per-hour = fundingRate / mark."""
    r = requests.get(KRAKEN_TICKERS, timeout=config.HTTP_TIMEOUT)
    r.raise_for_status()
    out: dict[str, dict] = {}
    for t in r.json()["tickers"]:
        if t.get("tag") != "perpetual" or t.get("suspended"):
            continue
        try:
            base = t["pair"].split(":")[0]
            base = config.KRAKEN_BASE_ALIASES.get(base, base)
            mark = float(t["markPrice"])
            if mark <= 0:
                continue
            out[base] = {
                "funding_1h": float(t["fundingRate"]) / mark,
                "mark": mark,
            }
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            continue
    return out


# ---------------------------------------------------------------------------
# Spread math
# ---------------------------------------------------------------------------

def fee_8h_bps(cex: str) -> float:
    """Round-trip fees on both legs (open+close, HL + CEX), amortized
    per 8h period over the configured hold, in bps of one leg's notional."""
    mode = config.FEE_MODE
    round_trip = 2 * config.FEES["hl"][mode] + 2 * config.FEES[cex][mode]
    periods = config.HOLD_DAYS * 3  # three 8h periods per day
    return round_trip * 1e4 / periods


def build_rows(ts: str, hl: dict, cexes: dict[str, dict]) -> list[dict]:
    rows = []
    for cex_name, cex_data in cexes.items():
        fee_bps = fee_8h_bps(cex_name)
        for sym, h in hl.items():
            c = cex_data.get(sym)
            if not c:
                continue
            gross_8h_bps = (h["funding_1h"] - c["funding_1h"]) * 8 * 1e4
            net_8h_bps = abs(gross_8h_bps) - fee_bps
            rows.append({
                "ts_utc": ts,
                "symbol": sym,
                "cex": cex_name,
                "hl_funding_1h": f"{h['funding_1h']:.10f}",
                "cex_funding_1h": f"{c['funding_1h']:.10f}",
                "hl_apr_pct": round(h["funding_1h"] * HOURS_PER_YEAR * 100, 3),
                "cex_apr_pct": round(c["funding_1h"] * HOURS_PER_YEAR * 100, 3),
                "gross_spread_8h_bps": round(gross_8h_bps, 4),
                "fee_8h_bps": round(fee_bps, 4),
                "net_spread_8h_bps": round(net_8h_bps, 4),
                "net_apr_pct": round(net_8h_bps * 3 * 365 / 100, 3),
                "direction": ("short_hl_long_cex" if gross_8h_bps > 0
                              else "short_cex_long_hl"),
                "hl_mark": h["mark"],
                "cex_mark": c["mark"],
                "hl_oi_usd": round(h["oi_usd"]),
                "hl_day_vlm_usd": round(h["day_vlm_usd"]),
            })
    rows.sort(key=lambda r: r["net_spread_8h_bps"], reverse=True)
    return rows


# ---------------------------------------------------------------------------
# HIP-3 / builder dexs (equity, RWA perps). LOG ONLY. NEVER TRADE.
# ---------------------------------------------------------------------------

def fetch_hip3_rows(ts: str) -> list[dict]:
    rows = []
    try:
        r = requests.post(HL_INFO, json={"type": "perpDexs"},
                          timeout=config.HTTP_TIMEOUT)
        r.raise_for_status()
        dexs = [d["name"] for d in r.json() if d and d.get("name")]
    except Exception as e:
        log(f"perpDexs: {e}")
        return rows
    for dex in dexs:
        try:
            assets = fetch_hl(dex=dex)
        except Exception as e:
            log(f"hip3 dex {dex}: {e}")
            continue
        for sym, h in assets.items():
            rows.append({
                "ts_utc": ts,
                "dex": dex,
                "symbol": h["hl_name"],
                "funding_1h": f"{h['funding_1h']:.10f}",
                "apr_pct": round(h["funding_1h"] * HOURS_PER_YEAR * 100, 3),
                "mark": h["mark"],
                "oi_usd": round(h["oi_usd"]),
                "day_vlm_usd": round(h["day_vlm_usd"]),
            })
        time.sleep(0.2)
    return rows


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------

def append_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not path.exists()
    with path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        if new_file:
            w.writeheader()
        w.writerows(rows)


def main() -> int:
    ts = utcnow_iso()
    log(f"run @ {ts}")

    try:
        hl = fetch_hl()
        log(f"hyperliquid: {len(hl)} perps")
    except Exception as e:
        log(f"FATAL: hyperliquid fetch failed: {e}")
        return 1  # no HL leg -> nothing to compare; fail loudly

    cexes: dict[str, dict] = {}
    try:
        cexes["kraken"] = fetch_kraken()
        log(f"kraken: {len(cexes['kraken'])} perps")
    except Exception as e:
        log(f"kraken failed (continuing): {e}")
    try:
        cexes["okx"] = fetch_okx(set(hl.keys()))
        log(f"okx: {len(cexes['okx'])} matched perps")
    except Exception as e:
        log(f"okx failed (continuing): {e}")

    if not any(cexes.values()):
        log("FATAL: no CEX data this run")
        return 1

    rows = build_rows(ts, hl, cexes)
    append_csv(Path(config.DATA_DIR) / config.MAIN_CSV, rows)
    log(f"wrote {len(rows)} spread rows")

    hip3 = fetch_hip3_rows(ts)
    append_csv(Path(config.DATA_DIR) / config.HIP3_CSV, hip3)
    log(f"wrote {len(hip3)} HIP-3 rows (log-only)")

    log("top 10 net spreads (bps/8h, after fees):")
    for r in rows[:10]:
        log(f"  {r['symbol']:>10} vs {r['cex']:<6} "
            f"net={r['net_spread_8h_bps']:>8.3f}  "
            f"gross={r['gross_spread_8h_bps']:>8.3f}  {r['direction']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
