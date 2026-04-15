"""Authentication — loads credentials from polymarket CLI config or .env."""

import json
import os
import sys
from pathlib import Path
from typing import Optional

from eth_account import Account

from py_clob_client.client import ClobClient

from . import config

# Suppress eth_account warnings
Account.enable_unaudited_hdwallet_features()


def _load_cli_config() -> dict:
    """Load credentials from ~/.config/polymarket/config.json."""
    path = Path.home() / ".config" / "polymarket" / "config.json"
    if not path.exists():
        raise FileNotFoundError(
            f"Polymarket CLI config not found at {path}. "
            "Please run `polymarket setup` first."
        )
    with open(path) as f:
        return json.load(f)


def _derive_funder_address(private_key: str) -> str:
    """Derive the EOA address from a private key."""
    acct = Account.from_key(private_key)
    return acct.address


def create_clob_client(
    key: Optional[str] = None,
    funder: Optional[str] = None,
) -> ClobClient:
    """
    Build a CLOBClient, loading private_key and funder from the polymarket
    CLI config if not provided via env / arguments.

    Matches the auth used by the polymarket CLI (signature_type=proxy).
    """
    # Load from CLI config as base
    cli_cfg = _load_cli_config()

    pk = key or cli_cfg.get("private_key")
    if not pk:
        raise ValueError("No private key found. Set PK env var or run polymarket setup.")

    chain_id = int(cli_cfg.get("chain_id", config.CHAIN_ID))
    sig_type_str = cli_cfg.get("signature_type", "proxy")

    # signature_type mapping: CLI uses string names, Python uses int
    sig_type_map = {"eoa": 0, "proxy": 1, "gnosis-safe": 2}
    sig_type = sig_type_map.get(sig_type_str, config.SIGNATURE_TYPE)

    # Funder: use provided address, then config, then derive from private key
    if funder:
        funder_addr = funder
    elif sig_type == 0:
        # EOA: funder is the key owner itself
        funder_addr = _derive_funder_address(pk)
    else:
        # Proxy / gnosis-safe: funder SHOULD be the proxy contract address.
        # Check if CLI config stores it explicitly.
        proxy_addr = cli_cfg.get("proxy_address") or cli_cfg.get("funder")
        if proxy_addr:
            funder_addr = proxy_addr
        else:
            # Fall back to EOA address — this works if py-clob-client or the
            # CLOB server maps EOA → proxy automatically, but may fail otherwise.
            funder_addr = _derive_funder_address(pk)
            sys.stderr.write(
                "Warning: signature_type=proxy but no 'proxy_address' in config. "
                "Using EOA as funder. If orders fail, add 'proxy_address' to "
                "~/.config/polymarket/config.json\n"
            )

    client = ClobClient(
        host=config.CLOB_HOST,
        key=pk,
        chain_id=chain_id,
        signature_type=sig_type,
        funder=funder_addr,
    )

    # Set API credentials (required for L2 auth on trading endpoints)
    try:
        client.set_api_creds(client.create_or_derive_api_creds())
    except Exception as e:
        sys.stderr.write(f"Warning: could not set API credentials: {e}\n")

    return client
