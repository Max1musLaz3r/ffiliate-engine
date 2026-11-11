"""S2S postback webhook server (FastAPI).

Endpoints:
  POST /postback/click         — operator click event
  POST /postback/registration  — new player registration
  POST /postback/ftd           — first-time deposit
  POST /postback/wager         — wagering activity (feeds NGR + churn checks)
  GET  /health
  GET  /dashboard/affiliates
  GET  /dashboard/affiliate/{code}
  GET  /dashboard/payouts
  POST /admin/commissions/{period}   — compute (and optionally approve) a month
  POST /admin/payouts/weekly        — run the weekly BTC payout job (mock)

Security: every /postback/* request must carry a valid HMAC-SHA256 signature
over the raw body in the X-Postback-Signature header, and an idempotency key
in X-Idempotency-Key (or the JSON body). Failed signatures and malformed
payloads go to the dead-letter table and are never processed.
"""
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

from . import fraud
from .commissions import calculate_monthly_commissions
from .config import ADMIN_TOKEN, DB_PATH, HMAC_HEADER, POSTBACK_SECRET
from .db import get_conn, init_db
from .payouts import MockPayoutGateway, aggregate_weekly_payouts, confirm_payouts
from .security import check_idempotency, extract_idempotency_key, verify_signature


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db(DB_PATH)
    yield


app = FastAPI(title="Affiliate Engine — S2S Postbacks", lifespan=lifespan)


def _conn():
    return get_conn(DB_PATH)


def _dead_letter(event_type: str, raw: bytes, reason: str) -> None:
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO dead_letter (event_type, payload, reason) VALUES (?, ?, ?)",
            (event_type, raw.decode("utf-8", "replace"), reason),
        )
        conn.commit()
    finally:
        conn.close()


async def _guard(request: Request, event_type: str):
    """Verify HMAC + idempotency. Returns (body_dict, error_response_or_None)."""
    raw = await request.body()
    signature = request.headers.get(HMAC_HEADER) or request.headers.get(HMAC_HEADER.lower())
    if not verify_signature(POSTBACK_SECRET, raw, signature):
        _dead_letter(event_type, raw, "invalid HMAC signature")
        return None, JSONResponse(
            {"status": "rejected", "reason": "invalid signature"}, status_code=401)
    try:
        body = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        _dead_letter(event_type, raw, "malformed JSON")
        return None, JSONResponse(
            {"status": "rejected", "reason": "malformed JSON"}, status_code=400)
    key = extract_idempotency_key(request.headers, body)
    if not key:
        return None, JSONResponse(
            {"status": "rejected", "reason": "missing idempotency key"},
            status_code=400)
    conn = _conn()
    try:
        if not check_idempotency(conn, key, event_type):
            return None, JSONResponse(
                {"status": "duplicate", "reason": "idempotency key already seen"})
    finally:
        conn.close()
    return body, None


@app.get("/health")
def health():
    return {"status": "ok", "db": DB_PATH}


@app.post("/postback/click")
async def postback_click(request: Request):
    body, err = await _guard(request, "click")
    if err is not None:
        return err
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO clicks (click_id, affiliate_code, operator, ip, user_agent, landing_url)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (body.get("click_id"), body.get("affiliate_code"), body.get("operator"),
             body.get("ip"), body.get("user_agent"), body.get("landing_url")),
        )
        conn.commit()
    except Exception as exc:  # unknown affiliate / duplicate click_id
        conn.rollback()
        return JSONResponse({"status": "rejected", "reason": str(exc)}, status_code=400)
    finally:
        conn.close()
    return {"status": "accepted", "click_id": body.get("click_id")}


@app.post("/postback/registration")
async def postback_registration(request: Request):
    body, err = await _guard(request, "registration")
    if err is not None:
        return err
    conn = _conn()
    try:
        reasons = fraud.review_registration(
            conn, body.get("operator"), body.get("ip"), body.get("player_id"),
            body.get("affiliate_code"), body.get("email"))
        fraud_flag = 1 if reasons else 0
        conn.execute(
            "INSERT INTO registrations (player_id, affiliate_code, operator, ip, email,"
            " fraud_flag, fraud_reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (body.get("player_id"), body.get("affiliate_code"), body.get("operator"),
             body.get("ip"), body.get("email"), fraud_flag,
             ",".join(reasons) if reasons else None),
        )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        return JSONResponse({"status": "rejected", "reason": str(exc)}, status_code=400)
    finally:
        conn.close()
    return {"status": "accepted", "player_id": body.get("player_id"),
            "fraud_flag": fraud_flag, "fraud_reasons": reasons}


@app.post("/postback/ftd")
async def postback_ftd(request: Request):
    body, err = await _guard(request, "ftd")
    if err is not None:
        return err
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO ftds (player_id, affiliate_code, operator, amount, currency)"
            " VALUES (?, ?, ?, ?, ?)",
            (body.get("player_id"), body.get("affiliate_code"), body.get("operator"),
             float(body.get("amount", 0)), body.get("currency", "USD")),
        )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        return JSONResponse({"status": "rejected", "reason": str(exc)}, status_code=400)
    finally:
        conn.close()
    # Churn is evaluated at commission time, once wagering data exists.
    return {"status": "accepted", "player_id": body.get("player_id")}


