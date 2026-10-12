"""DriveBot MCP server tests.

Run with:  uv run python -m tests.run_tests

Prints PASS or FAIL for every check, and a total at the end.
"""

import os

# Settings for the tests. These MUST be set before the app is imported,
# because config.py reads them once when it is first imported.
os.environ["DCC_MODE"] = "mock"
os.environ["MCP_API_KEY"] = "test-key"
os.environ["TRUST_WEB_PHONE"] = "false"
os.environ["DCC_TIMEOUT_SECONDS"] = "1"
os.environ["OTP_MODE"] = "mock"  # no SMS; the code is always 123456
os.environ["KEEP_ALIVE_URL"] = ""  # never ping the real server from tests
os.environ["ENABLE_DOCS"] = "false"

import base64
import json
import logging
import re
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fastapi.testclient import TestClient

from app import browser_sessions, config, identity, otp, sms
from app.drivecarclub import DriveCarClubClient
from app.errors import DCCUnavailable, SMSUnavailable
from app.mapper import INDIA
from app.server import app

# ----------------------------------------------------------------------
# Tiny test helpers
# ----------------------------------------------------------------------
results = {"pass": 0, "fail": 0}


def check(name, condition, detail=""):
    if condition:
        results["pass"] += 1
        print(f"PASS  {name}")
    else:
        results["fail"] += 1
        print(f"FAIL  {name}  {detail}")


# Catch the server's log lines in a list (instead of printing them), so
# we can check what gets logged.
log_lines = []


class ListHandler(logging.Handler):
    def emit(self, record):
        log_lines.append(record.getMessage())


drivebot_logger = logging.getLogger("drivebot")
drivebot_logger.handlers = [ListHandler()]

client = TestClient(app)
AUTH = {"Authorization": "Bearer test-key"}


def rpc(method, params=None, headers=None, message_id=1):
    """Send one JSON-RPC message to /mcp and return the HTTP response."""
    body = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        body["params"] = params
    if message_id is not None:
        body["id"] = message_id
    return client.post("/mcp", json=body, headers={**AUTH, **(headers or {})})


def call(tool, arguments=None, phone=None, channel="phone", conversation_id="conv-test", user_token=None):
    """Call a tool the way Perfox does. Returns (result dict, raw text)."""
    headers = {"X-Sa-Conversation-Id": conversation_id}
    if channel:
        headers["X-Sa-Channel"] = channel
    if phone:
        headers["X-Sa-End-User-Phone"] = phone
    if user_token:
        headers["X-Sa-User-Token"] = user_token  # our website's chat_token, via Perfox
    response = rpc("tools/call", {"name": tool, "arguments": arguments or {}}, headers)
    text = response.json()["result"]["content"][0]["text"]
    return json.loads(text), text


def contains_none(text, words):
    return not any(word in text for word in words)


# ----------------------------------------------------------------------
# 1. MCP wire contract
# ----------------------------------------------------------------------
print("\n--- MCP protocol ---")

response = rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}})
body = response.json()
check("initialize works", response.status_code == 200 and body["result"]["serverInfo"]["name"] == "drivebot-mcp")
check("initialize echoes protocolVersion", body["result"]["protocolVersion"] == "2025-03-26")
check("initialize sets mcp-session-id header", bool(response.headers.get("mcp-session-id")))
check("initialize declares tools capability", body["result"]["capabilities"] == {"tools": {}})

response = rpc("initialize", {})
check("initialize defaults protocolVersion to 2024-11-05",
      response.json()["result"]["protocolVersion"] == "2024-11-05")

response = rpc("tools/list")
tool_list = response.json()["result"]["tools"]
check("tools/list returns 4 tools", len(tool_list) == 4)
check("tools/list has the OTP tools",
      {t["name"] for t in tool_list} == {"check_dispatch_status", "get_chauffeur_details", "send_otp", "verify_otp"})
check("tools have name, description, input_schema",
      all(t.get("name") and t.get("description") and t.get("input_schema") for t in tool_list))

response = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                       headers={"Authorization": "Bearer wrong-key"})
check("wrong API key gets 401", response.status_code == 401)
response = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
check("missing API key gets 401", response.status_code == 401)

response = rpc("ping")
check("ping returns {}", response.json()["result"] == {})

response = rpc("notifications/initialized", message_id=None)
check("notification gets 202 with empty body", response.status_code == 202 and response.content == b"")

response = rpc("resources/list")
check("unknown method gets -32601", response.json()["error"]["code"] == -32601)

response = rpc("tools/call", {"name": "book_a_car", "arguments": {}})
check("unknown tool gets a JSON-RPC error", "error" in response.json())

response = rpc("tools/call", {"name": "check_dispatch_status", "arguments": {}},
               {"X-Sa-Channel": "phone", "X-Sa-End-User-Phone": "919000000001", "X-Sa-Conversation-Id": "c"})
check("tools/call returns isError false and text content",
      response.json()["result"]["isError"] is False
      and response.json()["result"]["content"][0]["type"] == "text")

response = client.get("/health")
check("health returns ok and mode", response.json() == {"ok": True, "mode": "mock"})

