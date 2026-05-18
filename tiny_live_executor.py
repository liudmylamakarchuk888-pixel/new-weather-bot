#!/usr/bin/env python3
"""Tiny explicit Polymarket live order executor.

Default mode is a connectivity/balance check. To submit an order, pass all
order details and --yes-live.
"""

import argparse
import json
from urllib.parse import urlparse

import requests
from py_clob_client_v2.clob_types import OrderArgs, OrderType

from my_bot import build_client, get_balance_allowance


VALID_SIDES = {"BUY", "SELL"}
VALID_ORDER_TYPES = {OrderType.GTC, OrderType.FOK, OrderType.GTD, OrderType.FAK}
GAMMA_BASE_URL = "https://gamma-api.polymarket.com"
CLOB_BASE_URL = "https://clob.polymarket.com"


def parse_args():
    parser = argparse.ArgumentParser(description="Tiny live executor for Polymarket CLOB orders.")
    parser.add_argument("--token-id", help="Conditional token ID to trade.")
    parser.add_argument("--side", choices=sorted(VALID_SIDES), help="BUY or SELL.")
    parser.add_argument("--price", type=float, help="Limit price, between 0 and 1.")
    parser.add_argument("--size", type=float, help="Share size.")
    parser.add_argument("--order-type", default=OrderType.GTC, choices=sorted(VALID_ORDER_TYPES))
    parser.add_argument("--post-only", action="store_true", help="Post-only limit order.")
    parser.add_argument("--yes-live", action="store_true", help="Actually submit the order.")
    parser.add_argument("--gamma-market-id", help="Fetch this Gamma market and print its YES token id.")
    parser.add_argument("--event-slug", help="Fetch a Gamma event slug and list its markets/tokens.")
    parser.add_argument("--event-url", help="Fetch a Polymarket event URL and list its markets/tokens.")
    return parser.parse_args()


def slug_from_event_url(url):
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2 and parts[0] == "event":
        return parts[1]
    raise SystemExit("Could not parse event slug from URL. Expected /event/<slug>.")


def parse_json_maybe(value, default=None):
    if default is None:
        default = []
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return default


def get_yes_token_id_from_gamma_market(market):
    token_ids = parse_json_maybe(market.get("clobTokenIds"), [])
    outcomes = parse_json_maybe(market.get("outcomes"), [])
    if not token_ids:
        return None

    if isinstance(outcomes, list) and len(outcomes) == len(token_ids):
        for index, name in enumerate(outcomes):
            if str(name).strip().lower() == "yes":
                return str(token_ids[index])

    return str(token_ids[0])


def fetch_gamma_market(market_id):
    response = requests.get(f"{GAMMA_BASE_URL}/markets/{market_id}", timeout=(5, 8))
    response.raise_for_status()
    return response.json()


def fetch_gamma_event(slug):
    response = requests.get(f"{GAMMA_BASE_URL}/events/slug/{slug}", timeout=(5, 8))
    response.raise_for_status()
    return response.json()


def market_summary(market):
    return {
        "gamma_market_id": str(market.get("id")),
        "question": market.get("question"),
        "active": market.get("active"),
        "closed": market.get("closed"),
        "archived": market.get("archived"),
        "acceptingOrders": market.get("acceptingOrders"),
        "enableOrderBook": market.get("enableOrderBook"),
        "bestBid": market.get("bestBid"),
        "bestAsk": market.get("bestAsk"),
        "outcomes": parse_json_maybe(market.get("outcomes"), []),
        "clobTokenIds": parse_json_maybe(market.get("clobTokenIds"), []),
        "yes_token_id": get_yes_token_id_from_gamma_market(market),
    }


def fetch_clob_book(token_id):
    response = requests.get(
        f"{CLOB_BASE_URL}/book",
        params={"token_id": str(token_id)},
        timeout=(5, 8),
    )
    return response


def ensure_clob_book_exists(token_id):
    response = fetch_clob_book(token_id)
    if response.status_code == 200:
        return response.json()

    try:
        body = response.json()
    except Exception:
        body = response.text

    raise SystemExit(
        "CLOB orderbook preflight failed. "
        f"token_id={token_id} status={response.status_code} body={body}"
    )


def validate_order(args):
    fields = [args.token_id, args.side, args.price, args.size]
    if any(value is None for value in fields):
        missing = []
        if args.token_id is None:
            missing.append("--token-id")
        if args.side is None:
            missing.append("--side")
        if args.price is None:
            missing.append("--price")
        if args.size is None:
            missing.append("--size")
        raise SystemExit(f"Missing order details: {', '.join(missing)}")
    if not 0 < args.price < 1:
        raise SystemExit("--price must be between 0 and 1.")
    if args.size <= 0:
        raise SystemExit("--size must be positive.")
    if args.post_only and args.order_type in {OrderType.FOK, OrderType.FAK}:
        raise SystemExit("--post-only is not valid with FOK/FAK.")


def main():
    args = parse_args()

    if args.event_url or args.event_slug:
        slug = args.event_slug or slug_from_event_url(args.event_url)
        event = fetch_gamma_event(slug)
        print(json.dumps({
            "event_id": str(event.get("id")),
            "slug": event.get("slug"),
            "title": event.get("title"),
            "active": event.get("active"),
            "closed": event.get("closed"),
            "archived": event.get("archived"),
            "endDate": event.get("endDate"),
            "markets": [market_summary(market) for market in event.get("markets", [])],
        }, indent=2))
        return

    if args.gamma_market_id:
        market = fetch_gamma_market(args.gamma_market_id)
        yes_token_id = get_yes_token_id_from_gamma_market(market)
        if not yes_token_id:
            raise SystemExit(f"No clobTokenIds found for Gamma market {args.gamma_market_id}.")

        summary = market_summary(market)
        summary["orderMinSize"] = market.get("orderMinSize")
        summary["orderPriceMinTickSize"] = market.get("orderPriceMinTickSize")
        print(json.dumps(summary, indent=2))
        return

    client = build_client()

    balance_allowance = get_balance_allowance(client)
    print("balance_allowance:")
    print(json.dumps(balance_allowance, indent=2))

    wants_order = any(
        value is not None
        for value in (args.token_id, args.side, args.price, args.size)
    )
    if not wants_order:
        print("No order requested. Pass --token-id --side --price --size --yes-live to submit.")
        return

    validate_order(args)
    order_args = OrderArgs(
        token_id=args.token_id,
        side=args.side,
        price=args.price,
        size=args.size,
    )

    print("order_request:")
    print(json.dumps({
        "token_id": args.token_id,
        "side": args.side,
        "price": args.price,
        "size": args.size,
        "order_type": args.order_type,
        "post_only": args.post_only,
        "notional": round(args.price * args.size, 6),
        "live": args.yes_live,
    }, indent=2))

    if not args.yes_live:
        print("Dry run only. Add --yes-live to submit.")
        return

    ensure_clob_book_exists(args.token_id)
    response = client.create_and_post_order(
        order_args,
        order_type=args.order_type,
        post_only=args.post_only,
    )
    print("order_response:")
    print(json.dumps(response, indent=2))


if __name__ == "__main__":
    main()
