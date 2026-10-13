"""Fake DriveCarClub data for testing (used when DCC_MODE=mock).

It works exactly like the live client: get_bookings(contact_no) returns
a list of raw bookings, or raises DCCUnavailable. The raw bookings use
the REAL DespatchedBooking field names and formats (DD-MM-YYYY dates,
"Despatched" status, no colour field), so mock and live mode go through
exactly the same mapping in mapper.py.

All names and numbers here are made up. The data deliberately includes
private fields (customer name, company, guest number, address, chauffeur
mobile and surname) so the tests can prove they never leak.

Test numbers:
  919000000001  one booking today, chauffeur allocated
  919000000002  three bookings: yesterday (Completed), today, tomorrow
  919000000003  no bookings
  919000000004  one booking tomorrow, chauffeur not allocated yet
  919000000005  server error
  919000000006  timeout

Fares (get_fare): see _RATES below, for example Midsize / Local / Gurugram.
"""

import time
from datetime import datetime, timedelta

from app import config, mapper
from app.errors import DCCUnavailable
from app.mapper import INDIA


def _pickup_date(days_from_today):
    """A pickup date relative to today in India, as DriveCarClub sends it: "09-10-2026"."""
    day = datetime.now(INDIA).date() + timedelta(days=days_from_today)
    return day.strftime("%d-%m-%Y")


def _booking(booking_id, days_from_today, time_text, city, status, chauffeur="",
             chauffeur_mobile="", car="", car_no="", guest_no="919000000099"):
    """Build one raw booking the way DriveCarClub's DespatchedBooking API sends it."""
    return {
        "msg": None,
        "actual_bk_num": booking_id,
        "location": "Bangalore",
        "company_Name": "Mockco Industries Pvt Ltd",
        "individualsname": "PRIYA TESTCUSTOMER",
        "b_Guest_Contact_No": guest_no,
        "reportingDate": _pickup_date(days_from_today),
        "reportingTime": time_text,
        "reportingAddress": "Flat 12B, Prestige Towers, 4th Cross",
        "standinginstructions": "Call guest at gate 3",
        "carBooked": car or "Sedan",
        "carAlloted": car,
        "car_Owned_By": None,
        "carNumber": car_no,
        "chauffeurName": chauffeur,
        "chaufferMobile": chauffeur_mobile,
        "dsNo": f"DS-{booking_id}",
        "status": status,
        "vip": "False",
        "city_Name": city,
    }


def _bookings_for(contact_no):
    if contact_no == "919000000001":
        return [
            _booking("DCC100101", 0, "18:30", "Bengaluru", "Despatched",
                     "RAMESH KUMAR", "9845012345", "Toyota Innova Crysta", "KA01AB1234"),
        ]
    if contact_no == "919000000002":
        return [
            _booking("DCC100201", -1, "09:00", "Mysuru", "Completed",
                     "SURESH BABU", "9845011111", "Honda City", "KA05CD5678"),
            _booking("DCC100203", 1, "08:15", "Whitefield", "Despatched",
                     "ANIL SHARMA", "9845022222", "Toyota Etios", "KA03GH3456"),
            _booking("DCC100202", 0, "15:00", "Hosur", "Despatched",
                     "MOHAMMED IRFAN KHAN", "9845033333", "Maruti Ciaz", "KA51EF9012"),
        ]
    if contact_no == "919000000004":
        return [
            _booking("DCC100401", 1, "10:00", "Jayanagar", "Confirmed"),
        ]
    return []  # 919000000003 and any other number: no bookings


def _rate(package, service, category, city, rate, extra_km, extra_hr,
          allowance="0.00", night_stay="0.00"):
    """Build one raw fare package the way the GetRate API sends it."""
    return {
        "packageName": package, "serviceType": service, "carCategory": category,
        "cityName": city, "rate": rate, "extra_Km_Rate": extra_km, "extra_Hr_Rate": extra_hr,
        "outstation_Allowance": allowance, "night_Stay": night_stay,
    }


LOCAL = "Local - within city limits"
OUTSTATION = "Outstation"

# Fake fares, keyed by (car category, service type, city) as the customer
# asks, in lower case. Any other combination has no rate ("not_found").
_RATES = {
    ("midsize", "local", "gurugram"): [
        _rate("8hrs 80kms", LOCAL, "Midsize", "Gurugram", "2400.00", "18.00", "150.00"),
        _rate("4hrs 40kms", LOCAL, "Midsize", "Gurugram", "1350.00", "18.00", "150.00"),
    ],
    ("midsize", "local", "new delhi"): [
        _rate("8hrs 80kms", LOCAL, "Midsize", "New Delhi", "2500.00", "18.00", "160.00"),
        _rate("4hrs 40kms", LOCAL, "Midsize", "New Delhi", "1400.00", "18.00", "160.00"),
    ],
    ("premium", "local", "gurugram"): [
        _rate("8hrs 80kms", LOCAL, "Premium", "Gurugram", "3800.00", "28.00", "250.00"),
        _rate("4hrs 40kms", LOCAL, "Premium", "Gurugram", "2100.00", "28.00", "250.00"),
    ],
    ("midsize", "outstation", "gurugram"): [
        _rate("250kms per day", OUTSTATION, "Midsize", "Gurugram", "4500.00", "18.00", "0.00",
              "300.00", "300.00"),
    ],
}


class MockDriveCarClub:
    """Same interface as DriveCarClubClient, but with fake data."""

    def get_bookings(self, contact_no):
        if contact_no == "919000000005":
            raise DCCUnavailable("server_error")
        if contact_no == "919000000006":
            # Behave like a real timeout: wait, then give up.
            time.sleep(config.DCC_TIMEOUT_SECONDS)
            raise DCCUnavailable("timeout")
        # Wrap the list like the real response, and unwrap it the same way.
        response = {"status": "200", "message": "Success", "data": _bookings_for(contact_no)}
        return mapper.unwrap_response_list(response)

    def get_rates(self, car_category, service_type, city_name):
        """Fake GetRate API: the packages for a known combination, else an empty list."""
        key = (car_category.lower(), service_type.lower(), city_name.lower())
        response = {"status": "200", "message": "Success", "data": _RATES.get(key, [])}
        return mapper.unwrap_response_list(response)