# ----------------------------------------------------------------------
# 2. check_dispatch_status (SOP 5)
# ----------------------------------------------------------------------
print("\n--- check_dispatch_status ---")

result, text = call("check_dispatch_status", phone="919000000001")
check("one booking -> outcome 'one'", result["outcome"] == "one", text)
check("one booking -> chauffeur_allocated true", result.get("booking", {}).get("chauffeur_allocated") is True)
check("one booking -> pickup area only", result.get("booking", {}).get("pickup_area") == "Koramangala")
check("one booking -> 'say' confirms car sent and offers details",
      "has been sent" in result["say"] and "chauffeur" in result["say"])
check("summary has no chauffeur name",
      contains_none(text, ["Ramesh", "Kumar"]), text)
check("summary has no full address, drop or driver mobile",
      contains_none(text, ["Prestige", "Flat", "Airport", "9845012345"]), text)
check("summary has only the allowlisted fields",
      set(result["booking"]) == {"booking_ref", "pickup_date", "pickup_time", "pickup_area",
                                 "dispatch_status", "chauffeur_allocated"})
today = datetime.now(INDIA).date().isoformat()
check("mock date is today in Asia/Kolkata", result["booking"]["pickup_date"] == today)

result, text = call("check_dispatch_status", phone="919000000002")
check("several bookings -> outcome 'several'", result["outcome"] == "several", text)
check("yesterday's completed trip hidden (2 current)", len(result.get("bookings", [])) == 2)
check("several sorted earliest first",
      [b["booking_ref"] for b in result.get("bookings", [])] == ["DCC100202", "DCC100203"])
check("several -> asks which pickup", "Which pickup" in result["say"])
check("several -> no chauffeur names, addresses or drop",
      contains_none(text, ["Mohammed", "Anil", "Sharma", "Prestige", "Airport"]), text)

result, text = call("check_dispatch_status", phone="919000000003")
check("no bookings -> outcome 'none'", result["outcome"] == "none", text)
check("none -> SOP wording", result["say"].startswith("I can't see a car sent for this number yet."))
check("none -> offers support number", "customer support" in result["say"])

result, text = call("check_dispatch_status", phone="919000000004")
check("not allocated booking -> 'one' with chauffeur_allocated false",
      result["outcome"] == "one" and result["booking"]["chauffeur_allocated"] is False, text)

log_lines.clear()
result, text = call("check_dispatch_status", phone="919000000005")
check("server error -> 'unavailable'", result["outcome"] == "unavailable" and result["reason"] == "server_error", text)
check("server error -> alert_perfox_team logged", any("alert_perfox_team" in line for line in log_lines))

started = time.monotonic()
result, text = call("check_dispatch_status", phone="919000000006")
check("timeout -> 'unavailable'", result["outcome"] == "unavailable" and result["reason"] == "timeout", text)
check("timeout -> gives up after DCC_TIMEOUT_SECONDS", time.monotonic() - started < 3)

# ----------------------------------------------------------------------
# 3. Trusted number rules
# ----------------------------------------------------------------------
print("\n--- trusted number ---")

result, text = call("check_dispatch_status", phone="919000000001", channel="web", conversation_id="web-1")
check("web chat without OTP -> verification_needed", result["outcome"] == "verification_needed", text)
check("verification_needed leaks no booking", "booking" not in result and "Koramangala" not in text)

identity.get_session("web-2")["verified_mobile"] = "919000000001"
result, text = call("check_dispatch_status", phone="919000000001", channel="web", conversation_id="web-2")
check("web chat after OTP (verified_mobile) -> works", result["outcome"] == "one", text)

result, text = call("check_dispatch_status", phone=None, channel="phone")
check("no phone -> unavailable / no_trusted_number",
      result["outcome"] == "unavailable" and result["reason"] == "no_trusted_number", text)

result, text = call("check_dispatch_status", phone="919000000001", channel="email")
check("email channel -> unavailable / no_trusted_number", result.get("reason") == "no_trusted_number", text)

result, text = call("check_dispatch_status", phone="919000000001", channel=None)
check("missing channel -> unavailable", result["outcome"] == "unavailable", text)

result, text = call("check_dispatch_status", {"ContactNo": "919000000002", "mobile": "919000000002"},
                    phone="919000000003")
check("phone number in tool arguments is ignored", result["outcome"] == "none", text)

result, _ = call("check_dispatch_status", phone="+91 90000 00001", channel="whatsapp")
check("'+91 90000 00001' is normalised", result["outcome"] == "one")
result, _ = call("check_dispatch_status", phone="09000000001", channel="sms")
check("'09000000001' is normalised", result["outcome"] == "one")

check("normalise '+91 98765 43210'", identity.normalise_phone("+91 98765 43210") == "919876543210")
check("normalise '09876543210'", identity.normalise_phone("09876543210") == "919876543210")
check("normalise '9876543210'", identity.normalise_phone("9876543210") == "919876543210")
check("normalise rejects '12345'", identity.normalise_phone("12345") is None)

# ----------------------------------------------------------------------
# 4. get_chauffeur_details (SOP 6)
# ----------------------------------------------------------------------
print("\n--- get_chauffeur_details ---")

