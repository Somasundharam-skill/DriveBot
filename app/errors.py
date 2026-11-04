"""Custom errors used by the DriveCarClub clients, the SMS sender and the
WhatsApp summary sender (perfox.py)."""


class DCCUnavailable(Exception):
    """DriveCarClub could not give us an answer we can trust.

    "reason" is a short code for the logs, for example "timeout",
    "server_error" or "auth_failed". It is never shown to the customer.
    """

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class OutboundFailed(Exception):
    """The WhatsApp summary could not be sent through Perfox.

    "reason" is a short code for the logs, for example "timeout",
    "http_422" or "provider_suppressed". "provider_status" is what Perfox
    answered (for example "suppressed" or "http_422"), or None if it
    never answered. Neither is ever shown to the customer.
    """

    def __init__(self, reason, provider_status=None):
        super().__init__(reason)
        self.reason = reason
        self.provider_status = provider_status


class SMSUnavailable(Exception):
    """The SMS (with the OTP code) could not be sent.

    "reason" is a short code for the logs, for example "timeout" or
    "http_401". It is never shown to the customer.
    """

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason
