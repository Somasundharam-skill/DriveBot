"""DriveBot MCP server tests.

Run with:  uv run python -m tests.run_tests

Prints PASS or FAIL for every check, and a total at the end.
"""

import os

# Settings for the tests. These MUST be set before the app is imported,
# because config.py reads them once when it is first imported.
os.environ["DCC_MODE"] = "mock"
os.environ["DCC_RATE_MODE"] = "mock"  # never call the real GetRate API from tests
os.environ["MCP_API_KEY"] = "test-key"
os.environ["TRUST_WEB_PHONE"] = "false"
os.environ["DCC_TIMEOUT_SECONDS"] = "1"
os.environ["OTP_MODE"] = "mock"  # no SMS; the code is always 123456
os.environ["KEEP_ALIVE_URL"] = ""  # never ping the real server from tests
os.environ["ENABLE_DOCS"] = "false"
os.environ["INCLUDE_PAST_BOOKINGS"] = "false"  # the default; one test switches it on
os.environ["PICKUP_AREA_FIELD"] = "city_Name"
for fare_list in ("FARE_CAR_CATEGORIES", "FARE_SERVICE_TYPES", "FARE_CITIES"):
    os.environ[fare_list] = ""  # no allow-lists; one test fills FARE_CITIES

import base64
import json
import logging
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fastapi.testclient import TestClient

from app import browser_sessions, config, fares, identity, otp, sms, tools
from app.drivecarclub import DriveCarClubClient
from app.errors import DCCUnavailable, SMSUnavailable
from app.mapper import INDIA
from app.server import app, log_startup_warnings

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
    """True if none of the words is in the text (ignoring upper/lower case)."""
    return not any(word.lower() in text.lower() for word in words)


# Private fields in the mock data (customer name, company, guest number,
# address, standing instructions). They must never appear in any result.
MOCK_PRIVATE = ["PRIYA", "TESTCUSTOMER", "Mockco", "919000000099", "Prestige", "Flat 12B", "gate 3"]


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
check("tools/list returns 5 tools", len(tool_list) == 5)
check("tools/list has the OTP and fare tools",
      {t["name"] for t in tool_list} == {"check_dispatch_status", "get_chauffeur_details", "send_otp", "verify_otp",
                                         "get_fare"})
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
check("health returns ok and both modes", response.json() == {"ok": True, "mode": "mock", "rate_mode": "mock"})

# ----------------------------------------------------------------------
# 2. check_dispatch_status (SOP 5)
# ----------------------------------------------------------------------
print("\n--- check_dispatch_status ---")

result, text = call("check_dispatch_status", phone="919000000001")
check("one booking -> outcome 'one'", result["outcome"] == "one", text)
check("one booking -> chauffeur_allocated true", result.get("booking", {}).get("chauffeur_allocated") is True)
check("one booking -> pickup area only", result.get("booking", {}).get("pickup_area") == "Bengaluru")
check("one booking -> 'Despatched' shown as 'Dispatched'",
      result.get("booking", {}).get("dispatch_status") == "Dispatched")
check("one booking -> 'say' confirms car sent and offers details",
      "has been sent" in result["say"] and "chauffeur" in result["say"])
check("summary has no chauffeur name",
      contains_none(text, ["Ramesh", "Kumar"]), text)
check("summary has no customer details, address or driver mobile",
      contains_none(text, MOCK_PRIVATE + ["9845012345"]), text)
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
check("several -> no chauffeur names, customer details or addresses",
      contains_none(text, ["Mohammed", "Anil", "Sharma"] + MOCK_PRIVATE), text)

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
check("verification_needed leaks no booking", "booking" not in result and "Bengaluru" not in text)

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
check("car model (carAlloted) and registration shared",
      details.get("car_model") == "Toyota Innova Crysta" and details.get("registration_number") == "KA01AB1234")
check("no colour field -> car_colour left out, never 'None'",
      "car_colour" not in details and "None" not in text, text)
