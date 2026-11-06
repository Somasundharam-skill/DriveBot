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
os.environ["VERIFICATION_MODE"] = "otp"  # the default; one section switches it to "none"
os.environ["WHATSAPP_NUMBER_MODE"] = "auto"  # the default; one section switches it to "ask"
os.environ["DCC_BOOKING_URL"] = "https://drivecarclub.com/CarclubCRMUAT/Api/Home/DespatchedBooking"
os.environ["KEEP_ALIVE_URL"] = ""  # never ping the real server from tests
os.environ["ENABLE_DOCS"] = "false"
os.environ["INCLUDE_PAST_BOOKINGS"] = "false"  # the default; one test switches it on
os.environ["MOCK_NUMBER_SCENARIOS"] = ""  # the default; section 11a fills it with FAKE numbers
os.environ["PICKUP_AREA_FIELD"] = "city_Name"
for fare_list in ("FARE_CAR_CATEGORIES", "FARE_SERVICE_TYPES", "FARE_CITIES"):
    os.environ[fare_list] = ""  # no allow-lists; one test fills FARE_CITIES
os.environ["WA_SUMMARY_MODE"] = "mock"  # never send a real WhatsApp; section 12a uses a fake Perfox
for perfox_setting in ("PERFOX_API_BASE", "PERFOX_API_KEY", "PERFOX_AGENT_ID"):
    os.environ[perfox_setting] = ""
os.environ["WA_TEMPLATE_CHAUFFEUR_NAME"] = "test_chauffeur_summary"
os.environ["WA_TEMPLATE_CHAUFFEUR_LANG"] = ""  # empty -> en_US
os.environ["WA_TEMPLATE_FARE_NAME"] = "test_fare_summary"
os.environ["WA_TEMPLATE_FARE_LANG"] = "en_GB"
os.environ["WA_CHAUFFEUR_PARAMS"] = ""  # empty -> the original layouts; section 12a tries custom ones
os.environ["WA_FARE_PARAMS"] = ""
os.environ["WA_INCLUDE_DRIVER_PHONE"] = "false"

import base64
import json
import logging
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fastapi.testclient import TestClient

from app import browser_sessions, call_summary, config, fares, identity, messages, otp, perfox, sms, tools
from app.drivecarclub import DriveCarClubClient
from app.errors import DCCUnavailable, OutboundFailed, SMSUnavailable
from app.mapper import INDIA
from app.server import app, check_whatsapp_number_lock, log_startup_warnings

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
all_log_lines = []  # never cleared: every log line of the whole run


class ListHandler(logging.Handler):
    def emit(self, record):
        log_lines.append(record.getMessage())
        all_log_lines.append(record.getMessage())


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
    all_tool_texts.append(text)
    return json.loads(text), text


all_tool_texts = []  # every tool result of the whole run


def contains_none(text, words):
    """True if none of the words is in the text (ignoring upper/lower case)."""
    return not any(word.lower() in text.lower() for word in words)


# Private fields in the mock data (customer name, company, guest number,
# address, standing instructions). They must never appear in any result.
MOCK_PRIVATE = ["PRIYA", "TESTCUSTOMER", "Mockco", "919000000099", "Prestige", "Flat 12B", "gate 3"]
# The fake customer names (individualsname) in mock.py. Only the WhatsApp
# summary's template parameters may ever have them.
CUSTOMER_NAMES = ["ARJUN", "MEERA", "NIKHIL", "KAVYA", "NEHA", "ROHAN", "TARUN", "POOJA",
                  "ZEPHYRINA", "QUILLON"]  # the last two: names given in MOCK_NUMBER_SCENARIOS
MOCK_PRIVATE += CUSTOMER_NAMES


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
check("tools/list returns 6 tools", len(tool_list) == 6)
check("tools/list has the OTP, fare and call summary tools",
      {t["name"] for t in tool_list} == {"check_dispatch_status", "get_chauffeur_details", "send_otp", "verify_otp",
                                         "get_fare", "send_call_summary"})
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
check("none -> support number left to the knowledge base",
      tools.SUPPORT_PLACEHOLDER in result["say"] and "share the customer support number from the knowledge base"
      in result["say"], result["say"])

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
check("no customer details or address", contains_none(text, MOCK_PRIVATE), text)
check("area only in the message, not the chauffeur fields",
      "Bengaluru" not in json.dumps(details) and "Bengaluru" not in result["say"], text)

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
# 6b. VERIFICATION_MODE: "otp" (default) and "none" (UAT demos only)
# ----------------------------------------------------------------------
print("\n--- VERIFICATION_MODE ---")


def listed_names():
    return {t["name"] for t in rpc("tools/list").json()["result"]["tools"]}


# "otp" (the default): as before.
check("VERIFICATION_MODE is otp by default", config.VERIFICATION_MODE == "otp")
check("otp: use_mobile_number is not listed", "use_mobile_number" not in listed_names())
result, text = call("use_mobile_number", {"mobile_number": "9000000001"}, channel="web", conversation_id="vm-otp")
check("otp: use_mobile_number -> not_available", result["outcome"] == "not_available", text)
check("otp: ...and the number is not trusted", identity.get_session("vm-otp")["verified_mobile"] is None)
browser_g = browser()
check("otp: /api/session says verification 'otp'",
      site_get(browser_g, "/api/session").json().get("verification") == "otp")
response = site_post(browser_g, "/api/login", {"mobile": "9000000001"})
check("otp: POST /api/login -> 404", response.status_code == 404, response.text)
check("otp: ...and not signed in", site_get(browser_g, "/api/session").json()["signed_in"] is False)
check("page: switches to the one-step login when verification is 'none'",
      'now.verification === "none"' in page and 'postJSON("/api/login",' in page and 'id="code-test-hint"' in page)

# "none": no OTP anywhere.
config.VERIFICATION_MODE = "none"
names = listed_names()
check("none: send_otp and verify_otp are not listed",
      "send_otp" not in names and "verify_otp" not in names, names)
check("none: use_mobile_number is listed", "use_mobile_number" in names, names)
dispatch_tool = next(t for t in rpc("tools/list").json()["result"]["tools"] if t["name"] == "check_dispatch_status")
check("none: booking tool description points to use_mobile_number, not send_otp",
      "use_mobile_number" in dispatch_tool["description"] and "send_otp" not in dispatch_tool["description"])

result, text = call("check_dispatch_status", channel="web", conversation_id="vm-none")
check("none: web chat without a number -> verification_needed", result["outcome"] == "verification_needed", text)
check("none: verification_needed asks for the number and points to use_mobile_number",
      "mobile number" in result["say"] and "use_mobile_number" in result.get("next_step", "")
      and "code" not in result["say"], text)
result, text = call("use_mobile_number", {"mobile_number": "+91 90000 00001"}, channel="web", conversation_id="vm-none")
check("none: use_mobile_number -> ok", result["outcome"] == "ok", text)
check("none: ok -> does not read the number back", "90000" not in text and "0001" not in text, text)
result, text = call("check_dispatch_status", channel="web", conversation_id="vm-none")
check("none: use_mobile_number then check_dispatch_status -> 'one'", result["outcome"] == "one", text)

result, text = call("use_mobile_number", {"mobile_number": "12345"}, channel="web", conversation_id="vm-invalid")
check("none: use_mobile_number with an invalid number -> invalid_number",
      result["outcome"] == "invalid_number", text)
check("none: ...and nothing is trusted", identity.get_session("vm-invalid")["verified_mobile"] is None)

result, text = call("send_otp", {"mobile_number": "9000000001"}, channel="web", conversation_id="vm-none-otp")
check("none: send_otp if called anyway -> not_available", result["outcome"] == "not_available", text)

# Phone, WhatsApp and SMS: the Perfox number only, never a typed one.
result, text = call("use_mobile_number", {"mobile_number": "9000000001"}, phone="919000000003",
                    channel="whatsapp", conversation_id="vm-wa")
check("none: use_mobile_number on whatsapp -> not_needed", result["outcome"] == "not_needed", text)
result, text = call("check_dispatch_status", phone="919000000003", channel="whatsapp", conversation_id="vm-wa")
check("none: whatsapp still uses the Perfox number", result["outcome"] == "none", text)

# Website one-step login, then the chat needs no number.
browser_h = browser()
session_h = site_get(browser_h, "/api/session").json()
check("none: /api/session says verification 'none'", session_h.get("verification") == "none", session_h)
response = site_post(browser_h, "/api/login/send-code", {"mobile": "9000000001"})
check("none: /api/login/send-code -> 404", response.status_code == 404, response.text)
response = site_post(browser_h, "/api/login/verify", {"code": MOCK_CODE})
check("none: /api/login/verify -> 404", response.status_code == 404, response.text)
response = site_post(browser_h, "/api/login", {"mobile": "12345"})
check("none: /api/login with an invalid number -> 400 invalid_number",
      response.status_code == 400 and response.json()["outcome"] == "invalid_number", response.text)
response = site_post(browser_h, "/api/login", {"mobile": "9000000001"}, headers={"Origin": "https://evil.example"})
check("none: /api/login from a foreign Origin -> 403", response.status_code == 403)
old_sid_h = sid_of(browser_h)
response = site_post(browser_h, "/api/login", {"mobile": "90000 00001"})
check("none: /api/login -> signed in straight away",
      response.status_code == 200 and response.json()["signed_in"] is True, response.text)
check("none: cookie value changes after logging in", sid_of(browser_h) and sid_of(browser_h) != old_sid_h)
session_h2 = site_get(browser_h, "/api/session").json()
check("none: /api/session shows the masked mobile and the same chat_token",
      session_h2["signed_in"] is True and session_h2["masked_mobile"] == "••••0001"
      and session_h2["chat_token"] == session_h["chat_token"], session_h2)
result, text = call("check_dispatch_status", channel="web", conversation_id="vm-site",
                    user_token=session_h2["chat_token"])
check("none: after website login the chat needs no number", result["outcome"] == "one", text)

