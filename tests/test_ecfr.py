from datetime import date

import pytest
from conftest import FakeResponse, fixture_bytes, fixture_json

from fedreg_digest.ecfr import ECFRClient, parse_section_xml
from fedreg_digest.http import APIError


def ecfr_session(session):
    session.add(
        lambda u, p: "/versions/" in u,
        FakeResponse(json_data=fixture_json("ecfr_versions_21_101.json")),
    )
    session.add(
        lambda u, p: "/full/2025-04-27/" in u and p.get("section") == "101.13",
        FakeResponse(body=fixture_bytes("ecfr_101.13_before.xml")),
    )
    session.add(
        lambda u, p: "/full/2025-04-28/" in u and p.get("section") == "101.13",
        FakeResponse(body=fixture_bytes("ecfr_101.13_after.xml")),
    )
    return session


def test_versions_filter_on_amendment_date_not_issue_date(session):
    versions = ECFRClient(ecfr_session(session)).versions(
        21, "101", date(2026, 10, 1), date(2026, 11, 5)
    )
    # 101.9, 101.12 and 101.30 were re-issued in Oct 2026 without being amended.
    assert versions == []


def test_duplicate_listings_are_merged(session):
    versions = ECFRClient(ecfr_session(session)).versions(
        21, "101", date(2025, 4, 1), date(2025, 4, 30)
    )
    assert [(v.identifier, v.substantive) for v in versions] == [("101.13", True), ("101.65", True)]


def test_detects_a_real_amendment_with_a_diff(session):
    session = ecfr_session(session)
    # 101.65's text is not in the fixtures: that call 404s, which must read as
    # "did not exist" for the before side and skip the section when both miss.
    changes = ECFRClient(session).changes(21, "101", date(2025, 4, 1), date(2025, 4, 30))
    change = next(c for c in changes if c.identifier == "101.13")
    assert change.change_type == "MODIFIED"
    assert change.lines_added == 1 and change.lines_removed == 1
    assert any("“healthy”" in line for line in change.diff if line.startswith("+"))
    assert change.compare_url.endswith("/compare/2025-04-28/to/2025-04-27/title-21/section-101.13")
    assert all(c.identifier != "101.65" for c in changes)


def test_parse_keeps_utf8_and_ignores_amendment_links():
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?><DIV8 N="1.1" TYPE="SECTION">'
        "<HEAD>§ 1.1 Test.</HEAD><P>(a) “Quoted” text.</P>"
        '<XREF ID="1">Link to an amendment published at 91 FR 1.</XREF>'
        "<DIV><TABLE><TR><TH>A</TH><TH>B</TH></TR><TR><TD>1</TD><TD>2</TD></TR></TABLE></DIV></DIV8>"
    ).encode()
    heading, text = parse_section_xml(xml)
    assert heading == "§ 1.1 Test."
    assert text.splitlines() == ["(a) “Quoted” text.", "A | B", "1 | 2"]


def test_truncated_version_list_is_an_error_not_an_empty_week(session):
    rows = fixture_json("ecfr_versions_21_101.json")
    rows["meta"]["result_count"] = "5000"
    session.add(lambda u, p: "/versions/" in u, FakeResponse(json_data=rows))
    with pytest.raises(APIError, match="truncated"):
        ECFRClient(session).versions(21, "101", date(2025, 4, 1), date(2025, 4, 30))
