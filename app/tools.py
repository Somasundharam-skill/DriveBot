"""The two DriveBot tools (pilot scope).

  check_dispatch_status   SOP 5: "Has my car been sent?"
  get_chauffeur_details   SOP 6: "Who is my chauffeur, which car?"

Every tool returns a dict with:
  "outcome"  a fixed code the AI can rely on
  "say"      what the AI should tell the customer (SOP wording)
plus only the allowlisted booking fields from mapper.py.

The business rules live HERE in code, not in the AI's prompt:
trusted number only, own bookings only, allowlisted fields only,
today's and upcoming pickups only.
"""

import time

from app import config, identity, log, mapper
from app.errors import DCCUnavailable

# ----------------------------------------------------------------------
# Tool definitions shown to the AI (tools/list).
# The descriptions tell the AI what each tool does AND when to call it.
# ----------------------------------------------------------------------
TOOLS = [
    {
        "name": "check_dispatch_status",
        "description": (
            "Checks whether the car for the customer's booking has been sent (dispatched). "
            "Call this when the customer asks 'Has my car been sent?', 'Where is my cab?', "
            "'Is my car coming?' or about the status of their booking. Also call it first "
            "when the customer asks for chauffeur or car details and you do not have a "
            "booking_ref yet. Takes no arguments: the customer's mobile number is taken "
            "automatically from the verified conversation, so never ask for a number or "
            "look one up for someone else. Only today's and upcoming pickups are returned. "
            "The result has an 'outcome' (one, several, none, verification_needed, "
            "unavailable) and a 'say' field: tell the customer what 'say' says, in their "
            "language, and do not add or guess any details that are not in the result. "
            "If the outcome is 'several', ask which pickup they mean using only the date, "
            "time and area, then use that booking's booking_ref."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "get_chauffeur_details",
        "description": (
            "Gets the allocated chauffeur's first name and the car model, colour and "
            "registration number for ONE of the customer's own bookings. Call this when "
            "the customer asks who their chauffeur is, which car is coming, the car number, "
            "or accepts your offer of chauffeur details after check_dispatch_status. "
            "Needs the booking_ref from a check_dispatch_status result; if you do not have "
            "one, call check_dispatch_status first (and if it finds several bookings, ask "
            "the customer which pickup they mean). The result has an 'outcome' (shared, "
            "not_allocated, not_found, verification_needed, unavailable) and a 'say' field: "
            "tell the customer what 'say' says, in their language. When the outcome is "
            "'shared', share exactly these details and nothing more. Never share details "
            "of any other driver or booking."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "booking_ref": {
                    "type": "string",
                    "description": "The booking_ref of the customer's booking, exactly as "
                    "returned by check_dispatch_status (for example DCC100101).",
                }
            },
            "required": ["booking_ref"],
            "additionalProperties": False,
        },
    },
]

# ----------------------------------------------------------------------
# Wording shared by both tools.
# ----------------------------------------------------------------------


def _support_line():
    return f"please call our customer support team on {config.SUPPORT_NUMBER}"


def _verification_needed_result():
    return {
        "outcome": "verification_needed",
        "say": (
            "For your security, I need to verify your mobile number before I can share "
            f"booking details, and I can't do that in this chat yet. For help, {_support_line()}."
        ),
    }


def _unavailable_result(reason):
    if reason == "no_trusted_number":
        say = (
            "I can't look up a booking without a verified mobile number. "
            f"For help with your booking, {_support_line()}."
        )
    else:
        say = f"I'm sorry, I can't check your booking right now. {_support_line().capitalize()}."
    return {"outcome": "unavailable", "reason": reason, "say": say}


# ----------------------------------------------------------------------
# Which DriveCarClub client to use (mock or live). Created once.
# ----------------------------------------------------------------------
_client = None


def get_client():
    global _client
    if _client is None:
        if config.DCC_MODE == "live":
            from app.drivecarclub import DriveCarClubClient
            _client = DriveCarClubClient()
        else:
            from app.mock import MockDriveCarClub
            _client = MockDriveCarClub()
    return _client


def _load_current_bookings(mobile):
    """Fetch the caller's bookings and keep only today's and upcoming ones.

    Raises DCCUnavailable if DriveCarClub fails.
    """
    raw_bookings = get_client().get_bookings(mobile)
    bookings, skipped = mapper.current_bookings(raw_bookings)
    if skipped:
        # Probably a field name we don't know yet: update FIELD_MAP in mapper.py.
        log.warning("bookings_skipped_unreadable", count=skipped)
    return bookings


# ----------------------------------------------------------------------
# Tool 1: check_dispatch_status (SOP 5)
# ----------------------------------------------------------------------


