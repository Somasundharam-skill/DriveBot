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

import base64
import json
import logging
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fastapi.testclient import TestClient

from app import identity
from app.drivecarclub import DriveCarClubClient
from app.errors import DCCUnavailable
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


def call(tool, arguments=None, phone=None, channel="phone", conversation_id="conv-test"):
    """Call a tool the way Perfox does. Returns (result dict, raw text)."""
    headers = {"X-Sa-Conversation-Id": conversation_id}
    if channel:
        headers["X-Sa-Channel"] = channel
    if phone:
        headers["X-Sa-End-User-Phone"] = phone
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
check("tools/list returns 2 tools", len(tool_list) == 2)
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
# 5. Logging
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
# 6. Live client against a small fake DriveCarClub server
# ----------------------------------------------------------------------
print("\n--- live client (fake DriveCarClub server) ---")

counts = {"token": 0, "bookings": 0}
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
    except DCCUnavailable as failure:
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

fake_server.shutdown()

# ----------------------------------------------------------------------
print(f"\n{results['pass']} passed, {results['fail']} failed")
sys.exit(1 if results["fail"] else 0)
