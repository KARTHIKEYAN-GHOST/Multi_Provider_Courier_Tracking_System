"""The helpers shared by the Indian courier adapters: local time, status wording, tables."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from adapters.india import local_time, place, status_code
from adapters.tables import history_events
from courier_adapter_sdk import ParseError, SchemaDrift, ShipmentNotFound

# 25 November 2026, 14:05 in India is 08:35 UTC.
AFTERNOON = datetime(2026, 11, 25, 8, 35, tzinfo=UTC)


@pytest.mark.parametrize(
    "text",
    [
        "2026-11-25T14:05:00",
        "2026-11-25 14:05:00.328000",
        "25-11-2026 14:05",
        "25/11/2026 14:05:00",
        "25.11.2026 14:05",
        "25-11-2026 02:05 PM",
        "25/11/2026 02:05PM",
        "25 Nov 2026 14:05",
        "25-Nov-2026 14:05",
        "25 Nov 2026, 14:05 IST",
        "25 November 2026 14:05",
        "  25-11-2026   14:05 hrs ",
        "2026-11-25T08:35:00Z",
        "2026-11-25T14:05:00+05:30",
    ],
)
def test_the_usual_indian_notations_are_read_as_ist(text: str) -> None:
    assert local_time(text).replace(microsecond=0) == AFTERNOON


def test_a_date_without_a_time_is_the_start_of_that_day_in_india() -> None:
    assert local_time("25-11-2026") == datetime(2026, 11, 24, 18, 30, tzinfo=UTC)
    assert local_time("03/04/2026") == datetime(2026, 4, 2, 18, 30, tzinfo=UTC), "day first: 3 April"


@pytest.mark.parametrize("value", [None, "", "   ", 20261125])
def test_a_missing_date_is_schema_drift(value: object) -> None:
    with pytest.raises(SchemaDrift):
        local_time(value)


def test_an_unreadable_date_is_a_parse_error_that_does_not_quote_the_value() -> None:
    with pytest.raises(ParseError) as raised:
        local_time("<script>alert(1)</script>")
    assert "script" not in str(raised.value)


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("Delivered", "delivered"),
        ("Shipment DELIVERED to consignee", "delivered"),
        ("Un-Delivered: door locked", "undelivered"),
        ("Undelivered", "undelivered"),
        ("Not delivered - consignee absent", "undelivered"),
        ("Out for Delivery", "out-for-delivery"),
        ("Out-For-Delivery from Tirupur", "out-for-delivery"),
        ("Booked", "booked"),
        ("Waybill Generated For 3 packages", "booked"),
        ("Picked up from shipper", "picked-up"),
        ("In Transit", "in-transit"),
        ("Arrived at Salem hub", "in-transit"),
        ("Dispatched to Coimbatore", "in-transit"),
        ("Waybill reached final destination", "in-transit"),
        ("RTO initiated", "returned"),
        ("Returned to shipper", "returned"),
    ],
)
def test_recognized_wording_becomes_one_of_seven_codes(text: str, code: str) -> None:
    assert status_code(text) == code


def test_wording_that_is_not_recognized_is_passed_on_for_a_person_to_map() -> None:
    assert status_code("RPS has been closed Successfully") == "RPS has been closed Successfully"
    assert len(status_code("x" * 500)) == 200


def test_a_place_keeps_the_providers_wording() -> None:
    location = place("  Tirupur   Hub ")
    assert location is not None
    assert (location.city, location.country, location.raw) == ("Tirupur Hub", "IN", "Tirupur Hub")
    assert place("") is None
    assert place(None) is None


# ----------------------------------------------------------------------------- history table

PAGE = """
<html><body>
<table><tr><td>Home</td><td>Contact</td></tr></table>
<h3>LR 12345</h3>
<table class="grid">
  <tr><th>Sl</th><th>Date</th><th>Time</th><th>Branch</th><th>Status</th></tr>
  <tr><td>2</td><td>26-11-2026</td><td>10:30</td><td>Chennai</td><td>Delivered</td></tr>
  <tr><td>1</td><td>25-11-2026</td><td>14:05</td><td>Tirupur</td><td>Booked</td></tr>
  <tr><td colspan="5">End of history</td></tr>
</table>
</body></html>
"""


def test_the_table_with_a_date_and_a_status_column_is_read_oldest_first() -> None:
    events = history_events(PAGE)
    assert [(e.raw_status, e.raw_description) for e in events] == [
        ("booked", "Booked"),
        ("delivered", "Delivered"),
    ]
    assert events[0].occurred_at == AFTERNOON, "date and time columns are joined"
    assert events[1].location is not None
    assert events[1].location.city == "Chennai"


def test_a_table_without_a_time_or_place_column_still_yields_events() -> None:
    page = (
        "<table><tr><td>Booking Date</td><td>Remarks</td></tr>"
        "<tr><td>25/11/2026</td><td>In transit</td></tr></table>"
    )
    (event,) = history_events(page)
    assert (event.raw_status, event.location) == ("in-transit", None)


@pytest.mark.parametrize(
    "message",
    [
        "No Record Found",
        "No data found for this LR",
        "Invalid LR Number",
        "No tracking information available",
    ],
)
def test_a_page_that_says_it_knows_no_such_shipment_means_not_found(message: str) -> None:
    with pytest.raises(ShipmentNotFound):
        history_events(f"<html><body><form></form><p>{message}</p></body></html>")


@pytest.mark.parametrize(
    "page",
    [
        "",
        "<html><body><form><input name='lrno'></form></body></html>",
        "<table><tr><th>From</th><th>To</th></tr><tr><td>Tirupur</td><td>Chennai</td></tr></table>",
        "<table><tr><th>Date</th><th>Status</th></tr></table>",
    ],
    ids=["empty", "form-only", "no-date-column", "header-only"],
)
def test_a_page_without_a_history_table_is_schema_drift_not_a_guess(page: str) -> None:
    with pytest.raises(SchemaDrift):
        history_events(page)


def test_nothing_on_the_page_is_followed_or_executed() -> None:
    page = PAGE.replace("Delivered", "<script>steal()</script><a href='http://evil.example'>Delivered</a>")
    assert history_events(page)[-1].raw_status == "delivered"