# A chat use_mobile_number signs in the matching browser session only.
browser_i, browser_j = browser(), browser()
token_i = site_get(browser_i, "/api/session").json()["chat_token"]
site_get(browser_j, "/api/session")
result, text = call("use_mobile_number", {"mobile_number": "9000000002"}, channel="web",
                    conversation_id="vm-chat", user_token=token_i)
check("none: chat use_mobile_number with a website token -> ok", result["outcome"] == "ok", text)
session_i = site_get(browser_i, "/api/session").json()
check("none: ...that browser is signed in",
      session_i["signed_in"] is True and session_i["masked_mobile"] == "••••0002", session_i)
check("none: ...another browser stays signed out", site_get(browser_j, "/api/session").json()["signed_in"] is False)

# Logout still works: the old token can't be signed in again from the chat.
site_post(browser_i, "/api/logout")
result, text = call("use_mobile_number", {"mobile_number": "9000000002"}, channel="web",
                    conversation_id="vm-chat", user_token=token_i)
check("none: use_mobile_number with an old token -> session_ended", result["outcome"] == "session_ended", text)

log_lines.clear()
log_startup_warnings()
check("none: startup logs the 'Verification is OFF' warning",
      any('"verification_off"' in line and "Verification is OFF: anyone can check any number's bookings. "
          "UAT demo only." in line for line in log_lines), log_lines)
config.VERIFICATION_MODE = "otp"


# The safety lock, checked in a fresh Python (settings are read once at import).
def start_server(**settings):
    return subprocess.run([sys.executable, "-c", "import app.server"],
                          env={**os.environ, **settings}, capture_output=True, text=True)


PROD_URL = "https://drivecarclub.com/CarclubCRM/Api/Home/DespatchedBooking"
started = start_server(VERIFICATION_MODE="none", DCC_BOOKING_URL=PROD_URL)
check("none + non-UAT booking URL -> the server refuses to start",
      started.returncode != 0 and "Refusing to start" in started.stderr, started.stdout + started.stderr)
started = start_server(VERIFICATION_MODE="none", DCC_BOOKING_URL=PROD_URL.replace("CRM", "crmuat"))
check("none + UAT booking URL (any case) -> starts, with the warning",
      started.returncode == 0 and "Verification is OFF" in started.stdout, started.stdout + started.stderr)
started = start_server(VERIFICATION_MODE="otp", DCC_BOOKING_URL=PROD_URL)
check("otp + non-UAT booking URL -> starts", started.returncode == 0, started.stdout + started.stderr)

# ----------------------------------------------------------------------
# 6c. WHATSAPP_NUMBER_MODE: "auto" (default) and "ask"
# ----------------------------------------------------------------------
print("\n--- WHATSAPP_NUMBER_MODE ---")

WA_ASK_SAY = "registered mobile number you booked with (10 digits, no country code needed)"


def whatsapp(tool, arguments=None, conversation_id="wa-1", phone="919000000001"):
    """Call a tool on WhatsApp. The sender's number is 919000000001 (one booking)."""
    return call(tool, arguments, phone=phone, channel="whatsapp", conversation_id=conversation_id)


# "auto" (the default): unchanged, the sender's number is trusted.
check("WHATSAPP_NUMBER_MODE is auto by default", config.WHATSAPP_NUMBER_MODE == "auto")
result, text = whatsapp("check_dispatch_status", conversation_id="wa-auto")
check("auto: whatsapp uses the sender's number -> 'one'", result["outcome"] == "one", text)
result, text = whatsapp("send_otp", {"mobile_number": "9000000003"}, conversation_id="wa-auto")
check("auto: send_otp on whatsapp -> not_needed", result["outcome"] == "not_needed", text)
check("auto: descriptions don't mention WhatsApp for the OTP tools",
      all("WhatsApp" not in t["description"] for t in tools.listed_tools() if t["name"] in tools.OTP_TOOLS))

# "ask" with VERIFICATION_MODE=otp: the typed number must pass an OTP.
config.WHATSAPP_NUMBER_MODE = "ask"
result, text = whatsapp("check_dispatch_status", conversation_id="wa-otp")
check("ask: whatsapp ignores the sender's number -> verification_needed",
      result["outcome"] == "verification_needed", text)
check("ask: verification_needed leaks no booking", "booking" not in result and "Bengaluru" not in text, text)
check("ask: whatsapp 'say' asks for the registered number (10 digits)", WA_ASK_SAY in result["say"], text)
check("ask + otp: next_step points to send_otp", "send_otp" in result.get("next_step", ""), text)
result, text = whatsapp("get_chauffeur_details", {"booking_ref": "DCC100101"}, conversation_id="wa-otp")
check("ask: get_chauffeur_details on whatsapp -> verification_needed",
      result["outcome"] == "verification_needed" and "Ramesh" not in text, text)
result, text = call("check_dispatch_status", phone="919000000001", channel="web", conversation_id="wa-web")
check("ask: web chat 'say' wording unchanged", WA_ASK_SAY not in result["say"], text)

result, text = whatsapp("send_otp", {"mobile_number": "9000000003"}, conversation_id="wa-otp")
check("ask + otp: send_otp works on whatsapp", result["outcome"] == "sent", text)
result, text = whatsapp("check_dispatch_status", conversation_id="wa-otp")
check("ask + otp: still verification_needed before the code", result["outcome"] == "verification_needed", text)
result, text = whatsapp("verify_otp", {"code": MOCK_CODE}, conversation_id="wa-otp")
check("ask + otp: verify_otp works on whatsapp", result["outcome"] == "verified", text)
result, text = whatsapp("check_dispatch_status", conversation_id="wa-otp")
check("ask + otp: then the TYPED number is used, not the sender's", result["outcome"] == "none", text)

# Phone and SMS: unchanged, the caller ID is trusted.
result, text = call("check_dispatch_status", phone="919000000001", channel="phone", conversation_id="wa-phone")
check("ask: phone still uses the caller ID", result["outcome"] == "one", text)
result, text = call("check_dispatch_status", phone="919000000001", channel="sms", conversation_id="wa-sms")
check("ask: sms still uses the caller ID", result["outcome"] == "one", text)
result, text = call("send_otp", {"mobile_number": "9000000003"}, phone="919000000001", channel="phone",
                    conversation_id="wa-phone")
check("ask: send_otp on phone -> not_needed", result["outcome"] == "not_needed", text)

check("ask: OTP tool descriptions mention WhatsApp",
      all("Website chat and WhatsApp only." in t["description"]
          for t in tools.listed_tools() if t["name"] in tools.OTP_TOOLS))

log_lines.clear()
log_startup_warnings()
check("ask: startup logs the whatsapp_number_ask warning",
      any('"whatsapp_number_ask"' in line for line in log_lines), log_lines)

# "ask" with VERIFICATION_MODE=none (UAT demos): use_mobile_number on WhatsApp.
config.VERIFICATION_MODE = "none"
check("ask + none: use_mobile_number is listed and mentions WhatsApp",
      any(t["name"] == "use_mobile_number" and "WhatsApp" in t["description"] for t in tools.listed_tools()))
# The sender's number (919000000003) has no bookings; the typed one has one.
result, text = whatsapp("check_dispatch_status", conversation_id="wa-none", phone="919000000003")
check("ask + none: whatsapp -> verification_needed", result["outcome"] == "verification_needed", text)
check("ask + none: 'say' asks for the registered number, no code",
      WA_ASK_SAY in result["say"] and "one-time code" not in result["say"] and "SMS" not in result["say"], text)
check("ask + none: next_step points to use_mobile_number", "use_mobile_number" in result.get("next_step", ""), text)
result, text = whatsapp("use_mobile_number", {"mobile_number": "12345"}, conversation_id="wa-none",
                        phone="919000000003")
check("ask + none: invalid typed number -> invalid_number", result["outcome"] == "invalid_number", text)
result, text = whatsapp("use_mobile_number", {"mobile_number": "90000 00001"}, conversation_id="wa-none",
                        phone="919000000003")
check("ask + none: use_mobile_number on whatsapp -> ok", result["outcome"] == "ok", text)
check("ask + none: ok -> does not read the number back", "90000" not in text and "0001" not in text, text)
result, text = whatsapp("check_dispatch_status", conversation_id="wa-none", phone="919000000003")
check("ask + none: use_mobile_number then check_dispatch_status -> 'one'", result["outcome"] == "one", text)
result, text = whatsapp("check_dispatch_status", conversation_id="wa-none-other", phone="919000000003")
check("ask + none: another conversation still needs a number", result["outcome"] == "verification_needed", text)

# The safety lock, in this process (the VERIFICATION_MODE lock would stop a
# real start first, so this one is also checked on its own).
config.DCC_BOOKING_URL = PROD_URL
try:
    check_whatsapp_number_lock()
    check("ask + none + non-UAT URL -> whatsapp lock refuses", False, "no SystemExit")
except SystemExit as stop:
    check("ask + none + non-UAT URL -> whatsapp lock refuses",
          "Refusing to start" in str(stop) and "WHATSAPP_NUMBER_MODE=ask" in str(stop), str(stop))
config.VERIFICATION_MODE = "otp"
try:
    check_whatsapp_number_lock()
    check("ask + otp + non-UAT URL -> whatsapp lock allows it", True)
except SystemExit as stop:
    check("ask + otp + non-UAT URL -> whatsapp lock allows it", False, str(stop))
config.DCC_BOOKING_URL = os.environ["DCC_BOOKING_URL"]
config.WHATSAPP_NUMBER_MODE = "auto"

# ...and in a fresh Python, the way the server really starts.
started = start_server(WHATSAPP_NUMBER_MODE="ask", VERIFICATION_MODE="none", DCC_BOOKING_URL=PROD_URL)
check("ask + none + non-UAT booking URL -> the server refuses to start",
      started.returncode != 0 and "Refusing to start" in started.stderr, started.stdout + started.stderr)