check("only the allowlisted chauffeur fields",
      set(details) == {"booking_ref", "pickup_date", "pickup_time", "chauffeur_first_name",
                       "car_model", "registration_number"})
check("no customer details, address or area", contains_none(text, MOCK_PRIVATE + ["Bengaluru"]), text)

result, text = call("get_chauffeur_details", {"booking_ref": "DCC100401"}, phone="919000000004")
check("not allocated -> 'not_allocated'", result["outcome"] == "not_allocated", text)
check("not allocated -> SOP wording",
      result["say"].startswith("Your chauffeur hasn't been assigned yet. You'll get the details once they are."))

result, text = call("get_chauffeur_details", {"booking_ref": "DCC100202"}, phone="919000000001")
check("someone else's booking -> not_found", result["outcome"] == "not_found", text)
check("someone else's booking leaks nothing",
      contains_none(text, ["Mohammed", "Irfan", "Maruti", "KA51", "Hosur", "DCC100202"]), text)

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
                                      "registration_number"}, pickups)
check("my-pickups has no surname, customer details, address or driver mobile",
      contains_none(response.text, ["Kumar", "9845012345"] + MOCK_PRIVATE), response.text)

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

counts = {"token": 0, "bookings": 0, "rates": 0}
plivo_requests = []
TODAY_BOOKING = {
    "actual_bk_num": "LIVE1", "reportingDate": datetime.now(INDIA).strftime("%d-%m-%Y"),
    "reportingTime": "23:00", "city_Name": "Testcity", "status": "Despatched",
    "chauffeurName": "TEST DRIVER", "carAlloted": "Swift", "carNumber": "KA01X1",
}
GOOD_BASIC = "Basic " + base64.b64encode(b"uat-user:uat-pass").decode()

# The real UAT DespatchedBooking response format, with FAKE values only.
# The private fields have distinct fake values so the tests can prove
# none of them leaks.
UAT_BOOKING = {
    "msg": None, "actual_bk_num": "BCR-XXX-0000001", "location": "New Delhi",
    "company_Name": "FAKECOMPANY PVT LTD", "individualsname": "GUESTNAME TESTCUSTOMER",
    "b_Guest_Contact_No": "919111111111",
    "reportingDate": "05-08-2026", "reportingTime": "12:00", "reportingAddress": "FAKE ADDRESS LINE",
    "standinginstructions": "STANDING NOTE", "carBooked": "Swift Dzire", "carAlloted": "Swift Dzire",
    "car_Owned_By": None, "carNumber": "5792-DL1ZD", "chauffeurName": "FIRSTNAME LASTNAME",
    "chaufferMobile": "9222222222", "dsNo": "BCR-XXX-0000002", "status": "Despatched",
    "vip": "False", "city_Name": "Gurugram",
}
UAT_PRIVATE = ["LASTNAME", "GUESTNAME", "TESTCUSTOMER", "FAKECOMPANY", "FAKE ADDRESS",
               "919111111111", "9222222222", "STANDING NOTE", "BCR-XXX-0000002"]
UAT_PHONE, UAT_ERROR_PHONE, UAT_BAD_DATE_PHONE = "919100000001", "919100000002", "919100000003"
uat_responses = {
    UAT_PHONE: {"status": "200", "message": "Success", "data": [UAT_BOOKING]},
    UAT_ERROR_PHONE: {"status": "500", "message": "Error", "data": None},
    # Same booking, but a date we can't read and no car allotted yet.
    UAT_BAD_DATE_PHONE: {"status": "200", "message": "Success",
                         "data": [{**UAT_BOOKING, "reportingDate": "not a date", "carAlloted": "",
                                   "carBooked": "Toyota Etios"}]},
}

