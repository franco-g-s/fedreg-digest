from datetime import date

from fedreg_digest.config import Profile
from fedreg_digest.federal_register import Document
from fedreg_digest.relevance import Scorer, compile_keywords


def profile(**kwargs):
    p = Profile(name="t", topic="t", keywords=kwargs.pop("keywords", ["drone", "remote ID"]))
    p.ecfr.title, p.ecfr.parts = 14, ["107"]
    return p


def doc(title="", abstract="", type_="NOTICE", refs=()):
    return Document(
        "2026-00001", title, type_, date(2026, 1, 1), abstract=abstract, cfr_references=list(refs)
    )


def test_keywords_match_whole_words_plurals_and_case():
    ((_, pattern),) = compile_keywords(["drone"])
    assert pattern.search("Drones over stadiums")
    assert pattern.search("the drone's battery")
    assert not pattern.search("a hydrone")
    ((_, phrase),) = compile_keywords(["remote ID"])
    assert phrase.search("Remote  ID rule")


def test_no_keyword_is_rejected():
    assert Scorer(profile()).assess(doc("Airspace change at Denver")).level == "REJECTED"


def test_title_outweighs_body_and_body_is_capped():
    scorer = Scorer(profile())
    titled = scorer.assess(doc("Drone operations"))
    buried = scorer.assess(doc("Misc"), body="drone " * 500)
    assert titled.score > buried.score
    assert buried.score == profile().scoring.body_cap * profile().scoring.body_weight
    assert buried.reasons == ["body text mentions drone"]


def test_document_amending_a_monitored_part_is_kept_without_keywords():
    a = Scorer(profile()).assess(doc("Technical amendments", type_="RULE", refs=[(14, "107")]))
    assert a.amends_monitored_part
    assert a.level == "HIGH"
    assert "amends monitored 14 CFR 107" in a.reasons[0]


def test_type_bonus_applies_to_codes():
    scorer = Scorer(profile())
    rule = scorer.assess(doc("Drone", type_="RULE"))
    notice = scorer.assess(doc("Drone", type_="NOTICE"))
    assert rule.score - notice.score == 3
