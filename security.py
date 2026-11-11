"""HMAC signature verification and idempotency for S2S postbacks.

Operators sign the raw request body with a shared secret:
    signature = HMAC-SHA256(secret, raw_body)   (hex, optionally "sha256=" prefixed)

Every postback must also carry an idempotency key (header or body field).
A replayed key is acknowledged but never processed twice.
"""
import hashlib
import hmac
import sqlite3

from config import HMAC_HEADER, IDEMPOTENCY_HEADER


def compute_signature(secret: str, raw_body: bytes) -> str:
    """Hex HMAC-SHA256 of the raw body under the shared secret."""
    return hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()


def verify_signature(secret: str, raw_body: bytes, signature: str | None) -> bool:
    """Constant-time check. Accepts bare hex or a 'sha256=<hex>' value."""
    if not signature:
        return False
    sig = signature.strip()
    if sig.startswith("sha256="):
        sig = sig[len("sha256="):]
    expected = compute_signature(secret, raw_body)
    return hmac.compare_digest(expected, sig.lower())


def check_idempotency(conn: sqlite3.Connection, key: str, event_type: str) -> bool:
    """Record an idempotency key.

    Returns True if the key is new (caller should process the event),
    False if the key was already seen (caller should acknowledge only).
    """
    try:
        conn.execute(
            "INSERT INTO idempotency (idempotency_key, event_type) VALUES (?, ?)",
            (key, event_type),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def extract_idempotency_key(headers, body: dict) -> str | None:
    """Idempotency key from header first, then the JSON body."""
    key = headers.get(IDEMPOTENCY_HEADER) or headers.get(IDEMPOTENCY_HEADER.lower())
    if key:
        return key
    return body.get("idempotency_key")
