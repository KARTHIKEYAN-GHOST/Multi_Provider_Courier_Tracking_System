from __future__ import annotations

import json

from ai_repair.scrub import scrub


def test_the_tracking_number_emails_and_phone_numbers_are_replaced() -> None:
    page = "<td>LR99887</td><p>Call +91 98765 43210 or 9876543210, mail Ravi.K@sbtl.co.in (lr99887)</p>"
    cleaned = scrub(page, "LR99887")
    assert cleaned == (
        "<td>TRACKING0001</td><p>Call 9000000000 or 9000000000, mail person@example.com (TRACKING0001)</p>"
    )


def test_json_stays_json_and_other_numbers_are_left_alone() -> None:
    body = {"awb": "1234567890123", "scans": [{"contact": "a@b.in", "pin": 641604, "ref": "AB9876543210"}]}
    cleaned = scrub(body, "1234567890123")
    assert cleaned == {
        "awb": "TRACKING0001",
        "scans": [{"contact": "person@example.com", "pin": 641604, "ref": "AB9876543210"}],
    }
    assert "1234567890123" not in json.dumps(cleaned)


def test_a_very_short_tracking_number_is_not_used_as_a_pattern() -> None:
    assert scrub("status 12 of 12", "12") == "status 12 of 12"
