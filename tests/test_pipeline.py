from datetime import date

from conftest import FakeResponse, fixture_bytes, fixture_json

from fedreg_digest.ai import AIError, ChangeAnalysis, Review, Summary
from fedreg_digest.cache import Cache
from fedreg_digest.config import Profile
from fedreg_digest.pipeline import Pipeline
from fedreg_digest.render import render

FR_PAGE = {
    "count": 40,
    "next_page_url": None,
    "results": [
        {
            "document_number": "2025-1",
            "title": "Front-of-package labeling for packaged food",
            "type": "Proposed Rule",
            "publication_date": "2025-04-02",
            "abstract": "Labeling.",
            "html_url": "https://fr.example/1",
            "raw_text_url": "https://fr.example/1.txt",
            "comments_close_on": "2025-07-01",
            "agencies": [{"name": "Food and Drug Administration"}],
        },
        {
            "document_number": "2025-2",
            "title": "Dexamethasone sodium phosphate drops",
            "type": "Notice",
            "publication_date": "2025-04-03",
            "abstract": "Not withdrawn for safety.",
            "html_url": "https://fr.example/2",
            "agencies": [],
        },
        {
            "document_number": "2025-3",
            "title": "Unrelated device classification",
            "type": "Rule",
            "publication_date": "2025-04-04",
            "abstract": "Devices.",
            "html_url": "https://fr.example/3",
            "agencies": [],
        },
    ],
}


def make_profile(watch_cfr=True):
    p = Profile(name="Food labels", topic="Food labeling", keywords=["front-of-package", "sodium"])
    p.federal_register.agencies = ["food-and-drug-administration"]
    if watch_cfr:
        p.ecfr.title, p.ecfr.parts = 21, ["101"]
    return p


def wire(session, fr_ok=True):
    session.add(lambda u, p: u.endswith(".txt"), FakeResponse(body="front-of-package " * 3))
    session.add(
        lambda u, p: "documents.json" in u and "conditions[cfr][part]" in p,
        FakeResponse(json_data=fixture_json("fr_cfr_21_101.json") | {"next_page_url": None}),
    )
    session.add(
        lambda u, p: "documents.json" in u,
        FakeResponse(json_data=FR_PAGE) if fr_ok else FakeResponse(503, "down"),
    )
    session.add(
        lambda u, p: u.endswith("titles.json"),
        FakeResponse(json_data={"titles": [{"number": 21, "up_to_date_as_of": "2025-05-05"}]}),
    )
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


class FakeAnalyst:
    def __init__(self, fail_summaries=False):
        self.calls = []
        self.fail_summaries = fail_summaries

    def review(self, document, matched):
        self.calls.append(("review", document.document_number))
        relevant = "sodium phosphate" not in document.title.lower()
        return Review(relevant, "high", "on topic" if relevant else "a drug, not food labeling")

    def summarize(self, document, body=""):
        self.calls.append(("summary", document.document_number))
        if self.fail_summaries:
            raise AIError("boom")
        return Summary(["Point one"], "Para one.\n\nPara two.", ["Comments close 1 Jul 2025"])

    def analyze_change(self, change):
        self.calls.append(("cfr", change.identifier))
        return ChangeAnalysis(80, "Redefines healthy.", "Labels must be checked.")


def run(session, analyst, cache=None, **kw):
    pipeline = Pipeline(
        make_profile(**kw), session=session, analyst=analyst, cache=cache or Cache(None)
    )
    return pipeline.run(date(2025, 4, 1), date(2025, 4, 30))


def test_full_run_scores_reviews_and_detects_cfr_changes(session):
    analyst = FakeAnalyst()
    digest = run(wire(session), analyst)

    numbers = {i.document.document_number: i for i in digest.documents}
    assert "2025-3" not in numbers  # no keyword
    assert numbers["2025-2"].assessment.level == "MINIMAL"  # demoted by review
    assert numbers["2025-1"].summary.key_points == ["Point one"]
    # The "healthy" rule arrives through the monitored-part query and skips review.
    assert numbers["2025-03118"].assessment.amends_monitored_part
    assert ("review", "2025-03118") not in analyst.calls

    [change] = [c for c in digest.changes if c.change.identifier == "101.13"]
    assert change.analysis.relevance == 80
    assert [r.document_number for r in change.change.related_documents] == ["2025-03118"]
    assert not digest.problems
    assert digest.fetched == 40
    # The agency query is a keyword search, so only candidates are downloaded.
    agency_calls = [
        p for u, p in session.calls if "documents.json" in u and "conditions[agencies][]" in p
    ]
    assert any(p.get("conditions[term]") == "(front of package) | sodium" for p in agency_calls)

    html = render(digest)["html"]
    assert "Front-of-package labeling" in html and "21 CFR § 101.13" in html
    assert "Comments close" in html


def test_ai_failure_is_reported_not_hidden(session):
    digest = run(wire(session), FakeAnalyst(fail_summaries=True))
    assert any("AI summary failed" in p for p in digest.problems)
    assert "may be incomplete" in render(digest)["html"]


def test_unreachable_federal_register_is_a_problem_not_an_empty_digest(session):
    digest = run(wire(session, fr_ok=False), FakeAnalyst(), watch_cfr=False)
    assert digest.documents == []
    assert any("could not be searched" in p for p in digest.problems)


def test_cache_avoids_paying_twice(session, tmp_path):
    cache = Cache(tmp_path / "c.sqlite")
    first = FakeAnalyst()
    run(wire(session), first, cache=cache)
    second = FakeAnalyst()
    run(wire(session), second, cache=cache)
    assert first.calls and second.calls == []
