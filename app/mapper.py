"""Turns DriveCarClub's raw booking data into the few safe fields we share.

FIELD_MAP below matches the REAL UAT DespatchedBooking response, which
looks like this (one booking shown, private fields marked *):

    {"status": "200", "message": "Success", "data": [{
        "actual_bk_num": "BCR-XXX-0000001", "location": "New Delhi",
        "city_Name": "Gurugram", "status": "Despatched",
        "reportingDate": "05-08-2026", "reportingTime": "12:00",
        "carBooked": "Swift Dzire", "carAlloted": "Swift Dzire",
        "carNumber": "5792-DL1ZD", "chauffeurName": "FIRSTNAME LASTNAME",
        "chaufferMobile"*, "company_Name"*, "individualsname"*,
        "b_Guest_Contact_No"*, "reportingAddress"*, "standinginstructions"*, ...}]}

Our older guessed names are kept after the real ones as fallbacks.

Safety rule: only the fields built in dispatch_summary() and
chauffeur_details() ever leave this server. The customer's name, company,
guest number, address, the chauffeur's surname and (for now) mobile are
never shared. reportingAddress is never read at all.
"""

import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app import config, log

INDIA = ZoneInfo(config.TIMEZONE)

# For each field we need: the raw field names it might arrive under.
# The first one that has a value wins. Matching ignores upper/lower case.
# The REAL DriveCarClub name comes first; the rest are our old guesses.
FIELD_MAP = {
    "booking_ref": ["actual_bk_num", "BookingId", "BookingID", "BookingNo", "BookingRef", "BookingNumber"],
    # Real API: date and time arrive separately ("05-08-2026" and "12:00").
    "pickup_date": ["reportingDate"],
    "pickup_time": ["reportingTime"],
    # Old guesses: date and time in one field. Used only if reportingDate is empty.
    "pickup_datetime": ["PickupDateTime", "PickUpDateTime", "ReportingDateTime", "TripDateTime"],
    # Which of city_Name / location is tried first is set by PICKUP_AREA_FIELD
    # (see area_names()). Never add reportingAddress: it is the full address.
    "pickup_area": ["city_Name", "location", "PickupArea", "PickUpArea", "PickupLocality", "Area"],
    "status": ["status", "BookingStatus", "DispatchStatus", "TripStatus"],
    "driver_name": ["chauffeurName", "DriverName", "Driver"],
    # Note DriveCarClub's spelling: "chauffer". Still hidden (SHARE_CHAUFFEUR_MOBILE).
    "driver_mobile": ["chaufferMobile", "chauffeurMobile", "DriverMobile", "DriverMobileNo", "DriverPhone"],
    # The car actually sent; the car booked only if none is allotted yet.
    "car_model": ["carAlloted", "carBooked", "CarModel", "VehicleModel", "Model", "CarType"],
    # The real API has no colour field, so this is usually empty (and then left out).
    "car_colour": ["CarColour", "CarColor", "VehicleColour", "VehicleColor", "Colour", "Color"],
    "registration_number": ["carNumber", "CarNo", "VehicleNo", "VehicleNumber", "RegistrationNo", "RegNo"],
}

# The response may be a plain list, or a dict holding the list under one of these keys.
LIST_KEYS = ["data", "Data", "bookings", "Bookings", "result", "Result"]

# Bookings with these words in their status are finished, so we hide them.
# Change this list if DriveCarClub uses other words for finished trips.
FINISHED_STATUS_WORDS = ["complete", "close", "cancel"]

# Statuses we show in a standard form. DriveCarClub spells it "Despatched".
STATUS_NAMES = {"despatched": "Dispatched"}


def unwrap_response_list(response_data):
    """Find the list (bookings or rates) in a DriveCarClub API response.

    The real response is {"status": "200", "message": ..., "data": [...]}.
    A dict whose "status" is not "200" is an error.
    Returns the list, or None if the response does not look right.
    """
    if isinstance(response_data, list):
        return response_data
    if not isinstance(response_data, dict):
        return None
    if str(response_data.get("status", "")).strip() != "200":
        return None
    for key in LIST_KEYS:
        if isinstance(response_data.get(key), list):
            return response_data[key]
    return None


def area_names():
    """The pickup area field names, with PICKUP_AREA_FIELD tried first.

    DriveCarClub hasn't confirmed whether city_Name or location is the
    right one, so it is a setting. Only those two can be chosen.
    """
    chosen = config.PICKUP_AREA_FIELD
    if chosen not in config.PICKUP_AREA_CHOICES:
        chosen = "city_Name"
    return [chosen] + [name for name in FIELD_MAP["pickup_area"] if name != chosen]


