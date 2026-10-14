"""The DriveBot test website: the page itself and its small login API.

  GET  /                   web/index.html (and any other files in web/)
  GET  /api/session        who is signed in, plus the chat_token for Perfox
  POST /api/login/send-code  text a login code (same OTP rules as the chat)
  POST /api/login/verify     check the code: signed in
  POST /api/login            UAT DEMOS ONLY (VERIFICATION_MODE=none): signed
                             in straight away, no code
  POST /api/logout           sign out (the chat needs OTP again)
  GET  /api/my-pickups     today's and upcoming pickups (signed in only)

The browser is recognised by the HttpOnly cookie "dcc_sid" (see
browser_sessions.py). The full mobile number is never sent to the page,
only "••••0001". Codes, cookies and tokens are never logged.

VERIFICATION_MODE decides which login works: "otp" has send-code and
verify, "none" has /api/login. The other endpoints answer 404.
"""

from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import browser_sessions, identity, log, mapper, otp, tools
from app.errors import DCCUnavailable, SMSUnavailable

COOKIE_NAME = "dcc_sid"
WEB_DIR = Path(__file__).resolve().parent.parent / "web"

router = APIRouter()


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def check_same_origin(request: Request):
    """Basic CSRF protection for the POST endpoints.

    Browsers always send an "Origin" header with a POST. If it isn't our own
    site, another website is trying to post with the customer's cookie,
    so we refuse.
    """
    origin = request.headers.get("origin")
    our_hosts = {request.headers.get("host"), request.headers.get("x-forwarded-host")}
    if not origin or urlsplit(origin).netloc not in our_hosts:
        log.warning("web_bad_origin", path=request.url.path)
        raise HTTPException(status_code=403, detail="Request refused.")


def only_with_otp():
    """For the code login: 404 when VERIFICATION_MODE=none."""
    if identity.verification_off():
        raise HTTPException(status_code=404, detail="Not Found")


def only_without_otp():
    """For the one-step login: 404 unless VERIFICATION_MODE=none."""
    if not identity.verification_off():
        raise HTTPException(status_code=404, detail="Not Found")


def _load_session(request):
    """Return (sid, session, is_new) for this browser's cookie."""
    return browser_sessions.get_or_create(request.cookies.get(COOKIE_NAME))


def _reply(body, status=200, set_sid=None):
    """A JSON answer that is never cached. Sets the cookie when asked to."""
    response = JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store"})
    if set_sid:
        response.set_cookie(
            COOKIE_NAME, set_sid,
            httponly=True,   # JavaScript can't read it
            secure=True,     # only sent over HTTPS
            samesite="lax",  # not sent with posts from other websites
            path="/",
        )
    return response


def _support_text():
    # Shown on the page as is. The support number is not kept on this
    # server (it lives in the Perfox knowledge base), so none is given here.
    return "Please call our customer support team."


# ----------------------------------------------------------------------
# Session
# ----------------------------------------------------------------------


@router.get("/api/session")
def get_session(request: Request):
    sid, session, is_new = _load_session(request)
    body = {
        "signed_in": bool(session["mobile"]),
        "masked_mobile": browser_sessions.masked(session["mobile"]),
        "chat_token": session["chat_token"],  # for Perfox 'identify'
        # Which login the page shows: two steps ("otp") or one ("none").
        "verification": "none" if identity.verification_off() else "otp",
    }
    return _reply(body, set_sid=sid if is_new else None)


# ----------------------------------------------------------------------
# Log in with a code (same OTP rules and limits as the chat)
# ----------------------------------------------------------------------


class SendCodeBody(BaseModel):
    mobile: str = ""


class VerifyBody(BaseModel):
    code: str = ""


def _sign_in(sid, session, mobile):
    """Sign the browser session in: save the number and give a new cookie.

    The chat_token stays the same, so the chat sees the login at once.
    """
    session["mobile"] = mobile
    new_sid = browser_sessions.change_sid(sid)  # new cookie after logging in
    return _reply({"outcome": "verified", "signed_in": True,
                   "masked_mobile": browser_sessions.masked(mobile)}, 200, new_sid)


def _invalid_number_reply(set_sid):
    return _reply({"outcome": "invalid_number",
                   "message": "Please enter a valid 10-digit Indian mobile number."}, 400, set_sid)


