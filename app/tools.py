"""The DriveBot tools (pilot scope).

  check_dispatch_status   SOP 5: "Has my car been sent?"
  get_chauffeur_details   SOP 6: "Who is my chauffeur, which car?"
  send_otp, verify_otp    SOP 5 rule 1: prove the number on website chat
  use_mobile_number       UAT DEMOS ONLY (VERIFICATION_MODE=none): the typed
                          number is trusted without a code
  get_fare                "How much does a car cost?" (standard rates)

Which tools are listed depends on VERIFICATION_MODE (see listed_tools):
"otp" lists send_otp and verify_otp, "none" lists use_mobile_number.
With WHATSAPP_NUMBER_MODE=ask, those tools work on WhatsApp too.

Every tool returns a dict with:
  "outcome"  a fixed code the AI can rely on
  "say"      what the AI should tell the customer (SOP wording)
  "message"  (booking and fare answers only) a ready-to-send card, see
             messages.py; the AI sends it as it is
plus only the allowlisted booking fields from mapper.py (or the fare
fields from fares.py).

The business rules live HERE in code, not in the AI's prompt:
trusted number only, own bookings only, allowlisted fields only,
today's and upcoming pickups only. get_fare needs no number, because
fares are not personal data, and it never guesses a fare.
"""

import time

from app import browser_sessions, config, fares, identity, log, mapper, messages, otp
from app.errors import DCCUnavailable, SMSUnavailable

# ----------------------------------------------------------------------
# The customer support number is NOT kept on this server: it lives only in
# the Perfox knowledge base. Where a "say" text sends the customer to
# support, it has this placeholder, and every tool description tells the
# AI to fill it in from the knowledge base. So no tool result or log line
# ever has a support phone number in it.
# ----------------------------------------------------------------------
SUPPORT_PLACEHOLDER = "[share the customer support number from the knowledge base]"
SUPPORT_HINT = (
    f" Where 'say' contains {SUPPORT_PLACEHOLDER}, replace it with the customer "
    "support number from your knowledge base; never make a number up."
)
# For the tools whose results have a ready-made "message" (messages.py).
MESSAGE_HINT = (
    " When the result has a 'message', send it to the customer exactly as written, "
    "keeping its line breaks and emojis, instead of writing your own sentence. Only if "
    "the customer is writing in another language, translate it line by line."
)

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
            + MESSAGE_HINT
            + SUPPORT_HINT
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
            + MESSAGE_HINT
            + SUPPORT_HINT
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
            "too_many_requests, locked, session_ended, unavailable) and a 'say' field: tell the customer "
            "what 'say' says, in their language. When the outcome is 'sent', wait for the "
            "customer to tell you the code, then call verify_otp. You never see the code "
            "yourself."
            + SUPPORT_HINT
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
            + SUPPORT_HINT
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
    {
        # Only listed when VERIFICATION_MODE=none (UAT demos).
        "name": "use_mobile_number",
        "description": (
            "Website chat only. Uses the customer's own mobile number to look up their "
            "bookings. Call this ONLY after check_dispatch_status or get_chauffeur_details "
            "returned 'verification_needed' and you have asked the customer for their own "
            "mobile number. Never read the number back to the customer and never guess or "
            "invent one. The result has an 'outcome' (ok, invalid_number, not_needed, "
            "session_ended, not_available) and a 'say' field: tell the customer what 'say' "
            "says, in their language. When the outcome is 'ok', call the tool the customer "
            "needed again (check_dispatch_status or get_chauffeur_details). If "
            "'invalid_number', ask them to check the number and tell you again."
            + SUPPORT_HINT
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "mobile_number": {
                    "type": "string",
                    "description": "The customer's own Indian mobile number exactly as they typed it "
                    "(for example 98765 43210 or +91 98765 43210).",
                }
            },
            "required": ["mobile_number"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_fare",
        "description": (
            "Gets DriveCarClub's standard fares (rate packages) for a type of car, a type of "
            "trip and a city. Call this when the customer asks how much a car costs, the "
            "fare, the rate or the price. All three details are required. If the customer "
            "hasn't told you any of them (the city, the type of trip such as local or "
            "outstation, or the type of car), ASK them first. Never guess or assume a "
            "value, especially the city: do not take it from a booking, the phone number "
            "or earlier in the chat unless the customer said it for this question. No "
            "mobile number or OTP is needed. The result has an 'outcome' (found, not_found, "
            "invalid_input, unavailable) and a 'say' field: tell the customer what 'say' "
            "says, in their language. Quote the amounts exactly as given; never round, add "
            "up, estimate or invent a fare. If the outcome is 'invalid_input', ask the "
            "customer for the details 'say' mentions and call get_fare again."
            + MESSAGE_HINT
            + SUPPORT_HINT
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "car_category": {
                    "type": "string",
                    "description": "The type of car, as the customer said it (for example Midsize).",
                },
                "service_type": {
                    "type": "string",
                    "description": "The type of trip, as the customer said it (for example Local or Outstation).",
                },
                "city_name": {
                    "type": "string",
                    "description": "The city, exactly as the customer said it (for example Gurugram). Never guess it.",
                },
            },
            "required": ["car_category", "service_type", "city_name"],
            "additionalProperties": False,
        },
    },
]

