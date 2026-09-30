"""Stateless L2 (HMAC) authentication for CLOB endpoints the SDK does not wrap.

The unified ``polymarket-client`` signs every SDK request internally, but the
dead-man-switch heartbeat endpoint (``POST /heartbeats``) is not covered. We
re-implement the exact L2 scheme here so we can self-sign that one request.

This is a *byte-for-byte* mirror of ``polymarket._internal.hmac.build_hmac_signature``
and the SDK's ``_make_l2_header_resolver`` header set — do not "tidy" it. The
golden-vector + cross-check test in ``tests/test_l2auth.py`` pins it against the
installed SDK; if the SDK changes its wire format, that test fails first.
"""

from __future__ import annotations

import base64
import hashlib
import hmac as _hmac
import time


def _pad_base64(value: str) -> str:
    """Restore urlsafe base64 padding (the API secret may ship unpadded)."""
    padding = (-len(value)) % 4
    return value + ("=" * padding)


def hmac_signature(
    secret: str,
    timestamp: int,
    method: str,
    path: str,
    body: str | None = None,
) -> str:
    """Sign ``{timestamp}{method}{path}{body?}`` with the L2 secret.

    ``body=None`` means *no body* (e.g. a bare POST) and is NOT the same as the
    empty string: the body segment is omitted entirely. An empty-string body is
    still appended (it matches the request that sent ``body=""``).
    """
    message = f"{timestamp}{method}{path}"
    if body is not None:
        message += body
    raw_secret = base64.urlsafe_b64decode(_pad_base64(secret))
    digest = _hmac.new(raw_secret, message.encode("utf-8"), hashlib.sha256).digest()
    # NOTE: keep the urlsafe base64 padding — the exchange expects it.
    return base64.urlsafe_b64encode(digest).decode("ascii")


def l2_headers(
    *,
    signer: str,
    key: str,
    secret: str,
    passphrase: str,
    method: str,
    path: str,
    body: str | None = None,
    timestamp: int | None = None,
) -> dict[str, str]:
    """Build the full set of L2 auth headers for one request.

    The HMAC goes in ``POLY_SIGNATURE`` (this is the SDK's L2 layout; there is no
    separate ``POLY_HMAC`` header).
    """
    ts = int(timestamp) if timestamp is not None else int(time.time())
    signature = hmac_signature(secret, ts, method, path, body)
    return {
        "POLY_ADDRESS": signer,
        "POLY_API_KEY": key,
        "POLY_PASSPHRASE": passphrase,
        "POLY_SIGNATURE": signature,
        "POLY_TIMESTAMP": str(ts),
    }


__all__ = ["hmac_signature", "l2_headers"]
