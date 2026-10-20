"""HMAC-SHA256 webhook signatures shared by the bank sandbox (signs) and the coordinator (verifies).

Header format (Stripe-style): `X-Bank-Signature: t=<unix ts>,v1=<hex hmac of "<ts>.<raw body>">`.
The timestamp is inside the MAC, so a captured webhook cannot be replayed outside the tolerance window.
"""

import hashlib
import hmac
import time

HEADER = "X-Bank-Signature"
TOLERANCE_SECONDS = 300


class SignatureError(Exception):
    pass


def _mac(secret: str, ts: int, body: bytes) -> str:
    return hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()


def sign(secret: str, body: bytes, ts: int | None = None) -> str:
    if not secret:
        raise SignatureError("webhook secret is not configured")
    ts = int(time.time()) if ts is None else ts
    return f"t={ts},v1={_mac(secret, ts, body)}"


def verify(secret: str, body: bytes, header: str | None, now: float | None = None) -> None:
    """Raises SignatureError unless `header` is a valid, fresh signature of `body`."""
    if not secret:
        raise SignatureError("webhook secret is not configured")  # fail closed
    if not header:
        raise SignatureError("missing signature header")
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        ts, given = int(parts["t"]), parts["v1"]
    except (ValueError, KeyError) as e:
        raise SignatureError("malformed signature header") from e
    if not hmac.compare_digest(_mac(secret, ts, body), given):
        raise SignatureError("signature mismatch")
    now = time.time() if now is None else now
    if abs(now - ts) > TOLERANCE_SECONDS:
        raise SignatureError("signature timestamp outside tolerance")
