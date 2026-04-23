#!/usr/bin/env python3.11
"""Probe Polymarket POST /order latency with an intentionally unfillable FAK order.

This script is designed for network-route comparison. It uses the same account
auth/signing flow as the bot, but deliberately submits a Fill-And-Kill order at
an obviously non-marketable price so it should be rejected rather than filled.

Typical usage:
    PYTHONPATH=/Users/forrestliao/workspace python3.11 tools/probe_post_order_latency.py \
      --token-id <TOKEN_ID> --side buy --price 0.01 --size 1 --repeats 5
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from typing import Any

from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL

from polybot.core.auth import create_clob_client
from polybot.core.client import get_tick_size, prefetch_order_params, get_order_options


def _extract_error(exc: Exception) -> dict[str, Any]:
    return {
        "error_type": type(exc).__name__,
        "message": str(exc),
        "status_code": getattr(exc, "status_code", None),
        "error_msg": getattr(exc, "error_msg", None),
    }


def _round_price_for_side(price: float, tick_size: float, side: str) -> float:
    if tick_size <= 0:
        tick_size = 0.01
    ticks = round(price / tick_size)
    rounded = ticks * tick_size
    rounded = max(tick_size, min(1.0 - tick_size, rounded))
    return round(rounded, 6)


def probe_once(
    token_id: str,
    side: str,
    price: float,
    size: float,
    use_cache: bool,
) -> dict[str, Any]:
    t0 = time.perf_counter()
    client = create_clob_client()
    auth_ms = round((time.perf_counter() - t0) * 1000)

    t1 = time.perf_counter()
    tick_size = get_tick_size(token_id)
    if use_cache:
        prefetch_order_params(token_id)
    options = get_order_options(token_id)
    prep_ms = round((time.perf_counter() - t1) * 1000)

    rounded_price = _round_price_for_side(price, tick_size, side)
    args = OrderArgs(
        token_id=token_id,
        price=rounded_price,
        size=size,
        side=BUY if side == "buy" else SELL,
    )

    t2 = time.perf_counter()
    signed = client.create_order(args, options=options)
    create_order_ms = round((time.perf_counter() - t2) * 1000)

    t3 = time.perf_counter()
    try:
        resp = client.post_order(signed, OrderType.FAK)
        post_order_ms = round((time.perf_counter() - t3) * 1000)
        total_ms = round((time.perf_counter() - t0) * 1000)
        return {
            "ok": True,
            "token_id": token_id,
            "side": side,
            "price": rounded_price,
            "size": size,
            "tick_size": tick_size,
            "auth_ms": auth_ms,
            "prep_ms": prep_ms,
            "create_order_ms": create_order_ms,
            "post_order_ms": post_order_ms,
            "total_ms": total_ms,
            "response": resp,
        }
    except Exception as exc:
        post_order_ms = round((time.perf_counter() - t3) * 1000)
        total_ms = round((time.perf_counter() - t0) * 1000)
        return {
            "ok": False,
            "token_id": token_id,
            "side": side,
            "price": rounded_price,
            "size": size,
            "tick_size": tick_size,
            "auth_ms": auth_ms,
            "prep_ms": prep_ms,
            "create_order_ms": create_order_ms,
            "post_order_ms": post_order_ms,
            "total_ms": total_ms,
            **_extract_error(exc),
        }


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Probe POST /order latency using an intentionally unfillable FAK order.",
    )
    ap.add_argument("--token-id", required=True, help="Polymarket token id to probe")
    ap.add_argument("--side", choices=["buy", "sell"], default="buy")
    ap.add_argument(
        "--price",
        type=float,
        default=0.01,
        help="Deliberately non-marketable limit price; default buy price 0.01",
    )
    ap.add_argument(
        "--size",
        type=float,
        default=1.0,
        help="Order size in shares for limit FAK probe",
    )
    ap.add_argument("--repeats", type=int, default=1, help="How many probes to run")
    ap.add_argument(
        "--sleep-sec",
        type=float,
        default=1.0,
        help="Sleep between probes when repeats > 1",
    )
    ap.add_argument(
        "--no-cache",
        action="store_true",
        help="Disable SDK order-parameter prefetch/cache warming",
    )
    args = ap.parse_args()

    if args.repeats <= 0:
        raise SystemExit("--repeats must be >= 1")

    if args.side == "buy" and args.price >= 0.20:
        print(
            "warning: buy probe price is not especially low; if you want a near-guaranteed reject, use --price 0.01",
            file=sys.stderr,
        )
    if args.side == "sell" and args.price <= 0.80:
        print(
            "warning: sell probe price is not especially high; if you want a near-guaranteed reject, use --price 0.99",
            file=sys.stderr,
        )

    total_latencies: list[float] = []
    post_latencies: list[float] = []

    for i in range(1, args.repeats + 1):
        result = probe_once(
            token_id=args.token_id,
            side=args.side,
            price=args.price,
            size=args.size,
            use_cache=not args.no_cache,
        )
        total_latencies.append(result["total_ms"])
        post_latencies.append(result["post_order_ms"])

        print(f"probe {i}/{args.repeats}")
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, default=str))

        if i < args.repeats and args.sleep_sec > 0:
            time.sleep(args.sleep_sec)

    if len(total_latencies) > 1:
        summary = {
            "count": len(total_latencies),
            "total_ms_min": min(total_latencies),
            "total_ms_p50": round(statistics.median(total_latencies), 1),
            "total_ms_mean": round(statistics.mean(total_latencies), 1),
            "total_ms_max": max(total_latencies),
            "post_order_ms_min": min(post_latencies),
            "post_order_ms_p50": round(statistics.median(post_latencies), 1),
            "post_order_ms_mean": round(statistics.mean(post_latencies), 1),
            "post_order_ms_max": max(post_latencies),
        }
        print("summary")
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
