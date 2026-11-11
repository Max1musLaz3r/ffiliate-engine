"""Automated BTC payout: aggregation, address validation, gateway adapter.

Pipeline (weekly):
  1. Sum each affiliate's APPROVED (unpaid) earnings.
  2. Skip balances below MIN_PAYOUT_USD (they roll over to next week).
  3. Validate the affiliate's BTC address. Invalid -> payout request recorded
     as `invalid_address`, no money moves, operator is alerted.
  4. Convert USD -> BTC at the configured rate (demo: fixed rate constant).
  5. Submit the batch through the payout gateway adapter.
  6. Record every payout with its TXID and timestamps; mark earnings paid.

NO REAL MONEY MOVES HERE. The default gateway is a mock that simulates
submission and returns deterministic fake TXIDs. A real gateway is a
separate adapter class that raises until the operator wires in their own
API credentials — this module never asks for, stores, or invents keys.
"""
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from btc import validate_btc_address
from config import DEMO_BTC_USD_RATE, MIN_PAYOUT_USD


# --- Gateway adapter ---------------------------------------------------------

@dataclass
class Payment:
    affiliate_code: str
    amount_btc: float
    btc_address: str
    request_id: int


@dataclass
class PaymentResult:
    request_id: int
    txid: str | None
    status: str          # submitted | failed
    error: str | None = None


class PayoutGateway:
    """Interface every payout gateway must implement."""

    def submit_batch(self, payments: list[Payment]) -> list[PaymentResult]:
        raise NotImplementedError


class MockPayoutGateway(PayoutGateway):
    """Simulated gateway for demo/testing.

    Returns deterministic fake TXIDs derived from the payment contents, so
    repeated runs are reproducible and no real transaction ever occurs.
    """

    def submit_batch(self, payments: list[Payment]) -> list[PaymentResult]:
        results = []
        for p in payments:
            digest = hashlib.sha256(
                f"mock|{p.request_id}|{p.affiliate_code}|{p.btc_address}|{p.amount_btc:.8f}"
                .encode()
            ).hexdigest()
            results.append(PaymentResult(
                request_id=p.request_id, txid=digest, status="submitted",
            ))
        return results

    def confirm(self, txid: str) -> str:
        """Simulate on-chain confirmation. Returns the confirmation timestamp."""
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class RealPayoutGateway(PayoutGateway):
    """REAL gateway — NOT implemented in this prototype, by design.

    To connect a real payout provider (e.g. a crypto payout API):
      1. Subclass PayoutGateway and implement submit_batch() against the
         provider's REST API.
      2. Pass credentials ONLY via environment variables / a secrets manager.
      3. Never paste API keys into code, logs, or chat.

    This stub exists so the integration point is explicit and auditable.
    """

    def __init__(self, *args, **kwargs):
        raise NotImplementedError(
            "Real payout gateway requires the operator's own API credentials. "
            "Configure a PayoutGateway subclass with credentials from a "
            "secrets manager; this prototype ships with the mock only."
        )


# --- Weekly aggregation ------------------------------------------------------

def usd_to_btc(amount_usd: float, rate: float = DEMO_BTC_USD_RATE) -> float:
    """Convert USD to BTC at the given rate, rounded to satoshi precision."""
    return round(amount_usd / rate, 8)


def aggregate_weekly_payouts(conn: sqlite3.Connection, week_label: str,
                             gateway: PayoutGateway,
                             rate: float = DEMO_BTC_USD_RATE,
                             min_payout_usd: float = MIN_PAYOUT_USD) -> dict:
    """Run one weekly payout cycle. Returns a summary dict.

    week_label is an ISO week string such as "2026-W46".
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    summary = {
        "week": week_label, "paid": [], "below_minimum": [],
        "invalid_address": [], "total_btc": 0.0,
    }

    rows = conn.execute(
        "SELECT affiliate_code, COALESCE(SUM(amount), 0) AS total"
        " FROM earnings WHERE status = 'approved'"
        " GROUP BY affiliate_code"
    ).fetchall()

    payments: list[Payment] = []
    for row in rows:
        code, total_usd = row["affiliate_code"], float(row["total"])
        aff = conn.execute(
            "SELECT btc_address FROM affiliates WHERE code = ?", (code,)
        ).fetchone()
        address = aff["btc_address"] if aff else ""
        valid, _atype, reason = validate_btc_address(address)

        if total_usd < min_payout_usd:
            conn.execute(
                "INSERT INTO payout_requests (affiliate_code, period, amount_usd,"
                " amount_btc, btc_address, status) VALUES (?, ?, ?, ?, ?, 'below_minimum')",
                (code, week_label, total_usd, usd_to_btc(total_usd, rate), address),
            )
            summary["below_minimum"].append(
                {"affiliate_code": code, "amount_usd": total_usd})
            continue

        if not valid:
            conn.execute(
                "INSERT INTO payout_requests (affiliate_code, period, amount_usd,"
                " amount_btc, btc_address, status) VALUES (?, ?, ?, ?, ?, 'invalid_address')",
                (code, week_label, total_usd, usd_to_btc(total_usd, rate), address),
            )
            summary["invalid_address"].append(
                {"affiliate_code": code, "amount_usd": total_usd, "reason": reason})
            continue

        amount_btc = usd_to_btc(total_usd, rate)
        cur = conn.execute(
            "INSERT INTO payout_requests (affiliate_code, period, amount_usd,"
            " amount_btc, btc_address, status) VALUES (?, ?, ?, ?, ?, 'approved')",
            (code, week_label, total_usd, amount_btc, address),
        )
        payments.append(Payment(code, amount_btc, address, cur.lastrowid))

    conn.commit()

    if not payments:
        return summary

    results = gateway.submit_batch(payments)
    for payment, result in zip(payments, results):
        if result.status == "submitted" and result.txid:
            conn.execute(
                "INSERT INTO payouts (payout_request_id, affiliate_code, amount_btc,"
                " btc_address, txid, status, submitted_at)"
                " VALUES (?, ?, ?, ?, ?, 'submitted', ?)",
                (payment.request_id, payment.affiliate_code, payment.amount_btc,
                 payment.btc_address, result.txid, now),
            )
            conn.execute(
                "UPDATE payout_requests SET status = 'submitted' WHERE id = ?",
                (payment.request_id,),
            )
            conn.execute(
                "UPDATE earnings SET status = 'paid'"
                " WHERE affiliate_code = ? AND status = 'approved'",
                (payment.affiliate_code,),
            )
            summary["paid"].append({
                "affiliate_code": payment.affiliate_code,
                "amount_usd": None, "amount_btc": payment.amount_btc,
                "txid": result.txid,
            })
            summary["total_btc"] += payment.amount_btc
        else:
            conn.execute(
                "UPDATE payout_requests SET status = 'approved' WHERE id = ?",
                (payment.request_id,),
            )
            conn.execute(
                "INSERT INTO payouts (payout_request_id, affiliate_code, amount_btc,"
                " btc_address, txid, status, submitted_at)"
                " VALUES (?, ?, ?, ?, ?, 'failed', ?)",
                (payment.request_id, payment.affiliate_code, payment.amount_btc,
                 payment.btc_address, None, now),
            )
    conn.commit()

    # Attach USD amounts to the paid list for the summary.
    for entry in summary["paid"]:
        row = conn.execute(
            "SELECT amount_usd FROM payout_requests WHERE affiliate_code = ?"
            " AND period = ? AND status = 'submitted'",
            (entry["affiliate_code"], week_label),
        ).fetchone()
        if row:
            entry["amount_usd"] = row["amount_usd"]
    summary["total_btc"] = round(summary["total_btc"], 8)
    return summary


def confirm_payouts(conn: sqlite3.Connection, gateway: MockPayoutGateway) -> int:
    """Mark submitted mock payouts as confirmed (demo reconciliation step)."""
    rows = conn.execute(
        "SELECT id, txid FROM payouts WHERE status = 'submitted'"
    ).fetchall()
    for row in rows:
        confirmed_at = gateway.confirm(row["txid"])
        conn.execute(
            "UPDATE payouts SET status = 'confirmed', confirmed_at = ? WHERE id = ?",
            (confirmed_at, row["id"]),
        )
    conn.commit()
    return len(rows)
