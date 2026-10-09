"""Logging helpers.

Every log line is one JSON object, so the logs are easy to search.
Rule: never log full phone numbers, tokens or passwords.
"""

import json
import logging
import sys
from datetime import datetime, timezone

_logger = logging.getLogger("drivebot")


def setup_logging():
    """Send log lines to the console, one JSON line each."""
    if _logger.handlers:
        return  # already set up
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(handler)
    _logger.setLevel(logging.INFO)
    _logger.propagate = False


def mask_mobile(mobile):
    """Hide the middle of a phone number: 919876543210 -> 91******3210."""
    if not mobile:
        return None
    if len(mobile) <= 6:
        return "*" * len(mobile)
    return mobile[:2] + "*" * (len(mobile) - 6) + mobile[-4:]


def _write(level, data):
    """Add a timestamp and write one JSON line."""
    line = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    line.update(data)
    _logger.log(level, json.dumps(line))


def info(event, **details):
    _write(logging.INFO, {"level": "info", "event": event, **details})


def warning(event, **details):
    _write(logging.WARNING, {"level": "warning", "event": event, **details})


def error(event, **details):
    _write(logging.ERROR, {"level": "error", "event": event, **details})


def alert_perfox_team(reason, **details):
    """Log a line the Perfox team watches for: DriveCarClub is failing.

    Later this could also send an email or chat message to the team.
    """
    error("alert_perfox_team", reason=reason, **details)


setup_logging()
