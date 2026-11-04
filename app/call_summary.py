"""The end-of-call WhatsApp summary: template parameters and limits.

Used by the send_call_summary tool (tools.py). The message itself is an
approved WhatsApp template; we only fill in its body parameters, in order.

  topic "chauffeur": booking ref; pickup ("05 Aug 2026, 6:30 PM, Bengaluru");
                     chauffeur FIRST name; car model; registration number
  topic "fare":      car category; trip type; city; one fare line, for example
                     "8hrs 80kms Rs 2,400; 4hrs 40kms Rs 1,350 (extra km Rs 18, extra hour Rs 150)"

Safety rules:
  - Only the values above are ever used. Never the customer's name, the
    chauffeur's surname or mobile, the address or the VIP flag.
  - Every parameter is one line (no newlines, tabs or runs of spaces).
  - If any value is missing, nothing is sent (the builders return None).

Limits (in memory, a restart clears them): 1 summary per topic per
conversation, and SUMMARY_MAX_PER_NUMBER_PER_DAY per number per day.
"""

import threading
from datetime import datetime

from app import config, mapper, messages
from app.mapper import INDIA

TOPICS = ("chauffeur", "fare")


def template_for(topic):
    """(template name, language) for a topic. An empty name = not set up."""
    if topic == "chauffeur":
        return config.WA_TEMPLATE_CHAUFFEUR_NAME, config.WA_TEMPLATE_CHAUFFEUR_LANG
    return config.WA_TEMPLATE_FARE_NAME, config.WA_TEMPLATE_FARE_LANG


def one_line(value):
    """Squash all whitespace (newlines, tabs, repeated spaces) into single spaces."""
    return " ".join(str(value or "").split())


def _all_or_nothing(values):
    """The values as one-line text, or None if any of them is missing."""
    cleaned = [one_line(value) for value in values]
    return cleaned if all(cleaned) else None


# ----------------------------------------------------------------------
# Template parameters
# ----------------------------------------------------------------------


def chauffeur_parameters(booking):
    """The 5 chauffeur parameters for one booking (internal format from mapper.py), or None."""
    pickup = messages.pickup_time(booking)  # "05 Aug 2026, 6:30 PM", or ""
    area = booking["pickup_area"]
    return _all_or_nothing([
        booking["booking_ref"],
        f"{pickup}, {area}" if pickup and area else "",
        mapper.first_name(booking["driver_name"]),  # first name only, never the surname
        booking["car_model"],
        booking["registration_number"],
    ])


def _package_extras(package):
    """"extra km Rs 18, extra hour Rs 150" (plus the outstation allowance and night stay when given)."""
    parts = [
        f"extra km {messages.rupees(package['extra_km_rate_inr'])}",
        f"extra hour {messages.rupees(package['extra_hour_rate_inr'])}",
    ]
    if "outstation_allowance_inr" in package:
        parts.append(f"outstation allowance {messages.rupees(package['outstation_allowance_inr'])}")
    if "night_stay_inr" in package:
        parts.append(f"night stay {messages.rupees(package['night_stay_inr'])}")
    return ", ".join(parts)


def fare_line(packages):
    """All packages on one line, built only from the API values.

    When every package has the same extras, they are written once at the end:
      "8hrs 80kms Rs 2,400; 4hrs 40kms Rs 1,350 (extra km Rs 18, extra hour Rs 150)"
    Otherwise each package gets its own: "A Rs 1 (extra ...); B Rs 2 (extra ...)".
    """
    extras = [_package_extras(p) for p in packages]
    prices = [f"{p['package_name']} {messages.rupees(p['rate_inr'])}" for p in packages]
    if len(set(extras)) == 1:
        return "; ".join(prices) + f" ({extras[0]})"
    return "; ".join(f"{price} ({extra})" for price, extra in zip(prices, extras))


def fare_parameters(car_category, trip_type, city, packages):
    """The 4 fare parameters, or None."""
    if not packages:
        return None
    return _all_or_nothing([car_category, trip_type, city, fare_line(packages)])


# ----------------------------------------------------------------------
# Limits
# ----------------------------------------------------------------------
_sent_per_day = {}  # (mobile, "2026-11-04") -> summaries sent that day (India time)
_lock = threading.Lock()


def reserve(session, topic, mobile):
    """Count one summary BEFORE sending it, so two calls at once can't both pass.

    Returns the day it was counted on, or None if a limit is reached.
    If the summary is then not sent, call release() to give it back.
    """
    day = datetime.now(INDIA).date().isoformat()
    with _lock:
        for key in [key for key in _sent_per_day if key[1] != day]:
            del _sent_per_day[key]  # forget earlier days
        sent_topics = session.setdefault("summaries_sent", set())
        if topic in sent_topics:
            return None
        if _sent_per_day.get((mobile, day), 0) >= config.SUMMARY_MAX_PER_NUMBER_PER_DAY:
            return None
        sent_topics.add(topic)
        _sent_per_day[(mobile, day)] = _sent_per_day.get((mobile, day), 0) + 1
        return day


def release(session, topic, mobile, day):
    """Undo reserve() for a summary that was not sent."""
    with _lock:
        session.get("summaries_sent", set()).discard(topic)
        if _sent_per_day.get((mobile, day)):
            _sent_per_day[(mobile, day)] -= 1


def clear_limits():
    """Forget the per-number counts (used by the tests)."""
    with _lock:
        _sent_per_day.clear()
