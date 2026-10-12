"""Website (browser) sessions, shared with the chatbot.

Every visitor to the test website gets a session, kept in memory:

  cookie "dcc_sid"  ->  {"mobile": None or the verified number,
                         "chat_token": "...",
                         "created_at": ..., "last_seen": ...,
                         plus the OTP data from otp.py while logging in}

There are two secret values per session, on purpose:
  - The cookie value (sid) never leaves the cookie. JavaScript can't read it
    (HttpOnly), so it is never in the page.
  - The chat_token is given to the page, which passes it to Perfox
    ('identify'). Perfox sends it back to /mcp as the X-Sa-User-Token header,
    so a tool call can find the same session.

Logging out on the website gives the session a NEW chat_token. The old one
is "retired": it still counts as signed out, so it can never fall back to
an older chat-level OTP.

A session that is not used for SESSION_IDLE_SECONDS (8 hours) is removed.
Everything is in memory: run only ONE server process.
"""

import secrets
import threading
import time

SESSION_IDLE_SECONDS = 8 * 60 * 60  # 8 hours without activity

_sessions = {}        # sid -> session dict
_by_chat_token = {}   # chat_token -> sid
_retired_tokens = {}  # chat_token -> when it was retired (always "signed out")
_lock = threading.Lock()


def _new_secret():
    return secrets.token_urlsafe(32)


def _remove_old(now):
    """Drop sessions and retired tokens that have been idle too long.

    Called with _lock held. Fine for a pilot with few visitors.
    """
    for sid, session in list(_sessions.items()):
        if now - session["last_seen"] > SESSION_IDLE_SECONDS:
            del _sessions[sid]
            token = session["chat_token"]
            _by_chat_token.pop(token, None)
            _retired_tokens[token] = now  # keep treating it as signed out for a while
    for token, retired_at in list(_retired_tokens.items()):
        if now - retired_at > SESSION_IDLE_SECONDS:
            del _retired_tokens[token]


def get_or_create(sid):
    """Return (sid, session, is_new) for this cookie value.

    A missing, unknown or expired cookie gets a brand-new session.
    """
    now = time.time()
    with _lock:
        _remove_old(now)
        session = _sessions.get(sid) if sid else None
        if session is not None:
            session["last_seen"] = now
            return sid, session, False

        sid = _new_secret()
        token = _new_secret()
        session = {"mobile": None, "chat_token": token, "created_at": now, "last_seen": now}
        _sessions[sid] = session
        _by_chat_token[token] = sid
        return sid, session, True


def change_sid(old_sid):
    """Give the session a new cookie value (done after logging in).

    Stops anyone who knew the old cookie value from using the signed-in
    session ("session fixation"). Returns the new sid.
    """
    with _lock:
        session = _sessions.pop(old_sid)
        new_sid = _new_secret()
        _sessions[new_sid] = session
        _by_chat_token[session["chat_token"]] = new_sid
        return new_sid


def log_out(session):
    """Sign the session out and give it a new chat_token."""
    with _lock:
        old_token = session["chat_token"]
        sid = _by_chat_token.pop(old_token, None)
        _retired_tokens[old_token] = time.time()

        session["mobile"] = None
        session["otp"] = None  # forget any half-finished login
        session["chat_token"] = _new_secret()
        if sid is not None:
            _by_chat_token[session["chat_token"]] = sid


def find_by_chat_token(token):
    """Look up the X-Sa-User-Token from a tool call.

    Returns (status, session):
      ("live", session)  a current browser session: it decides who is signed in
      ("retired", None)  an old token (after logout or expiry): signed out
      ("unknown", None)  no token, or one we never gave out
    """
    if not token:
        return "unknown", None
    now = time.time()
    with _lock:
        _remove_old(now)
        sid = _by_chat_token.get(token)
        if sid is not None:
            session = _sessions[sid]
            session["last_seen"] = now  # chatting counts as activity
            return "live", session
        if token in _retired_tokens:
            return "retired", None
        return "unknown", None


def masked(mobile):
    """What the website may show: "••••0001". Never the full number."""
    return "••••" + mobile[-4:] if mobile else None