# GetRate API responses, chosen by CityName (section 10).
RATES_EMPTY = {"status": "200", "message": "Success", "data": []}
rate_responses = {
    "Gurugram": {"status": "200", "message": "Success", "data": [
        {"packageName": "8hrs 80kms", "serviceType": "Local - within city limits", "carCategory": "Midsize",
         "cityName": "Gurugram", "rate": "2400.00", "extra_Km_Rate": "18.00", "extra_Hr_Rate": "150.00",
         "outstation_Allowance": "0.00", "night_Stay": "0.00"},
        {"packageName": "4hrs 40kms", "serviceType": "Local - within city limits", "carCategory": "Midsize",
         "cityName": "Gurugram", "rate": "1350.00", "extra_Km_Rate": "18.00", "extra_Hr_Rate": "150.00",
         "outstation_Allowance": "0.00", "night_Stay": "0.00"},
    ]},
    "Errorcity": {"status": "500", "message": "Error", "data": None},
}


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
            return self.send(200, {"status": "200", "message": "Success", "data": [TODAY_BOOKING]})

        if self.path == "/uat-bookings":  # the real UAT response format (section 9)
            return self.send(200, uat_responses[body.get("ContactNo")])

        if self.path == "/rates":  # the GetRate API (section 10)
            counts["rates"] += 1
            return self.send(200, rate_responses.get(body.get("CityName"), RATES_EMPTY))

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


