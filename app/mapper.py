"""Turns DriveCarClub's raw booking data into the few safe fields we share.

We have not seen a real DespatchedBooking response yet. So FIELD_MAP lists
the field names we THINK DriveCarClub might use. When the sample response
arrives, update FIELD_MAP here and nothing else should need to change.

Safety rule: only the fields built in dispatch_summary() and
chauffeur_details() ever leave this server. Addresses, drop location,
the chauffeur's surname and (for now) mobile are never shared.
"""

import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app import config

INDIA = ZoneInfo(config.TIMEZONE)

# For each field we need: the raw field names it might arrive under.
# The first one that has a value wins. Matching ignores upper/lower case.
FIELD_MAP = {
    "booking_ref": ["BookingId", "BookingID", "BookingNo", "BookingRef", "BookingNumber"],
    "pickup_datetime": ["PickupDateTime", "PickUpDateTime", "ReportingDateTime", "TripDateTime"],
    "pickup_area": ["PickupArea", "PickUpArea", "PickupLocality", "Area"],
    "status": ["Status", "BookingStatus", "DispatchStatus", "TripStatus"],
    "driver_name": ["DriverName", "ChauffeurName", "Driver"],
    "driver_mobile": ["DriverMobile", "DriverMobileNo", "DriverPhone", "ChauffeurMobile"],
    "car_model": ["CarModel", "VehicleModel", "Model", "CarType"],
    "car_colour": ["CarColour", "CarColor", "VehicleColour", "VehicleColor", "Colour", "Color"],
    "registration_number": ["CarNo", "CarNumber", "VehicleNo", "VehicleNumber", "RegistrationNo", "RegNo"],
}

# The response may be a plain list, or a dict holding the list under one of these keys.
LIST_KEYS = ["data", "Data", "bookings", "Bookings", "result", "Result"]

# Bookings with these words in their status are finished, so we hide them.
FINISHED_STATUS_WORDS = ["complete", "close", "cancel"]


def unwrap_booking_list(response_data):
    """Find the list of bookings in the API response.

    Returns the list, or None if the response does not look right.
    """
    if isinstance(response_data, list):
        return response_data
    if isinstance(response_data, dict):
        for key in LIST_KEYS:
            if isinstance(response_data.get(key), list):
                return response_data[key]
    return None


def read_field(raw, field):
    """Get one field from a raw booking, trying every name in FIELD_MAP."""
    lower_case_raw = {str(key).lower(): value for key, value in raw.items()}
    for name in FIELD_MAP[field]:
        value = lower_case_raw.get(name.lower())
        if value is not None and str(value).strip() != "":
            return str(value).strip()
    return ""


def parse_pickup_time(text):
    """Read a pickup date/time and return it in Indian time (or None).

    Accepts "2026-10-09T14:30:00", "2026-10-09 14:30", "09-10-2026 14:30",
    "09/10/2026 02:30 PM" and the old .NET style "/Date(1760000000000)/".
    """
    if not text:
        return None

    dotnet = re.search(r"/Date\((-?\d+)", text)
    if dotnet:
        moment = datetime.fromtimestamp(int(dotnet.group(1)) / 1000, tz=timezone.utc)
        return moment.astimezone(INDIA)

    moment = None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        for pattern in ("%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M", "%d/%m/%Y %H:%M:%S",
                        "%d/%m/%Y %H:%M", "%d-%m-%Y %I:%M %p", "%d/%m/%Y %I:%M %p"):
            try:
                moment = datetime.strptime(text, pattern)
                break
            except ValueError:
                continue
    if moment is None:
        return None

    if moment.tzinfo is None:
        return moment.replace(tzinfo=INDIA)  # no time zone given: assume India
    return moment.astimezone(INDIA)


def clean_booking(raw):
    """Turn one raw booking into our own internal format.

    The result is for use INSIDE the server only. It still holds the full
    driver name and mobile, so never send it out directly.
    Returns None if the booking is missing its reference or pickup time.
    """
    if not isinstance(raw, dict):
        return None
    booking = {field: read_field(raw, field) for field in FIELD_MAP}
    booking["pickup_at"] = parse_pickup_time(booking.pop("pickup_datetime"))
    if not booking["booking_ref"] or booking["pickup_at"] is None:
        return None
    return booking


def is_finished(booking):
    status = booking["status"].lower()
    return any(word in status for word in FINISHED_STATUS_WORDS)


def current_bookings(raw_list, now=None):
    """Keep only today's and upcoming pickups that are not finished.

    Sorted with the earliest pickup first.
    Returns (bookings, number_of_unreadable_bookings_skipped).
    """
    today = (now or datetime.now(INDIA)).date()
    bookings = []
    skipped = 0
    for raw in raw_list:
        booking = clean_booking(raw)
        if booking is None:
            skipped += 1
            continue
        if booking["pickup_at"].date() < today:
            continue  # a past trip
        if is_finished(booking):
            continue
        bookings.append(booking)
    bookings.sort(key=lambda b: b["pickup_at"])
    return bookings, skipped


def is_chauffeur_allocated(booking):
    """A chauffeur counts as allocated only if we have a name AND a car number."""
    return bool(booking["driver_name"]) and bool(booking["registration_number"])


def first_name(full_name):
    """Only the first word of the name: "Ramesh Kumar" -> "Ramesh"."""
    words = full_name.split()
    return words[0] if words else ""


def _date_and_time(booking):
    return (
        booking["pickup_at"].strftime("%Y-%m-%d"),
        booking["pickup_at"].strftime("%H:%M"),
    )


def dispatch_summary(booking):
    """The ONLY booking fields check_dispatch_status may share (SOP 5).

    No chauffeur details here, and only the area, never the full address.
    """
    pickup_date, pickup_time = _date_and_time(booking)
    return {
        "booking_ref": booking["booking_ref"],
        "pickup_date": pickup_date,
        "pickup_time": pickup_time,
        "pickup_area": booking["pickup_area"],
        "dispatch_status": booking["status"],
        "chauffeur_allocated": is_chauffeur_allocated(booking),
    }


def not_allocated_summary(booking):
    """What get_chauffeur_details shares when no chauffeur is assigned yet."""
    pickup_date, pickup_time = _date_and_time(booking)
    return {
        "booking_ref": booking["booking_ref"],
        "pickup_date": pickup_date,
        "pickup_time": pickup_time,
    }


def chauffeur_details(booking):
    """The ONLY fields get_chauffeur_details may share (SOP 6 rule 3)."""
    pickup_date, pickup_time = _date_and_time(booking)
    details = {
        "booking_ref": booking["booking_ref"],
        "pickup_date": pickup_date,
        "pickup_time": pickup_time,
        "chauffeur_first_name": first_name(booking["driver_name"]),
        "car_model": booking["car_model"],
        "car_colour": booking["car_colour"],
        "registration_number": booking["registration_number"],
    }
    if config.SHARE_CHAUFFEUR_MOBILE and booking["driver_mobile"]:
        details["chauffeur_mobile"] = booking["driver_mobile"]
    return details


def friendly_when(booking, now=None):
    """Words the AI can read out: "today (Friday 9 October) at 2:30 PM"."""
    pickup = booking["pickup_at"]
    today = (now or datetime.now(INDIA)).date()
    day = f"{pickup.strftime('%A')} {pickup.day} {pickup.strftime('%B')}"
    if pickup.date() == today:
        day = f"today ({day})"
    elif (pickup.date() - today).days == 1:
        day = f"tomorrow ({day})"
    time_text = pickup.strftime("%I:%M %p").lstrip("0")
    return f"{day} at {time_text}"