result, text = call("get_chauffeur_details", {"booking_ref": "DCC100101"}, phone="919000000001")
details = result.get("chauffeur", {})
check("chauffeur shared", result["outcome"] == "shared", text)
check("first name only", details.get("chauffeur_first_name") == "Ramesh")
check("no surname anywhere", "Kumar" not in text, text)
check("no chauffeur mobile (SHARE_CHAUFFEUR_MOBILE is False)",
      "chauffeur_mobile" not in details and "9845012345" not in text)
check("car model, colour and registration shared",
      details.get("car_model") == "Toyota Innova Crysta" and details.get("car_colour") == "White"
      and details.get("registration_number") == "KA 01 AB 1234")
check("only the allowlisted chauffeur fields",
      set(details) == {"booking_ref", "pickup_date", "pickup_time", "chauffeur_first_name",
                       "car_model", "car_colour", "registration_number"})
check("no address or drop location", contains_none(text, ["Prestige", "Airport", "Koramangala"]), text)

result, text = call("get_chauffeur_details", {"booking_ref": "DCC100401"}, phone="919000000004")
check("not allocated -> 'not_allocated'", result["outcome"] == "not_allocated", text)
check("not allocated -> SOP wording",
      result["say"].startswith("Your chauffeur hasn't been assigned yet. You'll get the details once they are."))

result, text = call("get_chauffeur_details", {"booking_ref": "DCC100202"}, phone="919000000001")
check("someone else's booking -> not_found", result["outcome"] == "not_found", text)
check("someone else's booking leaks nothing",
      contains_none(text, ["Mohammed", "Irfan", "Maruti", "KA 51", "HSR", "DCC100202"]), text)

result, text = call("get_chauffeur_details", {"booking_ref": "dcc100202"}, phone="919000000002")
check("choose one of several with lower-case ref -> shared",
      result["outcome"] == "shared" and result["chauffeur"]["booking_ref"] == "DCC100202", text)
check("multi-word name -> first word only",
      result.get("chauffeur", {}).get("chauffeur_first_name") == "Mohammed" and "Irfan" not in text)

result, text = call("get_chauffeur_details", {"booking_ref": "DCC100201"}, phone="919000000002")
check("yesterday's completed booking -> not_found", result["outcome"] == "not_found", text)

result, text = call("get_chauffeur_details", {"booking_ref": ""}, phone="919000000001")
check("empty booking_ref -> not_found", result["outcome"] == "not_found", text)

result, text = call("get_chauffeur_details", {"booking_ref": "DCC100101"}, phone="919000000001", channel="web")
check("chauffeur on web without OTP -> verification_needed", result["outcome"] == "verification_needed", text)

result, text = call("get_chauffeur_details", {"booking_ref": "DCC100101"}, phone="919000000005")
check("chauffeur with server error -> unavailable", result["outcome"] == "unavailable", text)

# ----------------------------------------------------------------------
# 5. OTP on website chat (SOP 5 rule 1)
# ----------------------------------------------------------------------
print("\n--- OTP (website chat) ---")

MOCK_CODE = config.MOCK_OTP_CODE
otp_texts = []  # every OTP-related tool result, to check the code never appears
log_lines.clear()


def web(tool, arguments=None, conversation_id="otp-1", phone=None):
    """Call a tool on website chat and remember the result text."""
    result, text = call(tool, arguments, phone=phone, channel="web", conversation_id=conversation_id)
    otp_texts.append(text)
    return result, text


def let_30_seconds_pass(conversation_id):
    """Pretend the 30-second wait between sends is over."""
    identity.get_session(conversation_id)["otp_last_sent_at"] -= 31


# No OTP yet: booking tools must still refuse (TRUST_WEB_PHONE is false).
check("TRUST_WEB_PHONE is false in the tests", config.TRUST_WEB_PHONE is False)
result, text = web("check_dispatch_status", phone="919000000001")
check("no OTP -> check_dispatch_status verification_needed", result["outcome"] == "verification_needed", text)
check("verification_needed asks for the number and points to send_otp",
      "mobile number" in result["say"] and "send_otp" in result.get("next_step", ""), text)
result, text = web("get_chauffeur_details", {"booking_ref": "DCC100101"}, phone="919000000001")
check("no OTP -> get_chauffeur_details verification_needed", result["outcome"] == "verification_needed", text)
result, text = web("check_dispatch_status", conversation_id="otp-no-header")
check("web chat with no phone header -> verification_needed", result["outcome"] == "verification_needed", text)

# Happy path. The phone header says ...0003 (no bookings), but the code is
# sent to ...0001, so ...0001 must be the verified number.
result, text = web("send_otp", {"mobile_number": "+91 90000 00001"}, phone="919000000003")
check("send_otp -> sent", result["outcome"] == "sent", text)
check("sent -> asks for the 6-digit code", "6-digit code" in result["say"])
check("sent -> does not read the number back", "90000" not in text and "0001" not in text, text)
result, text = web("verify_otp", {"code": MOCK_CODE}, phone="919000000003")
check("verify_otp with the mock code -> verified", result["outcome"] == "verified", text)
check("verified -> tells the AI to call the booking tool again", "check_dispatch_status" in result.get("next_step", ""))
check("verified number is the one the code was sent to",
      identity.get_session("otp-1")["verified_mobile"] == "919000000001")
