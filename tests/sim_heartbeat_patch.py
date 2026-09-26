"""Standalone verification of the patched ExecutionGateway.heartbeat().

Simulates the three live-mode branches (200 ack, 404->fallback, 400 reject)
with a fake client and a monkeypatched httpx.post + gateway.l2_headers;
no network involved.
"""

from __future__ import annotations

import asyncio

from polymaker.config import Config
from polymaker.execution import gateway as gw_mod


class FakeClient:
    """Minimal stand-in for the unified SDK client (records header signings)."""

    def __init__(self) -> None:
        self.called_paths: list[str] = []


class FakeResp:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


def make_gw() -> tuple[gw_mod.ExecutionGateway, FakeClient]:
    cfg = Config()
    gw = gw_mod.ExecutionGateway(cfg, paper=False)
    client = FakeClient()
    gw._client = client  # type: ignore[attr-defined]  # bypass connect()
    return gw, client


def install_fake_l2(gw_mod: object, client: FakeClient) -> None:
    """Patch gateway's l2_headers to record the requested path."""

    def fake_l2(client_: object, method: str, path: str, body: str | None = None) -> dict[str, str]:
        client.called_paths.append(path)
        return {"POLY_ADDRESS": "0xabc", "POLY_SIGNATURE": "sig"}

    gw_mod.l2_headers = fake_l2  # type: ignore[attr-defined]


def test_200_ack() -> None:
    gw, client = make_gw()
    install_fake_l2(gw_mod, client)
    responses = iter([FakeResp(200)])

    def fake_post(url: str, headers=None, timeout=None) -> FakeResp:
        return next(responses)

    gw_mod.httpx.post = fake_post
    ok = asyncio.run(gw.heartbeat())
    assert ok is True
    assert gw.heartbeat_failures == 0
    assert client.called_paths == ["/heartbeats"]
    print("test_200_ack OK")


def test_404_fallback() -> None:
    gw, client = make_gw()
    install_fake_l2(gw_mod, client)
    responses = iter([FakeResp(404), FakeResp(200)])

    def fake_post(url: str, headers=None, timeout=None) -> FakeResp:
        return next(responses)

    gw_mod.httpx.post = fake_post
    ok = asyncio.run(gw.heartbeat())
    assert ok is True
    assert gw.heartbeat_failures == 0
    assert client.called_paths == ["/heartbeats", "/v1/heartbeats"]
    print("test_404_fallback OK")


def test_400_reject_counts_failure() -> None:
    gw, client = make_gw()
    install_fake_l2(gw_mod, client)
    responses = iter([FakeResp(400)])

    def fake_post(url: str, headers=None, timeout=None) -> FakeResp:
        return next(responses)

    gw_mod.httpx.post = fake_post
    ok = asyncio.run(gw.heartbeat())
    assert ok is False
    assert gw.heartbeat_failures == 1
    assert client.called_paths == ["/heartbeats"]
    print("test_400_reject_counts_failure OK")


def test_paper_noop() -> None:
    gw = gw_mod.ExecutionGateway(Config(), paper=True)
    ok = asyncio.run(gw.heartbeat())
    assert ok is True
    assert gw.heartbeat_failures == 0
    print("test_paper_noop OK")


if __name__ == "__main__":
    test_200_ack()
    test_404_fallback()
    test_400_reject_counts_failure()
    test_paper_noop()
    print("ALL HEARTBEAT SIMULATIONS PASSED")
