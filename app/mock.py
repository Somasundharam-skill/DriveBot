"""Fake DriveCarClub data for testing (used when DCC_MODE=mock).

It works exactly like the live client: get_bookings(contact_no) returns
a list of raw bookings, or raises DCCUnavailable. The raw field names are
our best guess at what DriveCarClub will send, and the data deliberately
includes private fields (addresses, driver mobile, surnames) so the tests
can prove they never leak.

Test numbers:
  919000000001  one booking today, chauffeur allocated
  919000000002  three bookings: yesterday (Completed), today, tomorrow
  919000000003  no bookings
  919000000004  one booking tomorrow, chauffeur not allocated yet
  919000000005  server error
  919000000006  timeout
"""

import time
from datetime import datetime, timedelta

from app import config
from app.errors import DCCUnavailable
from app.mapper import INDIA


def _pickup(days_from_today, hour, minute):
    """A pickup date/time relative to today in India, e.g. "2026-10-09T18:30:00"."""
    day = datetime.now(INDIA).date() + timedelta(days=days_from_today)
    return f"{day.isoformat()}T{hour:02d}:{minute:02d}:00"


def _booking(booking_id, pickup, area, status, driver="", driver_mobile="",
             model="", colour="", car_no=""):
    """Build one raw booking the way we expect DriveCarClub to send it."""
    return {
        "BookingId": booking_id,
        "PickupDateTime": pickup,
        "PickupArea": area,
        "PickupAddress": f"Flat 12B, Prestige Towers, 4th Cross, {area}",
        "DropAddress": "Kempegowda International Airport, Terminal 2",
        "Status": status,
        "DriverName": driver,
        "DriverMobile": driver_mobile,
        "CarModel": model,
        "CarColour": colour,
        "CarNo": car_no,
    }


def _bookings_for(contact_no):
    if contact_no == "919000000001":
        return [
            _booking("DCC100101", _pickup(0, 18, 30), "Koramangala", "Dispatched",
                     "Ramesh Kumar", "9845012345", "Toyota Innova Crysta", "White", "KA 01 AB 1234"),
        ]
    if contact_no == "919000000002":
        return [
            _booking("DCC100201", _pickup(-1, 9, 0), "Indiranagar", "Completed",
                     "Suresh Babu", "9845011111", "Honda City", "Silver", "KA 05 CD 5678"),
            _booking("DCC100203", _pickup(1, 8, 15), "Whitefield", "Dispatched",
                     "Anil Sharma", "9845022222", "Toyota Etios", "Grey", "KA 03 GH 3456"),
            _booking("DCC100202", _pickup(0, 15, 0), "HSR Layout", "Dispatched",
                     "Mohammed Irfan Khan", "9845033333", "Maruti Ciaz", "Blue", "KA 51 EF 9012"),
        ]
    if contact_no == "919000000004":
        return [
            _booking("DCC100401", _pickup(1, 10, 0), "Jayanagar", "Confirmed"),
        ]
    return []  # 919000000003 and any other number: no bookings


class MockDriveCarClub:
    """Same interface as DriveCarClubClient, but with fake data."""

    def get_bookings(self, contact_no):
        if contact_no == "919000000005":
            raise DCCUnavailable("server_error")
        if contact_no == "919000000006":
            # Behave like a real timeout: wait, then give up.
            time.sleep(config.DCC_TIMEOUT_SECONDS)
            raise DCCUnavailable("timeout")
        return _bookings_for(contact_no)