BOOKING_TOOLS = ("check_dispatch_status", "get_chauffeur_details")
OTP_TOOLS = ("send_otp", "verify_otp")
NO_OTP_TOOLS = ("use_mobile_number",)  # VERIFICATION_MODE=none only
FARE_TOOLS = ("get_fare",)


def listed_tools():
    """The tools to show in tools/list for the current VERIFICATION_MODE.

    "otp":  everything except use_mobile_number (as before).
    "none": no send_otp or verify_otp; use_mobile_number instead, and the
            booking tools point to it instead of send_otp.
    WHATSAPP_NUMBER_MODE=ask: the descriptions say WhatsApp too.
    """
    hidden = OTP_TOOLS if identity.verification_off() else NO_OTP_TOOLS
    listed = []
    for tool in TOOLS:
        if tool["name"] in hidden:
            continue
        description = tool["description"]
        if identity.verification_off() and tool["name"] in BOOKING_TOOLS:
            description = description.replace("call send_otp with it", "call use_mobile_number with it")
        if identity.whatsapp_asks_number():
            description = description.replace(
                "Website chat only.", "Website chat and WhatsApp only."
            ).replace("(website chat)", "(website chat or WhatsApp)")
        listed.append({**tool, "description": description})
    return listed


# ----------------------------------------------------------------------
# Wording shared by both tools.
# ----------------------------------------------------------------------


def _support_line():
    return f"please call our customer support team on {SUPPORT_PLACEHOLDER}"


