"""Sending a WhatsApp template message through Perfox's Outbound API.

Used by send_call_summary (the end-of-call WhatsApp summary).
WA_SUMMARY_MODE in .env picks how:
  "mock"  nothing is sent; only the template name and the number of
          parameters are logged (default, testing)
  "live"  a real WhatsApp template message through Perfox

Perfox docs:
  POST {PERFOX_API_BASE}/api/v1/outbound
  Authorization: Bearer {PERFOX_API_KEY}
  {"agent_id": "...", "channel": "whatsapp", "to": "+91XXXXXXXXXX",
   "template": {"name": "...", "language": "en_US", "body_parameters": [...]}}
  Never send "opening_message" together with "template" (that is a 400).
  Success is 201 with "status" (sent, failed or suppressed) and
  "send_authorized". Errors: 400, 404, 422.

Rule: the parameter values, the number and the API key are never logged.
"""

import requests

from app import config, log
from app.errors import OutboundFailed

# The "status" values Perfox sends on a 201, and the 422 error codes it
# documents. Only these are ever logged, never other text from the reply.
KNOWN_STATUSES = ("sent", "failed", "suppressed")
KNOWN_ERROR_CODES = ("agent_not_published", "no_trigger_for_channel")


def is_single_line(value):
    """WhatsApp template parameters must be one line: no newlines, tabs
    or runs of 4 or more spaces, and not empty."""
    return (
        isinstance(value, str) and value.strip() != ""
        and not any(mark in value for mark in ("\n", "\r", "\t", "    "))
    )


def send_template(to, template_name, language, parameters):
    """Send one WhatsApp template message to "to" (E.164, with a "+").

    Returns the provider status for the log: "mock" or "sent".
    Raises OutboundFailed if it was not sent.
    """
    # A last check for every caller: a bad parameter is never sent.
    if not all(is_single_line(value) for value in parameters):
        raise OutboundFailed("bad_parameter")

    if config.WA_SUMMARY_MODE != "live":
        # mock (or an unknown mode, with a startup warning): send nothing.
        log.info("whatsapp_summary_mock", template=template_name, parameter_count=len(parameters))
        return "mock"
    return send_with_perfox(to, template_name, language, parameters)


def send_with_perfox(to, template_name, language, parameters, api_base=None, api_key=None,
                     agent_id=None, timeout=None):
    """Send one template message through Perfox's Outbound API.

    The settings come from .env unless passed in.
    Returns "sent". Raises OutboundFailed otherwise.
    """
    api_base = (api_base or config.PERFOX_API_BASE).rstrip("/")
    api_key = api_key or config.PERFOX_API_KEY
    agent_id = agent_id or config.PERFOX_AGENT_ID
    if not (api_base and api_key and agent_id):
        raise OutboundFailed("perfox_not_configured")

    body = {
        "agent_id": agent_id,
        "channel": "whatsapp",
        "to": to,
        # Only "template": never "opening_message" as well (Perfox answers 400).
        "template": {"name": template_name, "language": language, "body_parameters": list(parameters)},
    }
    try:
        reply = requests.post(
            f"{api_base}/api/v1/outbound", json=body,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout or config.PERFOX_TIMEOUT_SECONDS,
        )
    except requests.Timeout:
        raise OutboundFailed("timeout")
    except requests.RequestException:
        raise OutboundFailed("network_error")

    try:
        answer = reply.json()
    except ValueError:
        answer = None
    if not isinstance(answer, dict):
        answer = {}

    if reply.status_code != 201:
        # For example a 422 "agent_not_published": log that code, if it is one we know.
        code = next((value for value in answer.values() if value in KNOWN_ERROR_CODES), None)
        raise OutboundFailed(code or f"http_{reply.status_code}", provider_status=f"http_{reply.status_code}")

    # A 201 still only counts if Perfox really sent it.
    status = answer.get("status")
    status = status if status in KNOWN_STATUSES else "unknown"
    if status != "sent":
        raise OutboundFailed(f"provider_{status}", provider_status=status)
    if answer.get("send_authorized") is not True:
        raise OutboundFailed("not_authorized", provider_status=status)
    return status
