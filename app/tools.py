"""The DriveBot tools (pilot scope).

  check_dispatch_status   SOP 5: "Has my car been sent?"
  get_chauffeur_details   SOP 6: "Who is my chauffeur, which car?"
  send_otp, verify_otp    SOP 5 rule 1: prove the number on website chat

Every tool returns a dict with:
  "outcome"  a fixed code the AI can rely on
  "say"      what the AI should tell the customer (SOP wording)
plus only the allowlisted booking fields from mapper.py.

The business rules live HERE in code, not in the AI's prompt:
trusted number only, own bookings only, allowlisted fields only,
today's and upcoming pickups only.
"""

import time

from app import config, identity, log, mapper, otp
from app.errors import DCCUnavailable, SMSUnavailable

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
            "automatically from the verified conversation, so never pass a number or "
            "look one up for someone else. Only today's and upcoming pickups are returned. "
            "The result has an 'outcome' (one, several, none, verification_needed, "
            "unavailable) and a 'say' field: tell the customer what 'say' says, in their "
            "language, and do not add or guess any details that are not in the result. "
            "If the outcome is 'several', ask which pickup they mean using only the date, "
            "time and area, then use that booking's booking_ref. If the outcome is "
            "'verification_needed' (website chat), ask for their mobile number and call "
            "send_otp with it."
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
            "of any other driver or booking. If the outcome is 'verification_needed' "
            "(website chat), ask for their mobile number and call send_otp with it."
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
    {
        "name": "send_otp",
        "description": (
            "Website chat only. Texts a 6-digit one-time code to the customer's mobile "
            "number, to prove the number is theirs before any booking details are shared. "
            "Call this ONLY after check_dispatch_status or get_chauffeur_details returned "
            "'verification_needed' and the customer has told you their mobile number. "
            "Never read the number back to the customer and never guess or invent one. "
            "The result has an 'outcome' (sent, not_needed, invalid_number, "
            "too_many_requests, locked, unavailable) and a 'say' field: tell the customer "
            "what 'say' says, in their language. When the outcome is 'sent', wait for the "
            "customer to tell you the code, then call verify_otp. You never see the code "
            "yourself."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "mobile_number": {
                    "type": "string",
                    "description": "The Indian mobile number exactly as the customer typed it "
                    "(for example 98765 43210 or +91 98765 43210).",
                }
            },
            "required": ["mobile_number"],
            "additionalProperties": False,
        },
    },
    {
        "name": "verify_otp",
        "description": (
            "Website chat only. Checks the 6-digit code the customer received by SMS after "
            "send_otp. Call this when the customer tells you the code. The result has an "
            "'outcome' (verified, wrong_code, expired, locked, not_needed) and a 'say' "
            "field: tell the customer what 'say' says, in their language. When the outcome "
            "is 'verified', call the tool the customer needed again (check_dispatch_status "
            "or get_chauffeur_details). If 'wrong_code', ask them to check the code and try "
            "again. If 'expired', offer to send a new code with send_otp. If 'locked', do "
            "not try again; give the customer support number."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "The 6-digit code exactly as the customer typed it.",
                }
            },
            "required": ["code"],
            "additionalProperties": False,
        },
    },
]