def _verification_needed_result(caller):
    # On WhatsApp (WHATSAPP_NUMBER_MODE=ask) the customer is already on a
    # phone, so be clear we need the number they BOOKED with.
    on_whatsapp = identity.channel_of(caller) == "whatsapp"
    if identity.verification_off():
        # UAT demos: no code, just ask for the number.
        if on_whatsapp:
            say = (
                "To look up your booking, please tell me the registered mobile number "
                "you booked with (10 digits, no country code needed)."
            )
        else:
            say = (
                "To look up your booking, I need your mobile number. "
                "Please tell me the mobile number you booked with."
            )
        return {
            "outcome": "verification_needed",
            "say": say,
            "next_step": (
                "When the customer gives their own mobile number, call use_mobile_number "
                "with it. Never read the number back to them."
            ),
        }
    if on_whatsapp:
        number = "the registered mobile number you booked with (10 digits, no country code needed)"
    else:
        number = "your mobile number"
    return {
        "outcome": "verification_needed",
        "say": (
            "For your security, I need to verify your mobile number before I can share "
            f"booking details. Please tell me {number} and I'll send you a "
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
# The booking tools follow DCC_MODE; get_fare follows DCC_RATE_MODE.
# When both are live they share ONE live client, so one token is used.
# ----------------------------------------------------------------------
_client = None       # for the booking tools
_rate_client = None  # for get_fare
_shared = {}         # "live" / "mock" -> the one client of that kind


def _client_for(mode):
    """The shared live client if mode is "live", otherwise the shared mock client."""
    kind = "live" if mode == "live" else "mock"
    if kind not in _shared:
        if kind == "live":
            from app.drivecarclub import DriveCarClubClient
            _shared[kind] = DriveCarClubClient()
        else:
            from app.mock import MockDriveCarClub
            _shared[kind] = MockDriveCarClub()
    return _shared[kind]


def get_client():
    """The client for the booking tools (DCC_MODE)."""
    global _client
    if _client is None:
        _client = _client_for(config.DCC_MODE)
    return _client


def get_rate_client():
    """The client for get_fare (DCC_RATE_MODE)."""
    global _rate_client
    if _rate_client is None:
        _rate_client = _client_for(config.DCC_RATE_MODE)
    return _rate_client


def load_current_bookings(mobile):
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
    bookings = load_current_bookings(mobile)

    if not bookings:
        return {
            "outcome": "none",
            "say": (
                "I can't see a car sent for this number yet. "
                f"If you need help with a booking, {_support_line()}."
            ),
            "message": messages.dispatch_none(),
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
        return {"outcome": "one", "booking": summary, "say": say, "message": messages.dispatch_one(booking)}

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
        "message": messages.dispatch_several(bookings),
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
    bookings = load_current_bookings(mobile)
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
            "message": messages.chauffeur_not_allocated(),
        }

    details = mapper.chauffeur_details(booking)
    # The colour is left out when DriveCarClub doesn't send one.
    car = " ".join(part for part in (details.get("car_colour", "").lower(), details["car_model"]) if part) or "car"
    say = (
        f"Your chauffeur for the pickup {mapper.friendly_when(booking)} is "
        f"{details['chauffeur_first_name']}, driving a {car}, registration number "
        f"{details['registration_number']}."
    )
    if details.get("chauffeur_mobile"):
        say += f" You can reach them on {details['chauffeur_mobile']}."
    return {"outcome": "shared", "chauffeur": details, "say": say, "message": messages.chauffeur_shared(booking)}


# ----------------------------------------------------------------------
# Tools 3 and 4: send_otp and verify_otp (SOP 5 rule 1, website chat)
# The code itself is NEVER put in a result or a log line.
# ----------------------------------------------------------------------


def _browser_session(caller):
    """The website login linked to this chat (see browser_sessions.py).
    Website chat only: on WhatsApp there is no website login to share."""
    if identity.channel_of(caller) != "web":
        return "unknown", None
    return browser_sessions.find_by_chat_token(caller.get("user_token"))


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
    if not identity.asks_for_number(caller):
        return _not_needed_result(), None

    mobile = identity.normalise_phone(mobile_number)
    if not mobile:
        return {
            "outcome": "invalid_number",
            "say": "That doesn't look like an Indian mobile number. Could you check it and tell me again?",
        }, None

    # An old website token (the customer logged out in another tab): a chat
    # OTP would not count, so ask them to refresh the page instead.
    status, _ = _browser_session(caller)
    if status == "retired":
        return {
            "outcome": "session_ended",
            "say": "Your website session has ended. Please refresh the page and ask me again.",
        }, mobile

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
    if not identity.asks_for_number(caller):
        return _not_needed_result(), None

    session = identity.get_session(caller.get("conversation_id"))
    mobile = otp.pending_mobile(session)  # read before check_code clears it
    outcome, details = otp.check_code(session, code)

    if outcome == "verified":
        session["verified_mobile"] = details["mobile"]
        # Shared login: if the customer is on our website, sign the browser
        # in too, so the website shows them as signed in.
        status, browser = _browser_session(caller)
        if status == "live":
            browser["mobile"] = details["mobile"]
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
# Tool 5: use_mobile_number (UAT DEMOS ONLY, VERIFICATION_MODE=none)
# Website chat with no OTP: the typed number is trusted straight away.
# ----------------------------------------------------------------------


def _not_available_result():
    """For a tool that isn't listed in this VERIFICATION_MODE."""
    return {
        "outcome": "not_available",
        "say": f"I'm sorry, I can't do that here. For help with your booking, {_support_line()}.",
    }


def use_mobile_number(caller, mobile_number):
    """Trust the number the customer typed (no code).

    Returns (result, mobile) so call_tool can log the masked number.
    """
    if not identity.verification_off():
        return _not_available_result(), None
    # Phone and SMS (and WhatsApp unless WHATSAPP_NUMBER_MODE=ask) always use
    # the number from Perfox: a typed number is never used there.
    if not identity.asks_for_number(caller):
        return _not_needed_result(), None

    mobile = identity.normalise_phone(mobile_number)
    if not mobile:
        return {
            "outcome": "invalid_number",
            "say": "That doesn't look like an Indian mobile number. Could you check it and tell me again?",
        }, None

    # Same as send_otp: an old website token always counts as signed out.
    status, browser = _browser_session(caller)
    if status == "retired":
        return {
            "outcome": "session_ended",
            "say": "Your website session has ended. Please refresh the page and ask me again.",
        }, mobile

    # Store it where verify_otp does, and sign in the website too (shared login).
    identity.get_session(caller.get("conversation_id"))["verified_mobile"] = mobile
    if status == "live":
        browser["mobile"] = mobile
    return {
        "outcome": "ok",
        "say": "Thank you. Let me check that for you now.",
        "next_step": (
            "Now call check_dispatch_status (or get_chauffeur_details) again to answer the "
            "customer's question. Never read the number back to the customer."
        ),
    }, mobile


# ----------------------------------------------------------------------
# Tool 6: get_fare (standard fares from the GetRate API)
# Fares are not personal data: no identity check, works on every channel.
# ----------------------------------------------------------------------


def _rupees(amount):
    """2400 -> "₹2400", 18.5 -> "₹18.50". The amount itself is never rounded."""
    return f"₹{amount}" if isinstance(amount, int) else f"₹{amount:.2f}"


def _join_words(words):
    """["city", "type of trip"] -> "city and type of trip"."""
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


def _invalid_input_result(say):
    return {
        "outcome": "invalid_input",
        "say": say,
        "next_step": (
            "Ask the customer for these details, then call get_fare again with what they "
            "say. Never guess them."
        ),
    }


def _package_text(package):
    """One package read out: "8hrs 80kms: ₹2400, then ₹18 per extra km and ₹150 per extra hour"."""
    text = (
        f"{package['package_name']}: {_rupees(package['rate_inr'])}, then "
        f"{_rupees(package['extra_km_rate_inr'])} per extra km and "
        f"{_rupees(package['extra_hour_rate_inr'])} per extra hour"
    )
    if "outstation_allowance_inr" in package:
        text += f", plus an outstation allowance of {_rupees(package['outstation_allowance_inr'])}"
    if "night_stay_inr" in package:
        text += f" and a night stay charge of {_rupees(package['night_stay_inr'])}"
    return text


def get_fare(arguments, log_details):
    """Look up the standard fares for one car type, trip type and city.

    Fills "log_details" as it goes, so the log line has it even if
    DriveCarClub fails: only the inputs that passed the checks, and the
    "source" of the fares ("live", "cache" or "mock").
    """
    arguments = arguments or {}

    # 1. Every input must be given and be plain text.
    inputs, missing, unclear = log_details, [], []
    for field, (_, words) in fares.INPUTS.items():
        raw = arguments.get(field)
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            missing.append(words)
            continue
        value = fares.clean_input(raw)
        if value is None:
            unclear.append(words)
        else:
            inputs[field] = value
    if missing:
        return _invalid_input_result(
            f"To check the fare, I need to know the {_join_words(missing)}. Could you tell me?"
        )
    if unclear:
        return _invalid_input_result(
            f"Sorry, I didn't understand the {_join_words(unclear)}. Could you tell me again?"
        )

    # 2. If DriveCarClub's lists are set, the value must be on them.
    for field, (_, words) in fares.INPUTS.items():
        if not fares.is_allowed(field, inputs[field]):
            return _invalid_input_result(
                f"I can check fares for these options for the {words}: "
                f"{', '.join(fares.allow_list(field))}. Which one would you like?"
            )

    # 3. Ask DriveCarClub (or the 1-hour cache).
    client = get_rate_client()
    log_details["source"] = client.source  # if the call fails, it was this one
    packages, log_details["source"] = fares.lookup(
        client, inputs["car_category"], inputs["service_type"], inputs["city_name"]
    )
    if not packages:
        return {
            "outcome": "not_found",
            "say": (
                f"I'm sorry, I couldn't find a standard rate for a {inputs['car_category']} car, "
                f"{inputs['service_type']} trip in {inputs['city_name']}. "
                f"For a quote, {_support_line()}."
            ),
        }

    first = packages[0]
    return {
        "outcome": "found",
        "packages": packages,
        "message": messages.fare_found(
            first["car_category"] or inputs["car_category"],
            first["service_type"] or inputs["service_type"],
            first["city"] or inputs["city_name"],
            packages,
            config.FARE_NOTE,
        ),
        "say": (
            f"Our standard rates for a {first['car_category'] or inputs['car_category']} car, "
            f"{first['service_type'] or inputs['service_type']}, in "
            f"{first['city'] or inputs['city_name']} are: "
            + "; ".join(_package_text(p) for p in packages)
            + f". {config.FARE_NOTE}"
        ),
        "next_step": "Quote these amounts exactly. Never round, add up, estimate or invent a fare.",
    }


def _call_fare_tool(name, arguments, caller):
    """Run get_fare and log it. No identity check: fares are not personal."""
    started = time.monotonic()
    details = {}  # the checked inputs and the source, filled by get_fare
    try:
        result = get_fare(arguments, details)
    except DCCUnavailable as failure:
        log.alert_perfox_team(
            failure.reason, tool=name, conversation_id=caller.get("conversation_id")
        )
        result = _fare_unavailable_result(failure.reason)
    except Exception as crash:  # a bug in our code: still never guess
        log.alert_perfox_team(
            "internal_error", tool=name, conversation_id=caller.get("conversation_id"),
            error=f"{type(crash).__name__}: {crash}"[:200],
        )
        result = _fare_unavailable_result("internal_error")

    # The inputs are not personal, so they are logged. Inputs that failed
    # the checks are left out (they could be anything the customer typed).
    log.info(
        "tool_call",
        tool=name,
        conversation_id=caller.get("conversation_id"),
        channel=caller.get("channel"),
        car_category=details.get("car_category"),
        service_type=details.get("service_type"),
        city_name=details.get("city_name"),
        source=details.get("source"),  # None when the inputs were refused (no lookup)
        outcome=result["outcome"],
        reason=result.get("reason"),
        duration_ms=round((time.monotonic() - started) * 1000),
    )
    return result


def _fare_unavailable_result(reason):
    return {
        "outcome": "unavailable",
        "reason": reason,
        "say": f"I'm sorry, I can't check fares right now. {_support_line().capitalize()}.",
    }


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
    if name in OTP_TOOLS + NO_OTP_TOOLS:
        return _call_otp_tool(name, arguments, caller)
    if name in FARE_TOOLS:
        return _call_fare_tool(name, arguments, caller)
    if name not in BOOKING_TOOLS:
        raise UnknownTool(name)

    started = time.monotonic()
    booking_ref = (arguments or {}).get("booking_ref") if name == "get_chauffeur_details" else None

    mobile, problem = identity.find_trusted_mobile(caller)
    try:
        if problem == "verification_needed":
            result = _verification_needed_result(caller)
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
    """Run send_otp, verify_otp or use_mobile_number, and log it WITHOUT the code."""
    started = time.monotonic()
    arguments = arguments or {}
    mobile = None
    try:
        if name == "use_mobile_number":
            result, mobile = use_mobile_number(caller, arguments.get("mobile_number"))
        elif identity.verification_off():
            result = _not_available_result()  # OTP tools are not listed in "none" mode
        elif name == "send_otp":
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