result, text = web("check_dispatch_status", phone="919000000003")
check("after OTP -> check_dispatch_status works for that number", result["outcome"] == "one", text)
result, text = web("get_chauffeur_details", {"booking_ref": "DCC100101"}, phone="919000000003")
check("after OTP -> get_chauffeur_details works", result["outcome"] == "shared", text)
check("OTP data is cleared after verifying", identity.get_session("otp-1").get("otp") is None)

# Spaces in the typed code are fine.
web("send_otp", {"mobile_number": "9000000002"}, conversation_id="otp-spaces")
result, text = web("verify_otp", {"code": "123 456"}, conversation_id="otp-spaces")
check("code typed with a space -> verified", result["outcome"] == "verified", text)

# Wrong code 3 times -> locked; the right code no longer works.
web("send_otp", {"mobile_number": "9000000001"}, conversation_id="otp-wrong")
result, text = web("verify_otp", {"code": "000000"}, conversation_id="otp-wrong")
check("wrong code -> wrong_code with 2 attempts left",
      result["outcome"] == "wrong_code" and result.get("attempts_left") == 2, text)
result, _ = web("verify_otp", {"code": "111111"}, conversation_id="otp-wrong")
check("second wrong code -> 1 attempt left", result.get("attempts_left") == 1)
result, text = web("verify_otp", {"code": "222222"}, conversation_id="otp-wrong")
check("third wrong code -> locked", result["outcome"] == "locked", text)
check("locked -> gives the support number", "customer support" in result["say"])
result, text = web("verify_otp", {"code": MOCK_CODE}, conversation_id="otp-wrong")
check("correct code after locked -> still locked", result["outcome"] == "locked", text)
check("locked conversation is not verified", identity.get_session("otp-wrong")["verified_mobile"] is None)
let_30_seconds_pass("otp-wrong")
result, text = web("send_otp", {"mobile_number": "9000000001"}, conversation_id="otp-wrong")
check("send_otp after locked -> locked (no more codes)", result["outcome"] == "locked", text)

# Expired code.
web("send_otp", {"mobile_number": "9000000001"}, conversation_id="otp-expired")
identity.get_session("otp-expired")["otp"]["expires_at"] = time.time() - 1  # 5 minutes have passed
result, text = web("verify_otp", {"code": MOCK_CODE}, conversation_id="otp-expired")
check("expired code -> expired", result["outcome"] == "expired", text)
check("expired -> offers a new code", "send a new one" in result["say"])
result, text = web("verify_otp", {"code": MOCK_CODE}, conversation_id="otp-nothing-sent")
check("verify_otp with no code sent -> expired", result["outcome"] == "expired", text)

# Send limits: 30 seconds between sends, at most 3 codes.
result, _ = web("send_otp", {"mobile_number": "9000000001"}, conversation_id="otp-limits")
check("1st send -> sent", result["outcome"] == "sent")
result, text = web("send_otp", {"mobile_number": "9000000001"}, conversation_id="otp-limits")
check("2nd send within 30 seconds -> too_many_requests", result["outcome"] == "too_many_requests", text)
let_30_seconds_pass("otp-limits")
result, _ = web("send_otp", {"mobile_number": "9000000001"}, conversation_id="otp-limits")
check("2nd send after 30 seconds -> sent", result["outcome"] == "sent")
let_30_seconds_pass("otp-limits")
result, _ = web("send_otp", {"mobile_number": "9000000001"}, conversation_id="otp-limits")
check("3rd send -> sent", result["outcome"] == "sent")
let_30_seconds_pass("otp-limits")
result, text = web("send_otp", {"mobile_number": "9000000001"}, conversation_id="otp-limits")
check("4th send -> too_many_requests", result["outcome"] == "too_many_requests", text)

# A new code replaces the old one (it's sent to the new number).
web("send_otp", {"mobile_number": "9000000001"}, conversation_id="otp-replace")
let_30_seconds_pass("otp-replace")
web("send_otp", {"mobile_number": "9000000002"}, conversation_id="otp-replace")
web("verify_otp", {"code": MOCK_CODE}, conversation_id="otp-replace")
check("new code replaces the old one (latest number is verified)",
      identity.get_session("otp-replace")["verified_mobile"] == "919000000002")

# Other channels and bad input.
result, text = call("send_otp", {"mobile_number": "9000000001"}, phone="919000000001", channel="whatsapp")
check("send_otp on whatsapp -> not_needed", result["outcome"] == "not_needed", text)
result, text = call("verify_otp", {"code": MOCK_CODE}, phone="919000000001", channel="phone")
check("verify_otp on a phone call -> not_needed", result["outcome"] == "not_needed", text)
result, text = web("send_otp", {"mobile_number": "12345"}, conversation_id="otp-invalid")
check("invalid number -> invalid_number", result["outcome"] == "invalid_number", text)
result, text = web("send_otp", {}, conversation_id="otp-invalid")
check("missing number -> invalid_number", result["outcome"] == "invalid_number", text)