BOOKING_TOOLS = ("check_dispatch_status", "get_chauffeur_details")
OTP_TOOLS = ("send_otp", "verify_otp")

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
            "booking details. Please tell me your mobile number and I'll send you a "
            "one-time code by SMS."
        ),
        "next_step": (
            "When the customer gives their mobile number, call send_otp with it. "
            "Never read the number back to them."
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
# Tools 3 and 4: send_otp and verify_otp (SOP 5 rule 1, website chat)
# The code itself is NEVER put in a result or a log line.
# ----------------------------------------------------------------------


def _is_web(caller):
    return (caller.get("channel") or "").strip().lower() == "web"


def _not_needed_result():
    return {
        "outcome": "not_needed",
        "say": "Your number is already verified on this channel, so no code is needed.",
        "next_step": "Call check_dispatch_status or get_chauffeur_details directly.",
    }


def _locked_result():
    return {
        "outcome": "locked",
        "say": (
            "I'm sorry, I can't verify your number in this chat because of too many wrong "
            f"codes. For help with your booking, {_support_line()}."
        ),
    }


def send_otp(caller, mobile_number):
    """Text a code to the number the customer typed.

    Returns (result, mobile) so call_tool can log the masked number.
    """
    if not _is_web(caller):
        return _not_needed_result(), None

    mobile = identity.normalise_phone(mobile_number)
    if not mobile:
        return {
            "outcome": "invalid_number",
            "say": "That doesn't look like an Indian mobile number. Could you check it and tell me again?",
        }, None

    session = identity.get_session(caller.get("conversation_id"))
    outcome = otp.send_code(session, mobile)  # may raise SMSUnavailable

    if outcome == "locked":
        return _locked_result(), mobile
    if outcome == "too_many_requests":
        return {
            "outcome": "too_many_requests",
            "say": (
                "I can't send another code just now. Please use the last code I sent, "
                f"or wait a moment and ask again. If you still need help, {_support_line()}."
            ),
        }, mobile
    return {
        "outcome": "sent",
        "say": (
            "I've sent a 6-digit code by SMS to the number you gave me. "
            "Please tell me the code. It expires in 5 minutes."
        ),
        "next_step": "When the customer tells you the code, call verify_otp with it.",
    }, mobile


def verify_otp(caller, code):
    """Check the code the customer typed.

    Returns (result, mobile) so call_tool can log the masked number.
    """
    if not _is_web(caller):
        return _not_needed_result(), None

    session = identity.get_session(caller.get("conversation_id"))
    mobile = otp.pending_mobile(session)  # read before check_code clears it
    outcome, details = otp.check_code(session, code)

    if outcome == "verified":
        return {
            "outcome": "verified",
            "say": "Thank you, your number is verified.",
            "next_step": (
                "Now call check_dispatch_status (or get_chauffeur_details) again to "
                "answer the customer's question."
            ),
        }, details["mobile"]
    if outcome == "wrong_code":
        left = details["attempts_left"]
        return {
            "outcome": "wrong_code",
            "attempts_left": left,
            "say": (
                f"That code doesn't match. Please check the SMS and try again. "
                f"You have {left} {'try' if left == 1 else 'tries'} left."
            ),
        }, mobile
    if outcome == "locked":
        return _locked_result(), mobile
    return {
        "outcome": "expired",
        "say": "That code has expired or there's no code waiting. Would you like me to send a new one?",
        "next_step": "If the customer says yes, call send_otp again with their mobile number.",
    }, mobile


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
    if name in OTP_TOOLS:
        return _call_otp_tool(name, arguments, caller)
    if name not in BOOKING_TOOLS:
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


def _call_otp_tool(name, arguments, caller):
    """Run send_otp or verify_otp, and log it WITHOUT the code."""
    started = time.monotonic()
    arguments = arguments or {}
    mobile = None
    try:
        if name == "send_otp":
            result, mobile = send_otp(caller, arguments.get("mobile_number"))
        else:
            result, mobile = verify_otp(caller, arguments.get("code"))
    except SMSUnavailable as failure:
        log.alert_perfox_team(
            f"sms_{failure.reason}", tool=name, conversation_id=caller.get("conversation_id")
        )
        result = {
            "outcome": "unavailable",
            "reason": f"sms_{failure.reason}",
            "say": f"I'm sorry, I can't send a code right now. {_support_line().capitalize()}.",
        }
    except Exception as crash:  # a bug in our code
        log.alert_perfox_team(
            "internal_error", tool=name, conversation_id=caller.get("conversation_id"),
            error=type(crash).__name__,  # no message: it could contain the code
        )
        result = _unavailable_result("internal_error")

    log.info(
        "tool_call",
        tool=name,
        conversation_id=caller.get("conversation_id"),
        channel=caller.get("channel"),
        mobile=log.mask_mobile(mobile),
        outcome=result["outcome"],
        reason=result.get("reason"),
        duration_ms=round((time.monotonic() - started) * 1000),
    )
    return result