started = start_server(WHATSAPP_NUMBER_MODE="ask", VERIFICATION_MODE="none", DCC_BOOKING_URL=PROD_URL.replace("CRM", "CRMUAT"))
check("ask + none + UAT booking URL -> starts, with the warning",
      started.returncode == 0 and "whatsapp_number_ask" in started.stdout, started.stdout + started.stderr)
started = start_server(WHATSAPP_NUMBER_MODE="ask", VERIFICATION_MODE="otp", DCC_BOOKING_URL=PROD_URL)
check("ask + otp + non-UAT booking URL -> starts, with the warning",
      started.returncode == 0 and "whatsapp_number_ask" in started.stdout, started.stdout + started.stderr)

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

# ----------------------------------------------------------------------
# 11a. MOCK_NUMBER_SCENARIOS: fake bookings for chosen test numbers
# (fake numbers only); every other number still uses DCC_MODE.
# ----------------------------------------------------------------------
print("\n--- MOCK_NUMBER_SCENARIOS ---")

MOCK_NUMBERS = {"today": "919000000101", "several": "919000000102", "not_allocated": "919000000103",
                "none": "919000000104", "down": "919000000105", "slow": "919000000106"}
config.MOCK_NUMBER_SCENARIOS = ["919000000101=sunny", "12345=today", "919000000104=none"]
check("an unreadable entry is reported by its position", tools.mock.mock_numbers()[1] == [1, 2],
      tools.mock.mock_numbers()[1])
# Written in different formats, to prove they are normalised.
config.MOCK_NUMBER_SCENARIOS = ["+91 90000 00101=today", "09000000102=several", "9000000103 = not_allocated",
                                "919000000104=none", "0091 9000000105=DOWN", "919000000106=slow"]
check("mock numbers are normalised",
      tools.mock.mock_numbers() == ({number: scenario for scenario, number in MOCK_NUMBERS.items()}, []))

use_modes("live", "mock")  # every other number goes to the fake DriveCarClub server
log_lines.clear()
before = dict(counts)


def mock_call(scenario, tool="check_dispatch_status", arguments=None, channel="phone"):
    return call(tool, arguments, phone=MOCK_NUMBERS[scenario], channel=channel,
                conversation_id=f"mock-{scenario}")


result, text = mock_call("today")
check("today: one booking, chauffeur allocated",
      result["outcome"] == "one" and result["booking"]["booking_ref"] == "MOCK-1001"
      and result["booking"]["chauffeur_allocated"] is True, text)
result, text = mock_call("today", "get_chauffeur_details", {"booking_ref": "MOCK-1001"})
check("today: chauffeur details shared (first name only)",
      result["outcome"] == "shared" and result["chauffeur"]["chauffeur_first_name"] == "Rajesh"
      and result["chauffeur"]["car_model"] == "Swift Dzire"
      and result["chauffeur"]["registration_number"] == "KA00MK0001" and "TEST" not in text, text)

result, text = mock_call("several")
check("several: today and tomorrow, earliest first",
      result["outcome"] == "several"
      and [b["booking_ref"] for b in result["bookings"]] == ["MOCK-2001", "MOCK-2002"], text)
result, text = mock_call("several", "get_chauffeur_details", {"booking_ref": "MOCK-2001"})
check("several: today's booking has its own chauffeur and car",
      result["outcome"] == "shared" and result["chauffeur"]["chauffeur_first_name"] == "Sunil"
      and result["chauffeur"]["car_model"] == "Toyota Innova Crysta", text)
result, text = mock_call("several", "get_chauffeur_details", {"booking_ref": "MOCK-2002"})
check("several: tomorrow's booking has a different chauffeur and car",
      result["outcome"] == "shared" and result["chauffeur"]["chauffeur_first_name"] == "Vikram"
      and result["chauffeur"]["car_model"] == "Honda City", text)

result, text = mock_call("not_allocated")
check("not_allocated: one booking tomorrow, no chauffeur yet",
      result["outcome"] == "one" and result["booking"]["booking_ref"] == "MOCK-3001"
      and result["booking"]["chauffeur_allocated"] is False, text)
result, text = mock_call("not_allocated", "get_chauffeur_details", {"booking_ref": "MOCK-3001"})
check("not_allocated: get_chauffeur_details -> not_allocated", result["outcome"] == "not_allocated", text)

result, text = mock_call("none")
check("none: no bookings", result["outcome"] == "none", text)
result, text = mock_call("none", "get_chauffeur_details", {"booking_ref": "MOCK-1001"})
check("none: another scenario's booking -> not_found", result["outcome"] == "not_found", text)

for scenario, reason in (("down", "server_error"), ("slow", "timeout")):
    for tool, arguments in (("check_dispatch_status", None), ("get_chauffeur_details", {"booking_ref": "MOCK-1001"})):
        result, text = mock_call(scenario, tool, arguments)
        check(f"{scenario}: {tool} -> unavailable / {reason}",
              result["outcome"] == "unavailable" and result.get("reason") == reason, text)
check("down: alert_perfox_team is logged, with source mock_number",
      any('"alert_perfox_team"' in line and '"source": "mock_number"' in line for line in log_lines))

result, text = mock_call("today", channel="whatsapp")
check("works on WhatsApp", result["outcome"] == "one" and result["booking"]["booking_ref"] == "MOCK-1001", text)
config.TRUST_WEB_PHONE = True  # stands in for a verified website chat number
result, text = mock_call("today", channel="web")
config.TRUST_WEB_PHONE = False
check("works on website chat", result["outcome"] == "one" and result["booking"]["booking_ref"] == "MOCK-1001", text)
check("mock numbers never call DriveCarClub",
      counts["bookings"] == before["bookings"] and counts["token"] == before["token"], counts)

mock_logs = [json.loads(line) for line in log_lines if '"tool_call"' in line]
check("every mock answer is logged with source mock_number",
      len(mock_logs) == 15 and all(entry.get("source") == "mock_number" for entry in mock_logs), mock_logs)

log_lines.clear()
result, text = call("check_dispatch_status", phone="919000000001")
check("a non-mock number still uses the live API",
      result["outcome"] == "one" and result["booking"]["booking_ref"] == "LIVE1"
      and counts["bookings"] > before["bookings"], text)
live_logs = [json.loads(line) for line in log_lines if '"tool_call"' in line]
check("a non-mock number's log line has no source", live_logs and "source" not in live_logs[0], live_logs)

log_lines.clear()
log_startup_warnings()
mock_warnings = [json.loads(line) for line in log_lines if '"mock_numbers_on"' in line]
check("startup warns with the count of mock numbers", mock_warnings and mock_warnings[0]["count"] == 6, log_lines)


def has_mock_number(text):
    """True if any mock number (with or without 91) is in the text."""
    return any(number in text or number[2:] in text for number in MOCK_NUMBERS.values())


check("no log line has a mock number", not any(has_mock_number(line) for line in all_log_lines))

# The safety lock, in a fresh Python (settings are read once at import).
UAT_URL = PROD_URL.replace("CRM", "CRMUAT")
started = start_server(MOCK_NUMBER_SCENARIOS="919000000101=today,919000000105=down", DCC_BOOKING_URL=PROD_URL)
check("mock numbers + non-UAT booking URL -> the server refuses to start",
      started.returncode != 0 and "Refusing to start: MOCK_NUMBER_SCENARIOS" in started.stderr,
      started.stdout + started.stderr)
check("refusal output has no mock number", not has_mock_number(started.stdout + started.stderr))
started = start_server(MOCK_NUMBER_SCENARIOS="919000000101=today,919000000105=down", DCC_BOOKING_URL=UAT_URL)
check("mock numbers + UAT booking URL -> starts, warning with the count",
      started.returncode == 0 and '"mock_numbers_on"' in started.stdout and '"count": 2' in started.stdout,
      started.stdout + started.stderr)
check("startup output has no mock number", not has_mock_number(started.stdout + started.stderr))
started = start_server(MOCK_NUMBER_SCENARIOS="919000000101=sunny", DCC_BOOKING_URL=UAT_URL)
check("unknown scenario -> the server refuses to start, without the number",
      started.returncode != 0 and "Refusing to start" in started.stderr
      and not has_mock_number(started.stdout + started.stderr), started.stdout + started.stderr)
started = start_server(MOCK_NUMBER_SCENARIOS="", DCC_BOOKING_URL=PROD_URL)
check("empty MOCK_NUMBER_SCENARIOS + non-UAT URL -> starts, no warning",
      started.returncode == 0 and "mock_numbers_on" not in started.stdout, started.stdout + started.stderr)

# Optional customer name per number: "number=scenario:Name" (fake names only).
config.MOCK_NUMBER_SCENARIOS = ["+91 90000 00101=today:  Zephyrina   Testname ", "09000000102=several",
                                "919000000103=not_allocated:", "919000000104=None:Quillon"]
check("names: mock_numbers still gives number -> scenario (the name is not part of it)",
      tools.mock.mock_numbers() == ({"919000000101": "today", "919000000102": "several",
                                     "919000000103": "not_allocated", "919000000104": "none"}, []),
      tools.mock.mock_numbers())
check("names: the name given, tidied; none or empty -> ''",
      [tools.mock.customer_name_for(n) for n in ("919000000101", "919000000102", "919000000103", "919000000104",
                                                  "919000000001", None)]
      == ["Zephyrina Testname", "", "", "Quillon", "", ""])
check("names: a name given -> the mock bookings' customer name",
      [b["individualsname"] for b in tools._mock_numbers.get_bookings("919000000101")] == ["Zephyrina Testname"])
check("names: no name given -> the scenario's own mock name",
      [b["individualsname"] for b in tools._mock_numbers.get_bookings("919000000102")] == ["TARUN TEST", "TARUN TEST"]
      and [b["individualsname"] for b in tools._mock_numbers.get_bookings("919000000103")] == ["POOJA TEST"])
named, plain = tools._mock_numbers.get_bookings("919000000101")[0], tools.mock.SCENARIOS["today"]()[0]
check("names: only the customer name changes",
      {**named, "individualsname": ""} == {**plain, "individualsname": "", "b_Guest_Contact_No": "919000000101"})