# SMS failure -> unavailable + alert.
real_send_sms = sms.send_sms


def broken_send_sms(mobile, text):
    raise SMSUnavailable("timeout")


sms.send_sms = broken_send_sms
log_lines.clear()
result, text = web("send_otp", {"mobile_number": "9000000001"}, conversation_id="otp-sms-down")
sms.send_sms = real_send_sms
check("SMS failure -> unavailable", result["outcome"] == "unavailable", text)
check("SMS failure -> alert_perfox_team logged", any("alert_perfox_team" in line for line in log_lines))
check("SMS failure -> no code stored", identity.get_session("otp-sms-down").get("otp") is None)
otp_log_lines = list(log_lines)

# Real (random) codes, as in plivo mode: catch the SMS text instead of sending it.
sent_sms = []
config.OTP_MODE = "plivo"
sms.send_sms = lambda mobile, text: sent_sms.append(text)
log_lines.clear()
result, text = web("send_otp", {"mobile_number": "9000000001"}, conversation_id="otp-random")
random_code = sent_sms[0].split("code is ")[1][:6] if sent_sms else ""
check("plivo mode -> SMS text has a random 6-digit code",
      len(random_code) == 6 and random_code.isdigit() and sent_sms[0] == config.OTP_SMS_TEXT.format(code=random_code),
      sent_sms)
stored_hash = identity.get_session("otp-random")["otp"]["hash"]
pending_otp = dict(identity.get_session("otp-random")["otp"])
result, text = web("verify_otp", {"code": random_code}, conversation_id="otp-random")
check("plivo mode -> the random code verifies", result["outcome"] == "verified", text)
config.OTP_MODE = "mock"
sms.send_sms = real_send_sms
otp_log_lines += log_lines

check("the session stores only hash, number and expiry (never the code)",
      set(pending_otp) == {"hash", "mobile", "expires_at"} and random_code not in pending_otp["hash"],
      pending_otp)
check("the code never appears in any tool result",
      all(MOCK_CODE not in t and random_code not in t for t in otp_texts))
check("the code and its hash never appear in any log line",
      all(MOCK_CODE not in line and random_code not in line and stored_hash not in line
          for line in otp_log_lines))
otp_logs = [json.loads(line) for line in otp_log_lines if '"tool_call"' in line]
check("OTP tool logs mask the mobile",
      otp_logs and all(entry["mobile"] in (None, "91******0001") for entry in otp_logs), otp_logs)

# ----------------------------------------------------------------------
# 6. Shared login: test website + chat
# ----------------------------------------------------------------------
print("\n--- shared login (website + chat) ---")

SITE = "https://testserver"
SAME_ORIGIN = {"Origin": SITE}
web_replies = []  # every website response body, to check nothing leaks
log_lines.clear()


def browser():
    """A new browser with its own cookies. HTTPS, because the cookie is Secure."""
    return TestClient(app, base_url=SITE)


def site_get(browser_client, path):
    response = browser_client.get(path)
    web_replies.append(response.text)
    return response


def site_post(browser_client, path, body=None, headers=SAME_ORIGIN):
    response = browser_client.post(path, json=body or {}, headers=headers)
    web_replies.append(response.text)
    return response


def web_login(browser_client, mobile):
    site_post(browser_client, "/api/login/send-code", {"mobile": mobile})
    return site_post(browser_client, "/api/login/verify", {"code": MOCK_CODE})


def sid_of(browser_client):
    return browser_client.cookies.get("dcc_sid")


# The page and the API docs.
response = browser().get("/")
check("GET / serves the test page", response.status_code == 200 and "DriveCar" in response.text)
page = response.text
stub_at = page.find("w.Perfox = w.Perfox ||")
check("page: Perfox stub comes before our script",
      0 < stub_at < page.find("function perfox()") and page.find("function perfox()") < page.find('perfox("identify"'))
page_without_comments = re.sub(r"<!--.*?-->", "", page, flags=re.DOTALL)
check("page: no static widget.js tag (loaded only after identify)",
      '<script src="https://soma-api.perfox.ai' not in page_without_comments)
check("page: our script never assigns window.Perfox", "window.Perfox =" not in page)
check("/mcp and /health still work", client.get("/health").json()["ok"] is True)
check("/docs is off unless ENABLE_DOCS=true", client.get("/docs").status_code == 404)

# A new visitor.
browser_a = browser()
response = site_get(browser_a, "/api/session")
session_a = response.json()
cookie_header = response.headers.get("set-cookie", "").lower()
check("new visitor -> signed out with a chat_token",
      session_a["signed_in"] is False and session_a["masked_mobile"] is None and len(session_a["chat_token"]) > 30,
      session_a)
check("cookie dcc_sid is HttpOnly, Secure and SameSite=Lax",
      "dcc_sid=" in cookie_header and "httponly" in cookie_header and "secure" in cookie_header
      and "samesite=lax" in cookie_header, cookie_header)
