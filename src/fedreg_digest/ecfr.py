"""eCFR change detection without local snapshots.

The eCFR versioner API keeps every historical version of every section, so
"what changed in 21 CFR 101 this week?" can be answered directly:

1. List the section versions of a part whose *amendment date* falls in the
   window (``/versions``).
2. Fetch each section as it read the day before the amendment and on the
   amendment date (``/full/{date}``), and diff the two.

This needs no database and gives the same answer on every machine, which is
what makes the tool safe to run from a stateless CI job.

Filtering on ``amendment_date`` rather than ``issue_date`` matters: eCFR
re-issues unchanged sections when a title is republished, so ``issue_date``
moves for text that has not changed in years.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import requests
from defusedxml import ElementTree as ET

from fedreg_digest.http import APIError, get

BASE_URL = "https://www.ecfr.gov/api/versioner/v1"
SITE_URL = "https://www.ecfr.gov"

# Leaf-level elements that carry regulation text. Container elements (DIV,
# EXTRACT, NOTE) are skipped so their text is not collected twice, and XREF
# (amendment links eCFR adds alongside the text) is left out on purpose:
# those links change without the rule changing.
_TEXT_TAGS = {"P", "FP", "HD1", "HD2", "HD3", "CAPTION"}


@dataclass
class SectionVersion:
    identifier: str
    name: str
    kind: str  # "section" or "appendix"
    amendment_date: date
    substantive: bool
    removed: bool


@dataclass
class CFRChange:
    title: int
    part: str
    identifier: str
    heading: str
    change_type: str  # ADDED, MODIFIED, REMOVED
    amendment_date: date
    substantive: bool
    before_text: str = ""
    after_text: str = ""
    diff: list[str] = field(default_factory=list)
    lines_added: int = 0
    lines_removed: int = 0
    similarity: float = 0.0
    related_documents: list[Any] = field(default_factory=list)

    @property
    def citation(self) -> str:
        if self.identifier[:1].isdigit():
            return f"{self.title} CFR § {self.identifier}"
        return f"{self.title} CFR Part {self.part}, {self.identifier}"

    @property
    def url(self) -> str:
        if self.identifier[:1].isdigit():
            return (
                f"{SITE_URL}/on/{self.amendment_date}/title-{self.title}/section-{self.identifier}"
            )
        return f"{SITE_URL}/on/{self.amendment_date}/title-{self.title}/part-{self.part}"

    @property
    def compare_url(self) -> str | None:
        if self.change_type != "MODIFIED" or not self.identifier[:1].isdigit():
            return None
        before = self.amendment_date - timedelta(days=1)
        return (
            f"{SITE_URL}/compare/{self.amendment_date}/to/{before}"
            f"/title-{self.title}/section-{self.identifier}"
        )

    @property
    def key(self) -> str:
        return f"{self.title}:{self.identifier}:{self.amendment_date}"


def parse_section_xml(xml: bytes) -> tuple[str, str]:
    """Return (heading, text) for one section or appendix of eCFR XML."""
    root = ET.fromstring(xml)
    head = root.find("HEAD")
    heading = " ".join("".join(head.itertext()).split()) if head is not None else ""
    lines: list[str] = []
    for element in root.iter():
        if element.tag in _TEXT_TAGS:
            text = " ".join("".join(element.itertext()).split())
        elif element.tag == "TR":
            cells = [
                " ".join("".join(c.itertext()).split()) for c in element if c.tag in ("TD", "TH")
            ]
            text = " | ".join(cells)
        else:
            continue
        if text:
            lines.append(text)
    return heading, "\n".join(lines)


def diff_texts(before: str, after: str) -> tuple[list[str], int, int, float]:
    a, b = before.splitlines(), after.splitlines()
    diff = list(difflib.unified_diff(a, b, lineterm="", n=1))[2:]  # drop ---/+++ header
    added = sum(1 for line in diff if line.startswith("+"))
    removed = sum(1 for line in diff if line.startswith("-"))
    similarity = difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()  # by line: fast
    return diff, added, removed, similarity


class ECFRClient:
    def __init__(self, session: requests.Session, base_url: str = BASE_URL):
        self.session = session
        self.base_url = base_url

    def current_date(self, title: int) -> date:
        """The date the eCFR text of ``title`` is current as of."""
        payload = get(self.session, f"{self.base_url}/titles.json").json()
        for entry in payload.get("titles", []):
            if int(entry.get("number", -1)) == int(title):
                return date.fromisoformat(entry["up_to_date_as_of"])
        raise APIError(f"eCFR has no title {title}")

    def versions(self, title: int, part: str, start: date, end: date) -> list[SectionVersion]:
        """Section versions of a part whose amendment date is within [start, end]."""
        # issue_date >= amendment_date always holds, so this server-side filter
        # can only drop rows we would have discarded anyway.
        params = {"part": part, "issue_date[gte]": start.isoformat()}
        payload = get(
            self.session, f"{self.base_url}/versions/title-{title}.json", params=params
        ).json()
        rows = payload.get("content_versions", [])
        # The endpoint silently caps its response (1000 rows for a whole title).
        # A truncated list would read as "no changes", so refuse to use one.
        expected = payload.get("meta", {}).get("result_count")
        if (expected is not None and int(expected) > len(rows)) or len(rows) >= 1000:
            raise APIError(
                f"eCFR returned {len(rows)} of {expected or 'unknown'} versions for "
                f"{title} CFR {part}; refusing to work from a truncated list"
            )
        seen: dict[tuple[str, date], SectionVersion] = {}
        for row in rows:
            amended = date.fromisoformat(row["amendment_date"])
            if not start <= amended <= end:
                continue
            version = SectionVersion(
                identifier=row["identifier"],
                name=" ".join((row.get("name") or "").split()),
                kind=row.get("type", "section"),
                amendment_date=amended,
                substantive=bool(row.get("substantive")),
                removed=bool(row.get("removed")),
            )
            key = (version.identifier, amended)
            # The same amendment can be listed once per issue; keep one, and
            # call it substantive if any listing says so.
            if key in seen:
                seen[key].substantive |= version.substantive
                seen[key].removed |= version.removed
            else:
                seen[key] = version
        return sorted(seen.values(), key=lambda v: (v.amendment_date, v.identifier))

    def section_text(
        self, title: int, part: str, version: SectionVersion, on: date
    ) -> tuple[str, str] | None:
        """(heading, text) of a section on a date, or None if it did not exist then."""
        if version.kind == "appendix":
            params = {"part": part, "appendix": version.identifier}
        else:
            params = {"section": version.identifier}
        url = f"{self.base_url}/full/{on.isoformat()}/title-{title}.xml"
        response = self.session.get(
            url, params=params, timeout=getattr(self.session, "request_timeout", 30)
        )
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise APIError(f"{url} returned HTTP {response.status_code}: {response.text[:200]}")
        # Parse bytes, not response.text: the XML declares UTF-8 but the
        # Content-Type has no charset, so requests would decode it as Latin-1.
        return parse_section_xml(response.content)

    def changes(
        self, title: int, part: str, start: date, end: date, substantive_only: bool = True
    ) -> list[CFRChange]:
        changes = []
        for version in self.versions(title, part, start, end):
            if substantive_only and not version.substantive and not version.removed:
                continue
            day_before = version.amendment_date - timedelta(days=1)
            before = self.section_text(title, part, version, day_before)
            after = (
                None
                if version.removed
                else self.section_text(title, part, version, version.amendment_date)
            )

            if before is None and after is None:
                continue
            if before is None:
                change_type = "ADDED"
            elif after is None:
                change_type = "REMOVED"
            else:
                change_type = "MODIFIED"

            before_heading, before_text = before or ("", "")
            after_heading, after_text = after or ("", "")
            if (
                change_type == "MODIFIED"
                and before_text == after_text
                and before_heading == after_heading
            ):
                continue  # listed as amended, but the text is identical

            diff, added, removed, similarity = diff_texts(before_text, after_text)
            changes.append(
                CFRChange(
                    title=title,
                    part=part,
                    identifier=version.identifier,
                    heading=after_heading or before_heading or version.name,
                    change_type=change_type,
                    amendment_date=version.amendment_date,
                    substantive=version.substantive,
                    before_text=before_text,
                    after_text=after_text,
                    diff=diff,
                    lines_added=added,
                    lines_removed=removed,
                    similarity=similarity,
                )
            )
        return changes
