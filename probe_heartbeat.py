"""Probe the live Polymarket heartbeat contract path by path.

Prints the FULL request (URL, L2 headers — credentials masked —, body) and
FULL response (status, elapsed ms, body) for each candidate path, then tells
you which one the exchange actually acknowledges with HTTP 200.

Run on a machine that can reach clob.polymarket.com and has a valid .env
(PK + BROWSER_ADDRESS) next to the config dir:

    python probe_heartbeat.py --config-dir livecfg

Exit code: 0 = at least one path acknowledged; 1 = none; 2 = no credentials.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

import httpx

from polymaker.config import Config
from polymaker.l2auth import l2_headers

_MASK_HEADERS = {"POLY_API_KEY", "POLY_PASSPHRASE", "POLY_SIGNATURE"}


def _mask(v: str) -> str:
    if not v or len(v) < 12:
        return "***"
    return f"{v[:6]}...{v[-4:]}"


def _masked(headers: dict[str, str]) -> dict[str, str]:
    return {k: (_mask(v) if k in _MASK_HEADERS else v) for k, v in headers.items()}


def _pick_config_dir(default: str) -> Path:
    for name in (default, "config"):
        p = Path(name)
        if (p / "config.toml").exists():
            return p
    raise SystemExit(f"no config.toml found under {default!r} or 'config'")


def probe(path: str, client: object, host: str) -> bool:
    # Reuse the unified SDK's L2 signing (same headers gateway.heartbeat()
    # sends — built by polymaker.l2auth from the SDK credentials).
    headers = l2_headers(client, "POST", path)
    url = f"{host}{path}"
    print("\n" + "=" * 76)
    print(f"[REQUEST] POST {url}")
    print("  headers:")
    for k, v in _masked(headers).items():
        print(f"    {k}: {v}")
    print("  body: (none)")
    t0 = time.monotonic()
    try:
        r = httpx.post(url, headers=headers, timeout=10.0)
    except httpx.HTTPError as exc:
        print(f"  !! request failed: {exc!r}")
        return False
    elapsed_ms = (time.monotonic() - t0) * 1000.0
    print(f"[RESPONSE] HTTP {r.status_code} in {elapsed_ms:.0f} ms")
    print(f"  body: {r.text[:1000]}")
    return r.status_code == 200


async def main() -> int:
    ap = argparse.ArgumentParser(description="Probe Polymarket heartbeat endpoints")
    ap.add_argument("--config-dir", default="livecfg",
                    help="config dir with config.toml (default: livecfg)")
    args = ap.parse_args()

    cdir = _pick_config_dir(args.config_dir)
    cfg = Config.load(cdir)
    sec = cfg.secrets
    if not sec.has_wallet:
        print("FATAL: no wallet credentials. Put PK and BROWSER_ADDRESS in .env "
              "next to the config dir, then rerun.")
        return 2

    from polymarket import AsyncSecureClient

    client = await AsyncSecureClient.create(
        private_key=sec.pk,
        wallet=sec.browser_address,
    )

    print(f"signer EOA : {client.signer}")
    print(f"funder     : {sec.browser_address}")
    print(f"sig_type   : {cfg.wallet.signature_type}  host: {cfg.wallet.clob_host}")

    try:
        paths = ("/heartbeats", "/v1/heartbeats")
        ok = [p for p in paths if probe(p, client, cfg.wallet.clob_host)]
        print("\n" + "=" * 76)
        if ok:
            print(f"VERDICT: acknowledged on {ok} -> gateway.heartbeat() already "
                  f"tries {ok[0]} first, so the bot is on the right path.")
            return 0
        print("VERDICT: no path acknowledged (all non-200). Check creds/network.")
        return 1
    finally:
        await client.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
#（注：内容由AI生成）