check("the chat_token is not the cookie value", session_a["chat_token"] != sid_of(browser_a))
check("session answers are not cached", response.headers.get("cache-control") == "no-store")
check("same browser keeps the same session",
      site_get(browser_a, "/api/session").json()["chat_token"] == session_a["chat_token"])

response = site_get(browser_a, "/api/my-pickups")
check("my-pickups when signed out -> 401", response.status_code == 401)

# Signed out on the website: the chat needs OTP.
result, text = call("check_dispatch_status", channel="web", conversation_id="share-a",
                    user_token=session_a["chat_token"])
check("chat with a signed-out website token -> verification_needed",
      result["outcome"] == "verification_needed", text)

# Website login.
old_sid = sid_of(browser_a)
response = site_post(browser_a, "/api/login/send-code", {"mobile": "90000 00001"})
check("web send-code -> sent", response.status_code == 200 and response.json()["outcome"] == "sent", response.text)
response = site_post(browser_a, "/api/login/verify", {"code": "000000"})
check("web wrong code -> 400 wrong_code with tries left",
      response.status_code == 400 and response.json().get("attempts_left") == 2, response.text)
response = site_post(browser_a, "/api/login/verify", {"code": MOCK_CODE})
check("web verify with the mock code -> signed in",
      response.status_code == 200 and response.json()["signed_in"] is True, response.text)
check("cookie value changes after logging in", sid_of(browser_a) and sid_of(browser_a) != old_sid)
session_a = site_get(browser_a, "/api/session").json()
check("/api/session shows the masked mobile only",
      session_a["signed_in"] is True and session_a["masked_mobile"] == "••••0001", session_a)

# The chat uses the website login: no OTP needed.
result, text = call("check_dispatch_status", channel="web", conversation_id="share-a",
                    user_token=session_a["chat_token"])
check("chat with the website's token -> 'one', no OTP", result["outcome"] == "one", text)
result, text = call("get_chauffeur_details", {"booking_ref": "DCC100101"}, channel="web",
                    conversation_id="share-a", user_token=session_a["chat_token"])
check("chat with the website's token -> chauffeur shared", result["outcome"] == "shared", text)

# Your pickups: only the safe fields.
response = site_get(browser_a, "/api/my-pickups")
pickups = response.json().get("pickups", [])
check("my-pickups when signed in -> 1 pickup", response.status_code == 200 and len(pickups) == 1, response.text)
check("my-pickups has only the safe fields",
      pickups and set(pickups[0]) == {"booking_ref", "pickup_date", "pickup_time", "pickup_area", "dispatch_status",
                                      "chauffeur_allocated", "when", "chauffeur_first_name", "car_model",
                                      "car_colour", "registration_number"}, pickups)
check("my-pickups has no surname, address, drop or driver mobile",
      contains_none(response.text, ["Kumar", "Prestige", "Flat", "Airport", "9845012345"]), response.text)

browser_d = browser()
site_get(browser_d, "/api/session")
web_login(browser_d, "9000000004")
pickups = site_get(browser_d, "/api/my-pickups").json().get("pickups", [])
check("not-allocated pickup has no chauffeur fields",
      pickups and "chauffeur_first_name" not in pickups[0] and pickups[0]["chauffeur_allocated"] is False, pickups)

browser_e = browser()
site_get(browser_e, "/api/session")
web_login(browser_e, "9000000005")
log_lines_before = len(log_lines)
response = site_get(browser_e, "/api/my-pickups")
check("my-pickups when DriveCarClub is down -> 503 + alert",
      response.status_code == 503 and any("alert_perfox_team" in line for line in log_lines[log_lines_before:]))

# Chat login reaches the website (for that browser only).
browser_b, browser_c = browser(), browser()
token_b = site_get(browser_b, "/api/session").json()["chat_token"]
site_get(browser_c, "/api/session")
call("send_otp", {"mobile_number": "9000000002"}, channel="web", conversation_id="share-b", user_token=token_b)
result, text = call("verify_otp", {"code": MOCK_CODE}, channel="web", conversation_id="share-b", user_token=token_b)
check("chat verify_otp with a website token -> verified", result["outcome"] == "verified", text)
session_b = site_get(browser_b, "/api/session").json()
check("chat login -> that browser is signed in", session_b["signed_in"] is True and session_b["masked_mobile"] == "••••0002",
      session_b)
check("chat login -> another browser stays signed out",
      site_get(browser_c, "/api/session").json()["signed_in"] is False)

# Logout: the old token stops working, even if the conversation had OTP.
old_token = session_a["chat_token"]
identity.get_session("share-a")["verified_mobile"] = "919000000001"  # as if a chat OTP had happened too
response = site_post(browser_a, "/api/logout")
session_a = site_get(browser_a, "/api/session").json()
check("logout -> signed out with a NEW chat_token",
      response.status_code == 200 and session_a["signed_in"] is False and session_a["chat_token"] != old_token,
      session_a)
check("logout keeps the same cookie", site_get(browser_a, "/api/session").json()["chat_token"] == session_a["chat_token"])
result, text = call("check_dispatch_status", channel="web", conversation_id="share-a", user_token=old_token)
check("old chat_token after logout -> verification_needed (ignores chat OTP)",
      result["outcome"] == "verification_needed", text)
