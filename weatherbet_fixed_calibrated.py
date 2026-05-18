#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
weatherbet_hermes_self_learning.py — Weather Trading Bot for Polymarket
=====================================================
Tracks weather forecasts from ECMWF, a configurable US short-range model, and METAR,
compares with Polymarket markets, paper trades using Kelly criterion, and rebuilds
calibration from historical actual temperatures.

HERMES / SELF-LEARNING VERSION NOTES:
- Uses Gamma clobTokenIds + CLOB orderbook/Gamma bestBid-bestAsk for YES bid/ask.
- Paper-trading by default. Passing --live-execute submits live CLOB BUY orders.
- Rebuilds calibration after actual-temperature backfill.
- Counts closed/past historical calibration samples correctly, including no-position days.
- Removes external ECMWF bias correction to avoid double bias correction.
- Renames the fake HRRR path to configurable US short-range GFS-seamless by default.
- Reports early exits: stop-loss, trailing stop, take-profit, and forecast_changed.
- Writes data/hermes_learning.json with learning diagnostics and safe recommendations.

Usage:
    python weatherbet.py run              # paper main loop
    python weatherbet.py run --live-execute
    python weatherbet.py report           # full report, including early exits
    python weatherbet.py status           # balance and open positions
    python weatherbet.py backfill-actuals # fetch actual temps + rebuild calibration
    python weatherbet.py calibrate        # backfill + calibration summary
    python weatherbet.py learn            # full Hermes learning report
