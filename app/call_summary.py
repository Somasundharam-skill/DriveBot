"""The end-of-call WhatsApp summary: template parameters and limits.

Used by the send_call_summary tool (tools.py). The message itself is an
approved WhatsApp template; we only fill in its body parameters, in order.

Which value goes into {{1}}, {{2}}... is set in .env, so a changed template
needs new settings, not code (comma-separated names from FIELDS below):
  WA_CHAUFFEUR_PARAMS  default booking_ref,pickup_full,chauffeur_first_name,car_model,registration
  WA_FARE_PARAMS       default car_category,trip_type,city,rates_line
An unknown name stops the server at startup (server.py).

  both topics: customer_first_name ("Sanjay": the first word of the booking's
      "individualsname", or "there" if there is none). Chauffeur: from that
      booking. Fare: from the caller's most recent booking of any date or
      status (looked up by tools.py with the trusted caller number).
  booking fields (topic "chauffeur"): booking_ref,
      pickup_date ("05 Aug 2026"), pickup_time ("6:30 PM"), pickup_area,
      pickup_full ("05 Aug 2026, 6:30 PM, Bengaluru"), chauffeur_first_name,
      driver_contact (only with WA_INCLUDE_DRIVER_PHONE=true), car_model,
      registration
  fare fields (topic "fare"): car_category, trip_type, city, rates_line
      ("8hrs 80kms Rs 2,400; 4hrs 40kms Rs 1,350 (extra km Rs 18, extra hour Rs 150)"),
      extra_charges_line ("extra km Rs 18, extra hour Rs 150", or "None")
  A field the topic doesn't have (a fare field in a chauffeur summary, or
  the other way round) is empty, so nothing is sent.

Safety rules:
  - Only the fields above are ever used: first names only, never a
    surname, the address or the VIP flag.
  - The customer's name is only ever used here, to fill the template: it
    is never in a tool result, a "say" or "message" text, or a log line.
  - Every parameter is one line (no newlines, tabs or runs of spaces) and
    at most MAX_PARAMETER_LENGTH characters.
  - If any value is empty, nothing is sent (the builders return None).
  - The values are never logged: only the count and the field names.

Limits (in memory, a restart clears them): 1 summary per topic per
conversation, and SUMMARY_MAX_PER_NUMBER_PER_DAY per number per day.
"""

import threading
from datetime import datetime

from app import config, mapper, messages
from app.mapper import INDIA

TOPICS = ("chauffeur", "fare")

# Every field name WA_CHAUFFEUR_PARAMS and WA_FARE_PARAMS may use.
CHAUFFEUR_FIELDS = (
    "customer_first_name", "booking_ref", "pickup_date", "pickup_time", "pickup_area", "pickup_full",
    "chauffeur_first_name", "driver_contact", "car_model", "registration",
)
FARE_FIELDS = ("customer_first_name", "car_category", "trip_type", "city", "rates_line", "extra_charges_line")
TOPIC_FIELDS = {"chauffeur": CHAUFFEUR_FIELDS, "fare": FARE_FIELDS}
FIELDS = CHAUFFEUR_FIELDS + FARE_FIELDS[1:]

# customer_first_name when the booking has no name, or the caller has no booking.
NO_NAME = "there"

# The original template layouts, used when a setting is empty.
DEFAULT_FIELDS = {
    "chauffeur": ("booking_ref", "pickup_full", "chauffeur_first_name", "car_model", "registration"),
    "fare": ("car_category", "trip_type", "city", "rates_line"),
}
SETTINGS = {"chauffeur": "WA_CHAUFFEUR_PARAMS", "fare": "WA_FARE_PARAMS"}
MAX_PARAMETER_LENGTH = 500


def template_for(topic):
    """(template name, language) for a topic. An empty name = not set up."""
    if topic == "chauffeur":
        return config.WA_TEMPLATE_CHAUFFEUR_NAME, config.WA_TEMPLATE_CHAUFFEUR_LANG
    return config.WA_TEMPLATE_FARE_NAME, config.WA_TEMPLATE_FARE_LANG


def fields_for(topic):
    """The field names for a topic's template, in {{1}}, {{2}}... order."""
    configured = config.WA_CHAUFFEUR_PARAMS if topic == "chauffeur" else config.WA_FARE_PARAMS
    return [name.lower() for name in configured] or list(DEFAULT_FIELDS[topic])


