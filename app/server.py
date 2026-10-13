"""The MCP server that Perfox talks to (built with FastAPI).

Perfox sends JSON-RPC 2.0 messages to  POST /mcp  and we answer with
plain JSON. There is also  GET /health  for a quick "is it up?" check.
The test website (GET / and /api/...) lives in website.py.

Run it with:  uv run python -m app.server
"""

import hmac
import json
import threading
import uuid
from contextlib import asynccontextmanager

import requests
import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from app import config, log, tools, website

SERVER_INFO = {"name": "drivebot-mcp", "version": "0.1.0"}
DEFAULT_PROTOCOL_VERSION = "2024-11-05"


# ----------------------------------------------------------------------
# Keep-alive (Render free plan)
# ----------------------------------------------------------------------


def keep_alive(stop):
    """Call our own public health URL every KEEP_ALIVE_SECONDS until stopped.

    It must be the public URL: a call to localhost never reaches Render's
    proxy, so it does not count as traffic and the server would still sleep.
    """
    while not stop.wait(config.KEEP_ALIVE_SECONDS):
        try:
            reply = requests.get(config.KEEP_ALIVE_URL, timeout=10)
            log.info("keep_alive", status=reply.status_code)
        except requests.RequestException as error:
            log.warning("keep_alive_failed", error=type(error).__name__)


@asynccontextmanager
async def lifespan(app):
    stop = threading.Event()
    if config.KEEP_ALIVE_URL:
        # daemon=True: the thread never blocks the server from shutting down.
        threading.Thread(target=keep_alive, args=(stop,), daemon=True).start()
        log.info("keep_alive_started", url=config.KEEP_ALIVE_URL, every_seconds=config.KEEP_ALIVE_SECONDS)
    yield
    stop.set()


# The automatic API pages (/docs, /redoc, /openapi.json) only when ENABLE_DOCS=true.
app = FastAPI(
    title="DriveBot MCP server",
    version=SERVER_INFO["version"],
    lifespan=lifespan,
    docs_url="/docs" if config.ENABLE_DOCS else None,
    redoc_url="/redoc" if config.ENABLE_DOCS else None,
    openapi_url="/openapi.json" if config.ENABLE_DOCS else None,
)

# The test website and its login API (see website.py).
app.include_router(website.router)


# ----------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------


def rpc_result(message_id, result, headers=None):
    """A successful JSON-RPC answer."""
    body = {"jsonrpc": "2.0", "id": message_id, "result": result}
    return JSONResponse(body, headers=headers)