def read_field(raw, field):
    """Get one field from a raw booking, trying every name in FIELD_MAP."""
    names = area_names() if field == "pickup_area" else FIELD_MAP[field]
    lower_case_raw = {str(key).lower(): value for key, value in raw.items()}
    for name in names:
        value = lower_case_raw.get(name.lower())
        if value is not None and str(value).strip() != "":
            return str(value).strip()
    return ""


def parse_pickup_time(text):
    """Read a pickup date/time and return it in Indian time (or None).

    Accepts the real format "05-08-2026 12:00" (DD-MM-YYYY HH:MM), a date
    on its own, and the older guesses: "2026-10-09T14:30:00",
    "09/10/2026 02:30 PM" and the .NET style "/Date(1760000000000)/".
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
        for pattern in ("%d-%m-%Y %H:%M", "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %I:%M %p", "%d-%m-%Y",
                        "%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %I:%M %p", "%d/%m/%Y"):
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
    Returns None if the booking has no reference. A booking whose date
    can't be read is KEPT (pickup_at is None) and a warning is logged.
    """
    if not isinstance(raw, dict):
        return None
    booking = {field: read_field(raw, field) for field in FIELD_MAP}

    # Real API: "05-08-2026" + "12:00". Old guesses: one combined field.
    date_text = booking.pop("pickup_date")
    time_text = booking.pop("pickup_time")
    when_text = booking.pop("pickup_datetime")
    if date_text:
        when_text = f"{date_text} {time_text}".strip()
    booking["pickup_at"] = parse_pickup_time(when_text)

    if not booking["booking_ref"]:
        return None
    if booking["pickup_at"] is None:
        # A date is not private, so log the raw text to help fix the format.
        log.warning("booking_date_unreadable", date_text=when_text)
    return booking


def is_finished(booking):
    status = booking["status"].lower()
    return any(word in status for word in FINISHED_STATUS_WORDS)


def is_past(booking, today):
    """True if the pickup was before today. An unreadable date is not past."""
    return booking["pickup_at"] is not None and booking["pickup_at"].date() < today


def current_bookings(raw_list, now=None):
    """Keep only today's and upcoming pickups that are not finished.

    With INCLUDE_PAST_BOOKINGS=true (UAT testing only) past pickups are kept too.
    Sorted with the earliest pickup first; unreadable dates go last.
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
        if is_past(booking, today) and not config.INCLUDE_PAST_BOOKINGS:
            continue  # a past trip
        if is_finished(booking):
            continue
        bookings.append(booking)
    bookings.sort(key=lambda b: (b["pickup_at"] is None, b["pickup_at"] or datetime.min.replace(tzinfo=INDIA)))
    return bookings, skipped


def is_chauffeur_allocated(booking):
    """A chauffeur counts as allocated only if we have a name AND a car number."""
    return bool(booking["driver_name"]) and bool(booking["registration_number"])


def first_name(full_name):
    """Only the first word of the name, in title case: "RAMESH KUMAR" -> "Ramesh"."""
    words = full_name.split()
    return words[0].title() if words else ""


def status_name(status):
    """The status to show: "Despatched" -> "Dispatched", others unchanged."""
    return STATUS_NAMES.get(status.lower(), status)


def _date_and_time(booking):
    """("2026-08-05", "12:00"), or ("", "") if the date couldn't be read."""
    if booking["pickup_at"] is None:
        return "", ""
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
        "dispatch_status": status_name(booking["status"]),
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
    """The ONLY fields get_chauffeur_details may share (SOP 6 rule 3).

    car_colour is left out when DriveCarClub doesn't send one.
    """
    pickup_date, pickup_time = _date_and_time(booking)
    details = {
        "booking_ref": booking["booking_ref"],
        "pickup_date": pickup_date,
        "pickup_time": pickup_time,
        "chauffeur_first_name": first_name(booking["driver_name"]),
        "car_model": booking["car_model"],
        "registration_number": booking["registration_number"],
    }
    if booking["car_colour"]:
        details["car_colour"] = booking["car_colour"]
    if config.SHARE_CHAUFFEUR_MOBILE and booking["driver_mobile"]:
        details["chauffeur_mobile"] = booking["driver_mobile"]
    return details


def friendly_when(booking, now=None):
    """Words the AI can read out: "today (Friday 9 October) at 2:30 PM"."""
    pickup = booking["pickup_at"]
    if pickup is None:
        return "(date not available)"
    today = (now or datetime.now(INDIA)).date()
    day = f"{pickup.strftime('%A')} {pickup.day} {pickup.strftime('%B')}"
    if pickup.date() == today:
        day = f"today ({day})"
    elif (pickup.date() - today).days == 1:
        day = f"tomorrow ({day})"
    time_text = pickup.strftime("%I:%M %p").lstrip("0")
    return f"{day} at {time_text}"
