"""Custom errors used by the DriveCarClub clients and the SMS sender."""


class DCCUnavailable(Exception):
    """DriveCarClub could not give us an answer we can trust.

    "reason" is a short code for the logs, for example "timeout",
    "server_error" or "auth_failed". It is never shown to the customer.
    """

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class SMSUnavailable(Exception):
    """The SMS (with the OTP code) could not be sent.

    "reason" is a short code for the logs, for example "timeout" or
    "http_401". It is never shown to the customer.
    """

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason
