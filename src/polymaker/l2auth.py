"""L2 request-header signing (POLY_* headers) for the exchange heartbeat.

The unified SDK (``polymarket-client``) signs its own requests internally but
exposes no public API to sign an arbitrary ``{method, path, body}`` request.
The exchange's maker dead-man switch — ``POST /heartbeats`` with L2 headers and
no body — is NOT covered by the SDK, so we reproduce the SDK's header scheme
here instead of poking at ``polymarket._internal``.

The signature spec is the same one the SDK uses
(``polymarket._internal.hmac.build_hmac_signature``): base64url-decode the
credential secret, HMAC-SHA256 over ``"<timestamp><method><path>[<body>]"``,
and base64url-encode the digest. ``tests/test_l2auth.py`` cross-checks this
replica against the SDK's own function so the two schemes can never drift.
"""

from __future__ import annotations

import base64
import hashlib
import hmac as _hmac
import time
from typing import Any


def hmac_signature(
    *,
    secret: str,
    timestamp: int,
    method: str,
    path: str,
    body: str | None = None,
) -> str:
    """Build the L2 HMAC signature exactly like the unified SDK.

    Must stay byte-for-byte identical to
    ``polymarket._internal.hmac.build_hmac_signature`` — the exchange verifies
    both. ``tests/test_l2auth.py`` asserts parity on several vectors.
    """
    message = f"{timestamp}{method}{path}"
    if body is not None:
        message += body

    raw_secret = base64.urlsafe_b64decode(_pad_base64(secret))
    digest = _hmac.new(raw_secret, message.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii")


def _pad_base64(value: str) -> str:
    padding = (-len(value)) % 4
    return value + ("=" * padding)


def l2_headers(client: Any, method: str, path: str, body: str | None = None) -> dict[str, str]:
    """Build the L2 auth headers for ``client`` (a unified-SDK client).

    ``client`` must expose ``.credentials`` (ApiKeyCreds with key/secret/
    passphrase) and ``.signer`` (the signing EOA). This is the same header set
    the SDK attaches to its authenticated CLOB requests.
    """
    creds = client.credentials
    timestamp = int(time.time())
    signature = hmac_signature(
        secret=creds.secret,
        timestamp=timestamp,
        method=method,
        path=path,
        body=body,
    )
    return {
        "POLY_ADDRESS": str(client.signer),
        "POLY_API_KEY": creds.key,
        "POLY_PASSPHRASE": creds.passphrase,
        "POLY_SIGNATURE": signature,
        "POLY_TIMESTAMP": str(timestamp),
    }