@app.post("/postback/wager")
async def postback_wager(request: Request):
    body, err = await _guard(request, "wager")
    if err is not None:
        return err
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO wagers (player_id, operator, wagered, won, bonus)"
            " VALUES (?, ?, ?, ?, ?)",
            (body.get("player_id"), body.get("operator"),
             float(body.get("wagered", 0)), float(body.get("won", 0)),
             float(body.get("bonus", 0))),
        )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        return JSONResponse({"status": "rejected", "reason": str(exc)}, status_code=400)
    finally:
        conn.close()
    return {"status": "accepted", "player_id": body.get("player_id")}


# --- Dashboard (read-only) ---------------------------------------------------

@app.get("/dashboard/affiliates")
def dashboard_affiliates():
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT a.code, a.name, a.email, a.btc_address, a.status,"
            " COALESCE((SELECT SUM(amount) FROM earnings e"
            "  WHERE e.affiliate_code = a.code AND e.status = 'approved'), 0) AS approved_usd,"
            " COALESCE((SELECT SUM(amount) FROM earnings e"
            "  WHERE e.affiliate_code = a.code AND e.status = 'paid'), 0) AS paid_usd,"
            " COALESCE((SELECT SUM(amount) FROM earnings e"
            "  WHERE e.affiliate_code = a.code AND e.status = 'pending'), 0) AS pending_usd"
            " FROM affiliates a ORDER BY a.code"
        ).fetchall()
        return {"affiliates": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.get("/dashboard/affiliate/{code}")
def dashboard_affiliate(code: str):
    conn = _conn()
    try:
        aff = conn.execute(
            "SELECT * FROM affiliates WHERE code = ?", (code,)).fetchone()
        if not aff:
            return JSONResponse({"status": "not_found"}, status_code=404)
        earnings = conn.execute(
            "SELECT * FROM earnings WHERE affiliate_code = ? ORDER BY period, type",
            (code,)).fetchall()
        payouts = conn.execute(
            "SELECT * FROM payouts WHERE affiliate_code = ? ORDER BY id DESC",
            (code,)).fetchall()
        flags = conn.execute(
            "SELECT * FROM fraud_flags WHERE entity_id IN"
            " (SELECT player_id FROM registrations WHERE affiliate_code = ?)",
            (code,)).fetchall()
        return {
            "affiliate": dict(aff),
            "earnings": [dict(r) for r in earnings],
            "payouts": [dict(r) for r in payouts],
            "fraud_flags": [dict(r) for r in flags],
        }
    finally:
        conn.close()


@app.get("/dashboard/payouts")
def dashboard_payouts():
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT * FROM payouts ORDER BY id DESC").fetchall()
        return {"payouts": [dict(r) for r in rows]}
    finally:
        conn.close()


# --- Admin -------------------------------------------------------------------

def _admin_ok(token: str | None) -> bool:
    return token is not None and token == ADMIN_TOKEN


@app.post("/admin/commissions/{period}")
def admin_commissions(period: str, request: Request,
                      x_admin_token: str | None = Header(default=None),
                      approve: bool = False):
    if not _admin_ok(x_admin_token):
        return JSONResponse({"status": "rejected", "reason": "bad admin token"},
                            status_code=403)
    conn = _conn()
    try:
        # Re-run churn checks for FTDs in the period before computing.
        ftds = conn.execute(
            "SELECT id, player_id, amount, created_at FROM ftds"
            " WHERE strftime('%Y-%m', created_at) = ? AND fraud_flag = 0",
            (period,)).fetchall()
        for ftd in ftds:
            reason = fraud.review_ftd(
                conn, ftd["player_id"], ftd["amount"], ftd["created_at"])
            if reason:
                conn.execute(
                    "UPDATE ftds SET fraud_flag = 1, fraud_reason = ? WHERE id = ?",
                    (reason, ftd["id"]))
        conn.commit()
        results = calculate_monthly_commissions(conn, period, approve=approve)
        return {"status": "ok", "period": period, "results": results}
    finally:
        conn.close()


@app.post("/admin/payouts/weekly")
def admin_weekly_payouts(request: Request,
                         x_admin_token: str | None = Header(default=None),
                         week: str | None = None):
    if not _admin_ok(x_admin_token):
        return JSONResponse({"status": "rejected", "reason": "bad admin token"},
                            status_code=403)
    week_label = week or datetime.now(timezone.utc).strftime("%Y-W%V")
    conn = _conn()
    try:
        gateway = MockPayoutGateway()
        summary = aggregate_weekly_payouts(conn, week_label, gateway)
        confirmed = confirm_payouts(conn, gateway)
        summary["confirmed_count"] = confirmed
        return {"status": "ok", "summary": summary}
    finally:
        conn.close()
