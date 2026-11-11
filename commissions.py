"""Commission calculation engine — tiered CPA and NGR revenue share.

Both schemes use MARGINAL tiers: each unit of volume is paid at the rate of
the band it falls in. A month with 12 FTDs pays the first 9 at the tier-1
rate and the next 3 at the tier-2 rate — the higher rate never applies
retroactively to the whole month.

Flagged conversions are excluded BEFORE any tier math runs: a fraud-flagged
FTD does not count toward the FTD total, and a fraud-flagged player's
wagering does not count toward NGR. A flag on EITHER the registration or
the FTD excludes that conversion from both calculations — a flagged player
contributes nothing to commissions until a human review clears the flag.

NGR (Net Gaming Revenue) for a player in a month:
    NGR = sum(wagered) - sum(won) - sum(bonus)
Negative monthly NGR is clamped to 0 for commission purposes (no negative
commission; the shortfall is not carried forward in this prototype).
"""
import json
import sqlite3

from .config import CPA_TIERS, NGR_TIERS


def _marginal_bands(amount: float, tiers: list[tuple[float, float]]):
    """Split `amount` into marginal bands.

    tiers: [(threshold, rate), ...] sorted ascending by threshold.
    Yields (band_start, band_end_or_None, rate, band_amount).
    """
    bands = []
    for i, (threshold, rate) in enumerate(tiers):
        upper = tiers[i + 1][0] if i + 1 < len(tiers) else None
        if amount <= threshold:
            break
        band_amount = (amount if upper is None else min(amount, upper)) - threshold
        if band_amount > 0:
            bands.append((threshold, upper, rate, band_amount))
    return bands


def cpa_for_ftd_count(ftd_count: int, tiers: list = CPA_TIERS):
    """CPA earnings for `ftd_count` FTDs. Returns (total, breakdown)."""
    total = 0.0
    breakdown = []
    for start, end, rate, n in _marginal_bands(float(ftd_count), tiers):
        subtotal = n * rate
        total += subtotal
        breakdown.append({
            "ftds_from": int(start) + 1 if start == 0 else int(start) + 1,
            "ftds_to": int(end) if end is not None else None,
            "rate_per_ftd": rate,
            "ftd_count": int(n),
            "subtotal": round(subtotal, 2),
        })
    return round(total, 2), breakdown


def ngr_share(ngr: float, tiers: list = NGR_TIERS):
    """Revenue-share earnings for monthly NGR. Returns (total, breakdown)."""
    ngr = max(0.0, ngr)
    total = 0.0
    breakdown = []
    for start, end, rate, band in _marginal_bands(ngr, tiers):
        subtotal = band * rate
        total += subtotal
        breakdown.append({
            "ngr_from": start,
            "ngr_to": end,
            "share": rate,
            "band_ngr": round(band, 2),
            "subtotal": round(subtotal, 2),
        })
    return round(total, 2), breakdown


def monthly_ftd_count(conn: sqlite3.Connection, affiliate_code: str,
                      period: str) -> int:
    """Count commissionable FTDs for an affiliate in YYYY-MM.

    An FTD counts only if the FTD itself is clean AND the player's
    registration is clean — a flag on either holds the conversion out.
    """
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM ftds f"
        " WHERE f.affiliate_code = ? AND strftime('%Y-%m', f.created_at) = ?"
        " AND f.fraud_flag = 0"
        " AND NOT EXISTS (SELECT 1 FROM registrations r"
        "   WHERE r.player_id = f.player_id AND r.operator = f.operator"
        "   AND r.fraud_flag = 1)",
        (affiliate_code, period),
    ).fetchone()
    return row["n"]


def monthly_ngr(conn: sqlite3.Connection, affiliate_code: str,
                period: str) -> float:
    """NGR for an affiliate in YYYY-MM, excluding flagged players.

    A player's wagering counts only if their registration is clean AND they
    have no flagged FTD — a flag on either holds the player out entirely.
    """
    row = conn.execute(
        "SELECT COALESCE(SUM(w.wagered - w.won - w.bonus), 0) AS ngr"
        " FROM wagers w"
        " JOIN registrations r ON r.player_id = w.player_id"
        " WHERE r.affiliate_code = ? AND r.fraud_flag = 0"
        " AND NOT EXISTS (SELECT 1 FROM ftds f"
        "   WHERE f.player_id = w.player_id AND f.fraud_flag = 1)"
        " AND strftime('%Y-%m', w.created_at) = ?",
        (affiliate_code, period),
    ).fetchone()
    return float(row["ngr"] or 0.0)


def calculate_monthly_commissions(conn: sqlite3.Connection, period: str,
                                  approve: bool = False) -> list[dict]:
    """Compute and store one CPA row + one NGR row per active affiliate.

    Earnings are stored with status 'approved' when approve=True, otherwise
    'pending'. Existing rows for the same affiliate/period/type are replaced
    (the run is idempotent per period).
    """
    results = []
    affiliates = conn.execute(
        "SELECT code FROM affiliates WHERE status = 'active'"
    ).fetchall()
    for aff in affiliates:
        code = aff["code"]
        ftd_count = monthly_ftd_count(conn, code, period)
        cpa_total, cpa_breakdown = cpa_for_ftd_count(ftd_count)
        ngr = monthly_ngr(conn, code, period)
        ngr_total, ngr_breakdown = ngr_share(ngr)
        status = "approved" if approve else "pending"
        for kind, amount, basis in (
            ("CPA", cpa_total, {"ftd_count": ftd_count, "tiers": cpa_breakdown}),
            ("NGR", ngr_total, {"ngr": round(ngr, 2), "tiers": ngr_breakdown}),
        ):
            conn.execute(
                "DELETE FROM earnings WHERE affiliate_code = ? AND period = ? AND type = ?",
                (code, period, kind),
            )
            conn.execute(
                "INSERT INTO earnings (affiliate_code, period, type, amount, basis,"
                " tier_applied, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (code, period, kind, amount, json.dumps(basis),
                 json.dumps(basis["tiers"]), status),
            )
            results.append({
                "affiliate_code": code, "period": period, "type": kind,
                "amount": amount, "status": status, "basis": basis,
            })
    conn.commit()
    return results
