"""Ready-to-send messages ("message" field) for the booking and fare tools.

DriveBot relays these as they are, so every customer sees the same card.
Plain text with real line breaks: no markdown, no JSON. Each card is a
heading, a blank line, the detail lines together, a blank line, and a
closing line (see _card). Several bookings: a blank line between blocks.

Safety rules (the same as mapper.py, enforced here too):
  - Only these booking fields are ever read: booking_ref, pickup_at,
    pickup_area, status, car_model, registration_number and the
    chauffeur's FIRST name. Never the customer's name, the chauffeur's
    surname or mobile, the address, the VIP flag or standing instructions.
  - A line whose value is missing is left out.
  - No support phone number: it lives only in the Perfox knowledge base.
"""

from app import mapper

SUPPORT_LINE = "If you need help with a booking, please contact our customer support team."

# How a status is read out. Others are shown as mapper.status_name() gives them.
FRIENDLY_STATUS = {
    "despatched": "dispatched and on its way",
    "dispatched": "dispatched and on its way",
}


def _lines(*lines):
    """Join the lines, leaving out the empty ones (missing values)."""
    return "\n".join(line for line in lines if line)


def _card(heading, details, closing):
    """Heading, blank line, the detail lines together, blank line, closing.
    Empty details are left out, and so is the detail block if none are left."""
    return "\n\n".join(part for part in (heading, _lines(*details), closing) if part)


def _line(label, value):
    """One "label: value" line, or "" if there is no value."""
    return f"{label}: {value}" if value else ""


def pickup_time(booking):
    """"05 Aug 2026, 6:30 PM", or "" if the date couldn't be read."""
    pickup = booking["pickup_at"]
    if pickup is None:
        return ""
    return f"{pickup.strftime('%d %b %Y')}, {pickup.strftime('%I:%M %p').lstrip('0')}"


def car(booking):
    """"Toyota Innova Crysta (KA01AB1234)", or whichever part we have."""
    model, registration = booking["car_model"], booking["registration_number"]
    if model and registration:
        return f"{model} ({registration})"
    return model or registration


def friendly_status(status):
    return FRIENDLY_STATUS.get(status.lower(), mapper.status_name(status))


# ----------------------------------------------------------------------
# check_dispatch_status
# ----------------------------------------------------------------------


def dispatch_one(booking):
    if mapper.is_chauffeur_allocated(booking):
        return _card(
            "Good news, your car's on its way 🚗",
            [
                _line("🚗 Car", car(booking)),
                _line("📍 Pickup area", booking["pickup_area"]),
                _line("🕒 Pickup time", pickup_time(booking)),
                _line("🔖 Booking", booking["booking_ref"]),
                _line("✅ Status", friendly_status(booking["status"])),
            ],
            "Would you like your chauffeur's details?",
        )
    # No chauffeur yet, so no car has been sent: no car line.
    return _card(
        "I can see your booking 🚗",
        [
            _line("📍 Pickup area", booking["pickup_area"]),
            _line("🕒 Pickup time", pickup_time(booking)),
            _line("🔖 Booking", booking["booking_ref"]),
            _line("✅ Status", friendly_status(booking["status"])),
        ],
        "A chauffeur hasn't been assigned yet. You'll get the details once they are.",
    )


def dispatch_several(bookings):
    """A short intro, one block per booking (time, area, ref only), then the question."""
    blocks = [f"I can see {len(bookings)} upcoming pickups for this number:"]
    for number, booking in enumerate(bookings, start=1):
        blocks.append(_lines(
            f"{number})",
            _line("🕒 Pickup time", pickup_time(booking)),
            _line("📍 Pickup area", booking["pickup_area"]),
            _line("🔖 Booking", booking["booking_ref"]),
        ))
    blocks.append("Which pickup do you mean?")
    return "\n\n".join(blocks)


def dispatch_none():
    return _card("I can't see a car sent for this number yet.", [], SUPPORT_LINE)


# ----------------------------------------------------------------------
# get_chauffeur_details
# ----------------------------------------------------------------------


def chauffeur_shared(booking):
    return _card(
        "Here are your chauffeur and car details 🧑‍✈️",
        [
            _line("🧑‍✈️ Chauffeur", mapper.first_name(booking["driver_name"])),
            _line("🚗 Car", car(booking)),
            _line("🕒 Pickup time", pickup_time(booking)),
            _line("📍 Pickup area", booking["pickup_area"]),
        ],
        "Have a comfortable ride.",
    )


def chauffeur_not_allocated():
    return "Your chauffeur hasn't been assigned yet. You'll get the details once they are."


# ----------------------------------------------------------------------
# get_fare
# ----------------------------------------------------------------------


def rupees(amount):
    """2400 -> "Rs 2,400", 150000 -> "Rs 1,50,000", 18.5 -> "Rs 18.50" (Indian grouping)."""
    whole, _, paise = (f"{amount}" if isinstance(amount, int) else f"{amount:.2f}").partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        whole = ",".join([head] + groups + [tail])
    return f"Rs {whole}" + (f".{paise}" if paise else "")


def fare_package(package):
    """"8hrs 80kms: Rs 2,400 · extra km Rs 18 · extra hour Rs 150"."""
    parts = [
        f"{package['package_name']}: {rupees(package['rate_inr'])}",
        f"extra km {rupees(package['extra_km_rate_inr'])}",
        f"extra hour {rupees(package['extra_hour_rate_inr'])}",
    ]
    if "outstation_allowance_inr" in package:
        parts.append(f"outstation allowance {rupees(package['outstation_allowance_inr'])}")
    if "night_stay_inr" in package:
        parts.append(f"night stay {rupees(package['night_stay_inr'])}")
    return " · ".join(parts)


def fare_found(car_category, service_type, city, packages, note):
    heading = f"Standard fares for a {car_category} car, {service_type}, in {city}:"
    return _card(heading, [fare_package(p) for p in packages], note)
