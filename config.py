"""Configuration for the Stage 0 funding-rate scanner.

Every assumption that affects the numbers lives in this file.
If you change a fee or the hold period, the net-spread columns change --
document any change in the README so the dataset stays interpretable.
"""

# ---------------------------------------------------------------------------
# Fees (fraction of notional, per fill). Base-tier, no volume discounts.
# Sources: exchange fee schedules as of July 2026 -- re-verify before Stage 2.
# ---------------------------------------------------------------------------
FEES = {
    "hl":     {"maker": 0.00015, "taker": 0.00045},  # Hyperliquid perps
    "okx":    {"maker": 0.00020, "taker": 0.00050},  # OKX USDT perp, lvl 1
    "kraken": {"maker": 0.00020, "taker": 0.00050},  # Kraken Futures PF_, base
}

# Fee mode used for the headline net-spread columns.
# "maker" matches the plan's break-even math (~1.3 bps/8h on a 7-day hold).
FEE_MODE = "maker"

# Assumed hold period over which round-trip fees are amortized.
HOLD_DAYS = 7

# ---------------------------------------------------------------------------
# Symbol normalization
# ---------------------------------------------------------------------------
# Hyperliquid prefixes 1000x-denominated assets with "k" (kPEPE = 1000 PEPE).
# Funding rates are RELATIVE, so denomination doesn't matter for comparison;
# strip the prefix to match CEX listings.
HL_K_PREFIX = "k"

# Kraken uses legacy base codes in its "pair" field.
KRAKEN_BASE_ALIASES = {"XBT": "BTC", "XDG": "DOGE"}

# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------
HTTP_TIMEOUT = 15          # seconds per request
OKX_THROTTLE_S = 0.12      # OKX funding endpoint: 20 req / 2 s -> stay under

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
DATA_DIR = "data"
MAIN_CSV = "funding_log.csv"       # HL vs CEX spread rows (the dataset)
HIP3_CSV = "hip3_funding_log.csv"  # HIP-3 / builder-dex perps, LOG ONLY
