#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
weatherbet_fixed_paper.py — Weather Trading Bot for Polymarket
=====================================================
Tracks weather forecasts from 3 sources (ECMWF, HRRR, METAR),
compares with Polymarket markets, paper trades using Kelly criterion.

FIXED VERSION NOTES:
- Uses Gamma clobTokenIds + CLOB orderbook/Gamma bestBid-bestAsk for YES bid/ask.
- Does NOT submit live orders. This is still paper-trading only.
- Fixes calibration, actual temperature persistence, bucket probability, and double-close safety.

Usage:
    python weatherbet.py          # main loop
    python weatherbet.py report   # full report
    python weatherbet.py status   # balance and open positions
"""

import os
import re
import sys
import json
import math
import time
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# =============================================================================
# CONFIG
# =============================================================================

def load_config(path="config.json"):
    """Load config without crashing on first import. Secrets can come from env vars."""
    p = Path(path)
    if not p.exists():
        return {}
    with p.open(encoding="utf-8") as f:
        return json.load(f)

_cfg = load_config()

BALANCE          = float(_cfg.get("balance", 10000.0))
MAX_BET          = float(_cfg.get("max_bet", 20.0))        # max paper bet per trade
MIN_EV           = float(_cfg.get("min_ev", 0.10))
MAX_PRICE        = float(_cfg.get("max_price", 0.45))
MIN_VOLUME       = float(_cfg.get("min_volume", 500))
MIN_HOURS        = float(_cfg.get("min_hours", 2.0))
MAX_HOURS        = float(_cfg.get("max_hours", 72.0))
KELLY_FRACTION   = float(_cfg.get("kelly_fraction", 0.25))
MAX_SLIPPAGE     = float(_cfg.get("max_slippage", 0.03))  # max allowed ask-bid spread
SCAN_INTERVAL    = int(_cfg.get("scan_interval", 3600))   # every hour
CALIBRATION_MIN  = int(_cfg.get("calibration_min", 30))
VC_KEY           = os.getenv("VISUAL_CROSSING_KEY", _cfg.get("vc_key", ""))
USE_CLOB_QUOTES  = bool(_cfg.get("use_clob_quotes", True))
CLOB_BASE_URL    = _cfg.get("clob_base_url", "https://clob.polymarket.com")
GAMMA_BASE_URL   = _cfg.get("gamma_base_url", "https://gamma-api.polymarket.com")

# This fixed version is intentionally paper-only. Do not flip it to live without
# a separate py-clob-client-v2 execution layer and pUSD funding checks.
LIVE_TRADING_ENABLED = False

SIGMA_F = 2.0
SIGMA_C = 1.2

DATA_DIR         = Path("data")
DATA_DIR.mkdir(exist_ok=True)
STATE_FILE       = DATA_DIR / "state.json"
MARKETS_DIR      = DATA_DIR / "markets"
MARKETS_DIR.mkdir(exist_ok=True)
CALIBRATION_FILE = DATA_DIR / "calibration.json"

LOCATIONS = {
    "nyc":          {"lat": 40.7772,  "lon":  -73.8726, "name": "New York City", "station": "KLGA", "unit": "F", "region": "us"},
    "chicago":      {"lat": 41.9742,  "lon":  -87.9073, "name": "Chicago",       "station": "KORD", "unit": "F", "region": "us"},
    "miami":        {"lat": 25.7959,  "lon":  -80.2870, "name": "Miami",         "station": "KMIA", "unit": "F", "region": "us"},
    "dallas":       {"lat": 32.8471,  "lon":  -96.8518, "name": "Dallas",        "station": "KDAL", "unit": "F", "region": "us"},
    "seattle":      {"lat": 47.4502,  "lon": -122.3088, "name": "Seattle",       "station": "KSEA", "unit": "F", "region": "us"},
    "atlanta":      {"lat": 33.6407,  "lon":  -84.4277, "name": "Atlanta",       "station": "KATL", "unit": "F", "region": "us"},
    "london":       {"lat": 51.5048,  "lon":    0.0495, "name": "London",        "station": "EGLC", "unit": "C", "region": "eu"},
    "paris":        {"lat": 48.9962,  "lon":    2.5979, "name": "Paris",         "station": "LFPG", "unit": "C", "region": "eu"},
    "munich":       {"lat": 48.3537,  "lon":   11.7750, "name": "Munich",        "station": "EDDM", "unit": "C", "region": "eu"},
    "ankara":       {"lat": 40.1281,  "lon":   32.9951, "name": "Ankara",        "station": "LTAC", "unit": "C", "region": "eu"},
    "seoul":        {"lat": 37.4691,  "lon":  126.4505, "name": "Seoul",         "station": "RKSI", "unit": "C", "region": "asia"},
    "tokyo":        {"lat": 35.7647,  "lon":  140.3864, "name": "Tokyo",         "station": "RJTT", "unit": "C", "region": "asia"},
    "shanghai":     {"lat": 31.1443,  "lon":  121.8083, "name": "Shanghai",      "station": "ZSPD", "unit": "C", "region": "asia"},
    "singapore":    {"lat":  1.3502,  "lon":  103.9940, "name": "Singapore",     "station": "WSSS", "unit": "C", "region": "asia"},
    "lucknow":      {"lat": 26.7606,  "lon":   80.8893, "name": "Lucknow",       "station": "VILK", "unit": "C", "region": "asia"},
    "tel-aviv":     {"lat": 32.0114,  "lon":   34.8867, "name": "Tel Aviv",      "station": "LLBG", "unit": "C", "region": "asia"},
    "toronto":      {"lat": 43.6772,  "lon":  -79.6306, "name": "Toronto",       "station": "CYYZ", "unit": "C", "region": "ca"},
    "sao-paulo":    {"lat": -23.4356, "lon":  -46.4731, "name": "Sao Paulo",     "station": "SBGR", "unit": "C", "region": "sa"},
    "buenos-aires": {"lat": -34.8222, "lon":  -58.5358, "name": "Buenos Aires",  "station": "SAEZ", "unit": "C", "region": "sa"},
    "wellington":   {"lat": -41.3272, "lon":  174.8052, "name": "Wellington",    "station": "NZWN", "unit": "C", "region": "oc"},
}

TIMEZONES = {
    "nyc": "America/New_York", "chicago": "America/Chicago",
    "miami": "America/New_York", "dallas": "America/Chicago",
    "seattle": "America/Los_Angeles", "atlanta": "America/New_York",
    "london": "Europe/London", "paris": "Europe/Paris",
    "munich": "Europe/Berlin", "ankara": "Europe/Istanbul",
    "seoul": "Asia/Seoul", "tokyo": "Asia/Tokyo",
    "shanghai": "Asia/Shanghai", "singapore": "Asia/Singapore",
    "lucknow": "Asia/Kolkata", "tel-aviv": "Asia/Jerusalem",
    "toronto": "America/Toronto", "sao-paulo": "America/Sao_Paulo",
    "buenos-aires": "America/Argentina/Buenos_Aires", "wellington": "Pacific/Auckland",
}

MONTHS = ["january","february","march","april","may","june",
          "july","august","september","october","november","december"]

# =============================================================================
# MATH
# =============================================================================

def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def bucket_prob(forecast, t_low, t_high, sigma=None):
    """Probability that max temp falls inside the market bucket.

    The original code returned 1.0 for any non-edge bucket that matched the
    point forecast. That made EV/Kelly unrealistically large. This version uses
    a normal error distribution for every bucket.
    """
    s = float(sigma or 2.0)
    f = float(forecast)

    # Exact buckets like "be 72F" usually mean rounded observed max temp.
    # Use a +/-0.5 continuity correction around the integer bucket.
    if t_low == t_high:
        low = float(t_low) - 0.5
        high = float(t_high) + 0.5
    else:
        low = float(t_low)
        high = float(t_high)

    if low <= -999:
        return round(norm_cdf((high - f) / s), 6)
    if high >= 999:
        return round(1.0 - norm_cdf((low - f) / s), 6)
    return round(max(0.0, norm_cdf((high - f) / s) - norm_cdf((low - f) / s)), 6)

def calc_ev(p, price):
    if price <= 0 or price >= 1: return 0.0
    return round(p * (1.0 / price - 1.0) - (1.0 - p), 4)

def calc_kelly(p, price):
    if price <= 0 or price >= 1: return 0.0
    b = 1.0 / price - 1.0
    f = (p * b - (1.0 - p)) / b
    return round(min(max(0.0, f) * KELLY_FRACTION, 1.0), 4)

def bet_size(kelly, balance):
    raw = kelly * balance
    return round(min(raw, MAX_BET), 2)

# =============================================================================
# CALIBRATION
# =============================================================================

_cal: dict = {}

def load_cal():
    if CALIBRATION_FILE.exists():
        return json.loads(CALIBRATION_FILE.read_text(encoding="utf-8"))
    return {}

def get_sigma(city_slug, source="ecmwf"):
    key = f"{city_slug}_{source}"
    if key in _cal:
        return _cal[key]["sigma"]
    return SIGMA_F if LOCATIONS[city_slug]["unit"] == "F" else SIGMA_C

def run_calibration(markets):
    """Recalculate sigma from resolved markets with known actual max temp."""
    resolved = [
        m for m in markets
        if m.get("status") == "resolved" and m.get("actual_temp") is not None
    ]
    cal = load_cal()
    updated = []

    for source in ["ecmwf", "hrrr", "metar"]:
        for city in sorted(set(m["city"] for m in resolved)):
            group = [m for m in resolved if m["city"] == city]
            errors = []
            for m in group:
                # Use the latest snapshot that has that source. Snapshot schema is
                # {ecmwf, hrrr, metar, best, best_source}; there is no source/temp pair.
                snap = next((
                    s for s in reversed(m.get("forecast_snapshots", []))
                    if s.get(source) is not None
                ), None)
                if snap is not None:
                    errors.append(abs(float(snap[source]) - float(m["actual_temp"])))

            if len(errors) < CALIBRATION_MIN:
                continue

            mae = sum(errors) / len(errors)
            key = f"{city}_{source}"
            old = cal.get(key, {}).get(
                "sigma",
                SIGMA_F if LOCATIONS[city]["unit"] == "F" else SIGMA_C,
            )
            # Prevent sigma from becoming unrealistically tiny after a small sample.
            floor = 0.75 if LOCATIONS[city]["unit"] == "F" else 0.4
            new = round(max(floor, mae), 3)
            cal[key] = {
                "sigma": new,
                "mae": round(mae, 3),
                "n": len(errors),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            if abs(new - old) > 0.05:
                updated.append(f"{LOCATIONS[city]['name']} {source}: {old:.2f}->{new:.2f}")

    atomic_write_json(CALIBRATION_FILE, cal)
    if updated:
        print(f"  [CAL] {', '.join(updated)}")
    return cal

# =============================================================================
# FORECASTS
# =============================================================================

def get_ecmwf(city_slug, dates):
    """ECMWF via Open-Meteo with bias correction. For all cities."""
    loc = LOCATIONS[city_slug]
    unit = loc["unit"]
    temp_unit = "fahrenheit" if unit == "F" else "celsius"
    result = {}
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={loc['lat']}&longitude={loc['lon']}"
        f"&daily=temperature_2m_max&temperature_unit={temp_unit}"
        f"&forecast_days=7&timezone={TIMEZONES.get(city_slug, 'UTC')}"
        f"&models=ecmwf_ifs025&bias_correction=true"
    )
    for attempt in range(3):
        try:
            data = requests.get(url, timeout=(5, 10)).json()
            if "error" not in data:
                for date, temp in zip(data["daily"]["time"], data["daily"]["temperature_2m_max"]):
                    if date in dates and temp is not None:
                        result[date] = round(temp, 1) if unit == "C" else round(temp)
            break
        except Exception as e:
            if attempt < 2:
                time.sleep(3)
            else:
                print(f"  [ECMWF] {city_slug}: {e}")
    return result

def get_hrrr(city_slug, dates):
    """HRRR via Open-Meteo. US cities only, up to 48h horizon."""
    loc = LOCATIONS[city_slug]
    if loc["region"] != "us":
        return {}
    result = {}
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={loc['lat']}&longitude={loc['lon']}"
        f"&daily=temperature_2m_max&temperature_unit=fahrenheit"
        f"&forecast_days=3&timezone={TIMEZONES.get(city_slug, 'UTC')}"
        f"&models=gfs_seamless"  # HRRR+GFS seamless — best option for US
    )
    for attempt in range(3):
        try:
            data = requests.get(url, timeout=(5, 10)).json()
            if "error" not in data:
                for date, temp in zip(data["daily"]["time"], data["daily"]["temperature_2m_max"]):
                    if date in dates and temp is not None:
                        result[date] = round(temp)
            break
        except Exception as e:
            if attempt < 2:
                time.sleep(3)
            else:
                print(f"  [HRRR] {city_slug}: {e}")
    return result

def get_metar(city_slug):
    """Current observed temperature from METAR station. D+0 only."""
    loc = LOCATIONS[city_slug]
    station = loc["station"]
    unit = loc["unit"]
    try:
        url = f"https://aviationweather.gov/api/data/metar?ids={station}&format=json"
        data = requests.get(url, timeout=(5, 8)).json()
        if data and isinstance(data, list):
            temp_c = data[0].get("temp")
            if temp_c is not None:
                if unit == "F":
                    return round(float(temp_c) * 9/5 + 32)
                return round(float(temp_c), 1)
    except Exception as e:
        print(f"  [METAR] {city_slug}: {e}")
    return None

def get_actual_temp(city_slug, date_str):
    """Actual temperature via Visual Crossing for closed markets."""
    loc = LOCATIONS[city_slug]
    station = loc["station"]
    unit = loc["unit"]
    vc_unit = "us" if unit == "F" else "metric"
    url = (
        f"https://weather.visualcrossing.com/VisualCrossingWebServices/rest/services/timeline"
        f"/{station}/{date_str}/{date_str}"
        f"?unitGroup={vc_unit}&key={VC_KEY}&include=days&elements=tempmax"
    )
    try:
        data = requests.get(url, timeout=(5, 8)).json()
        days = data.get("days", [])
        if days and days[0].get("tempmax") is not None:
            return round(float(days[0]["tempmax"]), 1)
    except Exception as e:
        print(f"  [VC] {city_slug} {date_str}: {e}")
    return None

def check_market_resolved(market_id):
    """
    Checks if the market closed on Polymarket and who won.
    Returns: None (still open), True (YES won), False (NO won)
    """
    try:
        r = requests.get(f"{GAMMA_BASE_URL}/markets/{market_id}", timeout=(5, 8))
        data = r.json()
        closed = data.get("closed", False)
        if not closed:
            return None
        # Check YES price — if ~1.0 then WIN, if ~0.0 then LOSS
        prices = json.loads(data.get("outcomePrices", "[0.5,0.5]"))
        yes_price = float(prices[0])
        if yes_price >= 0.95:
            return True   # WIN
        elif yes_price <= 0.05:
            return False  # LOSS
        return None  # not yet determined
    except Exception as e:
        print(f"  [RESOLVE] {market_id}: {e}")
    return None

# =============================================================================
# POLYMARKET
# =============================================================================

def get_polymarket_event(city_slug, month, day, year):
    slug = f"highest-temperature-in-{city_slug}-on-{month}-{day}-{year}"
    try:
        r = requests.get(f"{GAMMA_BASE_URL}/events?slug={slug}", timeout=(5, 8))
        data = r.json()
        if data and isinstance(data, list) and len(data) > 0:
            return data[0]
    except Exception:
        pass
    return None

def get_market_price(market_id):
    try:
        r = requests.get(f"{GAMMA_BASE_URL}/markets/{market_id}", timeout=(3, 5))
        prices = json.loads(r.json().get("outcomePrices", "[0.5,0.5]"))
        return float(prices[0])
    except Exception:
        return None

def parse_temp_range(question):
    if not question: return None
    num = r'(-?\d+(?:\.\d+)?)'
    if re.search(r'or below', question, re.IGNORECASE):
        m = re.search(num + r'[°]?[FC] or below', question, re.IGNORECASE)
        if m: return (-999.0, float(m.group(1)))
    if re.search(r'or higher', question, re.IGNORECASE):
        m = re.search(num + r'[°]?[FC] or higher', question, re.IGNORECASE)
        if m: return (float(m.group(1)), 999.0)
    m = re.search(r'between ' + num + r'-' + num + r'[°]?[FC]', question, re.IGNORECASE)
    if m: return (float(m.group(1)), float(m.group(2)))
    m = re.search(r'be ' + num + r'[°]?[FC] on', question, re.IGNORECASE)
    if m:
        v = float(m.group(1))
        return (v, v)
    return None

def hours_to_resolution(end_date_str):
    try:
        end = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        return max(0.0, (end - datetime.now(timezone.utc)).total_seconds() / 3600)
    except Exception:
        return 999.0

def in_bucket(forecast, t_low, t_high):
    if t_low == t_high:
        return round(float(forecast)) == round(t_low)
    return t_low <= float(forecast) <= t_high


def parse_json_maybe(value, default=None):
    """Gamma fields are sometimes JSON strings and sometimes already lists."""
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


def safe_float(value, default=None):
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def get_yes_token_id(market):
    """Return the CLOB token ID for YES outcome from a Gamma market object."""
    token_ids = parse_json_maybe(market.get("clobTokenIds"), [])
    outcomes = parse_json_maybe(market.get("outcomes"), [])
    if not token_ids:
        return None

    # Prefer explicit outcome label mapping if available.
    if isinstance(outcomes, list) and len(outcomes) == len(token_ids):
        for i, name in enumerate(outcomes):
            if str(name).strip().lower() == "yes":
                return str(token_ids[i])

    # Polymarket binary markets conventionally expose YES first, NO second.
    return str(token_ids[0])


def get_gamma_quote_from_market(market):
    """Fallback quote from Gamma bestBid/bestAsk fields, not outcomePrices."""
    bid = safe_float(market.get("bestBid"))
    ask = safe_float(market.get("bestAsk"))
    if bid is None or ask is None:
        return None
    if bid <= 0 or ask <= 0 or bid >= 1 or ask >= 1 or ask < bid:
        return None
    return {
        "bid": round(bid, 4),
        "ask": round(ask, 4),
        "spread": round(ask - bid, 4),
        "source": "gamma_best_bid_ask",
    }


def get_clob_yes_quote(yes_token_id):
    """Fetch YES bid/ask from CLOB orderbook by token ID."""
    if not yes_token_id or not USE_CLOB_QUOTES:
        return None
    try:
        r = requests.get(
            f"{CLOB_BASE_URL}/book",
            params={"token_id": str(yes_token_id)},
            timeout=(3, 5),
        )
        r.raise_for_status()
        data = r.json()
        bids = data.get("bids") or []
        asks = data.get("asks") or []
        if not bids or not asks:
            return None

        bid_prices = [safe_float(x.get("price")) for x in bids if safe_float(x.get("price")) is not None]
        ask_prices = [safe_float(x.get("price")) for x in asks if safe_float(x.get("price")) is not None]
        if not bid_prices or not ask_prices:
            return None

        best_bid = max(bid_prices)
        best_ask = min(ask_prices)
        if best_bid <= 0 or best_ask <= 0 or best_bid >= 1 or best_ask >= 1 or best_ask < best_bid:
            return None
        return {
            "bid": round(best_bid, 4),
            "ask": round(best_ask, 4),
            "spread": round(best_ask - best_bid, 4),
            "source": "clob_orderbook",
        }
    except Exception:
        return None


def get_yes_quote(market):
    """Get executable-ish YES quote. CLOB first, Gamma bestBid/bestAsk fallback."""
    yes_token_id = get_yes_token_id(market)
    quote = get_clob_yes_quote(yes_token_id)
    if quote is None:
        quote = get_gamma_quote_from_market(market)
    if quote is not None:
        quote["yes_token_id"] = yes_token_id
    return quote


def get_city_dates(city_slug, days=4):
    """Return local market dates for the city, not UTC dates."""
    tz = ZoneInfo(TIMEZONES.get(city_slug, "UTC"))
    local_now = datetime.now(tz)
    return [(local_now + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]

# =============================================================================
# MARKET DATA STORAGE
# Each market is stored in a separate file: data/markets/{city}_{date}.json
# =============================================================================

def atomic_write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)

def market_path(city_slug, date_str):
    return MARKETS_DIR / f"{city_slug}_{date_str}.json"

def load_market(city_slug, date_str):
    p = market_path(city_slug, date_str)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return None

def save_market(market):
    p = market_path(market["city"], market["date"])
    atomic_write_json(p, market)

def load_all_markets():
    markets = []
    for f in MARKETS_DIR.glob("*.json"):
        try:
            markets.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            pass
    return markets

def new_market(city_slug, date_str, event, hours):
    loc = LOCATIONS[city_slug]
    return {
        "city":               city_slug,
        "city_name":          loc["name"],
        "date":               date_str,
        "unit":               loc["unit"],
        "station":            loc["station"],
        "event_end_date":     event.get("endDate", ""),
        "hours_at_discovery": round(hours, 1),
        "status":             "open",           # open | closed | resolved
        "position":           None,             # filled when position opens
        "actual_temp":        None,             # filled after resolution
        "resolved_outcome":   None,             # win / loss / no_position
        "pnl":                None,
        "forecast_snapshots": [],               # list of forecast snapshots
        "market_snapshots":   [],               # list of market price snapshots
        "all_outcomes":       [],               # all market buckets
        "created_at":         datetime.now(timezone.utc).isoformat(),
    }

# =============================================================================
# STATE (balance and open positions)
# =============================================================================

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {
        "balance":          BALANCE,
        "starting_balance": BALANCE,
        "total_trades":     0,
        "wins":             0,
        "losses":           0,
        "peak_balance":     BALANCE,
    }

def save_state(state):
    atomic_write_json(STATE_FILE, state)

# =============================================================================
# CORE LOGIC
# =============================================================================

def take_forecast_snapshot(city_slug, dates):
    """Fetches forecasts from all sources and returns a snapshot."""
    now_str = datetime.now(timezone.utc).isoformat()
    ecmwf   = get_ecmwf(city_slug, dates)
    hrrr    = get_hrrr(city_slug, dates)
    today   = datetime.now(ZoneInfo(TIMEZONES.get(city_slug, "UTC"))).strftime("%Y-%m-%d")

    snapshots = {}
    for date in dates:
        snap = {
            "ts":    now_str,
            "ecmwf": ecmwf.get(date),
            "hrrr":  hrrr.get(date) if date <= (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%d") else None,
            "metar": get_metar(city_slug) if date == today else None,
        }
        # Best forecast: HRRR for US D+0/D+1, otherwise ECMWF
        loc = LOCATIONS[city_slug]
        if loc["region"] == "us" and snap["hrrr"] is not None:
            snap["best"] = snap["hrrr"]
            snap["best_source"] = "hrrr"
        elif snap["ecmwf"] is not None:
            snap["best"] = snap["ecmwf"]
            snap["best_source"] = "ecmwf"
        else:
            snap["best"] = None
            snap["best_source"] = None
        snapshots[date] = snap
    return snapshots

def scan_and_update():
    """One cycle: update forecasts, paper-open/close positions, resolve markets."""
    global _cal
    now      = datetime.now(timezone.utc)
    state    = load_state()
    balance  = float(state["balance"])
    new_pos  = 0
    closed   = 0
    resolved = 0

    for city_slug, loc in LOCATIONS.items():
        unit = loc["unit"]
        unit_sym = "F" if unit == "F" else "C"
        print(f"  -> {loc['name']}...", end=" ", flush=True)

        try:
            dates = get_city_dates(city_slug, days=4)
            snapshots = take_forecast_snapshot(city_slug, dates)
            time.sleep(0.3)
        except Exception as e:
            print(f"skipped ({e})")
            continue

        for i, date in enumerate(dates):
            dt    = datetime.strptime(date, "%Y-%m-%d")
            event = get_polymarket_event(city_slug, MONTHS[dt.month - 1], dt.day, dt.year)
            if not event:
                continue

            end_date = event.get("endDate", "")
            hours    = hours_to_resolution(end_date) if end_date else 0
            horizon  = f"D+{i}"

            mkt = load_market(city_slug, date)
            if mkt is None:
                if hours < MIN_HOURS or hours > MAX_HOURS:
                    continue
                mkt = new_market(city_slug, date, event, hours)

            if mkt.get("status") == "resolved":
                continue

            # Update outcomes with executable-ish YES bid/ask.
            # Important: do NOT use outcomePrices as bid/ask; it is YES/NO display pricing.
            outcomes = []
            for market in event.get("markets", []):
                question = market.get("question", "")
                gamma_market_id = str(market.get("id", ""))
                volume = safe_float(market.get("volume"), 0.0) or 0.0
                rng = parse_temp_range(question)
                if not rng:
                    continue

                quote = get_yes_quote(market)
                if quote is None:
                    continue

                bid = quote["bid"]
                ask = quote["ask"]
                spread = quote["spread"]
                yes_token_id = quote.get("yes_token_id")

                outcomes.append({
                    "question": question,
                    "market_id": gamma_market_id,      # Gamma market ID, useful for metadata/resolution
                    "gamma_market_id": gamma_market_id,
                    "yes_token_id": yes_token_id,      # CLOB token ID, required for trading/orderbook
                    "range": rng,
                    "bid": round(bid, 4),              # sell YES here
                    "ask": round(ask, 4),              # buy YES here
                    "price": round(bid, 4),            # compatibility: current liquidation price
                    "spread": round(spread, 4),
                    "volume": round(volume, 0),
                    "quote_source": quote.get("source"),
                })

            outcomes.sort(key=lambda x: x["range"][0])
            mkt["all_outcomes"] = outcomes

            snap = snapshots.get(date, {})
            forecast_snap = {
                "ts":          snap.get("ts"),
                "horizon":     horizon,
                "hours_left":  round(hours, 1),
                "ecmwf":       snap.get("ecmwf"),
                "hrrr":        snap.get("hrrr"),
                "metar":       snap.get("metar"),
                "best":        snap.get("best"),
                "best_source": snap.get("best_source"),
            }
            mkt["forecast_snapshots"].append(forecast_snap)

            top = max(outcomes, key=lambda x: x["bid"]) if outcomes else None
            market_snap = {
                "ts":       snap.get("ts"),
                "top_bucket": f"{top['range'][0]}-{top['range'][1]}{unit_sym}" if top else None,
                "top_bid":   top["bid"] if top else None,
                "top_ask":   top["ask"] if top else None,
                "top_price": top["bid"] if top else None,
            }
            mkt["market_snapshots"].append(market_snap)

            forecast_temp = snap.get("best")
            best_source   = snap.get("best_source")

            # --- STOP-LOSS AND TRAILING STOP ---
            if mkt.get("position") and mkt["position"].get("status") == "open":
                pos = mkt["position"]
                current_price = None
                for outcome in outcomes:
                    if outcome.get("yes_token_id") == pos.get("yes_token_id") or outcome["market_id"] == pos["market_id"]:
                        current_price = outcome.get("bid", outcome["price"])
                        break

                if current_price is not None:
                    entry = pos["entry_price"]
                    stop  = pos.get("stop_price", entry * 0.80)

                    if current_price >= entry * 1.20 and stop < entry:
                        pos["stop_price"] = entry
                        pos["trailing_activated"] = True

                    if current_price <= stop:
                        pnl = round((current_price - entry) * pos["shares"], 2)
                        balance += pos["cost"] + pnl
                        pos["closed_at"]    = snap.get("ts")
                        pos["close_reason"] = "stop_loss" if current_price < entry else "trailing_stop"
                        pos["exit_price"]   = current_price
                        pos["pnl"]          = pnl
                        pos["status"]       = "closed"
                        closed += 1
                        reason = "STOP" if current_price < entry else "TRAILING BE"
                        print(f"  [{reason}] {loc['name']} {date} | entry ${entry:.3f} exit ${current_price:.3f} | PnL: {'+'if pnl>=0 else ''}{pnl:.2f}")

            # --- CLOSE POSITION if forecast shifted meaningfully ---
            if (
                mkt.get("position")
                and mkt["position"].get("status") == "open"
                and forecast_temp is not None
            ):
                pos = mkt["position"]
                old_bucket_low  = pos["bucket_low"]
                old_bucket_high = pos["bucket_high"]
                buffer = 2.0 if unit == "F" else 1.0

                if old_bucket_low <= -999 or old_bucket_high >= 999:
                    forecast_far = not in_bucket(forecast_temp, old_bucket_low, old_bucket_high)
                else:
                    mid_bucket = (old_bucket_low + old_bucket_high) / 2
                    half_width = abs(old_bucket_high - old_bucket_low) / 2
                    forecast_far = abs(float(forecast_temp) - mid_bucket) > (half_width + buffer)

                if not in_bucket(forecast_temp, old_bucket_low, old_bucket_high) and forecast_far:
                    current_price = None
                    for outcome in outcomes:
                        if outcome.get("yes_token_id") == pos.get("yes_token_id") or outcome["market_id"] == pos["market_id"]:
                            current_price = outcome.get("bid", outcome["price"])
                            break
                    if current_price is not None:
                        pnl = round((current_price - pos["entry_price"]) * pos["shares"], 2)
                        balance += pos["cost"] + pnl
                        pos["closed_at"]    = snap.get("ts")
                        pos["close_reason"] = "forecast_changed"
                        pos["exit_price"]   = current_price
                        pos["pnl"]          = pnl
                        pos["status"]       = "closed"
                        closed += 1
                        print(f"  [CLOSE] {loc['name']} {date} — forecast changed | PnL: {'+'if pnl>=0 else ''}{pnl:.2f}")

            # --- OPEN PAPER POSITION ---
            if not mkt.get("position") and forecast_temp is not None and hours >= MIN_HOURS:
                sigma = get_sigma(city_slug, best_source or "ecmwf")
                best_signal = None

                matched_bucket = None
                for outcome in outcomes:
                    t_low, t_high = outcome["range"]
                    if in_bucket(forecast_temp, t_low, t_high):
                        matched_bucket = outcome
                        break

                if matched_bucket:
                    outcome = matched_bucket
                    t_low, t_high = outcome["range"]
                    volume = outcome["volume"]
                    bid    = outcome["bid"]
                    ask    = outcome["ask"]
                    spread = outcome["spread"]

                    if volume >= MIN_VOLUME:
                        p  = bucket_prob(forecast_temp, t_low, t_high, sigma)
                        ev = calc_ev(p, ask)
                        if ev >= MIN_EV:
                            kelly = calc_kelly(p, ask)
                            size  = bet_size(kelly, balance)
                            if size >= 0.50 and ask > 0:
                                best_signal = {
                                    "market_id":     outcome["market_id"],
                                    "gamma_market_id": outcome["gamma_market_id"],
                                    "yes_token_id":  outcome.get("yes_token_id"),
                                    "question":      outcome["question"],
                                    "bucket_low":    t_low,
                                    "bucket_high":   t_high,
                                    "entry_price":   ask,
                                    "bid_at_entry":  bid,
                                    "spread":        spread,
                                    "shares":        round(size / ask, 2),
                                    "cost":          size,
                                    "p":             round(p, 4),
                                    "ev":            round(ev, 4),
                                    "kelly":         round(kelly, 4),
                                    "forecast_temp": forecast_temp,
                                    "forecast_src":  best_source,
                                    "sigma":         sigma,
                                    "quote_source":  outcome.get("quote_source"),
                                    "opened_at":     snap.get("ts"),
                                    "status":        "open",
                                    "pnl":           None,
                                    "exit_price":    None,
                                    "close_reason":  None,
                                    "closed_at":     None,
                                }

                if best_signal:
                    # Re-fetch one final quote before paper entry to avoid stale Gamma event data.
                    quote = None
                    yes_token_id = best_signal.get("yes_token_id")
                    if yes_token_id:
                        quote = get_clob_yes_quote(yes_token_id)
                    if quote is None:
                        # Fallback to latest cached quote from outcomes.
                        quote = {
                            "bid": best_signal["bid_at_entry"],
                            "ask": best_signal["entry_price"],
                            "spread": best_signal["spread"],
                            "source": best_signal.get("quote_source"),
                        }

                    real_ask = quote["ask"]
                    real_bid = quote["bid"]
                    real_spread = round(real_ask - real_bid, 4)
                    if real_spread > MAX_SLIPPAGE or real_ask >= MAX_PRICE:
                        print(f"  [SKIP] {loc['name']} {date} — ask ${real_ask:.3f} spread ${real_spread:.3f}")
                    else:
                        best_signal["entry_price"]  = real_ask
                        best_signal["bid_at_entry"] = real_bid
                        best_signal["spread"]       = real_spread
                        best_signal["shares"]       = round(best_signal["cost"] / real_ask, 2)
                        best_signal["ev"]           = round(calc_ev(best_signal["p"], real_ask), 4)
                        best_signal["quote_source"] = quote.get("source", best_signal.get("quote_source"))

                        balance -= best_signal["cost"]
                        mkt["position"] = best_signal
                        state["total_trades"] = int(state.get("total_trades", 0)) + 1
                        new_pos += 1
                        bucket_label = f"{best_signal['bucket_low']}-{best_signal['bucket_high']}{unit_sym}"
                        print(f"  [PAPER BUY] {loc['name']} {horizon} {date} | {bucket_label} | "
                              f"${best_signal['entry_price']:.3f} | EV {best_signal['ev']:+.2f} | "
                              f"${best_signal['cost']:.2f} ({best_signal['forecast_src'].upper()})")

            if hours < 0.5 and mkt.get("status") == "open":
                mkt["status"] = "closed"

            save_market(mkt)
            time.sleep(0.1)

        print("ok")

    # --- AUTO-RESOLUTION ---
    for mkt in load_all_markets():
        if mkt.get("status") == "resolved":
            continue

        pos = mkt.get("position")
        if not pos or pos.get("status") != "open":
            continue

        market_id = pos.get("market_id") or pos.get("gamma_market_id")
        if not market_id:
            continue

        won = check_market_resolved(market_id)
        if won is None:
            continue

        # Persist actual temp for calibration/reporting. If VC key is absent, resolution still works.
        actual = get_actual_temp(mkt["city"], mkt["date"]) if VC_KEY else None
        if actual is not None:
            mkt["actual_temp"] = actual

        price  = pos["entry_price"]
        size   = pos["cost"]
        shares = pos["shares"]
        pnl    = round(shares * (1 - price), 2) if won else round(-size, 2)

        balance += size + pnl
        pos["exit_price"]   = 1.0 if won else 0.0
        pos["pnl"]          = pnl
        pos["close_reason"] = "resolved"
        pos["closed_at"]    = now.isoformat()
        pos["status"]       = "closed"
        mkt["pnl"]          = pnl
        mkt["status"]       = "resolved"
        mkt["resolved_outcome"] = "win" if won else "loss"

        if won:
            state["wins"] = int(state.get("wins", 0)) + 1
        else:
            state["losses"] = int(state.get("losses", 0)) + 1

        result = "WIN" if won else "LOSS"
        print(f"  [{result}] {mkt['city_name']} {mkt['date']} | PnL: {'+'if pnl>=0 else ''}{pnl:.2f}")
        resolved += 1

        save_market(mkt)
        time.sleep(0.3)

    state["balance"]      = round(balance, 2)
    state["peak_balance"] = max(float(state.get("peak_balance", balance)), balance)
    save_state(state)

    all_mkts = load_all_markets()
    resolved_count = len([m for m in all_mkts if m.get("status") == "resolved"])
    if resolved_count >= CALIBRATION_MIN:
        _cal = run_calibration(all_mkts)

    return new_pos, closed, resolved

# =============================================================================
# REPORT
# =============================================================================

def print_status():
    state    = load_state()
    markets  = load_all_markets()
    open_pos = [m for m in markets if m.get("position") and m["position"].get("status") == "open"]
    resolved = [m for m in markets if m["status"] == "resolved" and m.get("pnl") is not None]

    bal     = state["balance"]
    start   = state["starting_balance"]
    ret_pct = (bal - start) / start * 100
    wins    = state["wins"]
    losses  = state["losses"]
    total   = wins + losses

    print(f"\n{'='*55}")
    print(f"  WEATHERBET — STATUS")
    print(f"{'='*55}")
    print(f"  Balance:     ${bal:,.2f}  (start ${start:,.2f}, {'+'if ret_pct>=0 else ''}{ret_pct:.1f}%)")
    print(f"  Trades:      {total} | W: {wins} | L: {losses} | WR: {wins/total:.0%}" if total else "  No trades yet")
    print(f"  Open:        {len(open_pos)}")
    print(f"  Resolved:    {len(resolved)}")

    if open_pos:
        print(f"\n  Open positions:")
        total_unrealized = 0.0
        for m in open_pos:
            pos      = m["position"]
            unit_sym = "F" if m["unit"] == "F" else "C"
            label    = f"{pos['bucket_low']}-{pos['bucket_high']}{unit_sym}"

            # Current price from latest market snapshot
            current_price = pos["entry_price"]
            snaps = m.get("market_snapshots", [])
            if snaps:
                # Find our bucket price in all_outcomes
                for o in m.get("all_outcomes", []):
                    if o["market_id"] == pos["market_id"]:
                        current_price = o.get("bid", o["price"])
                        break

            unrealized = round((current_price - pos["entry_price"]) * pos["shares"], 2)
            total_unrealized += unrealized
            pnl_str = f"{'+'if unrealized>=0 else ''}{unrealized:.2f}"

            print(f"    {m['city_name']:<16} {m['date']} | {label:<14} | "
                  f"entry ${pos['entry_price']:.3f} -> ${current_price:.3f} | "
                  f"PnL: {pnl_str} | {pos['forecast_src'].upper()}")

        sign = "+" if total_unrealized >= 0 else ""
        print(f"\n  Unrealized PnL: {sign}{total_unrealized:.2f}")

    print(f"{'='*55}\n")

def print_report():
    markets  = load_all_markets()
    resolved = [m for m in markets if m["status"] == "resolved" and m.get("pnl") is not None]

    print(f"\n{'='*55}")
    print(f"  WEATHERBET — FULL REPORT")
    print(f"{'='*55}")

    if not resolved:
        print("  No resolved markets yet.")
        return

    total_pnl = sum(m["pnl"] for m in resolved)
    wins      = [m for m in resolved if m["resolved_outcome"] == "win"]
    losses    = [m for m in resolved if m["resolved_outcome"] == "loss"]

    print(f"\n  Total resolved: {len(resolved)}")
    print(f"  Wins:           {len(wins)} | Losses: {len(losses)}")
    print(f"  Win rate:       {len(wins)/len(resolved):.0%}")
    print(f"  Total PnL:      {'+'if total_pnl>=0 else ''}{total_pnl:.2f}")

    print(f"\n  By city:")
    for city in sorted(set(m["city"] for m in resolved)):
        group = [m for m in resolved if m["city"] == city]
        w     = len([m for m in group if m["resolved_outcome"] == "win"])
        pnl   = sum(m["pnl"] for m in group)
        name  = LOCATIONS[city]["name"]
        print(f"    {name:<16} {w}/{len(group)} ({w/len(group):.0%})  PnL: {'+'if pnl>=0 else ''}{pnl:.2f}")

    print(f"\n  Market details:")
    for m in sorted(resolved, key=lambda x: x["date"]):
        pos      = m.get("position", {})
        unit_sym = "F" if m["unit"] == "F" else "C"
        snaps    = m.get("forecast_snapshots", [])
        first_fc = snaps[0]["best"] if snaps else None
        last_fc  = snaps[-1]["best"] if snaps else None
        label    = f"{pos.get('bucket_low')}-{pos.get('bucket_high')}{unit_sym}" if pos else "no position"
        result   = m["resolved_outcome"].upper()
        pnl_str  = f"{'+'if m['pnl']>=0 else ''}{m['pnl']:.2f}" if m["pnl"] is not None else "-"
        fc_str   = f"forecast {first_fc}->{last_fc}{unit_sym}" if first_fc else "no forecast"
        actual   = f"actual {m['actual_temp']}{unit_sym}" if m["actual_temp"] else ""
        print(f"    {m['city_name']:<16} {m['date']} | {label:<14} | {fc_str} | {actual} | {result} {pnl_str}")

    print(f"{'='*55}\n")

# =============================================================================
# MAIN LOOP
# =============================================================================

MONITOR_INTERVAL = 600  # monitor positions every 10 minutes

def monitor_positions():
    """Quick stop/take-profit check on open paper positions without full scan."""
    markets  = load_all_markets()
    open_pos = [m for m in markets if m.get("position") and m["position"].get("status") == "open"]
    if not open_pos:
        return 0

    state   = load_state()
    balance = float(state["balance"])
    closed  = 0

    for mkt in open_pos:
        pos = mkt["position"]
        yes_token_id = pos.get("yes_token_id")
        mid = pos.get("market_id")

        current_price = None
        quote = get_clob_yes_quote(yes_token_id) if yes_token_id else None
        if quote is not None:
            current_price = quote["bid"]  # sell YES at bid

        if current_price is None:
            for outcome in mkt.get("all_outcomes", []):
                if outcome.get("yes_token_id") == yes_token_id or outcome.get("market_id") == mid:
                    current_price = outcome.get("bid", outcome.get("price"))
                    break

        if current_price is None:
            continue

        entry = pos["entry_price"]
        stop  = pos.get("stop_price", entry * 0.80)
        city_name = LOCATIONS.get(mkt["city"], {}).get("name", mkt["city"])

        end_date = mkt.get("event_end_date", "")
        hours_left = hours_to_resolution(end_date) if end_date else 999.0

        if hours_left < 24:
            take_profit = None        # hold to resolution
        elif hours_left < 48:
            take_profit = 0.85
        else:
            take_profit = 0.75

        if current_price >= entry * 1.20 and stop < entry:
            pos["stop_price"] = entry
            pos["trailing_activated"] = True
            print(f"  [TRAILING] {city_name} {mkt['date']} — stop moved to breakeven ${entry:.3f}")

        take_triggered = take_profit is not None and current_price >= take_profit
        stop_triggered = current_price <= stop

        if take_triggered or stop_triggered:
            pnl = round((current_price - entry) * pos["shares"], 2)
            balance += pos["cost"] + pnl
            pos["closed_at"] = datetime.now(timezone.utc).isoformat()
            if take_triggered:
                pos["close_reason"] = "take_profit"
                reason = "TAKE"
            elif current_price < entry:
                pos["close_reason"] = "stop_loss"
                reason = "STOP"
            else:
                pos["close_reason"] = "trailing_stop"
                reason = "TRAILING BE"
            pos["exit_price"] = current_price
            pos["pnl"] = pnl
            pos["status"] = "closed"
            closed += 1
            print(f"  [{reason}] {city_name} {mkt['date']} | entry ${entry:.3f} exit ${current_price:.3f} | {hours_left:.0f}h left | PnL: {'+'if pnl>=0 else ''}{pnl:.2f}")
            save_market(mkt)

    if closed:
        state["balance"] = round(balance, 2)
        save_state(state)

    return closed


def run_loop():
    global _cal
    _cal = load_cal()

    print(f"\n{'='*55}")
    print(f"  WEATHERBET — STARTING")
    print(f"{'='*55}")
    print(f"  Cities:     {len(LOCATIONS)}")
    print(f"  Balance:    ${BALANCE:,.0f} | Max bet: ${MAX_BET}")
    print(f"  Scan:       {SCAN_INTERVAL//60} min | Monitor: {MONITOR_INTERVAL//60} min")
    print(f"  Sources:    ECMWF + GFS-seamless(US short horizon) + METAR(D+0)")
    print(f"  Mode:       PAPER ONLY — no live orders")
    print(f"  Data:       {DATA_DIR.resolve()}")
    print(f"  Ctrl+C to stop\n")

    last_full_scan = 0

    while True:
        now_ts  = time.time()
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Full scan once per hour
        if now_ts - last_full_scan >= SCAN_INTERVAL:
            print(f"[{now_str}] full scan...")
            try:
                new_pos, closed, resolved = scan_and_update()
                state = load_state()
                print(f"  balance: ${state['balance']:,.2f} | "
                      f"new: {new_pos} | closed: {closed} | resolved: {resolved}")
                last_full_scan = time.time()
            except KeyboardInterrupt:
                print(f"\n  Stopping — saving state...")
                save_state(load_state())
                print(f"  Done. Bye!")
                break
            except requests.exceptions.ConnectionError:
                print(f"  Connection lost — waiting 60 sec")
                time.sleep(60)
                continue
            except Exception as e:
                print(f"  Error: {e} — waiting 60 sec")
                time.sleep(60)
                continue
        else:
            # Quick stop monitoring
            print(f"[{now_str}] monitoring positions...")
            try:
                stopped = monitor_positions()
                if stopped:
                    state = load_state()
                    print(f"  balance: ${state['balance']:,.2f}")
            except Exception as e:
                print(f"  Monitor error: {e}")

        try:
            time.sleep(MONITOR_INTERVAL)
        except KeyboardInterrupt:
            print(f"\n  Stopping — saving state...")
            save_state(load_state())
            print(f"  Done. Bye!")
            break

# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "run":
        run_loop()
    elif cmd == "status":
        _cal = load_cal()
        print_status()
    elif cmd == "report":
        _cal = load_cal()
        print_report()
    else:
        print("Usage: python weatherbet.py [run|status|report]")