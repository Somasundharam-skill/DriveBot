"""Sending SMS messages (used for the OTP code).

OTP_MODE in .env picks how:
  "mock"   nothing is sent (testing only; the code is always 123456)
  "plivo"  a real SMS through Plivo's Message API

To add another SMS provider later, write one more send_with_... function
and pick it in send_sms() below.
"""

import requests

from app import config
from app.errors import SMSUnavailable

PLIVO_API_BASE = "https://api.plivo.com"


def send_sms(mobile, text):
    """Send "text" to "mobile" (91 + 10 digits).

    Raises SMSUnavailable if the message could not be sent.
    """
    if config.OTP_MODE == "plivo":
        send_with_plivo(mobile, text)
    # mock (or an unknown mode): send nothing. A warning is logged at startup.


def send_with_plivo(mobile, text, auth_id=None, auth_token=None, sender_id=None,
                    base_url=PLIVO_API_BASE, timeout=None):
    """Send one SMS through Plivo.

    Plivo docs: POST /v1/Account/{auth_id}/Message/ with HTTP Basic auth
    (Auth ID + Auth Token) and a JSON body with src, dst and text.
    The settings come from .env unless passed in (the tests pass them in).
    """
    auth_id = auth_id or config.PLIVO_AUTH_ID
    auth_token = auth_token or config.PLIVO_AUTH_TOKEN
    sender_id = sender_id or config.PLIVO_SENDER_ID
    if not (auth_id and auth_token and sender_id):
        raise SMSUnavailable("plivo_not_configured")

    url = f"{base_url}/v1/Account/{auth_id}/Message/"
    body = {
        "src": sender_id,
        "dst": "+" + mobile,  # Plivo wants E.164 format: +919876543210
        "text": text,
    }
    try:
        reply = requests.post(
            url, json=body, auth=(auth_id, auth_token),
            timeout=timeout or config.DCC_TIMEOUT_SECONDS,
        )
    except requests.Timeout:
        raise SMSUnavailable("timeout")
    except requests.RequestException:
        raise SMSUnavailable("network_error")

    # Plivo answers 202 ("message(s) queued") when it accepts the message.
    if not 200 <= reply.status_code < 300:
        raise SMSUnavailable(f"http_{reply.status_code}")
