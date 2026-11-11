"""Configuration for the affiliate engine.

All secrets come from environment variables. Never commit real keys.
The defaults below are DEMO values only and must be replaced in production.
"""
import os

# SQLite database path (created under data/ by default).
DB_PATH = os.environ.get("AFFILIATE_DB", "data/affiliate.db")

# Shared secret sportsbook operators use to HMAC-sign S2S postbacks.
# In production this is a per-operator secret stored in a secrets manager.
POSTBACK_SECRET = os.environ.get("POSTBACK_SECRET", "dev-secret-change-me")

# Simple token guarding the /admin/* endpoints in this prototype.
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "dev-admin-token")

HMAC_HEADER = "X-Postback-Signature"
IDEMPOTENCY_HEADER = "X-Idempotency-Key"

# ---------------------------------------------------------------------------
# Commission tiers (configurable). Tiers are MARGINAL:
# each unit of volume is paid at the rate of the band it falls in.
# ---------------------------------------------------------------------------

# CPA tiers: (cumulative_ftd_boundary, dollars_per_ftd)
# Boundaries are cumulative counts: the first 9 FTDs are paid at $100,
# FTDs 10-24 at $125, FTDs 25-49 at $150, FTDs 50+ at $200.
CPA_TIERS = [
    (0, 100.0),
    (9, 125.0),
    (24, 150.0),
    (49, 200.0),
]

# NGR revenue-share tiers: (monthly_ngr_threshold_usd, share)
#   NGR $0-$10k      -> 25%
#   NGR $10k-$50k    -> 30%
#   NGR $50k-$100k   -> 35%
#   NGR $100k+       -> 40%
NGR_TIERS = [
    (0, 0.25),
    (10_000, 0.30),
    (50_000, 0.35),
    (100_000, 0.40),
]

# Minimum weekly payout in USD-equivalent. Balances below this roll over.
MIN_PAYOUT_USD = 25.0

# Demo BTC/USD rate used by the mock payout flow.
# This is a FIXED demo constant, not a live price. A real deployment must
# use a live rate source at payout time and record the rate used.
DEMO_BTC_USD_RATE = 95_000.0

# ---------------------------------------------------------------------------
# Anti-fraud windows
# ---------------------------------------------------------------------------
CHURN_WINDOW_DAYS = 7            # wagering window after an FTD
CHURN_LOW_WAGER_RATIO = 0.10     # wagering < 10% of deposit = suspicious
DUPLICATE_IP_WINDOW_HOURS = 24   # registrations from one IP inside this window
