"""Fares from DriveCarClub's GetRate API: input checks, mapping and a cache.

The GetRate response looks like this (values are examples):

    {"status": "200", "message": "Success", "data": [
        {"packageName": "8hrs 80kms", "serviceType": "Local - within city limits",
         "carCategory": "Midsize", "cityName": "Gurugram", "rate": "2400.00",
         "extra_Km_Rate": "18.00", "extra_Hr_Rate": "150.00",
         "outstation_Allowance": "0.00", "night_Stay": "0.00"}, ...]}

Fares are not personal data, so no customer identity is needed. But we
still never guess: a package whose amounts can't be read is dropped.
"""

import re
import threading
import time

from app import config, log
from app.errors import DCCUnavailable

# Keep a looked-up fare for 1 hour, to avoid repeated calls.
CACHE_SECONDS = 60 * 60
# A safety limit so the cache can't grow forever (it is simply emptied).
CACHE_MAX_ENTRIES = 500

# The three inputs: plain text, letters, spaces and hyphens only, 1 to 40 characters.
VALID_INPUT = re.compile(r"[A-Za-z -]{1,40}")

# Our name for each input, the setting holding its allow-list, and the words
# we use when asking the customer for it.
INPUTS = {
    "car_category": ("FARE_CAR_CATEGORIES", "type of car"),
    "service_type": ("FARE_SERVICE_TYPES", "type of trip"),
    "city_name": ("FARE_CITIES", "city"),
}

_cache = {}  # (car_category, service_type, city_name) in lower case -> (expires_at, packages)
_cache_lock = threading.Lock()


def clean_input(value):
    """Trim and check one input. Returns the clean text, or None if it isn't allowed."""
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())  # trim, and squash repeated spaces
    if not value or not VALID_INPUT.fullmatch(value):
        return None
    return value


def allow_list(field):
    """The allowed values for one input from settings. Empty = no check."""
    return getattr(config, INPUTS[field][0])


def is_allowed(field, value):
    """True if the value is on the allow-list (ignoring case), or there is no list."""
    allowed = allow_list(field)
    return not allowed or value.lower() in (item.lower() for item in allowed)


def _amount(raw, key):
    """Read an amount like "2400.00" as a number: 2400 (or 2400.5).

    A missing or empty amount counts as 0. Raises ValueError if unreadable.
    """
    text = str(raw.get(key) if raw.get(key) is not None else "").strip() or "0"
    number = float(text)
    if number < 0 or number != number:  # negative, or "nan"
        raise ValueError(text)
    return int(number) if number.is_integer() else round(number, 2)


def clean_package(raw):
    """Turn one raw GetRate package into the fields we share, or None if unreadable.

    The outstation allowance and night stay are added only when above 0.
    """
    if not isinstance(raw, dict):
        return None
    try:
        package = {
            "package_name": str(raw.get("packageName") or "").strip(),
            "service_type": str(raw.get("serviceType") or "").strip(),
            "car_category": str(raw.get("carCategory") or "").strip(),
            "city": str(raw.get("cityName") or "").strip(),
            "rate_inr": _amount(raw, "rate"),
            "extra_km_rate_inr": _amount(raw, "extra_Km_Rate"),
            "extra_hour_rate_inr": _amount(raw, "extra_Hr_Rate"),
        }
        outstation_allowance = _amount(raw, "outstation_Allowance")
        night_stay = _amount(raw, "night_Stay")
    except (TypeError, ValueError):
        return None
    if not package["package_name"] or not package["rate_inr"]:
        return None  # no name or no fare: never guess
    if outstation_allowance > 0:
        package["outstation_allowance_inr"] = outstation_allowance
    if night_stay > 0:
        package["night_stay_inr"] = night_stay
    return package


def clean_packages(raw_list):
    """Clean every package. Returns (packages, number_skipped)."""
    packages = []
    skipped = 0
    for raw in raw_list:
        package = clean_package(raw)
        if package is None:
            skipped += 1
        else:
            packages.append(package)
    return packages, skipped


def lookup(client, car_category, service_type, city_name):
    """Return the fare packages for one combination (an empty list = no rate).

    Uses the 1-hour cache, so the same question (in any upper/lower case)
    only calls DriveCarClub once an hour. Failures are not cached.
    Raises DCCUnavailable if DriveCarClub fails.
    """
    key = (car_category.lower(), service_type.lower(), city_name.lower())
    with _cache_lock:
        cached = _cache.get(key)
        if cached and time.monotonic() < cached[0]:
            return cached[1]

    raw_list = client.get_rates(car_category, service_type, city_name)
    packages, skipped = clean_packages(raw_list)
    if skipped:
        # Probably a new field name or format: update clean_package() above.
        log.warning("fare_packages_skipped_unreadable", count=skipped)
    if raw_list and not packages:
        # DriveCarClub sent rates but we could read none: don't say "no rate".
        raise DCCUnavailable("bad_response")

    with _cache_lock:
        if len(_cache) >= CACHE_MAX_ENTRIES:
            _cache.clear()
        _cache[key] = (time.monotonic() + CACHE_SECONDS, packages)
    return packages


def clear_cache():
    """Forget all cached fares (used by the tests)."""
    with _cache_lock:
        _cache.clear()
