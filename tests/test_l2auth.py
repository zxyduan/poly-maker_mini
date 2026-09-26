"""Cross-check polymaker.l2auth.hmac_signature against the unified SDK's own
signature builder (polymarket._internal.hmac.build_hmac_signature).

The exchange's heartbeat contract is signed with the SDK's L2 header scheme;
our replica must stay byte-identical or heartbeats (and the dead-man switch)
would fail auth.
"""

from __future__ import annotations

from polymarket._internal.hmac import build_hmac_signature

from polymaker.l2auth import hmac_signature

_VECTORS: list[tuple[str, int, str, str, str | None]] = [
    ("abc123", 1700000000, "POST", "/heartbeats", None),
    ("abc123", 1700000000, "POST", "/v1/heartbeats", None),
    ("aGVsbG8=", 1700000001, "GET", "/book", "{}"),
    ("aGVsbG8=", 1700000002, "DELETE", "/orders", '{"ids":["1"]}'),
    ("a" * 40, 1700000003, "POST", "/auth/api-key", None),
]


def test_hmac_matches_unified_sdk() -> None:
    for secret, ts, method, path, body in _VECTORS:
        ours = hmac_signature(secret=secret, timestamp=ts, method=method, path=path, body=body)
        theirs = build_hmac_signature(
            secret=secret, timestamp=ts, method=method, path=path, body=body
        )
        assert ours == theirs, (secret, method, path)


def test_l2_headers_shape() -> None:
    from types import SimpleNamespace

    from polymaker.l2auth import l2_headers

    client = SimpleNamespace(
        signer="0xSIGNER",
        credentials=SimpleNamespace(key="KEY", secret="SECRET", passphrase="PASS"),
    )
    headers = l2_headers(client, "POST", "/heartbeats")
    assert headers["POLY_ADDRESS"] == "0xSIGNER"
    assert headers["POLY_API_KEY"] == "KEY"
    assert headers["POLY_PASSPHRASE"] == "PASS"
    assert headers["POLY_SIGNATURE"]
    assert headers["POLY_TIMESTAMP"].isdigit()
