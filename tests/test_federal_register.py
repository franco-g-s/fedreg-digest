from datetime import date

from conftest import FakeResponse, fixture_json

from fedreg_digest.federal_register import FederalRegisterClient, keyword_query, type_code


def test_repeated_conditions_are_sent_as_lists(session):
    session.add(lambda u, p: True, FakeResponse(json_data={"results": [], "next_page_url": None}))
    FederalRegisterClient(session).search(
        date(2025, 1, 1),
        date(2025, 1, 7),
        agencies=["food-and-drug-administration"],
        document_types=["RULE", "PRORULE", "NOTICE"],
    )
    _, params = session.calls[0]
    assert params["conditions[type][]"] == ["RULE", "PRORULE", "NOTICE"]
    assert "raw_text_url" in params["fields[]"] and "title" in params["fields[]"]


def test_follows_next_page_url(session):
    page = fixture_json("fr_cfr_21_101.json")
    first = dict(
        page,
        results=page["results"][:2],
        next_page_url="https://www.federalregister.gov/api/v1/documents?page=2&search_after_cursor=abc",
    )
    second = dict(page, results=page["results"][2:], next_page_url=None)
    session.add(lambda u, p: "search_after_cursor" in u, FakeResponse(json_data=second))
    session.add(lambda u, p: True, FakeResponse(json_data=first))
    docs = FederalRegisterClient(session).search(
        date(2025, 1, 1), date(2025, 5, 30), cfr=(21, "101")
    )
    assert [d.document_number for d in docs] == ["2025-08204", "2025-03118", "2025-00778"]
    rule = docs[1]
    assert rule.type == "RULE" and rule.type_label == "Final rule"
    assert rule.effective_on == date(2025, 4, 28)
    assert (21, "101") in rule.cfr_references


def test_type_code_maps_display_names():
    assert type_code("Proposed Rule") == "PRORULE"
    assert type_code("Rule") == "RULE"
    assert type_code("Presidential Document") == "PRESDOCU"


def test_keyword_query_groups_phrases_so_plurals_still_match():
    # Quoted phrases are not stemmed by the Federal Register's search, so a
    # phrase must be sent as an AND-group to stay a superset of local matching.
    assert (
        keyword_query(["drone", "remote pilot", 'say "hi"']) == "drone | (remote pilot) | (say hi)"
    )
    assert keyword_query(["front-of-package"]) == "(front of package)"
    assert keyword_query(["a", "b"], search="water") == "(water) (a | b)"