def live_client(token_path="/token", booking_path="/bookings", rate_path="/rates", timeout=2):
    return DriveCarClubClient(
        token_url=base_url + token_path, booking_url=base_url + booking_path, rate_url=base_url + rate_path,
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
check("real response: list under 'data' is unwrapped", bookings and bookings[0]["actual_bk_num"] == "LIVE1")

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

# ----------------------------------------------------------------------
# 9. The real UAT response format, through the tools (fake values)
# ----------------------------------------------------------------------
print("\n--- real UAT response format ---")

saved_client = tools._client
tools._client = live_client(booking_path="/uat-bookings")  # the live client, fed the UAT format
uat_texts = []
log_lines.clear()


def uat_call(tool, arguments=None, phone=UAT_PHONE):
    result, text = call(tool, arguments, phone=phone, conversation_id="uat")
    uat_texts.append(text)
    return result, text


check("INCLUDE_PAST_BOOKINGS is false by default", config.INCLUDE_PAST_BOOKINGS is False)
result, text = uat_call("check_dispatch_status")
check("past UAT booking hidden by default", result["outcome"] == "none", text)

config.INCLUDE_PAST_BOOKINGS = True
result, text = uat_call("check_dispatch_status")
booking = result.get("booking", {})
check("INCLUDE_PAST_BOOKINGS=true -> past booking shown", result["outcome"] == "one", text)
check("actual_bk_num is the booking_ref", booking.get("booking_ref") == "BCR-XXX-0000001", booking)
check("reportingDate (DD-MM-YYYY) and reportingTime parsed",
      booking.get("pickup_date") == "2026-08-05" and booking.get("pickup_time") == "12:00", booking)
check("say reads the date and time", "5 August at 12:00 PM" in result["say"], result["say"])
check("city_Name is the pickup area", booking.get("pickup_area") == "Gurugram", booking)
check("'Despatched' -> dispatch_status 'Dispatched'", booking.get("dispatch_status") == "Dispatched", booking)
check("chauffeur counts as allocated", booking.get("chauffeur_allocated") is True, booking)

result, text = uat_call("get_chauffeur_details", {"booking_ref": "BCR-XXX-0000001"})
details = result.get("chauffeur", {})
check("UAT chauffeur shared", result["outcome"] == "shared", text)
check("first name only, title case: 'Firstname'", details.get("chauffeur_first_name") == "Firstname", details)
check("car model is carAlloted, registration is carNumber unchanged",
      details.get("car_model") == "Swift Dzire" and details.get("registration_number") == "5792-DL1ZD", details)
check("no colour in the API -> car_colour left out, no 'None'",
      "car_colour" not in details and "None" not in text, text)
check("chauffeur mobile hidden (SHARE_CHAUFFEUR_MOBILE is False)", "chauffeur_mobile" not in details)

config.PICKUP_AREA_FIELD = "location"
result, text = uat_call("check_dispatch_status")
check("PICKUP_AREA_FIELD=location -> location is the area",
      result.get("booking", {}).get("pickup_area") == "New Delhi", text)
config.PICKUP_AREA_FIELD = "reportingAddress"
result, text = uat_call("check_dispatch_status")
check("PICKUP_AREA_FIELD=reportingAddress is refused (city_Name used)",
      result.get("booking", {}).get("pickup_area") == "Gurugram", text)
config.PICKUP_AREA_FIELD = "city_Name"
config.INCLUDE_PAST_BOOKINGS = False

result, text = uat_call("check_dispatch_status", phone=UAT_BAD_DATE_PHONE)
check("unreadable date -> booking kept, not dropped", result["outcome"] == "one", text)
check("unreadable date -> empty date and time",
      result.get("booking", {}).get("pickup_date") == "" and result.get("booking", {}).get("pickup_time") == "", text)
check("unreadable date -> warning logged", any("booking_date_unreadable" in line for line in log_lines))
result, text = uat_call("get_chauffeur_details", {"booking_ref": "BCR-XXX-0000001"}, phone=UAT_BAD_DATE_PHONE)
check("empty carAlloted -> carBooked used", result.get("chauffeur", {}).get("car_model") == "Toyota Etios", text)

result, text = uat_call("check_dispatch_status", phone=UAT_ERROR_PHONE)
check("top-level status not '200' -> unavailable / bad_response",
      result["outcome"] == "unavailable" and result.get("reason") == "bad_response", text)

check("no surname, customer name, company, address, guest number or chauffeur mobile in any tool result",
      all(contains_none(t, UAT_PRIVATE) for t in uat_texts))
check("none of them in any log line either", all(contains_none(line, UAT_PRIVATE) for line in log_lines))
tools._client = saved_client

# ----------------------------------------------------------------------
# 10. get_fare
# ----------------------------------------------------------------------
print("\n--- get_fare ---")


def fare(car_category="Midsize", service_type="Local", city_name="Gurugram", channel="phone", phone="919000000003"):
    """Call get_fare. Arguments set to None are left out."""
    arguments = {"car_category": car_category, "service_type": service_type, "city_name": city_name}
    return call("get_fare", {k: v for k, v in arguments.items() if v is not None},
                phone=phone, channel=channel, conversation_id="fare")


# Mock mode (the default client).
fares.clear_cache()
log_lines.clear()
result, text = fare()
packages = result.get("packages", [])
check("mock: found two packages", result["outcome"] == "found" and len(packages) == 2, text)
check("mock: amounts are numbers, not text",
      packages and packages[0]["rate_inr"] == 2400 and isinstance(packages[0]["rate_inr"], (int, float))
      and packages[0]["extra_km_rate_inr"] == 18 and packages[0]["extra_hour_rate_inr"] == 150, packages)
check("mock: zero allowances are left out",
      all("outstation_allowance_inr" not in p and "night_stay_inr" not in p for p in packages), packages)
check("mock: package has exactly the fare fields",
      packages and set(packages[0]) == {"package_name", "service_type", "car_category", "city", "rate_inr",
                                        "extra_km_rate_inr", "extra_hour_rate_inr"}, packages)
check("say quotes the exact amounts as standard rates, plus FARE_NOTE",
      "standard rates" in result["say"] and "₹2400" in result["say"] and "₹1350" in result["say"]
      and config.FARE_NOTE in result["say"], result["say"])
fare_logs = [json.loads(line) for line in log_lines if '"tool_call"' in line]
check("log has tool, the three inputs, outcome and duration",
      fare_logs and fare_logs[0]["tool"] == "get_fare" and fare_logs[0]["car_category"] == "Midsize"
      and fare_logs[0]["service_type"] == "Local" and fare_logs[0]["city_name"] == "Gurugram"
      and fare_logs[0]["outcome"] == "found" and "duration_ms" in fare_logs[0]
      and fare_logs[0]["source"] == "mock", fare_logs)

result, text = fare(service_type="Outstation")
package = (result.get("packages") or [{}])[0]
check("mock: allowances above 0 are included",
      package.get("outstation_allowance_inr") == 300 and package.get("night_stay_inr") == 300, text)

result, text = fare(city_name="Atlantis")
check("mock: unknown city -> not_found with support number",
      result["outcome"] == "not_found" and "customer support" in result["say"]
      and "packages" not in result and "₹" not in result["say"], text)

result, text = fare(channel="web", phone=None)
check("works on web chat without OTP", result["outcome"] == "found", text)

result, text = fare(city_name=None)
check("missing city -> invalid_input asking for the city",
      result["outcome"] == "invalid_input" and "city" in result["say"], text)

# The live client against the fake GetRate API.
saved_rate_client = tools._rate_client
tools._rate_client = live_client()  # get_fare uses the rate client
fares.clear_cache()
log_lines.clear()

result, text = fare()
packages = result.get("packages", [])
check("live: found two packages, amounts as numbers",
      result["outcome"] == "found" and len(packages) == 2 and packages[1]["rate_inr"] == 1350, text)
check("live: zero allowances left out",
      all("outstation_allowance_inr" not in p and "night_stay_inr" not in p for p in packages), packages)
check("live: one API call", counts["rates"] == 1, counts)

result, text = fare("midsize", "LOCAL", "gurugram")
check("cache: same question (any case) within the hour -> no second API call",
      result["outcome"] == "found" and counts["rates"] == 1, counts)
for key in fares._cache:
    fares._cache[key] = (time.monotonic() - 1, fares._cache[key][1])  # pretend an hour has passed
fare()
check("cache: after an hour -> called again", counts["rates"] == 2, counts)

result, text = fare(city_name="Nowhere")
check("empty data list -> not_found", result["outcome"] == "not_found", text)

result, text = fare(city_name="Errorcity")
check("status not '200' -> unavailable / bad_response",
      result["outcome"] == "unavailable" and result.get("reason") == "bad_response", text)
check("API failure -> alert_perfox_team logged", any("alert_perfox_team" in line for line in log_lines))

calls_before = counts["rates"]
for bad_input, label in (("<script>", "'<script>'"), ("a" * 200, "200 characters"),
                         ("Gurugram1", "a digit"), (123, "a number")):
    result, text = fare(city_name=bad_input)
    check(f"{label} as city -> invalid_input", result["outcome"] == "invalid_input", text)
check("invalid input -> no API call", counts["rates"] == calls_before, counts)
check("invalid input is not logged", all("<script>" not in line and "a" * 200 not in line for line in log_lines))

config.FARE_CITIES = ["Gurugram", "New Delhi"]
result, text = fare(city_name="Mumbai")
check("allow-list set -> city not on it is invalid_input, listing the options",
      result["outcome"] == "invalid_input" and "New Delhi" in result["say"], text)
check("allow-list rejection -> no API call", counts["rates"] == calls_before, counts)
result, text = fare(city_name="gurugram")
check("allow-list set -> city on it (any case) is found", result["outcome"] == "found", text)
config.FARE_CITIES = []
tools._rate_client = saved_rate_client

# ----------------------------------------------------------------------
# 11. Separate modes: DCC_MODE (bookings) and DCC_RATE_MODE (fares)
# ----------------------------------------------------------------------
print("\n--- DCC_MODE and DCC_RATE_MODE ---")

# Point the "live" settings at the fake DriveCarClub server.
saved_settings = {name: getattr(config, name) for name in (
    "DCC_MODE", "DCC_RATE_MODE", "DCC_TOKEN_URL", "DCC_BOOKING_URL", "DCC_RATE_URL",
    "DCC_USERNAME", "DCC_PASSWORD", "DCC_TIMEOUT_SECONDS")}
config.DCC_TOKEN_URL = base_url + "/token"
config.DCC_BOOKING_URL = base_url + "/bookings"
config.DCC_RATE_URL = base_url + "/rates"
config.DCC_USERNAME, config.DCC_PASSWORD = "uat-user", "uat-pass"
config.DCC_TIMEOUT_SECONDS = 2


def use_modes(booking_mode, rate_mode):
    """Switch modes as if the server had restarted with these settings."""
    config.DCC_MODE, config.DCC_RATE_MODE = booking_mode, rate_mode
    tools._client = tools._rate_client = None
    tools._shared.clear()
    fares.clear_cache()


def fare_sources():
    """The "source" of every get_fare log line since log_lines was cleared."""
    return [json.loads(line)["source"] for line in log_lines if '"get_fare"' in line and '"tool_call"' in line]


# Fares live, bookings mock.
use_modes("mock", "live")
log_lines.clear()
before = dict(counts)
result, text = call("check_dispatch_status", phone="919000000001")
check("fares live: bookings still come from mock data",
      result["outcome"] == "one" and result["booking"]["booking_ref"] == "DCC100101"
      and counts["bookings"] == before["bookings"], text)
result, text = fare()
check("fares live: get_fare calls the real (fake) GetRate API",
      result["outcome"] == "found" and counts["rates"] == before["rates"] + 1, text)
fare()
check("fares live: second question comes from the cache", counts["rates"] == before["rates"] + 1, counts)
check("log source: 'live' then 'cache'", fare_sources() == ["live", "cache"], fare_sources())
fare(city_name="Nowhere")
result, text = fare(city_name="Nowhere")
check("not_found is not cached (asked again)",
      result["outcome"] == "not_found" and counts["rates"] == before["rates"] + 3, counts)

# The reverse: bookings live, fares mock.
use_modes("live", "mock")
log_lines.clear()
before = dict(counts)
result, text = call("check_dispatch_status", phone="919000000001")
check("bookings live: check_dispatch_status calls the real (fake) API",
      result["outcome"] == "one" and result["booking"]["booking_ref"] == "LIVE1"
      and counts["bookings"] > before["bookings"], text)
result, text = fare()
check("bookings live: get_fare still uses mock fares",
      result["outcome"] == "found" and counts["rates"] == before["rates"], text)
check("log source: 'mock'", fare_sources() == ["mock"], fare_sources())

# Both live: one shared client, so one token for both APIs.
use_modes("live", "live")
before = dict(counts)
call("check_dispatch_status", phone="919000000001")
fare()
check("both live: the booking tools and get_fare share one client",
      tools.get_client() is tools.get_rate_client())
check("both live: one token used for both APIs", counts["token"] == before["token"] + 1, counts)

for name, value in saved_settings.items():
    setattr(config, name, value)
use_modes(config.DCC_MODE, config.DCC_RATE_MODE)

# DCC_RATE_MODE defaults to DCC_MODE (checked in a fresh Python, since
# settings are read once at import).
default_check = subprocess.run(
    [sys.executable, "-c", "from app import config; print(config.DCC_MODE, config.DCC_RATE_MODE)"],
    env={**os.environ, "DCC_MODE": "live", "DCC_RATE_MODE": ""}, capture_output=True, text=True,
)
check("DCC_RATE_MODE empty -> same as DCC_MODE", default_check.stdout.split() == ["live", "live"],
      default_check.stdout + default_check.stderr)

log_lines.clear()
log_startup_warnings()
mode_logs = [json.loads(line) for line in log_lines if '"dcc_modes"' in line]
check("startup logs both modes",
      mode_logs and mode_logs[0]["booking_mode"] == "mock" and mode_logs[0]["rate_mode"] == "mock", mode_logs)

fake_server.shutdown()

# ----------------------------------------------------------------------
print(f"\n{results['pass']} passed, {results['fail']} failed")
sys.exit(1 if results["fail"] else 0)
