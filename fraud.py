"""Anti-fraud checks.

Three checks, all enforced at ingestion time and re-checked at commission time:

1. duplicate_ip — two or more *different* players registering from the same
   IP inside the window. One household sharing an IP is plausible; the check
   flags it for review rather than auto-rejecting, but the conversion is held
   out of commission calculations until reviewed.
2. self_referral — a registration whose email matches the referring
   affiliate's own email (the affiliate referring themselves).
3. zero_wagering_bonus_churn / low_wagering_churn — a player who deposits
   (FTD) and then wagers nothing (or < 10% of the deposit) inside the churn
   window while having taken a bonus: classic bonus-abuse churn.

Flagged conversions are NEVER counted in commission calculations.
Flags live in `fraud_flags` and are cleared only by a human review.
"""
import sqlite3
from datetime import datetime, timedelta, timezone

from .config import (
    CHURN_LOW_WAGER_RATIO,
    CHURN_WINDOW_DAYS,
    DUPLICATE_IP_WINDOW_HOURS,
)


def _record_flag(conn: sqlite3.Connection, entity_type: str, entity_id: str,
                 reason: str, details: str) -> None:
    conn.execute(
        "INSERT INTO fraud_flags (entity_type, entity_id, reason, details)"
        " VALUES (?, ?, ?, ?)",
        (entity_type, entity_id, reason, details),
    )
    conn.commit()


def check_duplicate_ip(conn: sqlite3.Connection, operator: str, ip: str,
                       player_id: str,
                       window_hours: int = DUPLICATE_IP_WINDOW_HOURS,
                       created_at: str | None = None) -> str | None:
    """Return a reason string if another player registered from this IP
    BEFORE this registration inside the window.

    Only earlier registrations count: the first registration from an IP is
    presumed legitimate; later ones are suspicious. `created_at` is this
    registration's timestamp (defaults to now — the live postback case,
    where the new registration is not in the database yet). Same-second
    registrations count as "earlier" so a same-instant duplicate is still
    caught; the review itself always runs before the new row is inserted,
    so the row being reviewed can never match itself.
    """
    if not ip:
        return None
    ref = created_at or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    since = (datetime.strptime(ref, "%Y-%m-%d %H:%M:%S")
             - timedelta(hours=window_hours)).strftime("%Y-%m-%d %H:%M:%S")
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM registrations"
        " WHERE operator = ? AND ip = ? AND player_id != ?"
        " AND created_at >= ? AND created_at <= ?",
        (operator, ip, player_id, since, ref),
    ).fetchone()
    if row and row["n"] >= 1:
        return (
            f"{row['n'] + 1} registrations from IP {ip} within "
            f"{window_hours}h (player {player_id})"
        )
    return None


def check_self_referral(conn: sqlite3.Connection, affiliate_code: str,
                        email: str | None) -> str | None:
    """Return a reason string if the player email matches the affiliate's email."""
    if not email:
        return None
    row = conn.execute(
        "SELECT email FROM affiliates WHERE code = ?", (affiliate_code,)
    ).fetchone()
    if row and row["email"].strip().lower() == email.strip().lower():
        return f"player email {email} matches affiliate {affiliate_code} email"
    return None


def check_wagering_churn(conn: sqlite3.Connection, player_id: str,
                         ftd_amount: float, ftd_time: str | None = None,
                         window_days: int = CHURN_WINDOW_DAYS) -> str | None:
    """Check wagering in the window after an FTD.

    Returns a reason string, or None if wagering looks legitimate.
    A player is evaluated on the wagers recorded up to `window_days` after
    the FTD timestamp (or all wagers if no FTD timestamp is given).
    """
    if ftd_time:
        start = ftd_time
        end = (datetime.strptime(ftd_time, "%Y-%m-%d %H:%M:%S")
               + timedelta(days=window_days)).strftime("%Y-%m-%d %H:%M:%S")
        rows = conn.execute(
            "SELECT COALESCE(SUM(wagered), 0) AS wagered,"
            " COALESCE(SUM(bonus), 0) AS bonus FROM wagers"
            " WHERE player_id = ? AND created_at >= ? AND created_at <= ?",
            (player_id, start, end),
        ).fetchone()
    else:
        rows = conn.execute(
            "SELECT COALESCE(SUM(wagered), 0) AS wagered,"
            " COALESCE(SUM(bonus), 0) AS bonus FROM wagers WHERE player_id = ?",
            (player_id,),
        ).fetchone()
    wagered = rows["wagered"] or 0.0
    bonus = rows["bonus"] or 0.0
    if wagered == 0 and bonus > 0:
        return (
            f"zero wagering after FTD of ${ftd_amount:.2f} with "
            f"${bonus:.2f} bonus claimed in {window_days}d window"
        )
    if wagered == 0 and ftd_amount > 0:
        return (
            f"zero wagering after FTD of ${ftd_amount:.2f} "
            f"in {window_days}d window"
        )
    if wagered < CHURN_LOW_WAGER_RATIO * ftd_amount:
        return (
            f"low wagering: ${wagered:.2f} wagered on ${ftd_amount:.2f} deposit "
            f"(< {int(CHURN_LOW_WAGER_RATIO * 100)}%) in {window_days}d window"
        )
    return None


def review_registration(conn: sqlite3.Connection, operator: str, ip: str,
                        player_id: str, affiliate_code: str,
                        email: str | None,
                        created_at: str | None = None) -> list[str]:
    """Run registration-time fraud checks, record flags, return reasons."""
    reasons: list[str] = []
    dup = check_duplicate_ip(conn, operator, ip, player_id,
                             created_at=created_at)
    if dup:
        reasons.append("duplicate_ip")
        _record_flag(conn, "registration", player_id, "duplicate_ip", dup)
    self_ref = check_self_referral(conn, affiliate_code, email)
    if self_ref:
        reasons.append("self_referral")
        _record_flag(conn, "registration", player_id, "self_referral", self_ref)
    return reasons


def review_ftd(conn: sqlite3.Connection, player_id: str, ftd_amount: float,
               ftd_time: str | None = None) -> str | None:
    """Run the churn check for an FTD. Returns the reason if flagged."""
    reason = check_wagering_churn(conn, player_id, ftd_amount, ftd_time)
    if reason:
        kind = "zero_wagering_bonus_churn" if "zero wagering" in reason else "low_wagering_churn"
        _record_flag(conn, "ftd", player_id, kind, reason)
        return reason
    return None