result, text = call("check_dispatch_status", channel="web", conversation_id="share-a",
                    user_token=session_a["chat_token"])
check("new chat_token after logout -> verification_needed (ignores chat OTP)",
      result["outcome"] == "verification_needed", text)
result, text = call("send_otp", {"mobile_number": "9000000001"}, channel="web", conversation_id="share-a",
                    user_token=old_token)
check("send_otp with an old token -> session_ended (refresh the page)", result["outcome"] == "session_ended", text)
check("my-pickups after logout -> 401", site_get(browser_a, "/api/my-pickups").status_code == 401)

# An unknown token: the old conversation-level OTP rules.
result, text = call("check_dispatch_status", channel="web", conversation_id="share-unknown",
                    user_token="not-a-token-we-gave-out")
check("unknown token, no chat OTP -> verification_needed", result["outcome"] == "verification_needed", text)
identity.get_session("share-unknown")["verified_mobile"] = "919000000001"
result, text = call("check_dispatch_status", channel="web", conversation_id="share-unknown",
                    user_token="not-a-token-we-gave-out")
check("unknown token, chat OTP done -> 'one'", result["outcome"] == "one", text)

# Sessions expire after 8 hours without activity.
token_b = site_get(browser_b, "/api/session").json()["chat_token"]
browser_sessions._sessions[sid_of(browser_b)]["last_seen"] -= browser_sessions.SESSION_IDLE_SECONDS + 1
session_b = site_get(browser_b, "/api/session").json()
check("idle 8 hours -> new signed-out session", session_b["signed_in"] is False and session_b["chat_token"] != token_b)
result, text = call("check_dispatch_status", channel="web", conversation_id="share-b", user_token=token_b)
check("expired session's token -> verification_needed", result["outcome"] == "verification_needed", text)

# CSRF: posts from another website are refused.
response = site_post(browser_d, "/api/logout", headers={"Origin": "https://evil.example"})
check("POST from a foreign Origin -> 403", response.status_code == 403)
check("...and nothing happened", site_get(browser_d, "/api/session").json()["signed_in"] is True)
response = browser_d.post("/api/login/send-code", json={"mobile": "9000000001"})
check("POST with no Origin -> 403", response.status_code == 403)

# Same limits as the chat.
browser_f = browser()
site_get(browser_f, "/api/session")
response = site_post(browser_f, "/api/login/send-code", {"mobile": "12345"})
check("web invalid number -> 400 invalid_number", response.status_code == 400 and response.json()["outcome"] == "invalid_number")
site_post(browser_f, "/api/login/send-code", {"mobile": "9000000003"})
response = site_post(browser_f, "/api/login/send-code", {"mobile": "9000000003"})
check("web second send within 30 seconds -> 429", response.status_code == 429)

# Nothing secret in any response or log line.
all_cookies = [c for b in (browser_a, browser_b, browser_c, browser_d, browser_e, browser_f)
               for c in b.cookies.values()] + [old_sid]
all_tokens = [old_token, session_a["chat_token"], token_b]
check("no full mobile in any website response",
      all("9000000001" not in t and "9000000002" not in t for t in web_replies))
check("no code in any website response", all(MOCK_CODE not in t for t in web_replies))
check("no cookie value in any website response", all(c not in t for c in all_cookies for t in web_replies))
check("no full mobile, code, cookie or token in any log line",
      all("9000000001" not in line and "9000000002" not in line and MOCK_CODE not in line
          and all(secret not in line for secret in all_cookies + all_tokens)
          for line in log_lines))

# ----------------------------------------------------------------------
# 7. Logging
# ----------------------------------------------------------------------
print("\n--- logging ---")

log_lines.clear()
call("check_dispatch_status", phone="919000000001", conversation_id="log-test")
tool_logs = [json.loads(line) for line in log_lines if '"tool_call"' in line]
check("one JSON log line per tool call", len(tool_logs) == 1)
entry = tool_logs[0] if tool_logs else {}
check("log has the required fields",
      all(key in entry for key in ("tool", "conversation_id", "channel", "mobile",
                                   "booking_ref", "outcome", "reason", "duration_ms")), entry)
check("log masks the mobile", entry.get("mobile") == "91******0001", entry)
check("log never has the full mobile", all("919000000001" not in line for line in log_lines))

# ----------------------------------------------------------------------
# 8. Live client against a small fake DriveCarClub server
# ----------------------------------------------------------------------
print("\n--- live client (fake DriveCarClub server) ---")

counts = {"token": 0, "bookings": 0}
plivo_requests = []
TODAY_BOOKING = {
    "BookingId": "LIVE1", "PickupDateTime": datetime.now(INDIA).strftime("%Y-%m-%dT23:00:00"),
    "PickupArea": "MG Road", "Status": "Dispatched", "DriverName": "Test Driver",
    "CarModel": "Swift", "CarColour": "Red", "CarNo": "KA 01 X 1",
}
GOOD_BASIC = "Basic " + base64.b64encode(b"uat-user:uat-pass").decode()


