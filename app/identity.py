"""Who is the customer, and can we trust their phone number?

SOP 5 rule 1: DriveBot only looks up a TRUSTED number.
  - Phone call, WhatsApp, SMS: the number Perfox gives us is trusted.
  - Website chat: anyone can type any number, so it is only trusted after
    an OTP check: either a login on our website (browser_sessions.py,
    found through the X-Sa-User-Token header), or an OTP in the chat
    ("verified_mobile" on the conversation session).
The booking tools NEVER take the phone number from the AI's tool arguments.
The only place a typed number is accepted is send_otp, which just texts a
code to it and returns no booking data.
"""

import re
import threading
import time

from app import browser_sessions, config

# Channels where the phone number comes from the network, not from typing.
TRUSTED_CHANNELS = ("phone", "whatsapp", "sms")


def normalise_phone(raw):
    """Turn any Indian mobile format into 91 + 10 digits.

    "+91 98765 43210" -> "919876543210"
    "09876543210"     -> "919876543210"
    "9876543210"      -> "919876543210"
    Returns None if it does not look like an Indian mobile number.
    """
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)  # keep only the digits

    if len(digits) == 14 and digits.startswith("0091"):
        digits = digits[2:]  # 0091XXXXXXXXXX -> 91XXXXXXXXXX
    if len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]  # 0XXXXXXXXXX -> XXXXXXXXXX
    if len(digits) == 10:
        digits = "91" + digits

    if len(digits) == 12 and digits.startswith("91"):
        return digits
    return None


# --- Sessions ----------------------------------------------------------
# One small dict per Perfox conversation (X-Sa-Conversation-Id).
# Kept in memory for now; a restart clears it. The OTP step stores
# "verified_mobile" and the OTP data (see otp.py) here.
_sessions = {}
_sessions_lock = threading.Lock()


def get_session(conversation_id):
    """Return the session dict for a conversation, creating it if needed."""
    with _sessions_lock:
        if conversation_id not in _sessions:
            _sessions[conversation_id] = {
                "verified_mobile": None,
                "created_at": time.time(),
            }
        return _sessions[conversation_id]


def find_trusted_mobile(caller):
    """Decide which mobile number (if any) we may look up.

    "caller" is the dict of Perfox headers built in server.py.
    Returns (mobile, problem):
      ("919876543210", None)         -> OK, use this number
      (None, "verification_needed")  -> website chat, needs OTP first
      (None, "no_trusted_number")    -> nothing we can trust
    """
    channel = (caller.get("channel") or "").strip().lower()
    mobile = normalise_phone(caller.get("phone"))

    if channel in TRUSTED_CHANNELS:
        if mobile:
            return mobile, None
        return None, "no_trusted_number"

    if channel == "web":
        # Shared login: if Perfox passed our website's chat_token, the
        # website session decides, and any chat-level OTP is ignored. That
        # way logging out on the website also logs out the chat.
        status, browser = browser_sessions.find_by_chat_token(caller.get("user_token"))
        if status == "live":
            if browser["mobile"]:
                return browser["mobile"], None
            return None, "verification_needed"
        if status == "retired":
            return None, "verification_needed"

        # No website token (or one we don't know): chat-level OTP.
        session = get_session(caller.get("conversation_id"))
        if session.get("verified_mobile"):
            return session["verified_mobile"], None  # passed OTP
        if mobile and config.TRUST_WEB_PHONE:
            return mobile, None  # development shortcut only
        # With or without a phone header: ask for the number and send an OTP.
        return None, "verification_needed"

    # email, unknown or missing channel
    return None, "no_trusted_number"