log_lines.clear()
name_texts = [mock_call("today")[1], mock_call("today", "get_chauffeur_details", {"booking_ref": "MOCK-1001"})[1],
              mock_call("today", channel="whatsapp")[1]]
check("names: the booking tools still work", all('"outcome": "one"' in t or '"shared"' in t for t in name_texts),
      name_texts)
check("names: never in a tool result", all(contains_none(t, ["Zephyrina", "Testname", "Quillon"]) for t in name_texts),
      name_texts)
log_startup_warnings()
check("names: never in a log line (tool calls and startup)",
      log_lines and all(contains_none(l, ["Zephyrina", "Testname", "Quillon"]) for l in log_lines), log_lines)
started = start_server(MOCK_NUMBER_SCENARIOS="919000000101=today:Zephyrina,919000000104=none:Quillon",
                       DCC_BOOKING_URL=UAT_URL)
check("names: the server starts, and its output has no name or number",
      started.returncode == 0 and '"count": 2' in started.stdout
      and contains_none(started.stdout + started.stderr, ["Zephyrina", "Quillon"])
      and not has_mock_number(started.stdout + started.stderr), started.stdout + started.stderr)
started = start_server(MOCK_NUMBER_SCENARIOS="919000000101=sunny:Zephyrina", DCC_BOOKING_URL=UAT_URL)
check("names: a bad entry is refused without the name or number",
      started.returncode != 0 and "number=scenario:Name" in started.stderr
      and contains_none(started.stdout + started.stderr, ["Zephyrina"])
      and not has_mock_number(started.stdout + started.stderr), started.stdout + started.stderr)

config.MOCK_NUMBER_SCENARIOS = []
before = dict(counts)
result, text = call("check_dispatch_status", phone=MOCK_NUMBERS["today"])
check("setting empty -> the number follows DCC_MODE again (the live API)",
      result["outcome"] == "one" and result["booking"]["booking_ref"] == "LIVE1"
      and counts["bookings"] > before["bookings"], text)

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
# 11b. Ready-to-send messages ("message" field, messages.py)
# ----------------------------------------------------------------------
print("\n--- messages ---")

now = datetime.now(INDIA)
TODAY_TEXT = now.strftime("%d %b %Y")
TOMORROW_TEXT = (now + timedelta(days=1)).strftime("%d %b %Y")
all_messages = []


def message_of(result):
    all_messages.append(result.get("message", ""))
    return result.get("message")


result, _ = call("check_dispatch_status", phone="919000000001")
check("message: one booking", message_of(result) == (
    "Good news, your car's on its way 🚗\n\n"
    "🚗 Car: Toyota Innova Crysta (KA01AB1234)\n"
    "📍 Pickup area: Bengaluru\n"
    f"🕒 Pickup time: {TODAY_TEXT}, 6:30 PM\n"
    "🔖 Booking: DCC100101\n"
    "✅ Status: dispatched and on its way\n\n"
    "Would you like your chauffeur's details?"), result.get("message"))
check("message: existing fields kept", result["outcome"] == "one" and result.get("booking") and result.get("say"))

result, _ = call("check_dispatch_status", phone="919000000002")
check("message: several bookings (time, area, ref only)", message_of(result) == (
    "I can see 2 upcoming pickups for this number:\n\n"
    f"1)\n🕒 Pickup time: {TODAY_TEXT}, 3:00 PM\n📍 Pickup area: Hosur\n🔖 Booking: DCC100202\n\n"
    f"2)\n🕒 Pickup time: {TOMORROW_TEXT}, 8:15 AM\n📍 Pickup area: Whitefield\n🔖 Booking: DCC100203\n\n"
    "Which pickup do you mean?"), result.get("message"))

result, _ = call("check_dispatch_status", phone="919000000003")
check("message: no booking", message_of(result) == (
    "I can't see a car sent for this number yet.\n\n"
    "If you need help with a booking, please contact our customer support team."), result.get("message"))

result, _ = call("check_dispatch_status", phone="919000000004")
message = message_of(result) or ""
check("message: one booking, no chauffeur yet -> no car line, says not assigned",
      "Car:" not in message and "on its way" not in message and "🔖 Booking: DCC100401" in message
      and message.startswith("I can see your booking 🚗\n\n📍 Pickup area: Jayanagar\n")
      and message.endswith("✅ Status: Confirmed\n\nA chauffeur hasn't been assigned yet. "
                            "You'll get the details once they are."), message)

result, _ = call("get_chauffeur_details", {"booking_ref": "DCC100101"}, phone="919000000001")
check("message: chauffeur shared", message_of(result) == (
    "Here are your chauffeur and car details 🧑‍✈️\n\n"
    "🧑‍✈️ Chauffeur: Ramesh\n"
    "🚗 Car: Toyota Innova Crysta (KA01AB1234)\n"
    f"🕒 Pickup time: {TODAY_TEXT}, 6:30 PM\n"
    "📍 Pickup area: Bengaluru\n\n"
    "Have a comfortable ride."), result.get("message"))

result, _ = call("get_chauffeur_details", {"booking_ref": "DCC100401"}, phone="919000000004")
check("message: chauffeur not allocated", message_of(result) ==
      "Your chauffeur hasn't been assigned yet. You'll get the details once they are.", result.get("message"))

fares.clear_cache()
result, _ = fare()
check("message: fares", message_of(result) == (
    "Standard fares for a Midsize car, Local - within city limits, in Gurugram:\n\n"
    "8hrs 80kms: Rs 2,400 · extra km Rs 18 · extra hour Rs 150\n"
    "4hrs 40kms: Rs 1,350 · extra km Rs 18 · extra hour Rs 150\n\n"
    f"{config.FARE_NOTE}"), result.get("message"))
result, _ = fare(service_type="Outstation")
check("message: outstation fares include allowance and night stay",
      "250kms per day: Rs 4,500 · extra km Rs 18 · extra hour Rs 0 · outstation allowance Rs 300 · night stay Rs 300"
      in (message_of(result) or ""), result.get("message"))
check("message: Indian grouping and paise",
      [messages.rupees(a) for a in (999, 150000, 12345678, 18.5)]
      == ["Rs 999", "Rs 1,50,000", "Rs 1,23,45,678", "Rs 18.50"])

result, _ = call("check_dispatch_status", phone="919000000005")
check("message: unavailable has no message (the AI uses 'say')", "message" not in result, result)
result, _ = fare(city_name="Atlantis")
check("message: fare not_found has no message", "message" not in result, result)

# A missing value leaves its line out (and never shows "None").
bare = {"booking_ref": "DCC9", "pickup_at": None, "pickup_area": "", "status": "",
        "driver_name": "RAMESH KUMAR", "driver_mobile": "9845012345", "car_model": "",
        "car_colour": "", "registration_number": "KA01AB1234"}
message = messages.dispatch_one(bare)
all_messages.append(message)
check("message: missing area, time, status and model -> lines left out",
      "Pickup area" not in message and "Pickup time" not in message and "Status" not in message
      and "🚗 Car: KA01AB1234" in message and "None" not in message, message)
message = messages.chauffeur_shared({**bare, "registration_number": "", "car_model": "Honda City"})
all_messages.append(message)
check("message: chauffeur card with missing values",
      "🚗 Car: Honda City" in message and "Pickup" not in message and "None" not in message, message)
message = messages.dispatch_one({**bare, "booking_ref": "", "registration_number": "", "driver_name": ""})
all_messages.append(message)
check("message: no detail lines at all -> heading and closing with one blank line",
      message == "I can see your booking 🚗\n\n"
                 "A chauffeur hasn't been assigned yet. You'll get the details once they are.", message)

# Never: customer name, company, guest number, address, standing instructions,
# VIP flag, the chauffeur's surname or mobile, or a support phone number.
FORBIDDEN = MOCK_PRIVATE + ["KUMAR", "Kumar", "Irfan", "Khan", "9845012345", "9845033333", "vip"]
all_messages = [m for m in all_messages if m]
check("messages were checked", len(all_messages) == 11, len(all_messages))
check("no message has two blank lines in a row, or starts or ends with one",
      all("\n\n\n" not in m and m == m.strip("\n") for m in all_messages))
check("no message has a forbidden field", all(contains_none(m, FORBIDDEN) for m in all_messages),
      [m for m in all_messages if not contains_none(m, FORBIDDEN)])
check("no message has a phone number or support number",
      not any(re.search(r"\d{10}", m) or "support team on" in m or tools.SUPPORT_PLACEHOLDER in m
              for m in all_messages))
check("messages are plain text: no markdown or JSON",
      all(not any(mark in m for mark in ("**", "`", "#", "{", "}", "[", "]", "\\n")) for m in all_messages))
check("cards use real line breaks", sum("\n" in m for m in all_messages) == 10)
check("booking and fare tool descriptions tell the AI to send 'message' as it is",
      all("send it to the customer exactly as written" in t["description"]
          for t in tools.TOOLS if t["name"] in tools.BOOKING_TOOLS + tools.FARE_TOOLS))

# ----------------------------------------------------------------------
# 12a. send_call_summary: the end-of-call WhatsApp summary (fake Perfox server)
# ----------------------------------------------------------------------
print("\n--- send_call_summary ---")

use_modes("mock", "mock")  # mock bookings and fares
PERFOX_TEST_KEY = "perfox-secret-key-123"
perfox_requests = []
perfox_reply = {"status": 201, "body": {"status": "sent", "send_authorized": True}, "delay": 0}


