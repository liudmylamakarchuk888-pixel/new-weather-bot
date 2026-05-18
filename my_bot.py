from py_clob_client_v2 import ClobClient
from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams
import json
import os
from env_loader import load_env


def get_polymarket_event(city_slug, month, day, year):
    slug = f"highest-temperature-in-{city_slug}-on-{month}-{day}-{year}"
    try:
        r = requests.get(f"https://gamma-api.polymarket.com/events?slug={slug}", timeout=(5, 8))
        data = r.json()
        if data and isinstance(data, list) and len(data) > 0:
            return data[0]
    except Exception:
        pass
    return None

def build_client():
    load_env()

    host = "https://clob.polymarket.com"
    chain = 137  # Polygon mainnet
    private_key = os.getenv("PRIVATE_KEY")
    funder = os.getenv("YOUR_WALLET_ADDRESS") or os.getenv("WALLET_ADDRESS")

    if not private_key:
        raise RuntimeError("PRIVATE_KEY is missing. Add it to .env or export it in your shell.")

    if not funder:
        raise RuntimeError("YOUR_WALLET_ADDRESS is missing. Add it to .env or export WALLET_ADDRESS.")

    # Derive API credentials (L1 -> L2 auth)
    temp_client = ClobClient(host, key=private_key, chain_id=chain)
    api_creds = temp_client.create_or_derive_api_key()

    return ClobClient(
        host,
        key=private_key,
        chain_id=chain,
        creds=api_creds,
        signature_type=3,  # POLY_1271 / deposit wallet
        funder=funder,
    )


def get_balance_allowance(client, asset_type=AssetType.COLLATERAL, token_id=None):
    params = BalanceAllowanceParams(asset_type=asset_type, token_id=token_id)
    return client.get_balance_allowance(params=params)


if __name__ == "__main__":
    client = build_client()
    balance_allowance = get_balance_allowance(client)
    print(json.dumps(balance_allowance, indent=2))