# only_with_otp comes first, so in "none" mode these are 404 before anything else.
@router.post("/api/login/send-code", dependencies=[Depends(only_with_otp), Depends(check_same_origin)])
def send_code(body: SendCodeBody, request: Request):
    sid, session, is_new = _load_session(request)
    set_sid = sid if is_new else None

    mobile = identity.normalise_phone(body.mobile)
    if not mobile:
        log.info("web_login", step="send_code", outcome="invalid_number")
        return _invalid_number_reply(set_sid)

    try:
        outcome = otp.send_code(session, mobile)
    except SMSUnavailable as failure:
        log.alert_perfox_team(f"sms_{failure.reason}", tool="web_login")
        return _reply({"outcome": "unavailable",
                       "message": f"We can't send a code right now. {_support_text()}"}, 503, set_sid)

    log.info("web_login", step="send_code", mobile=log.mask_mobile(mobile), outcome=outcome)
    if outcome == "sent":
        return _reply({"outcome": "sent",
                       "message": "We've sent a 6-digit code by SMS. It expires in 5 minutes."}, 200, set_sid)
    if outcome == "locked":
        return _reply({"outcome": "locked",
                       "message": f"Too many wrong codes, so you can't log in for now. {_support_text()}"}, 429, set_sid)
    return _reply({"outcome": "too_many_requests",
                   "message": "Please wait a moment before asking for another code."}, 429, set_sid)


@router.post("/api/login/verify", dependencies=[Depends(only_with_otp), Depends(check_same_origin)])
def verify_code(body: VerifyBody, request: Request):
    sid, session, is_new = _load_session(request)
    set_sid = sid if is_new else None
    mobile = otp.pending_mobile(session)  # read before check_code clears it

    outcome, details = otp.check_code(session, body.code)
    log.info("web_login", step="verify", mobile=log.mask_mobile(mobile), outcome=outcome)

    if outcome == "verified":
        return _sign_in(sid, session, details["mobile"])
    if outcome == "wrong_code":
        left = details["attempts_left"]
        return _reply({"outcome": "wrong_code", "attempts_left": left,
                       "message": f"That code doesn't match. {left} {'try' if left == 1 else 'tries'} left."},
                      400, set_sid)
    if outcome == "locked":
        return _reply({"outcome": "locked",
                       "message": f"Too many wrong codes, so you can't log in for now. {_support_text()}"}, 429, set_sid)
    return _reply({"outcome": "expired",
                   "message": "That code has expired. Please ask for a new one."}, 400, set_sid)


# ----------------------------------------------------------------------
# Log in without a code (UAT DEMOS ONLY, VERIFICATION_MODE=none)
# ----------------------------------------------------------------------


class LoginBody(BaseModel):
    mobile: str = ""


@router.post("/api/login", dependencies=[Depends(only_without_otp), Depends(check_same_origin)])
def login_without_code(body: LoginBody, request: Request):
    sid, session, is_new = _load_session(request)

    mobile = identity.normalise_phone(body.mobile)
    if not mobile:
        log.info("web_login", step="no_code", outcome="invalid_number")
        return _invalid_number_reply(sid if is_new else None)

    log.info("web_login", step="no_code", mobile=log.mask_mobile(mobile), outcome="verified")
    return _sign_in(sid, session, mobile)


@router.post("/api/logout", dependencies=[Depends(check_same_origin)])
def logout(request: Request):
    sid, session, is_new = _load_session(request)
    mobile = session["mobile"]
    browser_sessions.log_out(session)  # also gives a new chat_token
    log.info("web_logout", mobile=log.mask_mobile(mobile))
    return _reply({"signed_in": False}, set_sid=sid if is_new else None)


# ----------------------------------------------------------------------
# Your pickups
# ----------------------------------------------------------------------


def _pickup_for_page(booking):
    """The same safe fields as check_dispatch_status, plus the chauffeur's
    first name and car when a chauffeur is allocated (as in SOP 6)."""
    item = mapper.dispatch_summary(booking)
    item["when"] = mapper.friendly_when(booking)
    if item["chauffeur_allocated"]:
        details = mapper.chauffeur_details(booking)
        for field in ("chauffeur_first_name", "car_model", "car_colour", "registration_number"):
            if field in details:  # car_colour is left out when there is none
                item[field] = details[field]
    return item


@router.get("/api/my-pickups")
def my_pickups(request: Request):
    sid, session, is_new = _load_session(request)
    set_sid = sid if is_new else None
    mobile = session["mobile"]
    if not mobile:
        return _reply({"message": "Please log in to see your pickups."}, 401, set_sid)

    try:
        bookings = tools.load_current_bookings(mobile)
    except DCCUnavailable as failure:
        log.alert_perfox_team(failure.reason, tool="web_my_pickups")
        return _reply({"message": f"We can't load your pickups right now. {_support_text()}"}, 503, set_sid)

    log.info("web_my_pickups", mobile=log.mask_mobile(mobile), count=len(bookings))
    return _reply({"pickups": [_pickup_for_page(b) for b in bookings]}, set_sid=set_sid)


# ----------------------------------------------------------------------
# The page itself
# ----------------------------------------------------------------------


def serve_static_files(app):
    """Serve web/ at "/" (index.html for "/"). Call this AFTER all other routes."""
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
