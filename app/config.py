"""All settings in one place.

Values come from environment variables, which are loaded from the
".env" file. Secrets (passwords, keys) must only ever live in ".env",
never in the code.
"""

import os

from dotenv import load_dotenv

# Read the .env file. Variables that are already set (for example by the
# tests) are NOT overwritten.
load_dotenv()


def _text(name, default=""):
    """Read a text setting, trimming spaces."""
    return os.getenv(name, default).strip()


def _number(name, default):
    """Read a number setting. Falls back to the default if it is not a number."""
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return float(default)


def _yes_no(name, default="false"):
    """Read a true/false setting. "true", "yes" and "1" all count as true."""
    return _text(name, default).lower() in ("true", "yes", "1")


def _list(name):
    """Read a comma-separated setting: "Midsize, Premium" -> ["Midsize", "Premium"]."""
    return [item.strip() for item in _text(name).split(",") if item.strip()]


# --- MCP server --------------------------------------------------------
MCP_API_KEY = _text("MCP_API_KEY")
HOST = _text("HOST", "127.0.0.1")
PORT = int(_number("PORT", 8000))

# Keep-alive: Render's free plan sleeps after 15 minutes with no outside
# traffic, so the server calls its own PUBLIC health URL on a timer.
# Empty = switched off (local runs and tests).
KEEP_ALIVE_URL = _text("KEEP_ALIVE_URL")
KEEP_ALIVE_SECONDS = _number("KEEP_ALIVE_SECONDS", 300)  # every 5 minutes

# Show FastAPI's automatic API pages (/docs)? Off by default, because the
# server is public on Render.
ENABLE_DOCS = _yes_no("ENABLE_DOCS")

# --- DriveCarClub API --------------------------------------------------
# "mock" or "live". Anything else counts as "mock" (with a startup warning).
# DCC_MODE is for the booking tools (check_dispatch_status, get_chauffeur_details).
DCC_MODE = _text("DCC_MODE", "mock").lower()
# DCC_RATE_MODE is for get_fare only. Empty = the same as DCC_MODE. So fares
# can use the real API while booking lookups stay on mock data.
DCC_RATE_MODE = (_text("DCC_RATE_MODE") or DCC_MODE).lower()
DCC_TOKEN_URL = _text(
    "DCC_TOKEN_URL", "https://drivecarclub.com/CarclubCRMUAT/api/auth/Token"
)
DCC_BOOKING_URL = _text(
    "DCC_BOOKING_URL",
    "https://drivecarclub.com/CarclubCRMUAT/Api/Home/DespatchedBooking",
)
DCC_RATE_URL = _text(
    "DCC_RATE_URL",
    "https://drivecarclub.com/CarclubcrmUAT/Api/Home/GetRate",
)
DCC_USERNAME = _text("DCC_USERNAME")
DCC_PASSWORD = _text("DCC_PASSWORD")
DCC_TOKEN_HEADER = _text("DCC_TOKEN_HEADER", "Authorization")
DCC_TOKEN_PREFIX = _text("DCC_TOKEN_PREFIX", "Bearer")
DCC_TOKEN_TTL_SECONDS = _number("DCC_TOKEN_TTL_SECONDS", 3000)
DCC_TIMEOUT_SECONDS = _number("DCC_TIMEOUT_SECONDS", 5)

# Which booking field is read out as the pickup area: "city_Name" or
# "location". DriveCarClub hasn't confirmed which is right. The full
# address (reportingAddress) is never an option.
PICKUP_AREA_CHOICES = ("city_Name", "location")
PICKUP_AREA_FIELD = _text("PICKUP_AREA_FIELD", "city_Name")

# UAT TESTING ONLY: also show pickups from before today (the UAT test
# booking is in the past). Must be "false" in production.
INCLUDE_PAST_BOOKINGS = _yes_no("INCLUDE_PAST_BOOKINGS")

# --- Fares (get_fare tool) ---------------------------------------------
# Optional allow-lists, comma-separated (for example "Midsize, Premium").
# Empty = no check, because DriveCarClub hasn't given us the lists yet.
FARE_CAR_CATEGORIES = _list("FARE_CAR_CATEGORIES")
FARE_SERVICE_TYPES = _list("FARE_SERVICE_TYPES")
FARE_CITIES = _list("FARE_CITIES")
# Added to every fare answer.
FARE_NOTE = _text(
    "FARE_NOTE",
    "Tolls, parking and taxes may be extra. For contract rates or an exact quote, "
    "please call our customer support team.",
)

# --- OTP for website chat (SOP 5 rule 1) -------------------------------
# "mock" = no SMS is sent and the code is always 123456 (testing only).
# "plivo" = a real SMS is sent through Plivo.
OTP_MODE = _text("OTP_MODE", "mock").lower()
# Secret used to hash the codes, so the real code is never stored.
# If empty, a random secret is made at startup (fine, because sessions
# are only kept in memory anyway).
OTP_SECRET = _text("OTP_SECRET")
# The SMS text. "{code}" is replaced with the 6-digit code.
OTP_SMS_TEXT = _text(
    "OTP_SMS_TEXT",
    "Your DriveCarClub verification code is {code}. It expires in 5 minutes. Do not share it.",
)
PLIVO_AUTH_ID = _text("PLIVO_AUTH_ID")
PLIVO_AUTH_TOKEN = _text("PLIVO_AUTH_TOKEN")
PLIVO_SENDER_ID = _text("PLIVO_SENDER_ID")

# --- Verification on website chat --------------------------------------
# "otp"  = the customer proves their number with a code (default).
# "none" = UAT DEMOS ONLY: no code, the number the customer types is
#          trusted. The server refuses to start with "none" unless
#          DCC_BOOKING_URL is a UAT URL (see server.py). The OTP code stays
#          in place, so switching back is just VERIFICATION_MODE=otp.
VERIFICATION_MODE = _text("VERIFICATION_MODE", "otp").lower()

# Development only: trust the phone number on website chat without OTP.
TRUST_WEB_PHONE = _yes_no("TRUST_WEB_PHONE")

# All dates and times are in Indian time.
TIMEZONE = "Asia/Kolkata"

# --- Business decisions (changed in code, on purpose) ------------------
# Should the chauffeur's mobile number be shared with the customer?
# Still undecided by DriveCarClub (SOP 6, open item 2). Keep False until
# they confirm in writing.
SHARE_CHAUFFEUR_MOBILE = False

# OTP limits for one conversation.
OTP_VALID_SECONDS = 5 * 60      # a code works for 5 minutes
OTP_MAX_SENDS = 3               # at most 3 codes sent
OTP_RESEND_WAIT_SECONDS = 30    # at least 30 seconds between sends
OTP_MAX_WRONG_ATTEMPTS = 3      # 3 wrong codes = locked
MOCK_OTP_CODE = "123456"        # the code in OTP_MODE=mock