class FakeDriveCarClub(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # keep the test output clean

    def send(self, status, body):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except ConnectionError:
            pass  # the client already gave up (the timeout test), that's fine

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")

        if self.path in ("/token", "/token-text"):
            if self.headers.get("Authorization") != GOOD_BASIC:
                return self.send(401, {"error": "bad credentials"})
            counts["token"] += 1
            token = f"tok{counts['token']}"
            if self.path == "/token-text":
                return self.send(200, f'"{token}"'.encode())  # a plain JSON string
            return self.send(200, {"access_token": token})  # no expiry given

        if self.path == "/bookings":
            counts["bookings"] += 1
            contact = body.get("ContactNo")
            auth = self.headers.get("Authorization")
            if contact == "919999999401":
                return self.send(401, {"error": "always unauthorised"})
            if contact == "919999999500":
                return self.send(500, {"error": "boom"})
            if auth == "Bearer tok1":
                return self.send(401, {"error": "token expired"})  # first token is "expired"
            return self.send(200, {"Data": [TODAY_BOOKING]})

        if self.path.startswith("/v1/Account/"):  # fake Plivo Message API
            plivo_requests.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
            if body.get("dst") == "+919999999500":
                return self.send(500, {"error": "boom"})
            return self.send(202, {"message": "message(s) queued", "message_uuid": ["x"], "api_id": "y"})

        if self.path == "/slow":
            time.sleep(1.5)
            return self.send(200, [])

        self.send(404, {"error": "not found"})


fake_server = ThreadingHTTPServer(("127.0.0.1", 0), FakeDriveCarClub)
threading.Thread(target=fake_server.serve_forever, daemon=True).start()
base_url = f"http://127.0.0.1:{fake_server.server_address[1]}"


def live_client(token_path="/token", booking_path="/bookings", timeout=2):
    return DriveCarClubClient(
        token_url=base_url + token_path, booking_url=base_url + booking_path,
        username="uat-user", password="uat-pass", timeout=timeout,
        token_ttl=3000, token_header="Authorization", token_prefix="Bearer",
    )


def failure_reason(function):
    try:
        function()
    except (DCCUnavailable, SMSUnavailable) as failure:
        return failure.reason
    return None


dcc = live_client()
bookings = dcc.get_bookings("919000000001")
check("expired token: one 401, then refresh and success",
      len(bookings) == 1 and counts["token"] == 2 and counts["bookings"] == 2, counts)

bookings = dcc.get_bookings("919000000001")
check("next lookup reuses the cached token", counts["token"] == 2 and counts["bookings"] == 3, counts)
check("dict response with 'Data' list is unwrapped", bookings and bookings[0]["BookingId"] == "LIVE1")

tokens_before = counts["token"]
reason = failure_reason(lambda: dcc.get_bookings("919999999401"))
check("second 401 is a failure (auth_failed)", reason == "auth_failed", reason)
check("only ONE refresh after a 401", counts["token"] == tokens_before + 1, counts)

check("5xx is a failure (server_error)",
      failure_reason(lambda: dcc.get_bookings("919999999500")) == "server_error")

check("slow response is a failure (timeout)",
      failure_reason(lambda: live_client(booking_path="/slow", timeout=0.5).get_bookings("919000000001")) == "timeout")

check("unreachable server is a failure",
      failure_reason(lambda: DriveCarClubClient(token_url="http://127.0.0.1:1/token", timeout=1).get_token())
      in ("token_network_error", "token_timeout"))

check("wrong credentials on Token API is a failure",
      failure_reason(lambda: DriveCarClubClient(token_url=base_url + "/token", username="x",
                                                password="y", timeout=1).get_token()) == "token_http_401")

text_client = live_client(token_path="/token-text")
check("plain-string token response is accepted", text_client.get_token().startswith("tok"))

# --- Plivo SMS sender against the same fake server ---
PLIVO = {"auth_id": "MAFAKE", "auth_token": "secret-token", "sender_id": "DCCLUB", "base_url": base_url, "timeout": 2}
sms.send_with_plivo("919876543210", "hello", **PLIVO)
sent = plivo_requests[-1] if plivo_requests else {}
check("plivo: posts to /v1/Account/{auth_id}/Message/", sent.get("path") == "/v1/Account/MAFAKE/Message/", sent)
check("plivo: Basic auth with Auth ID and Auth Token",
      sent.get("auth") == "Basic " + base64.b64encode(b"MAFAKE:secret-token").decode(), sent)
check("plivo: body has src, dst (E.164) and text",
      sent.get("body") == {"src": "DCCLUB", "dst": "+919876543210", "text": "hello"}, sent)
check("plivo: 5xx is a failure",
      failure_reason(lambda: sms.send_with_plivo("919999999500", "hi", **PLIVO)) == "http_500")
check("plivo: missing settings is a failure",
      failure_reason(lambda: sms.send_with_plivo("919876543210", "hi", auth_id="", auth_token="",
                                                 sender_id="", base_url=base_url)) == "plivo_not_configured")

fake_server.shutdown()

# ----------------------------------------------------------------------
print(f"\n{results['pass']} passed, {results['fail']} failed")
sys.exit(1 if results["fail"] else 0)
