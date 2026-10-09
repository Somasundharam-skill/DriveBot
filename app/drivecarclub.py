"""Talks to the real DriveCarClub API (used when DCC_MODE=live).

Two calls (see the API reference in the SOP document):
  1. Token API: sign in with HTTP Basic auth and get an access token.
  2. DespatchedBooking API: get the bookings for a mobile number.

The token is kept (cached) and reused until 60 seconds before it expires.
Any problem raises DCCUnavailable, so the tool can say "unavailable"
instead of guessing.
"""

import threading
import time

import requests

from app import config, log, mapper
from app.errors import DCCUnavailable

# Get a new token this many seconds BEFORE the old one expires.
REFRESH_EARLY_SECONDS = 60

# Places the token might be in the Token API's JSON response.
TOKEN_KEYS = ["access_token", "token", "Token", "AccessToken"]


class DriveCarClubClient:
    def __init__(
        self,
        token_url=None,
        booking_url=None,
        username=None,
        password=None,
        timeout=None,
        token_ttl=None,
        token_header=None,
        token_prefix=None,
    ):
        # Anything not passed in comes from the .env settings.
        # (The tests pass their own values to talk to a fake server.)
        self.token_url = token_url or config.DCC_TOKEN_URL
        self.booking_url = booking_url or config.DCC_BOOKING_URL
        self.username = username if username is not None else config.DCC_USERNAME
        self.password = password if password is not None else config.DCC_PASSWORD
        self.timeout = timeout or config.DCC_TIMEOUT_SECONDS
        self.token_ttl = token_ttl or config.DCC_TOKEN_TTL_SECONDS
        self.token_header = token_header or config.DCC_TOKEN_HEADER
        self.token_prefix = token_prefix if token_prefix is not None else config.DCC_TOKEN_PREFIX

        # The cached token, and when (time.monotonic) it expires.
        self._token = None
        self._token_expires_at = 0.0
        # Several conversations can run at once. The lock makes sure only
        # one of them fetches a new token at a time.
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Token
    # ------------------------------------------------------------------
    def get_token(self):
        """Return a valid token, fetching a new one only when needed."""
        with self._lock:
            still_fresh = time.monotonic() < self._token_expires_at - REFRESH_EARLY_SECONDS
            if self._token and still_fresh:
                return self._token
            self._token, lifetime = self._fetch_token()
            self._token_expires_at = time.monotonic() + lifetime
            return self._token

    def forget_token(self, old_token):
        """Throw away a token DriveCarClub rejected (only if nobody replaced it yet)."""
        with self._lock:
            if self._token == old_token:
                self._token = None
                self._token_expires_at = 0.0

    def _fetch_token(self):
        """Call the Token API. Returns (token, lifetime_in_seconds)."""
        try:
            response = requests.post(
                self.token_url,
                auth=(self.username, self.password),
                timeout=self.timeout,
            )
        except requests.Timeout:
            raise DCCUnavailable("token_timeout")
        except requests.RequestException:
            raise DCCUnavailable("token_network_error")

        if response.status_code != 200:
            raise DCCUnavailable(f"token_http_{response.status_code}")

        token, expires_in = self._read_token_response(response)
        if not token:
            raise DCCUnavailable("token_bad_response")
        lifetime = expires_in or self.token_ttl
        log.info("dcc_token_refreshed", lifetime_seconds=lifetime)  # never log the token
        return token, lifetime

    @staticmethod
    def _read_token_response(response):
        """Find the token in the response. The format is not confirmed yet,
        so we accept a plain string or JSON with one of several key names.
        Returns (token, expires_in_seconds or None).
        """
        try:
            body = response.json()
        except ValueError:
            # Not JSON: treat the whole body as the token.
            return response.text.strip().strip('"'), None

        if isinstance(body, str):
            return body.strip(), None
        if not isinstance(body, dict):
            return None, None

        token = None
        for key in TOKEN_KEYS:
            if isinstance(body.get(key), str) and body[key].strip():
                token = body[key].strip()
                break
        data = body.get("data")
        if token is None and isinstance(data, dict) and isinstance(data.get("token"), str):
            token = data["token"].strip()

        expires_in = None
        try:
            if body.get("expires_in") is not None:
                expires_in = float(body["expires_in"])
        except (TypeError, ValueError):
            expires_in = None
        return token, expires_in

    # ------------------------------------------------------------------
    # Bookings
    # ------------------------------------------------------------------
    def get_bookings(self, contact_no):
        """Return the raw list of dispatched bookings for "91XXXXXXXXXX".

        If the token has expired (401) we get a new one and try ONCE more.
        """
        token = self.get_token()
        response = self._post_booking_request(contact_no, token)

        if response.status_code == 401:
            log.info("dcc_token_rejected_retrying")
            self.forget_token(token)
            token = self.get_token()
            response = self._post_booking_request(contact_no, token)
            if response.status_code == 401:
                raise DCCUnavailable("auth_failed")

        if response.status_code >= 500:
            raise DCCUnavailable("server_error")
        if response.status_code != 200:
            raise DCCUnavailable(f"http_{response.status_code}")

        try:
            body = response.json()
        except ValueError:
            raise DCCUnavailable("bad_response")
        bookings = mapper.unwrap_booking_list(body)
        if bookings is None:
            raise DCCUnavailable("bad_response")
        return bookings

    def _post_booking_request(self, contact_no, token):
        """One call to the DespatchedBooking API."""
        header_value = f"{self.token_prefix} {token}" if self.token_prefix else token
        try:
            return requests.post(
                self.booking_url,
                json={"ContactNo": contact_no},
                headers={self.token_header: header_value},
                timeout=self.timeout,
            )
        except requests.Timeout:
            raise DCCUnavailable("timeout")
        except requests.RequestException:
            raise DCCUnavailable("network_error")
