"""Keyword gate and weighted scoring: the cheap, deterministic first two stages.

Stage 1 drops anything that never mentions a keyword. Stage 2 scores what is
left by where the keywords appear (a hit in the title says far more than one
on page 40) and by document type. The AI review, stage 3, only ever sees what
survives these two, which keeps the cost of a weekly run to cents.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from fedreg_digest.config import Profile
from fedreg_digest.federal_register import Document


def compile_keywords(keywords: list[str]) -> list[tuple[str, re.Pattern]]:
    """Whole-word, case-insensitive patterns that also match simple plurals.

    "drone" matches "Drone", "drones" and "drone's" but not "drones" inside
    another word. Irregular plurals ("therapies" for "therapy") need their own
    entry, and so does any other inflection you care about.
    """
    patterns = []
    for keyword in keywords:
        words = [re.escape(w) for w in keyword.split()]
        body = r"\s+".join(words)
        patterns.append((keyword, re.compile(rf"\b{body}(?:s|es|'s)?\b", re.IGNORECASE)))
    return patterns


@dataclass
class Assessment:
    score: int = 0
    level: str = "REJECTED"
    matched: list[str] = field(default_factory=list)
    amends_monitored_part: bool = False
    reasons: list[str] = field(default_factory=list)
    ai_reviewed: bool = False
    ai_verdict: str | None = None
    ai_failed: bool = False


class Scorer:
    def __init__(self, profile: Profile):
        self.profile = profile
        self.patterns = compile_keywords(profile.keywords)
        self.monitored = {(profile.ecfr.title, p) for p in profile.ecfr.parts}

    def _hits(self, text: str) -> dict[str, int]:
        if not text:
            return {}
        counts = {}
        for keyword, pattern in self.patterns:
            n = len(pattern.findall(text))
            if n:
                counts[keyword] = n
        return counts

    def level_for(self, score: int) -> str:
        s = self.profile.scoring
        if score >= s.high:
            return "HIGH"
        if score >= s.medium:
            return "MEDIUM"
        if score >= s.low:
            return "LOW"
        return "MINIMAL" if score > 0 else "REJECTED"

    def assess(self, document: Document, body: str = "") -> Assessment:
        s = self.profile.scoring
        title_hits = self._hits(document.title)
        abstract_hits = self._hits(f"{document.abstract}\n{document.action}")
        body_hits = self._hits(body)
        amends = any(ref in self.monitored for ref in document.cfr_references)

        result = Assessment(amends_monitored_part=amends)
        if not (title_hits or abstract_hits or body_hits or amends):
            result.reasons.append("no keyword match")
            return result

        body_total = min(sum(body_hits.values()), s.body_cap)
        score = (
            sum(title_hits.values()) * s.title_weight
            + sum(abstract_hits.values()) * s.abstract_weight
            + body_total * s.body_weight
            + s.type_bonus.get(document.type, 0)
        )
        if amends:
            score += s.monitored_part_bonus
            parts = sorted(
                {f"{t} CFR {p}" for t, p in document.cfr_references if (t, p) in self.monitored}
            )
            result.reasons.append(f"amends monitored {', '.join(parts)}")
        if title_hits:
            result.reasons.append(f"title mentions {', '.join(title_hits)}")
        if abstract_hits:
            result.reasons.append(f"summary mentions {', '.join(abstract_hits)}")
        if body_hits and not (title_hits or abstract_hits):
            result.reasons.append(f"body text mentions {', '.join(body_hits)}")

        result.score = score
        result.level = self.level_for(score)
        result.matched = sorted({*title_hits, *abstract_hits, *body_hits})
        return result
