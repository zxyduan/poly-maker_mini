"""Pinned L2 HMAC vectors + cross-check against the installed SDK's own signer.

If these tests break, the exchange's L2 wire format (or the SDK's) changed and
the self-signed heartbeat will start getting 401'd — do not adjust the expected
values to make it pass; fix ``l2auth.py`` to match the SDK again.
"""

from __future__ import annotations

import base64

from polymaker.l2auth import _pad_base64, hmac_signature, l2_headers

# Fixed inputs the plan used to derive the reference vector.
_SECRET = "MzQ2NTc4OTAxMjM0NTY3ODkwMTIzNDU2Nzg5MDEyMzQ1"
_TS = 1700000000


def test_golden_vector_post_heartbeats():
    sig = hmac_signature(_SECRET, _TS, "POST", "/heartbeats", body=None)
    assert sig == "iYoH9v4zM9qFn1KQOQWmF4wuPmUgm0BlYvZn5VDHnLM="


def test_body_none_matches_empty_but_payload_differs():
    # The SDK only branches on `body is not None`; appending "" is a no-op, so
    # a bare POST and an empty-body POST hash identically (there is nothing to
    # hash either way). A real payload MUST change the signature.
    bare = hmac_signature(_SECRET, _TS, "POST", "/x", body=None)
    empty = hmac_signature(_SECRET, _TS, "POST", "/x", body="")
    assert bare == empty
    assert hmac_signature(_SECRET, _TS, "POST", "/x", body="{}") != bare


def test_pad_base64_roundtrip():
    assert _pad_base64("YWJj") == "YWJj"
    assert _pad_base64("YWJjZA") == "YWJjZA=="
    # the secret decodes cleanly to raw bytes (no SigningError)
    raw = base64.urlsafe_b64decode(_pad_base64(_SECRET))
    assert isinstance(raw, bytes) and len(raw) > 0


def test_cross_check_against_sdk_signer():
    from polymarket._internal.hmac import build_hmac_signature

    cases = [
        (_SECRET, _TS, "POST", "/heartbeats", None),
        (_SECRET, _TS, "GET", "/book", None),
        (_SECRET, 1717171717, "DELETE", "/order", "abc123"),
        ("c2VjcmV0LXlldC1hbm90aGVyLW9uZQ", 1, "POST", "/v1/heartbeats", ""),
    ]
    for secret, ts, method, path, body in cases:
        mine = hmac_signature(secret, ts, method, path, body)
        theirs = build_hmac_signature(
            secret=secret, timestamp=ts, method=method, path=path, body=body
        )
        assert mine == theirs


def test_l2_headers_shape():
    h = l2_headers(
        signer="0xABC",
        key="k",
        secret=_SECRET,
        passphrase="p",
        method="POST",
        path="/heartbeats",
        body=None,
        timestamp=_TS,
    )
    assert h == {
        "POLY_ADDRESS": "0xABC",
        "POLY_API_KEY": "k",
        "POLY_PASSPHRASE": "p",
        "POLY_SIGNATURE": hmac_signature(_SECRET, _TS, "POST", "/heartbeats", None),
        "POLY_TIMESTAMP": str(_TS),
    }