class FakePerfox(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        perfox_requests.append({"path": self.path, "auth": self.headers.get("Authorization"),
                                "body": json.loads(self.rfile.read(length) or b"{}")})
        time.sleep(perfox_reply["delay"])
        data = json.dumps(perfox_reply["body"]).encode()
        try:
            self.send_response(perfox_reply["status"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except ConnectionError:
            pass  # the client already gave up (the timeout test)


perfox_server = ThreadingHTTPServer(("127.0.0.1", 0), FakePerfox)
threading.Thread(target=perfox_server.serve_forever, daemon=True).start()
perfox_base = f"http://127.0.0.1:{perfox_server.server_address[1]}"


def reply_with(status=201, body=None, delay=0):
    perfox_reply.update(status=status, body=body if body is not None else {"status": "sent", "send_authorized": True},
                        delay=delay)


summary_conversations = iter(range(1, 1000))


def summary(arguments, phone="919000000001", channel="phone", conversation_id=None):
    """Call send_call_summary in a NEW conversation (unless one is given)."""
    return call("send_call_summary", arguments, phone=phone, channel=channel,
                conversation_id=conversation_id or f"sum-{next(summary_conversations)}")


def summary_logs():
    return [json.loads(line) for line in log_lines if '"send_call_summary"' in line and '"tool_call"' in line]


CHAUFFEUR = {"topic": "chauffeur", "booking_ref": "DCC100101"}
FARE = {"topic": "fare", "car_category": "Midsize", "trip_type": "Local", "city": "Gurugram"}
CHAUFFEUR_PARAMETERS = ["DCC100101", f"{TODAY_TEXT}, 6:30 PM, Bengaluru", "Ramesh", "Toyota Innova Crysta",
                        "KA01AB1234"]
FARE_PARAMETERS = ["Midsize", "Local - within city limits", "Gurugram",
                   "8hrs 80kms Rs 2,400; 4hrs 40kms Rs 1,350 (extra km Rs 18, extra hour Rs 150)"]

check("send_call_summary is listed, phone only, after the caller agreed",
      any(t["name"] == "send_call_summary" and "Phone calls only" in t["description"]
          and "ONLY at the end of a phone call" in t["description"] and "agreed" in t["description"]
          for t in rpc("tools/list").json()["result"]["tools"]))
summary_tool = next(t for t in tools.TOOLS if t["name"] == "send_call_summary")
check("send_call_summary takes no phone number argument",
      not any("mobile" in name or "phone" in name or name == "to" for name in summary_tool["input_schema"]["properties"])
      and summary_tool["input_schema"]["additionalProperties"] is False)

# --- Mock mode (the default): nothing is sent ---
call_summary.clear_limits()
log_lines.clear()
result, text = summary(CHAUFFEUR)
check("mock: chauffeur summary -> sent", result["outcome"] == "sent" and "on its way" in result["say"], text)
check("mock: nothing sent to Perfox", perfox_requests == [])
mock_lines = [json.loads(line) for line in log_lines if '"whatsapp_summary_mock"' in line]
check("mock: logs only the template name and the parameter count",
      len(mock_lines) == 1 and set(mock_lines[0]) == {"ts", "level", "event", "template", "parameter_count"}
      and mock_lines[0]["template"] == "test_chauffeur_summary" and mock_lines[0]["parameter_count"] == 5, mock_lines)
check("mock: tool log has provider_status 'mock'", summary_logs()[-1]["provider_status"] == "mock", summary_logs())

# --- Live mode against the fake Perfox server ---
config.WA_SUMMARY_MODE = "live"
config.PERFOX_API_BASE = perfox_base + "/"  # a trailing slash is fine
config.PERFOX_API_KEY = PERFOX_TEST_KEY
config.PERFOX_AGENT_ID = "agent-test-1"
reply_with()

call_summary.clear_limits()
log_lines.clear()
result, text = summary(CHAUFFEUR)
sent = perfox_requests[-1] if perfox_requests else {}
check("chauffeur: sent", result["outcome"] == "sent" and len(perfox_requests) == 1, text)
check("chauffeur: URL and auth header",
      sent.get("path") == "/api/v1/outbound" and sent.get("auth") == f"Bearer {PERFOX_TEST_KEY}", sent)
check("chauffeur: exact JSON shape and parameter order", sent.get("body") == {
    "agent_id": "agent-test-1", "channel": "whatsapp", "to": "+919000000001",
    "template": {"name": "test_chauffeur_summary", "language": "en_US", "body_parameters": CHAUFFEUR_PARAMETERS},
}, sent.get("body"))
check("chauffeur: no opening_message with the template", "opening_message" not in json.dumps(sent.get("body")))
line = summary_logs()[-1]
check("chauffeur: log has tool, topic, outcome, masked number, template, count, provider status",
      line["tool"] == "send_call_summary" and line["topic"] == "chauffeur" and line["outcome"] == "sent"
      and line["mobile"] == "91******0001" and line["template"] == "test_chauffeur_summary"
      and line["parameter_count"] == 5 and line["provider_status"] == "sent", line)

result, text = summary({"topic": "chauffeur", "booking_ref": "dcc100101"})
check("chauffeur: booking_ref matched in any upper/lower case", result["outcome"] == "sent", text)

fares.clear_cache()
rate_calls = []
mock_get_rates = tools.get_rate_client().get_rates
tools.get_rate_client().get_rates = lambda *inputs: rate_calls.append(inputs) or mock_get_rates(*inputs)
result, text = summary(FARE)
sent = perfox_requests[-1]
check("fare: sent", result["outcome"] == "sent", text)
check("fare: exact JSON shape and parameter order", sent["body"] == {
    "agent_id": "agent-test-1", "channel": "whatsapp", "to": "+919000000001",
    "template": {"name": "test_fare_summary", "language": "en_GB", "body_parameters": FARE_PARAMETERS},
}, sent["body"])
summary(FARE)
check("fare: the second summary comes from the get_fare cache",
      len(rate_calls) == 1 and perfox_requests[-1]["body"]["template"]["body_parameters"] == FARE_PARAMETERS,
      rate_calls)
del tools.get_rate_client().get_rates  # back to the normal mock method
outstation = summary({"topic": "fare", "car_category": "midsize", "trip_type": "outstation", "city": "gurugram"})[0]
check("fare: outstation line has the allowance and night stay",
      outstation["outcome"] == "sent" and perfox_requests[-1]["body"]["template"]["body_parameters"][3]
      == "250kms per day Rs 4,500 (extra km Rs 18, extra hour Rs 0, outstation allowance Rs 300, night stay Rs 300)",
      perfox_requests[-1]["body"])
check("fare line: different extras per package -> each gets its own",
      call_summary.fare_line([
          {"package_name": "A", "rate_inr": 100, "extra_km_rate_inr": 1, "extra_hour_rate_inr": 2},
          {"package_name": "B", "rate_inr": 200, "extra_km_rate_inr": 3, "extra_hour_rate_inr": 4},
      ]) == "A Rs 100 (extra km Rs 1, extra hour Rs 2); B Rs 200 (extra km Rs 3, extra hour Rs 4)")

# --- Phone only, and only the caller's number ---
call_summary.clear_limits()  # the checks above used up 919000000001's 5 for today
before = len(perfox_requests)
for channel in ("whatsapp", "web", "sms", "email"):
    result, text = summary(CHAUFFEUR, channel=channel)
    check(f"{channel}: not_available", result["outcome"] == "not_available", text)
result, text = summary(CHAUFFEUR, phone=None)
check("phone without a caller number: not_available", result["outcome"] == "not_available", text)
check("...and nothing was sent", len(perfox_requests) == before)

result, text = summary({**CHAUFFEUR, "to": "+919999999999", "mobile_number": "9999999999", "phone": "9999999999"})
check("a number the AI passes is ignored: sent to the caller's number",
      result["outcome"] == "sent" and perfox_requests[-1]["body"]["to"] == "+919000000001", perfox_requests[-1])
result, text = summary(CHAUFFEUR, phone="+91 90000 00001")
check("caller number in any format -> E.164 with '+'", perfox_requests[-1]["body"]["to"] == "+919000000001")

# --- Own bookings only, allocated chauffeur, nothing missing ---
before = len(perfox_requests)
result, text = summary({"topic": "chauffeur", "booking_ref": "DCC100202"})  # 919000000002's booking
check("someone else's booking -> not_available, reveals nothing",
      result["outcome"] == "not_available" and contains_none(text, ["Irfan", "KA51EF9012", "Hosur"]), text)
result, text = summary({"topic": "chauffeur", "booking_ref": "DCC100401"}, phone="919000000004")
check("chauffeur not assigned -> not_allocated", result["outcome"] == "not_allocated", text)
result, text = summary({"topic": "chauffeur"})
check("no booking_ref -> incomplete", result["outcome"] == "incomplete", text)
result, text = summary({"topic": "fare", "car_category": "Midsize", "trip_type": "Local"})
check("fare without a city -> incomplete", result["outcome"] == "incomplete", text)
result, text = summary({"topic": "fare", "car_category": "Midsize", "trip_type": "Local", "city": "Atlantis"})
check("fare with no rate -> incomplete", result["outcome"] == "incomplete", text)
result, text = summary({"topic": "weather"})
check("unknown topic -> not_available", result["outcome"] == "not_available", text)
config.WA_TEMPLATE_FARE_NAME = ""
result, text = summary(FARE)
check("no fare template -> not_available", result["outcome"] == "not_available", text)
config.WA_TEMPLATE_FARE_NAME = "test_fare_summary"
check("...and none of these sent anything", len(perfox_requests) == before)

bare = {"booking_ref": "DCC9", "pickup_at": datetime.now(INDIA), "pickup_area": "Bengaluru", "status": "Despatched",
        "driver_name": "RAMESH KUMAR", "driver_mobile": "9845012345", "car_model": "Toyota Innova Crysta",
        "car_colour": "", "registration_number": "KA01AB1234"}
check("chauffeur parameters: all five when complete", len(call_summary.chauffeur_parameters(bare) or []) == 5)
for field in ("pickup_area", "car_model", "registration_number", "driver_name"):
    check(f"chauffeur parameters: missing {field} -> None (send nothing)",
          call_summary.chauffeur_parameters({**bare, field: ""}) is None)
check("chauffeur parameters: missing date -> None", call_summary.chauffeur_parameters({**bare, "pickup_at": None}) is None)
check("parameters are squashed to one line",
      call_summary.chauffeur_parameters({**bare, "car_model": "Toyota\nInnova\t\tCrysta     X"})[3]
      == "Toyota Innova Crysta X")
check("single-line check",
      [perfox.is_single_line(v) for v in ("ok value", "a\nb", "a\rb", "a\tb", "a    b", "a   b", "", "  ")]
      == [True, False, False, False, False, True, False, False])
try:
    perfox.send_template("+919000000001", "t", "en_US", ["fine", "two\nlines"])
    refused = None
except OutboundFailed as failure:
    refused = failure.reason
check("a multi-line parameter is never sent", refused == "bad_parameter" and len(perfox_requests) == before, refused)

# --- Limits ---
call_summary.clear_limits()
result, _ = summary(CHAUFFEUR, conversation_id="sum-limit")
again, text = summary(CHAUFFEUR, conversation_id="sum-limit")
check("limit: 2nd chauffeur summary in a conversation -> too_many_requests",
      result["outcome"] == "sent" and again["outcome"] == "too_many_requests", text)
result, text = summary(FARE, conversation_id="sum-limit")
check("limit: the other topic still works in that conversation", result["outcome"] == "sent", text)
outcomes = [summary(CHAUFFEUR)[0]["outcome"] for _ in range(4)]
check("limit: 5 per number per day, then too_many_requests",
      outcomes == ["sent", "sent", "sent", "too_many_requests"], outcomes)
result, text = summary(FARE, phone="919000000002")
check("limit: counted per number (another number still works)",
      result["outcome"] == "sent" and perfox_requests[-1]["body"]["to"] == "+919000000002", text)

# --- Failures: say sorry, offer to read it out, alert ---
call_summary.clear_limits()
failures = [
    ("201 status failed", dict(body={"status": "failed", "send_authorized": True}), "provider_failed", "failed"),
    ("201 status suppressed", dict(body={"status": "suppressed", "send_authorized": True}),
     "provider_suppressed", "suppressed"),
    ("201 sent but not authorised", dict(body={"status": "sent", "send_authorized": False}), "not_authorized", "sent"),
    ("200 instead of 201", dict(status=200), "http_200", "http_200"),
    ("400", dict(status=400, body={"error": "bad_request"}), "http_400", "http_400"),
    ("404", dict(status=404, body={"error": "not_found"}), "http_404", "http_404"),
    ("422 agent_not_published", dict(status=422, body={"error": "agent_not_published"}),
     "agent_not_published", "http_422"),
    ("422 no_trigger_for_channel", dict(status=422, body={"code": "no_trigger_for_channel"}),
     "no_trigger_for_channel", "http_422"),
    ("500 with non-JSON", dict(status=500, body="oops"), "http_500", "http_500"),
]
for label, reply, reason, provider_status in failures:
    reply_with(**reply)
    log_lines.clear()
    result, text = summary(CHAUFFEUR, conversation_id="sum-fail")
    alerts = [json.loads(l) for l in log_lines if '"alert_perfox_team"' in l]
    check(f"{label} -> failed, alert '{reason}', provider status '{provider_status}'",
          result["outcome"] == "failed" and alerts and alerts[0]["reason"] == reason
          and summary_logs()[-1]["provider_status"] == provider_status, (text, alerts, summary_logs()))
check("failed: apologises and offers to read the details out",
      "sorry" in result["say"] and "couldn't send" in result["say"] and "read the details out" in result["say"])

reply_with()
result, text = summary(CHAUFFEUR, conversation_id="sum-fail")
check("a failed summary doesn't use up the limit (retry sends)", result["outcome"] == "sent", text)

saved_timeout = config.PERFOX_TIMEOUT_SECONDS
config.PERFOX_TIMEOUT_SECONDS = 0.3
reply_with(delay=1)
log_lines.clear()
result, text = summary(CHAUFFEUR)
check("Perfox timeout -> failed with alert 'timeout'",
      result["outcome"] == "failed" and '"reason": "timeout"' in "".join(log_lines), text)
config.PERFOX_TIMEOUT_SECONDS = saved_timeout
check("timeout is 5 seconds", config.PERFOX_TIMEOUT_SECONDS == 5)
reply_with()

config.PERFOX_API_KEY = ""
result, text = summary(CHAUFFEUR)
check("live without PERFOX_API_KEY -> failed (perfox_not_configured)",
      result["outcome"] == "failed" and result["reason"] == "perfox_not_configured", text)
config.PERFOX_API_KEY = PERFOX_TEST_KEY
result, text = summary(CHAUFFEUR, phone="919000000005")
check("DriveCarClub down -> failed", result["outcome"] == "failed", text)

# --- No secrets or personal data in logs or results ---
summary_lines = [l for l in all_log_lines if "send_call_summary" in l or "whatsapp_summary" in l]
SUMMARY_SECRETS = ["9000000001", "9000000002", "9000000004", "9999999999"]  # full numbers, any format
SUMMARY_VALUES = ["DCC100101", "Ramesh", "Innova", "KA01AB1234", "Bengaluru", "Gurugram", "2,400", "1,350",
                  "6:30 PM", "Midsize"]
check("summary log lines were checked", len(summary_lines) > 30, len(summary_lines))
check("no log line of the whole run has the Perfox API key", not any(PERFOX_TEST_KEY in l for l in all_log_lines))
check("no summary log line has a full number",
      all(contains_none(l, SUMMARY_SECRETS) for l in summary_lines),
      [l for l in summary_lines if not contains_none(l, SUMMARY_SECRETS)])
check("no summary log line has a parameter value",
      all(contains_none(l, SUMMARY_VALUES) for l in summary_lines),
      [l for l in summary_lines if not contains_none(l, SUMMARY_VALUES)])
all_sent_parameters = json.dumps([r["body"] for r in perfox_requests])
check("sent parameters never have the customer, surname, mobile, address or VIP flag",
      contains_none(all_sent_parameters, MOCK_PRIVATE + ["KUMAR", "Kumar", "9845012345", "vip"]),
      all_sent_parameters)
check("send_call_summary results have only outcome and say (no parameters echoed)",
      set(summary(CHAUFFEUR, conversation_id="sum-echo")[0]) <= {"outcome", "say", "reason"})

# --- Configurable parameters: WA_CHAUFFEUR_PARAMS and WA_FARE_PARAMS ---
# (After the check above: these layouts send the customer's first name and
# the chauffeur's mobile on purpose.)
check("default chauffeur layout when WA_CHAUFFEUR_PARAMS is empty",
      config.WA_CHAUFFEUR_PARAMS == [] and call_summary.fields_for("chauffeur")
      == ["booking_ref", "pickup_full", "chauffeur_first_name", "car_model", "registration"])
check("default fare layout when WA_FARE_PARAMS is empty",
      config.WA_FARE_PARAMS == [] and call_summary.fields_for("fare")
      == ["car_category", "trip_type", "city", "rates_line"])
check("default layouts are known names", call_summary.unknown_fields() == {})


def sent_parameters():
    return perfox_requests[-1]["body"]["template"]["body_parameters"]


call_summary.clear_limits()
log_lines.clear()
config.WA_CHAUFFEUR_PARAMS = ["registration", "customer_first_name", "pickup_date", "pickup_time",
                              "pickup_area", "chauffeur_first_name", "Booking_Ref"]  # any case
result, text = summary(CHAUFFEUR)
check("custom chauffeur order: sent in that order",
      result["outcome"] == "sent" and sent_parameters()
      == ["KA01AB1234", "Arjun", TODAY_TEXT, "6:30 PM", "Bengaluru", "Ramesh", "DCC100101"], sent_parameters())
line = summary_logs()[-1]
check("custom chauffeur order: log has the count and the field names in order",
      line["parameter_count"] == 7 and line["parameter_fields"]
      == ["registration", "customer_first_name", "pickup_date", "pickup_time", "pickup_area",
          "chauffeur_first_name", "booking_ref"], line)

config.WA_FARE_PARAMS = ["rates_line", "City", "extra_charges_line", "car_category"]
result, text = summary(FARE)
check("custom fare order: sent in that order",
      result["outcome"] == "sent" and sent_parameters()
      == ["8hrs 80kms Rs 2,400; 4hrs 40kms Rs 1,350", "Gurugram",  # extras only in extra_charges_line
          "extra km Rs 18, extra hour Rs 150", "Midsize"], sent_parameters())
line = summary_logs()[-1]
check("custom fare order: log has the count and the field names in order",
      line["parameter_count"] == 4
      and line["parameter_fields"] == ["rates_line", "city", "extra_charges_line", "car_category"], line)
check("extra charges line: different extras per package -> each package named",
      call_summary.extra_charges_line([
          {"package_name": "A", "rate_inr": 100, "extra_km_rate_inr": 1, "extra_hour_rate_inr": 2},
          {"package_name": "B", "rate_inr": 200, "extra_km_rate_inr": 3, "extra_hour_rate_inr": 4},
      ]) == "A: extra km Rs 1, extra hour Rs 2; B: extra km Rs 3, extra hour Rs 4")

# An empty value -> "incomplete" and nothing is sent.
before = len(perfox_requests)
config.WA_CHAUFFEUR_PARAMS = ["booking_ref", "driver_contact"]
result, text = summary(CHAUFFEUR)
check("driver_contact without WA_INCLUDE_DRIVER_PHONE -> incomplete",
      result["outcome"] == "incomplete" and len(perfox_requests) == before, text)
line = summary_logs()[-1]
check("incomplete: log still has the count and field names",
      line["parameter_count"] == 2 and line["parameter_fields"] == ["booking_ref", "driver_contact"], line)
config.WA_FARE_PARAMS = ["car_category", "booking_ref"]  # a booking field in a fare summary
result, text = summary(FARE)
check("a field the topic doesn't have -> incomplete",
      result["outcome"] == "incomplete" and len(perfox_requests) == before, text)
check("build: any empty value -> None",
      call_summary.build(["a", "b"], {"a": "x", "b": "  \n "}) is None
      and call_summary.build(["a", "b"], {"a": "x"}) is None)

config.WA_INCLUDE_DRIVER_PHONE = True
config.WA_CHAUFFEUR_PARAMS = ["chauffeur_first_name", "driver_contact"]
result, text = summary(CHAUFFEUR)
check("driver_contact with WA_INCLUDE_DRIVER_PHONE=true -> sent",
      result["outcome"] == "sent" and sent_parameters() == ["Ramesh", "9845012345"], sent_parameters())
config.WA_INCLUDE_DRIVER_PHONE = False

# Sanitising: one line, at most 500 characters.
check("clean: newlines, tabs and runs of spaces squashed",
      call_summary.clean("Toyota\nInnova\r\n\tCrysta     X ") == "Toyota Innova Crysta X")
long_value = call_summary.clean("A" * 499 + "     B" + "C" * 600)
check("clean: capped at 500 characters, no trailing space",
      len(long_value) <= 500 and long_value == "A" * 499 and perfox.is_single_line(long_value), len(long_value))
check("clean: exactly 500 kept", call_summary.clean("x" * 600) == "x" * 500)
config.WA_CHAUFFEUR_PARAMS = ["car_model", "registration"]
parameters = call_summary.chauffeur_parameters({**bare, "car_model": "Toyota\tInnova\n" + "Z" * 700})
check("chauffeur parameters are sanitised and capped",
      parameters and len(parameters[0]) == 500 and parameters[0].startswith("Toyota Innova Z")
      and all(perfox.is_single_line(p) for p in parameters), parameters)

# The values are never logged: not the customer's or chauffeur's details.
config.WA_CHAUFFEUR_PARAMS = list(call_summary.CHAUFFEUR_FIELDS)
config.WA_INCLUDE_DRIVER_PHONE = True
log_lines.clear()
result, text = summary(CHAUFFEUR)
check("every chauffeur field at once -> sent", result["outcome"] == "sent" and len(sent_parameters()) == 10,
      sent_parameters())
config.WA_FARE_PARAMS = list(call_summary.FARE_FIELDS)
result, text = summary(FARE)
check("every fare field at once -> sent", result["outcome"] == "sent" and len(sent_parameters()) == 6)
sent_values = [v for r in perfox_requests[-2:] for v in r["body"]["template"]["body_parameters"]]
check("no log line has a sent parameter value",
      all(contains_none(l, sent_values + ["Arjun", "9845012345"]) for l in log_lines),
      [l for l in log_lines if not contains_none(l, sent_values)])
check("...but the field names are logged",
      [l["parameter_fields"] for l in summary_logs()[-2:]]
      == [list(call_summary.CHAUFFEUR_FIELDS), list(call_summary.FARE_FIELDS)])
config.WA_INCLUDE_DRIVER_PHONE = False

# Startup: the layouts are logged (names only), with warnings for a layout
# that can never be filled.
config.WA_CHAUFFEUR_PARAMS = ["booking_ref", "driver_contact", "city"]
config.WA_FARE_PARAMS = ["car_category", "rates_line"]
log_lines.clear()
log_startup_warnings()
startup = [json.loads(l) for l in log_lines]
check("startup: logs each layout's count and field names",
      [(l["topic"], l["parameter_count"], l["parameter_fields"]) for l in startup if l["event"] == "wa_summary_params"]
      == [("chauffeur", 3, ["booking_ref", "driver_contact", "city"]), ("fare", 2, ["car_category", "rates_line"])],
      startup)
check("startup: a field the topic doesn't have warns",
      any(l["event"] == "wa_params_never_filled" and l["fields"] == ["city"] for l in startup), startup)
check("startup: driver_contact without WA_INCLUDE_DRIVER_PHONE warns",
      any(l["event"] == "wa_driver_phone_off" for l in startup), startup)
config.WA_CHAUFFEUR_PARAMS = ["booking_ref", "colour", "car_model"]
check("unknown_fields lists the unknown names per setting",
      call_summary.unknown_fields() == {"WA_CHAUFFEUR_PARAMS": ["colour"]}, call_summary.unknown_fields())
config.WA_CHAUFFEUR_PARAMS, config.WA_FARE_PARAMS = [], []

# An unknown field name stops the server (in a fresh Python: settings are read once at import).
started = start_server(WA_FARE_PARAMS="car_category, trip_type, colour, city")
check("unknown field name in WA_FARE_PARAMS -> the server refuses to start",
      started.returncode != 0 and "Refusing to start" in started.stderr and "WA_FARE_PARAMS" in started.stderr
      and "colour" in started.stderr, started.stdout + started.stderr)
check("...and the error lists every allowed name",
      all(name in started.stderr for name in call_summary.FIELDS), started.stderr)
started = start_server(WA_CHAUFFEUR_PARAMS="booking_ref,driver_phone")
check("unknown field name in WA_CHAUFFEUR_PARAMS -> the server refuses to start",
      started.returncode != 0 and "WA_CHAUFFEUR_PARAMS" in started.stderr and "driver_phone" in started.stderr,
      started.stdout + started.stderr)
started = start_server(WA_CHAUFFEUR_PARAMS="registration,booking_ref", WA_FARE_PARAMS="city,rates_line")
check("known custom field names -> starts", started.returncode == 0, started.stdout + started.stderr)

# --- customer_first_name: the caller's name, only in the template ---
call_summary.clear_limits()
SANJAY = {"customer_name": "SANJAY MINHAS", "guest_contact": "98765 43210"}
check("customer_first_name: first word only, title case (guest contact = caller)",
      call_summary.customer_first_name(SANJAY, "919876543210") == "Sanjay")
check("customer_first_name: numbers compared after normalising both",
      all(call_summary.customer_first_name({**SANJAY, "guest_contact": guest}, caller) == "Sanjay"
          for guest, caller in (("+91 98765 43210", "919876543210"), ("09876543210", "+91-98765-43210"),
                                ("919876543210", "9876543210"), ("0091 9876543210", "09876543210"))))
check("customer_first_name: another guest number -> 'there'",
      call_summary.customer_first_name(SANJAY, "919000000001") == "there")
check("customer_first_name: no or unreadable guest number, or no caller number -> 'there'",
      call_summary.customer_first_name({**SANJAY, "guest_contact": ""}, "919876543210") == "there"
      and call_summary.customer_first_name({**SANJAY, "guest_contact": "12345"}, "12345") == "there"
      and call_summary.customer_first_name(SANJAY, None) == "there")
check("customer_first_name: no name or no booking -> 'there'",
      call_summary.customer_first_name({**SANJAY, "customer_name": "  "}, "919876543210") == "there"
      and call_summary.customer_first_name({}, "919876543210") == "there"
      and call_summary.customer_first_name(None, "919876543210") == "there")
check("name_source: mock data, a real booking, or the fallback",
      [call_summary.name_source(SANJAY, "919876543210", "mock"),
       call_summary.name_source(SANJAY, "919876543210", "mock_number"),
       call_summary.name_source(SANJAY, "919876543210", "live"),
       call_summary.name_source(SANJAY, "919000000001", "live"),
       call_summary.name_source(None, "919876543210", "mock")]
      == ["mock", "mock", "booking", "fallback", "fallback"])
check("customer_first_name is allowed for both topics",
      "customer_first_name" in call_summary.TOPIC_FIELDS["chauffeur"]
      and "customer_first_name" in call_summary.TOPIC_FIELDS["fare"] and call_summary.FIELDS.count("customer_first_name") == 1)

config.WA_CHAUFFEUR_PARAMS = ["customer_first_name", "booking_ref"]
result, text = summary(CHAUFFEUR)
check("chauffeur: name from the booking already fetched",
      result["outcome"] == "sent" and sent_parameters() == ["Arjun", "DCC100101"], sent_parameters())
check("chauffeur: booking without a name -> 'there'",
      call_summary.chauffeur_parameters({**bare, "customer_name": ""}) == ["there", "DCC9"]
      and call_summary.chauffeur_parameters(bare) == ["there", "DCC9"])

config.WA_FARE_PARAMS = ["customer_first_name", "car_category", "city"]
fare_names = {}
for label, phone in (("one booking", "919000000001"), ("several bookings", "919000000002"),
                     ("only a past, Completed booking", "919000000007"), ("no bookings", "919000000003"),
                     ("DriveCarClub down", "919000000005")):
    result, text = summary(FARE, phone=phone)
    fare_names[label] = sent_parameters()[0] if result["outcome"] == "sent" else result["outcome"]
check("fare: caller with a booking -> their first name", fare_names["one booking"] == "Arjun", fare_names)
check("fare: several bookings -> the most recent one's name (tomorrow's)",
      fare_names["several bookings"] == "Nikhil", fare_names)
check("fare: no date or status filters (a past, Completed booking still counts)",
      fare_names["only a past, Completed booking"] == "Neha"
      and call("check_dispatch_status", phone="919000000007")[0]["outcome"] == "none", fare_names)
check("fare: caller without a booking -> 'there'", fare_names["no bookings"] == "there", fare_names)
check("fare: DriveCarClub down for the name lookup -> 'there' (the fare is still sent)",
      fare_names["DriveCarClub down"] == "there", fare_names)
check("fare: the name lookup uses the caller's number, never one the AI passes",
      summary({**FARE, "mobile_number": "9000000002", "phone": "919000000002"}, phone="919000000001")[0]["outcome"]
      == "sent" and sent_parameters()[0] == "Arjun", sent_parameters())

booking_calls = []
mock_get_bookings = tools.booking_client_for("919000000001").get_bookings
tools.booking_client_for("919000000001").get_bookings = lambda number: booking_calls.append(1) or mock_get_bookings(number)
config.WA_FARE_PARAMS = []
summary(FARE)
check("fare: bookings are not fetched when the template has no customer_first_name", booking_calls == [], booking_calls)
del tools.booking_client_for("919000000001").get_bookings
check("fare: no customer_first_name -> no name_source in the log", summary_logs()[-1]["name_source"] is None,
      summary_logs()[-1])

# The name only on a match: the same bookings, but with someone else's guest number.
config.WA_CHAUFFEUR_PARAMS = ["customer_first_name", "booking_ref"]
config.WA_FARE_PARAMS = ["customer_first_name", "city"]
call_summary.clear_limits()  # 919000000001 used up its 5 for today above
log_lines.clear()
result, _ = summary(CHAUFFEUR)
chauffeur_match = (result["outcome"], sent_parameters(), summary_logs()[-1]["name_source"])
result, _ = summary(FARE)
fare_match = (result["outcome"], sent_parameters(), summary_logs()[-1]["name_source"])
check("match: chauffeur uses the name, name_source 'mock'",
      chauffeur_match == ("sent", ["Arjun", "DCC100101"], "mock"), chauffeur_match)
check("match: fare uses the name, name_source 'mock'", fare_match == ("sent", ["Arjun", "Gurugram"], "mock"),
      fare_match)


def someone_elses_guest_number(number):
    return [{**raw, "b_Guest_Contact_No": "919000000099"} for raw in mock_get_bookings(number)]


tools.booking_client_for("919000000001").get_bookings = someone_elses_guest_number
result, _ = summary(CHAUFFEUR)
chauffeur_mismatch = (result["outcome"], sent_parameters(), summary_logs()[-1]["name_source"])
result, _ = summary(FARE)
fare_mismatch = (result["outcome"], sent_parameters(), summary_logs()[-1]["name_source"])
del tools.booking_client_for("919000000001").get_bookings
check("mismatch: chauffeur says 'there', name_source 'fallback'",
      chauffeur_mismatch == ("sent", ["there", "DCC100101"], "fallback"), chauffeur_mismatch)
check("mismatch: fare says 'there', name_source 'fallback'",
      fare_mismatch == ("sent", ["there", "Gurugram"], "fallback"), fare_mismatch)
result, _ = summary(FARE, phone="919000000003")
check("no booking: name_source 'fallback'", summary_logs()[-1]["name_source"] == "fallback", summary_logs()[-1])
check("name_source is logged, the name never is",
      all("name_source" in l for l in summary_logs()) and contains_none("".join(log_lines), CUSTOMER_NAMES),
      log_lines)
config.WA_CHAUFFEUR_PARAMS, config.WA_FARE_PARAMS = [], []

# rates_line: the packages only when extra_charges_line carries the extras.
gurugram = tools.get_fare({"car_category": "Midsize", "service_type": "Local", "city_name": "Gurugram"}, {})["packages"]
config.WA_FARE_PARAMS = ["rates_line", "extra_charges_line"]
check("rates_line with extra_charges_line: packages only, no bracketed extras",
      call_summary.fare_parameters("Midsize", "Local", "Gurugram", gurugram)
      == ["8hrs 80kms Rs 2,400; 4hrs 40kms Rs 1,350", "extra km Rs 18, extra hour Rs 150"])
config.WA_FARE_PARAMS = ["city", "rates_line"]
check("rates_line without extra_charges_line: the old combined text",
      call_summary.fare_parameters("Midsize", "Local", "Gurugram", gurugram)[1]
      == "8hrs 80kms Rs 2,400; 4hrs 40kms Rs 1,350 (extra km Rs 18, extra hour Rs 150)")
config.WA_FARE_PARAMS = []
check("default fare layout keeps the old combined text", call_summary.fare_parameters(
    "Midsize", "Local", "Gurugram", gurugram)[3] == FARE_PARAMETERS[3])
check("fare_line: different extras per package, packages only -> no brackets",
      call_summary.fare_line([
          {"package_name": "A", "rate_inr": 100, "extra_km_rate_inr": 1, "extra_hour_rate_inr": 2},
          {"package_name": "B", "rate_inr": 200, "extra_km_rate_inr": 3, "extra_hour_rate_inr": 4},
      ], with_extras=False) == "A Rs 100; B Rs 200")

# A MOCK_NUMBER_SCENARIOS name reaches the WhatsApp template, and only that.
config.MOCK_NUMBER_SCENARIOS = ["919000000101=today:Zephyrina Testname", "919000000102=several"]
config.WA_CHAUFFEUR_PARAMS = ["customer_first_name", "booking_ref"]
config.WA_FARE_PARAMS = ["customer_first_name", "city"]
call_summary.clear_limits()
log_lines.clear()
named, named_params = [], []
for arguments in ({"topic": "chauffeur", "booking_ref": "MOCK-1001"}, FARE):
    named.append(summary(arguments, phone="919000000101"))
    named_params.append(sent_parameters() if named[-1][0]["outcome"] == "sent" else named[-1][0]["outcome"])
check("scenario name given: used in the template (first name), name_source 'mock'",
      named_params == [["Zephyrina", "MOCK-1001"], ["Zephyrina", "Gurugram"]]
      and [l["name_source"] for l in summary_logs()[-2:]] == ["mock", "mock"], (named_params, summary_logs()))
result, _ = summary({"topic": "chauffeur", "booking_ref": "MOCK-2001"}, phone="919000000102")
check("scenario without a name: the mock name", result["outcome"] == "sent" and sent_parameters()
      == ["Tarun", "MOCK-2001"], sent_parameters())
check("scenario name: not in the tool results or the log lines",
      all(contains_none(text, ["Zephyrina", "Testname"]) for _, text in named)
      and all(contains_none(l, ["Zephyrina", "Testname"]) for l in log_lines), log_lines)
config.MOCK_NUMBER_SCENARIOS, config.WA_CHAUFFEUR_PARAMS, config.WA_FARE_PARAMS = [], [], []

# extra_charges_line: "None" instead of empty (an empty value stops the send).
free = {"package_name": "A", "rate_inr": 100, "extra_km_rate_inr": 0, "extra_hour_rate_inr": 0}
check("extra_charges_line: no extra charges -> 'None'", call_summary.extra_charges_line([free, free]) == "None")
check("extra_charges_line: Rs 0 extras left out",
      call_summary.extra_charges_line([{**free, "extra_km_rate_inr": 18}]) == "extra km Rs 18")
check("extra_charges_line: one package with none -> 'None' for it",
      call_summary.extra_charges_line([{**free, "extra_km_rate_inr": 18}, {**free, "package_name": "B"}])
      == "A: extra km Rs 18; B: None")
config.WA_FARE_PARAMS = ["city", "extra_charges_line"]
check("extra_charges_line 'None' is sent, not 'incomplete'",
      call_summary.fare_parameters("Midsize", "Local", "Gurugram", [free]) == ["Gurugram", "None"])
config.WA_FARE_PARAMS = []

# The name reached the template, and nowhere else: no tool result (so no
# "say" or "message"), and no log line of the whole run.
sent_names = json.dumps([r["body"]["template"]["body_parameters"] for r in perfox_requests])
check("the customer names did reach the template parameters",
      all(name.title() in sent_names for name in ("Arjun", "Nikhil", "Neha")), sent_names)
check("no summary log line has a full caller number",
      all(contains_none(l, ["9000000001", "9000000002", "9000000003", "9000000007"])
          for l in all_log_lines if "send_call_summary" in l or "whatsapp_summary" in l))

log_lines.clear()
config.WA_SUMMARY_MODE = "smoke"
log_startup_warnings()
check("startup: unknown WA_SUMMARY_MODE warns", '"unknown_wa_summary_mode"' in "".join(log_lines))
config.WA_SUMMARY_MODE, config.PERFOX_AGENT_ID = "live", ""
log_lines.clear()
log_startup_warnings()
check("startup: live with a missing PERFOX_* setting warns", '"missing_perfox_settings"' in "".join(log_lines))
check("startup warnings never have the API key", PERFOX_TEST_KEY not in "".join(log_lines))

config.WA_SUMMARY_MODE, config.PERFOX_API_BASE, config.PERFOX_API_KEY = "mock", "", ""
perfox_server.shutdown()

# ----------------------------------------------------------------------
# 12. No support number on the server (it lives in the Perfox knowledge base)
# ----------------------------------------------------------------------
print("\n--- support number ---")


def has_support_number(text):
    """True if a support sentence has digits in it, or the old placeholder."""
    return bool(re.search(r"customer support[^.\"]*\d", text)) or "[customer support number]" in text


check("SUPPORT_NUMBER setting is gone", not hasattr(config, "SUPPORT_NUMBER"))
check("every tool description says to take the number from the knowledge base",
      all("support number from your knowledge base" in t["description"] for t in tools.TOOLS))
# Where a number used to be ("...support team on <number>"). FARE_NOTE only
# says "call our customer support team", with no number, so it isn't one.
support_texts = [t for t in all_tool_texts if "customer support team on" in t]
check("support answers were checked", len(support_texts) > 10, len(support_texts))
check("support answers use the knowledge-base placeholder",
      all(f"customer support team on {tools.SUPPORT_PLACEHOLDER}" in t for t in support_texts))
check("no tool result has a support phone number", not any(has_support_number(t) for t in all_tool_texts))
check("no log line has a support phone number", not any(has_support_number(line) for line in all_log_lines))
check("no website reply has a support phone number", not any(has_support_number(t) for t in web_replies))

# The customer's name (individualsname) only ever goes into the WhatsApp
# template (section 12a): never a tool result, "say", "message" or log line.
check("no tool result of the whole run has a customer name",
      all(contains_none(t, CUSTOMER_NAMES) for t in all_tool_texts),
      [t for t in all_tool_texts if not contains_none(t, CUSTOMER_NAMES)])
check("no log line of the whole run has a customer name",
      all(contains_none(l, CUSTOMER_NAMES) for l in all_log_lines),
      [l for l in all_log_lines if not contains_none(l, CUSTOMER_NAMES)])
check("no website reply has a customer name", all(contains_none(t, CUSTOMER_NAMES) for t in web_replies))

# ----------------------------------------------------------------------
print(f"\n{results['pass']} passed, {results['fail']} failed")
sys.exit(1 if results["fail"] else 0)