def unknown_fields():
    """{"WA_FARE_PARAMS": ["bad_name"], ...}: every setting with a name not in FIELDS."""
    unknown = {}
    for topic, setting in SETTINGS.items():
        bad = [name for name in fields_for(topic) if name not in FIELDS]
        if bad:
            unknown[setting] = bad
    return unknown


def one_line(value):
    """Squash all whitespace (newlines, tabs, repeated spaces) into single spaces."""
    return " ".join(str(value or "").split())


def clean(value):
    """One line, at most MAX_PARAMETER_LENGTH characters."""
    return one_line(one_line(value)[:MAX_PARAMETER_LENGTH])


def build(fields, values):
    """The cleaned values of "fields", in order, or None if any of them is empty."""
    cleaned = [clean(values.get(name)) for name in fields]
    return cleaned if all(cleaned) else None


# ----------------------------------------------------------------------
# Template parameters
# ----------------------------------------------------------------------


def customer_first_name(booking):
    """"SANJAY MINHAS" -> "Sanjay"; "there" if the booking has no name (or there is no booking)."""
    return mapper.first_name((booking or {}).get("customer_name", "")) or NO_NAME


def most_recent(bookings):
    """The booking with the latest pickup (unreadable dates count as oldest), or None."""
    if not bookings:
        return None
    return max(bookings, key=lambda b: (b["pickup_at"] is not None,
                                        b["pickup_at"] or datetime.min.replace(tzinfo=INDIA)))


def chauffeur_values(booking):
    """Every booking field by name, for one booking (internal format from mapper.py)."""
    pickup = booking["pickup_at"]
    date = pickup.strftime("%d %b %Y") if pickup else ""
    time = pickup.strftime("%I:%M %p").lstrip("0") if pickup else ""
    area = booking["pickup_area"]
    return {
        "customer_first_name": customer_first_name(booking),
        "booking_ref": booking["booking_ref"],
        "pickup_date": date,
        "pickup_time": time,
        "pickup_area": area,
        "pickup_full": f"{date}, {time}, {area}" if date and area else "",
        "chauffeur_first_name": mapper.first_name(booking["driver_name"]),  # never the surname
        # Empty (so nothing is sent) unless WA_INCLUDE_DRIVER_PHONE=true.
        "driver_contact": booking.get("driver_mobile", "") if config.WA_INCLUDE_DRIVER_PHONE else "",
        "car_model": booking["car_model"],
        "registration": booking["registration_number"],
    }


def chauffeur_parameters(booking):
    """The chauffeur parameters in WA_CHAUFFEUR_PARAMS order, or None."""
    return build(fields_for("chauffeur"), chauffeur_values(booking))


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


def _charged_extras(package):
    """Like _package_extras, but only the extras above Rs 0; "None" if there are none
    (an empty value would stop the summary from being sent)."""
    amounts = [
        ("extra km", package.get("extra_km_rate_inr")),
        ("extra hour", package.get("extra_hour_rate_inr")),
        ("outstation allowance", package.get("outstation_allowance_inr")),
        ("night stay", package.get("night_stay_inr")),
    ]
    parts = [f"{label} {messages.rupees(amount)}" for label, amount in amounts if amount]
    return ", ".join(parts) or "None"


def extra_charges_line(packages):
    """The extras only: "extra km Rs 18, extra hour Rs 150" when every package
    has the same, otherwise per package: "A: extra km Rs 1, ...; B: None".
    Rs 0 extras are left out; "None" when no package has any."""
    extras = [_charged_extras(p) for p in packages]
    if len(set(extras)) == 1:
        return extras[0]
    return "; ".join(f"{p['package_name']}: {extra}" for p, extra in zip(packages, extras))


def fare_values(car_category, trip_type, city, packages, customer_booking=None):
    """Every fare field by name. "customer_booking" is the caller's most recent booking (or None)."""
    return {
        "customer_first_name": customer_first_name(customer_booking),
        "car_category": car_category,
        "trip_type": trip_type,
        "city": city,
        "rates_line": fare_line(packages),
        "extra_charges_line": extra_charges_line(packages),
    }


def fare_parameters(car_category, trip_type, city, packages, customer_booking=None):
    """The fare parameters in WA_FARE_PARAMS order, or None."""
    if not packages:
        return None
    return build(fields_for("fare"), fare_values(car_category, trip_type, city, packages, customer_booking))


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
