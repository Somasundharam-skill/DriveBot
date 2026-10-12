"""One-time codes (OTP) for website chat (SOP 5 rule 1).

On website chat anyone can type any number, so before DriveBot shares
booking details the customer proves the number is theirs:
  1. send_code():  we text a 6-digit code to the number they typed.
  2. check_code(): they tell DriveBot the code. If it matches, it returns
                   the number the code was sent to, and the caller saves it.

The same functions work for a chat conversation (send_otp / verify_otp
tools) and for a website login (website.py): "session" is just a dict.

What is kept in the session (never the code itself):
  session["otp"]              the pending code: its hash, the number it was
                              sent to, and when it expires
  session["otp_sends"]        how many codes were sent in this conversation
  session["otp_last_sent_at"] when the last code was sent
  session["otp_wrong"]        how many wrong codes were typed
  session["otp_locked"]       True after too many wrong codes
"""

import hashlib
import hmac
import re
import secrets
import threading
import time

from app import config, sms

# The secret used to hash codes. A random one is made if .env has none.
_SECRET = (config.OTP_SECRET or secrets.token_hex(32)).encode("utf-8")

# Only one OTP change at a time, so two quick messages can't both pass a limit.
_lock = threading.Lock()


def _hash(mobile, code):
    """A one-way fingerprint of the code. The code can't be worked out from it."""
    message = f"{mobile}:{code}".encode("utf-8")
    return hmac.new(_SECRET, message, hashlib.sha256).hexdigest()


def _new_code():
    """A random 6-digit code (always 123456 in mock mode)."""
    if config.OTP_MODE == "plivo":
        return f"{secrets.randbelow(1_000_000):06d}"  # 000000 to 999999
    return config.MOCK_OTP_CODE


def send_code(session, mobile):
    """Text a new code to "mobile" (already normalised).

    Returns "sent", "locked" or "too_many_requests".
    Raises SMSUnavailable if the SMS could not be sent.
    """
    with _lock:
        if session.get("otp_locked"):
            return "locked"

        now = time.time()
        sends = session.get("otp_sends", 0)
        last_sent_at = session.get("otp_last_sent_at")
        if sends >= config.OTP_MAX_SENDS:
            return "too_many_requests"
        if last_sent_at and now - last_sent_at < config.OTP_RESEND_WAIT_SECONDS:
            return "too_many_requests"

        # Count the attempt BEFORE sending, so failed sends can't be
        # repeated forever (each SMS costs money).
        session["otp_sends"] = sends + 1
        session["otp_last_sent_at"] = now

    # Send outside the lock: the SMS can take a few seconds, and other
    # conversations shouldn't have to wait for it.
    code = _new_code()
    sms.send_sms(mobile, config.OTP_SMS_TEXT.format(code=code))

    with _lock:
        # A new code replaces any older one.
        session["otp"] = {
            "hash": _hash(mobile, code),
            "mobile": mobile,
            "expires_at": now + config.OTP_VALID_SECONDS,
        }
        return "sent"


def check_code(session, code):
    """Check the code the customer typed.

    Returns (outcome, details):
      ("verified", {"mobile": ...})          right code: the caller saves the number
      ("wrong_code", {"attempts_left": n})   try again
      ("locked", {})                         too many wrong codes
      ("expired", {})                        no code waiting, or too old
    """
    with _lock:
        if session.get("otp_locked"):
            return "locked", {}

        pending = session.get("otp")
        if not pending or time.time() > pending["expires_at"]:
            session["otp"] = None
            return "expired", {}

        typed = re.sub(r"\D", "", code or "")  # "123 456" -> "123456"
        # compare_digest takes the same time however much of it matches.
        if hmac.compare_digest(_hash(pending["mobile"], typed), pending["hash"]):
            # The caller saves the number where it belongs: the chat
            # conversation (tools.py) or the browser session (website.py).
            session["otp"] = None
            return "verified", {"mobile": pending["mobile"]}

        wrong = session.get("otp_wrong", 0) + 1
        session["otp_wrong"] = wrong
        if wrong >= config.OTP_MAX_WRONG_ATTEMPTS:
            session["otp_locked"] = True
            session["otp"] = None
            return "locked", {}
        return "wrong_code", {"attempts_left": config.OTP_MAX_WRONG_ATTEMPTS - wrong}


def pending_mobile(session):
    """The number the current code was sent to (for masked logging), or None."""
    pending = session.get("otp")
    return pending["mobile"] if pending else None