def check_dispatch_status(mobile):
    bookings = _load_current_bookings(mobile)

    if not bookings:
        return {
            "outcome": "none",
            "say": (
                "I can't see a car sent for this number yet. "
                f"If you need help with a booking, {_support_line()}."
            ),
        }

    if len(bookings) == 1:
        booking = bookings[0]
        summary = mapper.dispatch_summary(booking)
        when = mapper.friendly_when(booking)
        area = f" from {summary['pickup_area']}" if summary["pickup_area"] else ""
        if summary["chauffeur_allocated"]:
            say = (
                f"Your car has been sent for your pickup {when}{area}. "
                "Would you like the chauffeur and car details?"
            )
        else:
            say = (
                f"I can see your booking for pickup {when}{area}, but a chauffeur hasn't "
                "been assigned yet. You'll get the details once they are. "
                f"For anything urgent, {_support_line()}."
            )
        return {"outcome": "one", "booking": summary, "say": say}

    # Several bookings: read out only date, time and area of each (SOP 5 rule 6).
    choices = []
    for number, booking in enumerate(bookings, start=1):
        area = booking["pickup_area"]
        choices.append(f"{number}) {mapper.friendly_when(booking)}" + (f" from {area}" if area else ""))
    return {
        "outcome": "several",
        "bookings": [mapper.dispatch_summary(b) for b in bookings],
        "say": (
            f"I can see {len(bookings)} upcoming pickups for this number: "
            + "; ".join(choices)
            + ". Which pickup do you mean?"
        ),
        "next_step": (
            "When the customer chooses, confirm that pickup: if chauffeur_allocated is true "
            "say the car has been sent and offer chauffeur details (get_chauffeur_details "
            "with its booking_ref); if false, say the chauffeur hasn't been assigned yet."
        ),
    }


# ----------------------------------------------------------------------
# Tool 2: get_chauffeur_details (SOP 6)
# ----------------------------------------------------------------------


def get_chauffeur_details(mobile, booking_ref):
    not_found = {
        "outcome": "not_found",
        "say": (
            "I can't find that booking for this number. "
            "I can check your upcoming pickups if you like."
        ),
    }
    wanted = (booking_ref or "").strip().lower()
    if not wanted:
        return not_found

    # Always fetch again: the chauffeur may have changed (SOP 6 rule 5), and
    # this proves the booking belongs to THIS caller's number.
    bookings = _load_current_bookings(mobile)
    booking = next((b for b in bookings if b["booking_ref"].lower() == wanted), None)
    if booking is None:
        return not_found  # not theirs, past, or finished: reveal nothing

    if not mapper.is_chauffeur_allocated(booking):
        return {
            "outcome": "not_allocated",
            "booking": mapper.not_allocated_summary(booking),
            "say": (
                "Your chauffeur hasn't been assigned yet. You'll get the details once they are. "
                f"For anything urgent, {_support_line()}."
            ),
        }

    details = mapper.chauffeur_details(booking)
    car = " ".join(part for part in (details["car_colour"].lower(), details["car_model"]) if part) or "car"
    say = (
        f"Your chauffeur for the pickup {mapper.friendly_when(booking)} is "
        f"{details['chauffeur_first_name']}, driving a {car}, registration number "
        f"{details['registration_number']}."
    )
    if details.get("chauffeur_mobile"):
        say += f" You can reach them on {details['chauffeur_mobile']}."
    return {"outcome": "shared", "chauffeur": details, "say": say}


# ----------------------------------------------------------------------
# One entry point for tools/call: identity check, run, log.
# ----------------------------------------------------------------------


class UnknownTool(Exception):
    pass


def call_tool(name, arguments, caller):
    """Run a tool and return its result dict.

    "caller" holds the Perfox headers (phone, channel, conversation_id...).
    The phone number comes ONLY from there, never from "arguments".
    """
    if name not in ("check_dispatch_status", "get_chauffeur_details"):
        raise UnknownTool(name)

    started = time.monotonic()
    booking_ref = (arguments or {}).get("booking_ref") if name == "get_chauffeur_details" else None

    mobile, problem = identity.find_trusted_mobile(caller)
    try:
        if problem == "verification_needed":
            result = _verification_needed_result()
        elif problem:
            result = _unavailable_result(problem)
        elif name == "check_dispatch_status":
            result = check_dispatch_status(mobile)
        else:
            result = get_chauffeur_details(mobile, booking_ref)
    except DCCUnavailable as failure:
        log.alert_perfox_team(
            failure.reason, tool=name, conversation_id=caller.get("conversation_id")
        )
        result = _unavailable_result(failure.reason)
    except Exception as crash:  # a bug in our code: still never guess
        log.alert_perfox_team(
            "internal_error", tool=name, conversation_id=caller.get("conversation_id"),
            error=f"{type(crash).__name__}: {crash}"[:200],
        )
        result = _unavailable_result("internal_error")

    # The booking that was talked about, if exactly one.
    logged_ref = booking_ref
    if logged_ref is None and result.get("booking"):
        logged_ref = result["booking"]["booking_ref"]

    log.info(
        "tool_call",
        tool=name,
        conversation_id=caller.get("conversation_id"),
        channel=caller.get("channel"),
        mobile=log.mask_mobile(mobile or identity.normalise_phone(caller.get("phone"))),
        booking_ref=logged_ref,
        outcome=result["outcome"],
        reason=result.get("reason"),
        duration_ms=round((time.monotonic() - started) * 1000),
    )
    return result