def rpc_error(message_id, code, text, http_status=200):
    """A JSON-RPC error answer."""
    body = {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": text}}
    return JSONResponse(body, status_code=http_status)


def is_authorised(request):
    """Check "Authorization: Bearer <MCP_API_KEY>".

    hmac.compare_digest takes the same time whether the first or the last
    character is wrong, so attackers can't guess the key bit by bit.
    """
    if not config.MCP_API_KEY:
        return True  # local testing only (a warning is logged at startup)
    sent = request.headers.get("Authorization", "")
    expected = f"Bearer {config.MCP_API_KEY}"
    return hmac.compare_digest(sent.encode("utf-8"), expected.encode("utf-8"))


def read_caller(request):
    """Collect the customer details Perfox sends as headers.

    Name, email and external id are collected for later use but are not
    used or logged by the pilot tools.
    """
    headers = request.headers  # header names are not case-sensitive here
    return {
        "phone": headers.get("X-Sa-End-User-Phone"),
        "name": headers.get("X-Sa-End-User-Name"),
        "email": headers.get("X-Sa-End-User-Email"),
        "external_id": headers.get("X-Sa-End-User-External-Id"),
        "channel": headers.get("X-Sa-Channel"),
        "conversation_id": headers.get("X-Sa-Conversation-Id"),
        # Our website's chat_token, passed on by Perfox ('identify'). Secret:
        # never log it.
        "user_token": headers.get("X-Sa-User-Token"),
    }


def tool_list_for_perfox():
    """The tool list. We send the schema as "input_schema" (as Perfox asks)
    and also as "inputSchema" (the name in the MCP standard), to be safe."""
    listed = []
    for tool in tools.TOOLS:
        listed.append({**tool, "inputSchema": tool["input_schema"]})
    return listed


# ----------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------


@app.get("/health")
def health():
    return {"ok": True, "mode": _mode(config.DCC_MODE), "rate_mode": _mode(config.DCC_RATE_MODE)}


def _mode(value):
    """The mode actually used: "live" only if set to exactly that."""
    return "live" if value == "live" else "mock"


@app.post("/mcp")
async def mcp(request: Request):
    if not is_authorised(request):
        log.warning("unauthorised_request", remote=request.client.host if request.client else None)
        return JSONResponse({"error": "unauthorised"}, status_code=401)

    try:
        message = await request.json()
    except ValueError:
        message = None
    if not isinstance(message, dict):
        return rpc_error(None, -32700, "Parse error: send one JSON-RPC object", 400)

    method = message.get("method", "")
    params = message.get("params") or {}

    # A message without an "id" is a notification: no answer is expected.
    if "id" not in message:
        return Response(status_code=202)
    message_id = message["id"]

    if method == "initialize":
        result = {
            "protocolVersion": params.get("protocolVersion") or DEFAULT_PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        }
        return rpc_result(message_id, result, headers={"mcp-session-id": uuid.uuid4().hex})

    if method == "ping":
        return rpc_result(message_id, {})

    if method == "tools/list":
        return rpc_result(message_id, {"tools": tool_list_for_perfox()})

    if method == "tools/call":
        try:
            # The tools wait on DriveCarClub (normal, blocking code). Running
            # them in a worker thread keeps the server free to answer other
            # conversations at the same time.
            result = await run_in_threadpool(
                tools.call_tool, params.get("name"), params.get("arguments") or {}, read_caller(request)
            )
        except tools.UnknownTool:
            return rpc_error(message_id, -32602, f"Unknown tool: {params.get('name')}")
        content = [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]
        return rpc_result(message_id, {"content": content, "isError": False})

    return rpc_error(message_id, -32601, f"Method not found: {method}")


# ----------------------------------------------------------------------
# Startup
# ----------------------------------------------------------------------


def log_startup_warnings():
    if not config.MCP_API_KEY:
        log.warning("no_api_key", note="MCP_API_KEY is empty: ALL requests are allowed. Local testing only!")
    if config.TRUST_WEB_PHONE:
        log.warning("trust_web_phone_on", note="TRUST_WEB_PHONE=true: web chat numbers are trusted without OTP. Development only!")
    # Which DriveCarClub data each tool uses (bookings and fares are separate).
    log.info("dcc_modes", booking_mode=_mode(config.DCC_MODE), rate_mode=_mode(config.DCC_RATE_MODE))
    for name, value in (("DCC_MODE", config.DCC_MODE), ("DCC_RATE_MODE", config.DCC_RATE_MODE)):
        if value not in ("mock", "live"):
            log.warning("unknown_dcc_mode", note=f"{name}={value!r} is not mock or live; using mock.")
    if "live" in (config.DCC_MODE, config.DCC_RATE_MODE) and not (config.DCC_USERNAME and config.DCC_PASSWORD):
        log.warning("missing_dcc_credentials",
                    note="DCC_MODE or DCC_RATE_MODE is live but DCC_USERNAME or DCC_PASSWORD is empty.")
    if config.INCLUDE_PAST_BOOKINGS:
        log.warning("include_past_bookings_on", note="INCLUDE_PAST_BOOKINGS=true: past pickups are shown. UAT testing only!")
    if config.PICKUP_AREA_FIELD not in config.PICKUP_AREA_CHOICES:
        log.warning("unknown_pickup_area_field",
                    note=f"PICKUP_AREA_FIELD={config.PICKUP_AREA_FIELD!r} is not city_Name or location; using city_Name.")
    if config.OTP_MODE == "plivo":
        if not (config.PLIVO_AUTH_ID and config.PLIVO_AUTH_TOKEN and config.PLIVO_SENDER_ID):
            log.warning("missing_plivo_settings", note="OTP_MODE=plivo but a PLIVO_* setting is empty: no code can be sent.")
        if not config.OTP_SECRET:
            log.warning("no_otp_secret", note="OTP_SECRET is empty: using a random secret for this run.")
    else:
        log.warning("mock_otp_on", note=f"OTP_MODE={config.OTP_MODE!r}: no SMS is sent and the code is always the mock code. Never use in production!")


log_startup_warnings()

# Static files from web/ (index.html at "/"). Added LAST, so /mcp, /health,
# /api/... and /docs are matched first.
website.serve_static_files(app)

if __name__ == "__main__":
    log.info("server_starting", host=config.HOST, port=config.PORT,
             mode=_mode(config.DCC_MODE), rate_mode=_mode(config.DCC_RATE_MODE))
    uvicorn.run(app, host=config.HOST, port=config.PORT)