"""

import os
import re
import sys
import json
import math
import time
import requests
from collections import Counter
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
# Historical actual temperatures are used only after resolution to calibrate
# future forecasts. Never use current/future actual_temp as a live signal.
CALIBRATION_USE_BIAS = bool(_cfg.get("calibration_use_bias", True))
MAX_BIAS_F       = float(_cfg.get("max_bias_f", 4.0))   # cap learned adjustment in °F
MAX_BIAS_C       = float(_cfg.get("max_bias_c", 2.0))   # cap learned adjustment in °C
USE_CLOB_QUOTES  = bool(_cfg.get("use_clob_quotes", True))
CLOB_BASE_URL    = _cfg.get("clob_base_url", "https://clob.polymarket.com")
GAMMA_BASE_URL   = _cfg.get("gamma_base_url", "https://gamma-api.polymarket.com")

# The old script called this source "HRRR" while requesting Open-Meteo
# gfs_seamless. Keep it configurable, but do not label GFS data as HRRR.
US_SHORT_FORECAST_SOURCE = str(_cfg.get("us_short_forecast_source", "gfs")).lower()
US_SHORT_FORECAST_MODEL  = str(_cfg.get("us_short_forecast_model", "gfs_seamless"))
US_SHORT_FORECAST_LABEL  = str(_cfg.get("us_short_forecast_label", "GFS-SEAMLESS"))
US_SHORT_MAX_DAYS        = int(_cfg.get("us_short_max_days", 3))

# Hermes is intentionally conservative: it updates learned calibration and writes
# diagnostics/recommendations, but it does not mutate trading thresholds by itself.
HERMES_ENABLED     = bool(_cfg.get("hermes_enabled", True))
HERMES_MIN_TRADES  = int(_cfg.get("hermes_min_trades", 10))

# Default remains paper-only. Passing --live-execute enables live BUY order
# submission for signals that pass all existing filters.
LIVE_TRADING_ENABLED = False
LIVE_ORDER_TYPE = str(_cfg.get("live_order_type", "FOK")).upper()
_live_client = None

SIGMA_F = 2.0
SIGMA_C = 1.2

DATA_DIR         = Path("data")
DATA_DIR.mkdir(exist_ok=True)
STATE_FILE       = DATA_DIR / "state.json"
MARKETS_DIR      = DATA_DIR / "markets"
MARKETS_DIR.mkdir(exist_ok=True)
CALIBRATION_FILE = DATA_DIR / "calibration.json"
HERMES_FILE      = DATA_DIR / "hermes_learning.json"

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
    "hong-kong":    {"lat": 22.3080,  "lon":  113.9185, "name": "Hong Kong",     "station": "VHHH", "unit": "C", "region": "asia"},
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
    "hong-kong": "Asia/Hong_Kong",
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

def default_sigma(city_slug):
    return SIGMA_F if LOCATIONS[city_slug]["unit"] == "F" else SIGMA_C


def clamp(value, low, high):
    return max(low, min(high, value))


def canonical_source(source):
    """Normalize forecast-source names used by old and new market records."""
    if not source:
        return "ecmwf"
    source = str(source).lower()
    if source in {"hrrr", "gfs", "gfs_seamless", "us_short"}:
        return US_SHORT_FORECAST_SOURCE
    return source


def calibration_key(city_slug, source):
    return f"{city_slug}_{canonical_source(source)}"


def legacy_calibration_keys(city_slug, source):
    src = canonical_source(source)
    keys = [f"{city_slug}_{src}"]
    # Backward compatibility for historical calibration.json files produced
    # when gfs_seamless data was mislabeled as hrrr.
    if src == US_SHORT_FORECAST_SOURCE:
        keys.extend([f"{city_slug}_hrrr", f"{city_slug}_gfs", f"{city_slug}_gfs_seamless", f"{city_slug}_us_short"])
    seen = set()
    return [k for k in keys if not (k in seen or seen.add(k))]


def get_calibration_entry(city_slug, source="ecmwf"):
    for key in legacy_calibration_keys(city_slug, source):
        if key in _cal:
            return _cal.get(key, {})
    return {}


def source_snapshot_keys(source):
    """Return raw/adjusted snapshot key candidates for a source.

    New records use canonical keys such as gfs_raw. Old records may still have
    hrrr_raw because the previous implementation mislabeled gfs_seamless data.
    """
    src = canonical_source(source)
    raw_keys = [f"{src}_raw"]
    adjusted_keys = [src]
    if src == US_SHORT_FORECAST_SOURCE:
        raw_keys += ["us_short_raw", "hrrr_raw", "gfs_raw", "gfs_seamless_raw"]
        adjusted_keys += ["us_short", "hrrr", "gfs", "gfs_seamless"]
    seen = set()
    raw_keys = [k for k in raw_keys if not (k in seen or seen.add(k))]
    seen = set()
    adjusted_keys = [k for k in adjusted_keys if not (k in seen or seen.add(k))]
    return raw_keys, adjusted_keys


def is_past_market_day(mkt):
    city = mkt.get("city")
    date_str = mkt.get("date")
    if not city or not date_str or city not in LOCATIONS:
        return False
    try:
        city_tz = ZoneInfo(TIMEZONES.get(city, "UTC"))
        market_day = datetime.strptime(date_str, "%Y-%m-%d").date()
        return market_day < datetime.now(city_tz).date()
    except Exception:
        return False


def is_calibration_candidate(mkt):
    """A market/day can train calibration only after the day is over."""
    if mkt.get("actual_temp") is None or not mkt.get("forecast_snapshots"):
        return False
    if mkt.get("status") in {"resolved", "closed"}:
        return True
    # Handles older records that already have actual_temp but were never marked
    # closed because the bot was offline around market close.
    return is_past_market_day(mkt)


def calibration_candidates(markets):
    return [m for m in markets if is_calibration_candidate(m)]


def count_calibration_samples(markets):
    return len(calibration_candidates(markets))


def get_sigma(city_slug, source="ecmwf"):
    """Return residual forecast uncertainty after bias correction."""
    entry = get_calibration_entry(city_slug, source)
    if entry.get("n", 0) >= CALIBRATION_MIN and entry.get("sigma") is not None:
        return float(entry["sigma"])
    return default_sigma(city_slug)


def get_bias(city_slug, source="ecmwf"):
    """Return learned source/city bias: actual_temp - raw_forecast.

    Positive bias means the source has historically forecast too cold, so we
    add degrees to future forecasts. Negative bias means forecast too hot.
    """
    if not CALIBRATION_USE_BIAS:
        return 0.0
    entry = get_calibration_entry(city_slug, source)
    if entry.get("n", 0) < CALIBRATION_MIN:
        return 0.0
    raw_bias = float(entry.get("bias", 0.0))
    limit = MAX_BIAS_F if LOCATIONS[city_slug]["unit"] == "F" else MAX_BIAS_C
    return round(clamp(raw_bias, -limit, limit), 3)


def apply_calibration(city_slug, source, raw_temp):
    """Apply historical bias correction to a raw forecast value.

    This is where actual_temp influences prediction, but only through already
    resolved historical markets stored in calibration.json.
    """
    if raw_temp is None:
        return None
    adjusted = float(raw_temp) + get_bias(city_slug, source)
    if LOCATIONS[city_slug]["unit"] == "F":
        return round(adjusted)
    return round(adjusted, 1)


def run_calibration(markets):
    """Recalculate per-city/source bias and sigma from closed historical actuals.

    actual_temp is the historical ground truth. It updates:
      - bias: mean(actual_temp - raw_forecast)
      - sigma: residual stddev after applying the learned bias

    The training set includes resolved trades, early-exited/closed trades, and
    closed no-position market records. It also accepts old past-day records that
    already have actual_temp but were never marked closed.
    """
    historical = calibration_candidates(markets)
    cal = load_cal()
    updated = []
    sources = ["ecmwf", US_SHORT_FORECAST_SOURCE]

    for source in sources:
        raw_keys, adjusted_keys = source_snapshot_keys(source)
        for city in sorted(set(m.get("city") for m in historical if m.get("city") in LOCATIONS)):
            signed_errors = []  # actual - raw forecast
            for m in [x for x in historical if x.get("city") == city]:
                actual = m.get("actual_temp")
                if actual is None:
                    continue

                snap = next((
                    s for s in reversed(m.get("forecast_snapshots", []))
                    if any(s.get(k) is not None for k in raw_keys + adjusted_keys)
                ), None)
                if snap is None:
                    continue

                # Prefer raw external model output. Fall back to adjusted field
                # for old data that did not store raw values.
                forecast_value = None
                for key in raw_keys:
                    if snap.get(key) is not None:
                        forecast_value = snap.get(key)
                        break
                if forecast_value is None:
                    for key in adjusted_keys:
                        if snap.get(key) is not None:
                            forecast_value = snap.get(key)
                            break
                if forecast_value is None:
                    continue
                signed_errors.append(float(actual) - float(forecast_value))

            if len(signed_errors) < CALIBRATION_MIN:
                continue

            n = len(signed_errors)
            bias = sum(signed_errors) / n
            residuals = [e - bias for e in signed_errors]
            mse = sum(r * r for r in residuals) / n
            raw_mae = sum(abs(e) for e in signed_errors) / n
            calibrated_mae = sum(abs(r) for r in residuals) / n

            floor = 0.75 if LOCATIONS[city]["unit"] == "F" else 0.4
            sigma = round(max(floor, math.sqrt(mse)), 3)
            bias = round(bias, 3)

            key = calibration_key(city, source)
            old = get_calibration_entry(city, source)
            old_sigma = float(old.get("sigma", default_sigma(city)))
            old_bias = float(old.get("bias", 0.0))

            cal[key] = {
                "bias": bias,
                "sigma": sigma,
                "mae_raw": round(raw_mae, 3),
                "mae_calibrated": round(calibrated_mae, 3),
                "n": n,
                "source": canonical_source(source),
                "raw_keys_used": raw_keys,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            if abs(sigma - old_sigma) > 0.05 or abs(bias - old_bias) > 0.05:
                updated.append(
                    f"{LOCATIONS[city]['name']} {canonical_source(source)}: "
                    f"bias {old_bias:+.2f}->{bias:+.2f}, sigma {old_sigma:.2f}->{sigma:.2f}"
                )

    atomic_write_json(CALIBRATION_FILE, cal)
    if updated:
        print(f"  [CAL] {', '.join(updated)}")
    return cal

# =============================================================================
# FORECASTS
# =============================================================================

def get_ecmwf(city_slug, dates):
    """Raw ECMWF via Open-Meteo. Internal calibration applies bias correction."""
    loc = LOCATIONS[city_slug]
    unit = loc["unit"]
    temp_unit = "fahrenheit" if unit == "F" else "celsius"
    result = {}
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={loc['lat']}&longitude={loc['lon']}"
        f"&daily=temperature_2m_max&temperature_unit={temp_unit}"
        f"&forecast_days=7&timezone={TIMEZONES.get(city_slug, 'UTC')}"
        f"&models=ecmwf_ifs025"
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

def get_us_short_forecast(city_slug, dates):
    """Configurable US short-range model via Open-Meteo.

    Previous versions called this HRRR while requesting gfs_seamless. By default
    this now reports the source as gfs, matching the requested model. If you
    later switch config.us_short_forecast_model to a true HRRR identifier, set
    config.us_short_forecast_source to "hrrr" as well.
    """
    loc = LOCATIONS[city_slug]
    if loc["region"] != "us":
        return {}
    result = {}
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={loc['lat']}&longitude={loc['lon']}"
        f"&daily=temperature_2m_max&temperature_unit=fahrenheit"
        f"&forecast_days={US_SHORT_MAX_DAYS}&timezone={TIMEZONES.get(city_slug, 'UTC')}"
        f"&models={US_SHORT_FORECAST_MODEL}"
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
                print(f"  [{US_SHORT_FORECAST_LABEL}] {city_slug}: {e}")
    return result


def get_hrrr(city_slug, dates):
    """Backward-compatible wrapper for old imports; returns the configured US short model."""
    return get_us_short_forecast(city_slug, dates)

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


def get_live_client():
    """Build the py-clob-client-v2 client lazily, only when live trading is enabled."""
    global _live_client
    if _live_client is None:
        from my_bot import build_client
        _live_client = build_client()
    return _live_client


def clob_orderbook_exists(yes_token_id):
    """Return True only if CLOB recognizes this token id as an orderbook."""
    if not yes_token_id:
        return False
    try:
        r = requests.get(
            f"{CLOB_BASE_URL}/book",
            params={"token_id": str(yes_token_id)},
            timeout=(3, 5),
        )
        return r.status_code == 200
    except Exception:
        return False


def live_buy_yes(signal):
    """Submit a live limit BUY for the same shares/price selected by paper logic."""
    yes_token_id = signal.get("yes_token_id")
    if not yes_token_id:
        return False, "missing yes_token_id", None
    if not clob_orderbook_exists(yes_token_id):
        return False, "CLOB orderbook does not exist for yes_token_id", None

    try:
        from py_clob_client_v2.clob_types import OrderArgs, OrderType

        order_args = OrderArgs(
            token_id=str(yes_token_id),
            side="BUY",
            price=float(signal["entry_price"]),
            size=float(signal["shares"]),
        )
        order_type = getattr(OrderType, LIVE_ORDER_TYPE, OrderType.FOK)
        response = get_live_client().create_and_post_order(
            order_args,
            order_type=order_type,
            post_only=False,
        )
        if isinstance(response, dict) and response.get("success") is False:
            return False, json.dumps(response), response
        return True, None, response
    except Exception as e:
        return False, str(e), None


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
    """Fetch forecasts and apply learned historical actual-temp calibration.

    raw fields are external model values. Adjusted fields are used for bucket
    matching and EV. This prevents leakage: only already-resolved/past actuals
    influence future forecasts through calibration.json.
    """
    now_str = datetime.now(timezone.utc).isoformat()
    ecmwf = get_ecmwf(city_slug, dates)
    us_short = get_us_short_forecast(city_slug, dates)
    city_tz = ZoneInfo(TIMEZONES.get(city_slug, "UTC"))
    today = datetime.now(city_tz).strftime("%Y-%m-%d")
    us_short_cutoff = (datetime.now(city_tz) + timedelta(days=max(0, US_SHORT_MAX_DAYS - 1))).strftime("%Y-%m-%d")

    snapshots = {}
    for date in dates:
        raw_ecmwf = ecmwf.get(date)
        raw_us_short = us_short.get(date) if date <= us_short_cutoff else None
        raw_metar = get_metar(city_slug) if date == today else None

        adj_ecmwf = apply_calibration(city_slug, "ecmwf", raw_ecmwf)
        adj_us_short = apply_calibration(city_slug, US_SHORT_FORECAST_SOURCE, raw_us_short)
        # METAR is an observation, not a max-temperature forecast. Keep it raw
        # unless you later create a separate D+0 nowcast model.
        adj_metar = raw_metar

        snap = {
            "ts": now_str,
            "ecmwf_raw": raw_ecmwf,
            "ecmwf_bias": get_bias(city_slug, "ecmwf"),
            "ecmwf": adj_ecmwf,
            "us_short_source": US_SHORT_FORECAST_SOURCE,
            "us_short_model": US_SHORT_FORECAST_MODEL,
            "us_short_raw": raw_us_short,
            "us_short_bias": get_bias(city_slug, US_SHORT_FORECAST_SOURCE),
            "us_short": adj_us_short,
            "metar_raw": raw_metar,
            "metar_bias": 0.0,
            "metar": adj_metar,
        }
        # Also store the canonical source key, e.g. gfs_raw/gfs. This is what
        # new calibration records use. Do not store new data as hrrr unless the
        # configured source really is hrrr.
        snap[f"{US_SHORT_FORECAST_SOURCE}_raw"] = raw_us_short
        snap[f"{US_SHORT_FORECAST_SOURCE}_bias"] = get_bias(city_slug, US_SHORT_FORECAST_SOURCE)
        snap[US_SHORT_FORECAST_SOURCE] = adj_us_short

        # Best forecast: calibrated US short-range model for US short horizon,
        # otherwise calibrated ECMWF. METAR is stored for context only.
        loc = LOCATIONS[city_slug]
        if loc["region"] == "us" and snap["us_short"] is not None:
            snap["best"] = snap["us_short"]
            snap["best_raw"] = snap["us_short_raw"]
            snap["best_bias"] = snap["us_short_bias"]
            snap["best_source"] = US_SHORT_FORECAST_SOURCE
        elif snap["ecmwf"] is not None:
            snap["best"] = snap["ecmwf"]
            snap["best_raw"] = snap["ecmwf_raw"]
            snap["best_bias"] = snap["ecmwf_bias"]
            snap["best_source"] = "ecmwf"
        else:
            snap["best"] = None
            snap["best_raw"] = None
            snap["best_bias"] = 0.0
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
                "ts":              snap.get("ts"),
                "horizon":         horizon,
                "hours_left":      round(hours, 1),
                "ecmwf_raw":       snap.get("ecmwf_raw"),
                "ecmwf_bias":      snap.get("ecmwf_bias"),
                "ecmwf":           snap.get("ecmwf"),
                "us_short_source": snap.get("us_short_source"),
                "us_short_model":  snap.get("us_short_model"),
                "us_short_raw":    snap.get("us_short_raw"),
                "us_short_bias":   snap.get("us_short_bias"),
                "us_short":        snap.get("us_short"),
                "metar_raw":       snap.get("metar_raw"),
                "metar_bias":      snap.get("metar_bias"),
                "metar":           snap.get("metar"),
                "best_raw":        snap.get("best_raw"),
                "best_bias":       snap.get("best_bias"),
                "best":            snap.get("best"),
                "best_source":     snap.get("best_source"),
            }
            if snap.get("us_short_source"):
                src_key = snap.get("us_short_source")
                forecast_snap[f"{src_key}_raw"] = snap.get("us_short_raw")
                forecast_snap[f"{src_key}_bias"] = snap.get("us_short_bias")
                forecast_snap[src_key] = snap.get("us_short")
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

            forecast_temp = snap.get("best")            # calibrated forecast used for decisions
            raw_forecast_temp = snap.get("best_raw")     # external model value before calibration
            forecast_bias = snap.get("best_bias", 0.0)
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
                        mark_trade_closed_on_market(mkt, pos["close_reason"], pnl)
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
                        mark_trade_closed_on_market(mkt, pos["close_reason"], pnl)
                        closed += 1
                        print(f"  [CLOSE] {loc['name']} {date} — forecast changed | PnL: {'+'if pnl>=0 else ''}{pnl:.2f}")

            # --- OPEN PAPER POSITION ---
            if not mkt.get("position") and forecast_temp is not None and MIN_HOURS <= hours <= MAX_HOURS:
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
                                    "raw_forecast_temp": raw_forecast_temp,
                                    "forecast_bias": forecast_bias,
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

                        bucket_label = f"{best_signal['bucket_low']}-{best_signal['bucket_high']}{unit_sym}"
                        bias_note = ""
                        if best_signal.get("forecast_bias"):
                            bias_note = f" raw {best_signal.get('raw_forecast_temp')}->{best_signal.get('forecast_temp')}"

                        if LIVE_TRADING_ENABLED:
                            ok, error, response = live_buy_yes(best_signal)
                            if not ok:
                                mkt["live_last_error"] = {
                                    "ts": snap.get("ts"),
                                    "market_id": best_signal.get("market_id"),
                                    "yes_token_id": best_signal.get("yes_token_id"),
                                    "error": error,
                                }
                                print(f"  [LIVE SKIP] {loc['name']} {date} | {bucket_label} | {error}")
                            else:
                                best_signal["execution_mode"] = "live"
                                best_signal["live_order_response"] = response
                                balance -= best_signal["cost"]
                                mkt["position"] = best_signal
                                state["total_trades"] = int(state.get("total_trades", 0)) + 1
                                new_pos += 1
                                print(f"  [LIVE BUY] {loc['name']} {horizon} {date} | {bucket_label} | "
                                      f"${best_signal['entry_price']:.3f} | EV {best_signal['ev']:+.2f} | "
                                      f"${best_signal['cost']:.2f} ({best_signal['forecast_src'].upper()}{bias_note})")
                        else:
                            best_signal["execution_mode"] = "paper"
                            balance -= best_signal["cost"]
                            mkt["position"] = best_signal
                            state["total_trades"] = int(state.get("total_trades", 0)) + 1
                            new_pos += 1
                            print(f"  [PAPER BUY] {loc['name']} {horizon} {date} | {bucket_label} | "
                                  f"${best_signal['entry_price']:.3f} | EV {best_signal['ev']:+.2f} | "
                                  f"${best_signal['cost']:.2f} ({best_signal['forecast_src'].upper()}{bias_note})")

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
        mark_trade_closed_on_market(mkt, "resolved", pnl)

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
    calibration_sample_count = count_calibration_samples(all_mkts)
    if calibration_sample_count >= CALIBRATION_MIN:
        _cal = run_calibration(all_mkts)
        if HERMES_ENABLED:
            run_hermes_learning(backfill=False, rebuild_calibration=False, silent=True)

    return new_pos, closed, resolved

# =============================================================================
# TRADE / LEARNING HELPERS
# =============================================================================

def closed_trade_markets(markets):
    """Markets with a closed position, including early exits and resolved holds."""
    return [
        m for m in markets
        if m.get("position")
        and m["position"].get("status") == "closed"
        and m["position"].get("pnl") is not None
    ]


def trade_pnl(mkt):
    pos = mkt.get("position") or {}
    if pos.get("pnl") is not None:
        return float(pos.get("pnl"))
    if mkt.get("pnl") is not None:
        return float(mkt.get("pnl"))
    return 0.0


def mark_trade_closed_on_market(mkt, close_reason, pnl):
    """Persist summary fields so early exits show up in status/report/learning."""
    mkt["pnl"] = pnl
    mkt["trade_outcome"] = close_reason
    if close_reason != "resolved":
        mkt["early_exit"] = True
        mkt["resolved_outcome"] = "exited"
        # The position is closed even if the underlying Polymarket market is
        # still trading. Keeping this market record closed makes reports and
        # learning counts deterministic.
        mkt["status"] = "closed"
    history = mkt.setdefault("trade_close_history", [])
    history.append({
        "ts": datetime.now(timezone.utc).isoformat(),
        "reason": close_reason,
        "pnl": pnl,
    })


def forecast_error_records(markets):
    records = []
    for m in calibration_candidates(markets):
        city = m.get("city")
        if city not in LOCATIONS:
            continue
        actual = m.get("actual_temp")
        unit = m.get("unit", LOCATIONS[city]["unit"])
        for source in ["ecmwf", US_SHORT_FORECAST_SOURCE]:
            raw_keys, adjusted_keys = source_snapshot_keys(source)
            snap = next((
                s for s in reversed(m.get("forecast_snapshots", []))
                if any(s.get(k) is not None for k in raw_keys + adjusted_keys)
            ), None)
            if not snap:
                continue
            forecast = None
            source_key = None
            for key in raw_keys:
                if snap.get(key) is not None:
                    forecast = snap.get(key)
                    source_key = key
                    break
            if forecast is None:
                for key in adjusted_keys:
                    if snap.get(key) is not None:
                        forecast = snap.get(key)
                        source_key = key
                        break
            if forecast is None:
                continue
            error = float(actual) - float(forecast)
            records.append({
                "city": city,
                "city_name": LOCATIONS[city]["name"],
                "date": m.get("date"),
                "unit": unit,
                "source": canonical_source(source),
                "source_key": source_key,
                "actual": float(actual),
                "forecast": float(forecast),
                "error": round(error, 3),
                "abs_error": round(abs(error), 3),
            })
    return records


def summarize_numeric(rows, group_key, value_key="pnl"):
    grouped = {}
    for row in rows:
        key = row.get(group_key) or "unknown"
        grouped.setdefault(key, []).append(float(row.get(value_key, 0.0)))
    out = {}
    for key, vals in grouped.items():
        out[key] = {
            "n": len(vals),
            "sum": round(sum(vals), 2),
            "avg": round(sum(vals) / len(vals), 3) if vals else 0.0,
            "min": round(min(vals), 3) if vals else 0.0,
            "max": round(max(vals), 3) if vals else 0.0,
        }
    return out


def run_hermes_learning(backfill=False, rebuild_calibration=True, silent=False):
    """Run the local Hermes learning loop.

    Safe self-learning contract:
      1. optionally backfill actual temperatures;
      2. rebuild calibration.json from closed/past samples;
      3. write diagnostics/recommendations to hermes_learning.json;
      4. never auto-mutates EV/Kelly/threshold config.
    """
    global _cal
    if backfill and VC_KEY:
        backfill_actual_temps(rebuild_calibration=False)

    markets = load_all_markets()
    sample_count = count_calibration_samples(markets)
    if rebuild_calibration:
        _cal = run_calibration(markets)

    closed_trades = closed_trade_markets(markets)
    trade_rows = []
    for m in closed_trades:
        pos = m.get("position", {})
        trade_rows.append({
            "city": m.get("city"),
            "city_name": m.get("city_name"),
            "date": m.get("date"),
            "source": canonical_source(pos.get("forecast_src")),
            "close_reason": pos.get("close_reason") or m.get("trade_outcome") or "unknown",
            "pnl": trade_pnl(m),
            "entry_price": pos.get("entry_price"),
            "exit_price": pos.get("exit_price"),
            "p": pos.get("p"),
            "ev": pos.get("ev"),
            "sigma": pos.get("sigma"),
        })

    errors = forecast_error_records(markets)
    error_summary = {}
    for source in sorted(set(r["source"] for r in errors)):
        rows = [r for r in errors if r["source"] == source]
        if not rows:
            continue
        mae = sum(r["abs_error"] for r in rows) / len(rows)
        bias = sum(r["error"] for r in rows) / len(rows)
        rmse = math.sqrt(sum(r["error"] ** 2 for r in rows) / len(rows))
        error_summary[source] = {
            "n": len(rows),
            "bias_actual_minus_forecast": round(bias, 3),
            "mae": round(mae, 3),
            "rmse": round(rmse, 3),
        }

    by_city = summarize_numeric(trade_rows, "city", "pnl")
    by_source = summarize_numeric(trade_rows, "source", "pnl")
    by_close_reason = summarize_numeric(trade_rows, "close_reason", "pnl")
    early_exits = [r for r in trade_rows if r.get("close_reason") != "resolved"]
    resolved_holds = [r for r in trade_rows if r.get("close_reason") == "resolved"]

    recommendations = []
    if sample_count < CALIBRATION_MIN:
        recommendations.append(
            f"Need {CALIBRATION_MIN - sample_count} more closed historical samples before learned bias/sigma is fully active."
        )
    if len(closed_trades) < HERMES_MIN_TRADES:
        recommendations.append(
            f"Need {HERMES_MIN_TRADES - len(closed_trades)} more closed trades before judging strategy-level PnL by source/reason."
        )
    if early_exits:
        early_pnl = sum(r["pnl"] for r in early_exits)
        recommendations.append(
            f"Early exits are now tracked: {len(early_exits)} exits, total PnL {early_pnl:+.2f}. Review by close_reason before changing stop/take rules."
        )
    if error_summary:
        worst = max(error_summary.items(), key=lambda kv: kv[1]["mae"])
        recommendations.append(
            f"Largest forecast MAE is {worst[0]} at {worst[1]['mae']:.2f}; keep calibration source-specific instead of applying one global bias."
        )

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "safe_learning_mode": True,
        "notes": [
            "Calibration is the only automatically learned trading input.",
            "Thresholds such as MIN_EV, MAX_PRICE, MAX_SLIPPAGE, Kelly, stop-loss, and take-profit are reported but not auto-mutated.",
        ],
        "counts": {
            "markets_total": len(markets),
            "calibration_samples": sample_count,
            "closed_trades": len(closed_trades),
            "resolved_holds": len(resolved_holds),
            "early_exits": len(early_exits),
        },
        "forecast_error_summary": error_summary,
        "trade_summary": {
            "total_pnl": round(sum(r["pnl"] for r in trade_rows), 2),
            "by_city": by_city,
            "by_source": by_source,
            "by_close_reason": by_close_reason,
        },
        "calibration": load_cal(),
        "recommendations": recommendations,
        "recent_closed_trades": trade_rows[-50:],
    }
    atomic_write_json(HERMES_FILE, report)

    if not silent:
        print(f"\n{'='*55}")
        print("  HERMES SELF-LEARNING REPORT")
        print(f"{'='*55}")
        print(f"  Calibration samples: {sample_count}")
        print(f"  Closed trades:       {len(closed_trades)}")
        print(f"  Resolved holds:      {len(resolved_holds)}")
        print(f"  Early exits:         {len(early_exits)}")
        print(f"  Report file:         {HERMES_FILE.resolve()}")
        if error_summary:
            print("\n  Forecast error summary:")
            for source, row in sorted(error_summary.items()):
                print(f"    {source:<12} n={row['n']:<4} bias={row['bias_actual_minus_forecast']:+.2f} mae={row['mae']:.2f} rmse={row['rmse']:.2f}")
        if by_close_reason:
            print("\n  Trade PnL by close reason:")
            for reason, row in sorted(by_close_reason.items()):
                print(f"    {reason:<18} n={row['n']:<4} pnl={row['sum']:+.2f} avg={row['avg']:+.2f}")
        if recommendations:
            print("\n  Recommendations:")
            for rec in recommendations:
                print(f"    - {rec}")
        print(f"{'='*55}\n")
    return report

# =============================================================================
# REPORT
# =============================================================================

def print_status():
    state    = load_state()
    markets  = load_all_markets()
    open_pos = [m for m in markets if m.get("position") and m["position"].get("status") == "open"]
    closed_trades = closed_trade_markets(markets)
    resolved_trades = [m for m in closed_trades if (m.get("position") or {}).get("close_reason") == "resolved"]
    early_exits = [m for m in closed_trades if (m.get("position") or {}).get("close_reason") != "resolved"]

    bal     = state["balance"]
    start   = state["starting_balance"]
    ret_pct = (bal - start) / start * 100
    wins    = state.get("wins", 0)
    losses  = state.get("losses", 0)
    total_resolved   = wins + losses
    total_closed_pnl = sum(trade_pnl(m) for m in closed_trades)

    print(f"\n{'='*55}")
    print(f"  WEATHERBET — STATUS")
    print(f"{'='*55}")
    print(f"  Balance:       ${bal:,.2f}  (start ${start:,.2f}, {'+'if ret_pct>=0 else ''}{ret_pct:.1f}%)")
    print(f"  Closed trades: {len(closed_trades)} | closed PnL: {'+'if total_closed_pnl>=0 else ''}{total_closed_pnl:.2f}")
    print(f"  Resolved:      {total_resolved} | W: {wins} | L: {losses} | WR: {wins/total_resolved:.0%}" if total_resolved else "  No resolved holds yet")
    print(f"  Early exits:   {len(early_exits)}")
    print(f"  Open:          {len(open_pos)}")
    print(f"  Calibration samples: {count_calibration_samples(markets)}")

    if open_pos:
        print(f"\n  Open positions:")
        total_unrealized = 0.0
        for m in open_pos:
            pos      = m["position"]
            unit_sym = "F" if m["unit"] == "F" else "C"
            label    = f"{pos['bucket_low']}-{pos['bucket_high']}{unit_sym}"

            current_price = pos["entry_price"]
            for o in m.get("all_outcomes", []):
                if o.get("yes_token_id") == pos.get("yes_token_id") or o.get("market_id") == pos.get("market_id"):
                    current_price = o.get("bid", o.get("price", current_price))
                    break

            unrealized = round((current_price - pos["entry_price"]) * pos["shares"], 2)
            total_unrealized += unrealized
            pnl_str = f"{'+'if unrealized>=0 else ''}{unrealized:.2f}"
            src = canonical_source(pos.get("forecast_src")).upper()

            print(f"    {m['city_name']:<16} {m['date']} | {label:<14} | "
                  f"entry ${pos['entry_price']:.3f} -> ${current_price:.3f} | "
                  f"PnL: {pnl_str} | {src}")

        sign = "+" if total_unrealized >= 0 else ""
        print(f"\n  Unrealized PnL: {sign}{total_unrealized:.2f}")

    print(f"{'='*55}\n")


def print_report():
    markets  = load_all_markets()
    closed_trades = closed_trade_markets(markets)
    resolved_trades = [m for m in closed_trades if (m.get("position") or {}).get("close_reason") == "resolved"]
    early_exits = [m for m in closed_trades if (m.get("position") or {}).get("close_reason") != "resolved"]

    print(f"\n{'='*55}")
    print(f"  WEATHERBET — FULL REPORT")
    print(f"{'='*55}")

    if not closed_trades:
        print("  No closed trades yet.")
        print(f"  Calibration samples available: {count_calibration_samples(markets)}")
        return

    total_pnl = sum(trade_pnl(m) for m in closed_trades)
    wins      = [m for m in resolved_trades if m.get("resolved_outcome") == "win"]
    losses    = [m for m in resolved_trades if m.get("resolved_outcome") == "loss"]

    print(f"\n  Total closed trades: {len(closed_trades)}")
    print(f"  Resolved holds:      {len(resolved_trades)} | Wins: {len(wins)} | Losses: {len(losses)}")
    if resolved_trades:
        print(f"  Resolved win rate:   {len(wins)/len(resolved_trades):.0%}")
    print(f"  Early exits:         {len(early_exits)}")
    print(f"  Total closed PnL:    {'+'if total_pnl>=0 else ''}{total_pnl:.2f}")
    print(f"  Calibration samples: {count_calibration_samples(markets)}")

    print(f"\n  By close reason:")
    reason_rows = []
    for m in closed_trades:
        pos = m.get("position", {})
        reason_rows.append({"reason": pos.get("close_reason") or "unknown", "pnl": trade_pnl(m)})
    for reason, row in sorted(summarize_numeric(reason_rows, "reason", "pnl").items()):
        print(f"    {reason:<18} {row['n']:>3} trades  PnL: {row['sum']:+.2f}  Avg: {row['avg']:+.2f}")

    print(f"\n  By city:")
    for city in sorted(set(m.get("city") for m in closed_trades)):
        group = [m for m in closed_trades if m.get("city") == city]
        resolved_group = [m for m in group if (m.get("position") or {}).get("close_reason") == "resolved"]
        w = len([m for m in resolved_group if m.get("resolved_outcome") == "win"])
        pnl = sum(trade_pnl(m) for m in group)
        name = LOCATIONS.get(city, {}).get("name", city)
        wr = f"{w}/{len(resolved_group)} ({w/len(resolved_group):.0%})" if resolved_group else "no resolved holds"
        print(f"    {name:<16} {wr:<18} closed={len(group):<3} PnL: {'+'if pnl>=0 else ''}{pnl:.2f}")

    print(f"\n  Market details:")
    for m in sorted(closed_trades, key=lambda x: (x.get("date", ""), x.get("city", ""))):
        pos      = m.get("position", {})
        unit_sym = "F" if m.get("unit") == "F" else "C"
        snaps    = m.get("forecast_snapshots", [])
        first_fc = snaps[0].get("best") if snaps else None
        last_fc  = snaps[-1].get("best") if snaps else None
        label    = f"{pos.get('bucket_low')}-{pos.get('bucket_high')}{unit_sym}" if pos else "no position"
        reason   = pos.get("close_reason") or m.get("trade_outcome") or "unknown"
        result   = (m.get("resolved_outcome") or reason).upper()
        pnl      = trade_pnl(m)
        pnl_str  = f"{'+'if pnl>=0 else ''}{pnl:.2f}"
        fc_str   = f"forecast {first_fc}->{last_fc}{unit_sym}" if first_fc is not None else "no forecast"
        actual   = f"actual {m['actual_temp']}{unit_sym}" if m.get("actual_temp") is not None else "actual pending"
        src      = canonical_source(pos.get("forecast_src")).upper() if pos else "-"
        print(f"    {m.get('city_name', m.get('city')):<16} {m.get('date')} | {label:<14} | {src:<6} | "
              f"{fc_str} | {actual} | {result} {pnl_str}")

    print(f"{'='*55}\n")


# =============================================================================
# ACTUAL TEMP BACKFILL / CALIBRATION COMMANDS
# =============================================================================

def backfill_actual_temps(rebuild_calibration=True):
    """Fill missing actual_temp for historical market records.

    Conservative rules:
      - never uses same-day or future actual temperatures;
      - converts past open records with forecast_snapshots into closed records;
      - rebuilds calibration by default, so `backfill-actuals` immediately feeds
        the self-learning loop.
    """
    global _cal
    if not VC_KEY:
        print("VISUAL_CROSSING_KEY or config.vc_key is required for actual-temp backfill.")
        if rebuild_calibration:
            _cal = run_calibration(load_all_markets())
        return 0

    updated = 0
    normalized = 0
    skipped = 0
    skip_reasons = Counter()
    markets = load_all_markets()

    for mkt in markets:
        city = mkt.get("city")
        date_str = mkt.get("date")

        if not city or not date_str:
            skipped += 1
            skip_reasons["missing_city_or_date"] += 1
            continue
        if city not in LOCATIONS:
            skipped += 1
            skip_reasons["unknown_city"] += 1
            continue

        city_tz = ZoneInfo(TIMEZONES.get(city, "UTC"))
        today = datetime.now(city_tz).date()
        try:
            market_day = datetime.strptime(date_str, "%Y-%m-%d").date()
        except Exception:
            skipped += 1
            skip_reasons["bad_date_format"] += 1
            continue

        if market_day >= today:
            skipped += 1
            skip_reasons["same_day_or_future"] += 1
            continue

        if not mkt.get("forecast_snapshots"):
            skipped += 1
            skip_reasons["no_forecast_snapshots"] += 1
            continue

        changed = False
        status = mkt.get("status")
        if status not in {"resolved", "closed"}:
            # The day is over. Even if we never traded it, this is valid base
            # data for calibration because we have forecast snapshots + actuals.
            mkt["status"] = "closed"
            mkt["closed_by_backfill"] = True
            mkt["closed_by_backfill_at"] = datetime.now(timezone.utc).isoformat()
            changed = True
            normalized += 1

        if mkt.get("actual_temp") is not None:
            if changed:
                save_market(mkt)
            skipped += 1
            skip_reasons["already_has_actual_temp"] += 1
            continue

        actual = get_actual_temp(city, date_str)
        if actual is None:
            if changed:
                save_market(mkt)
            skipped += 1
            skip_reasons["visual_crossing_no_data"] += 1
            continue

        mkt["actual_temp"] = actual
        mkt["actual_temp_source"] = "visual_crossing"
        mkt["actual_temp_fetched_at"] = datetime.now(timezone.utc).isoformat()
        save_market(mkt)
        updated += 1
        print(f"  [ACTUAL] {mkt.get('city_name', city)} {date_str}: {actual}{mkt.get('unit', '')}")
        time.sleep(0.25)

    print(f"Actual-temp backfill complete: updated={updated}, normalized_closed={normalized}, skipped={skipped}")
    if skip_reasons:
        print("Skip reasons:")
        for reason, count in skip_reasons.most_common():
            print(f"  {reason}: {count}")

    if rebuild_calibration:
        _cal = run_calibration(load_all_markets())
        print(f"Calibration rebuilt from {count_calibration_samples(load_all_markets())} historical samples.")
    return updated

def calibrate_from_history():
    """Backfill actuals if possible, then rebuild calibration.json."""
    global _cal
    if VC_KEY:
        backfill_actual_temps(rebuild_calibration=False)
    _cal = run_calibration(load_all_markets())
    if not _cal:
        print("No calibration entries yet. Need enough resolved markets with actual_temp.")
        return _cal
    print("\nCalibration entries:")
    for key in sorted(_cal):
        c = _cal[key]
        print(
            f"  {key:<18} bias={c.get('bias', 0):+5.2f} "
            f"sigma={c.get('sigma', 0):.2f} n={c.get('n', 0)} "
            f"mae_raw={c.get('mae_raw', '-')}, mae_cal={c.get('mae_calibrated', '-')}"
        )
    return _cal

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
            mark_trade_closed_on_market(mkt, pos["close_reason"], pnl)
            closed += 1
            print(f"  [{reason}] {city_name} {mkt['date']} | entry ${entry:.3f} exit ${current_price:.3f} | {hours_left:.0f}h left | PnL: {'+'if pnl>=0 else ''}{pnl:.2f}")
            save_market(mkt)

    if closed:
        state["balance"] = round(balance, 2)
        save_state(state)

    return closed


def run_loop(live_execute=False):
    global _cal, LIVE_TRADING_ENABLED
    _cal = load_cal()
    LIVE_TRADING_ENABLED = bool(live_execute)

    print(f"\n{'='*55}")
    print(f"  WEATHERBET — STARTING")
    print(f"{'='*55}")
    print(f"  Cities:     {len(LOCATIONS)}")
    print(f"  Balance:    ${BALANCE:,.0f} | Max bet: ${MAX_BET}")
    print(f"  Scan:       {SCAN_INTERVAL//60} min | Monitor: {MONITOR_INTERVAL//60} min")
    print(f"  Sources:    ECMWF raw + {US_SHORT_FORECAST_LABEL}({US_SHORT_FORECAST_SOURCE}, US short horizon) + METAR(D+0)")
    print(f"  Mode:       {'LIVE EXECUTION' if LIVE_TRADING_ENABLED else 'PAPER ONLY — no live orders'}")
    if LIVE_TRADING_ENABLED:
        print(f"  Live:       BUY YES limit orders only | order type {LIVE_ORDER_TYPE} | max bet ${MAX_BET}")
    print(f"  Calibration: bias={'on' if CALIBRATION_USE_BIAS else 'off'} | min samples={CALIBRATION_MIN}")
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
    args = sys.argv[1:]
    live_execute = "--live-execute" in args
    args = [arg for arg in args if arg != "--live-execute"]
    cmd = args[0] if args else "run"
    if cmd == "run":
        run_loop(live_execute=live_execute)
    elif cmd == "status":
        _cal = load_cal()
        print_status()
    elif cmd == "report":
        _cal = load_cal()
        print_report()
    elif cmd == "backfill-actuals":
        _cal = load_cal()
        backfill_actual_temps(rebuild_calibration=True)
    elif cmd == "calibrate":
        _cal = load_cal()
        calibrate_from_history()
    elif cmd in {"learn", "hermes", "hermes-learn"}:
        _cal = load_cal()
        run_hermes_learning(backfill=True, rebuild_calibration=True, silent=False)
    else:
        print("Usage: python weatherbet.py [run|status|report|backfill-actuals|calibrate|learn] [--live-execute]")