"""Federal Register API client (https://www.federalregister.gov/developers/documentation/api/v1).

No key needed. Two things about this API are easy to get wrong and silently
lose documents, so they are handled here once:

* Repeated query keys (``conditions[type][]``) must be sent as a list. Building
  a dict in a loop keeps only the last value.
* ``fields[]`` replaces the default field set rather than adding to it, so
  every field used anywhere downstream is requested explicitly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import requests

from fedreg_digest.http import get

BASE_URL = "https://www.federalregister.gov/api/v1"

FIELDS = [
    "abstract",
    "action",
    "agencies",
    "cfr_references",
    "citation",
    "comments_close_on",
    "document_number",
    "effective_on",
    "html_url",
    "pdf_url",
    "publication_date",
    "raw_text_url",
    "title",
    "type",
]

# The API reports display names but filters on codes.
_TYPE_CODES = {
    "RULE": "RULE",
    "PROPOSED RULE": "PRORULE",
    "PRORULE": "PRORULE",
    "NOTICE": "NOTICE",
    "PRESIDENTIAL DOCUMENT": "PRESDOCU",
    "PRESDOCU": "PRESDOCU",
}
TYPE_LABELS = {
    "RULE": "Final rule",
    "PRORULE": "Proposed rule",
    "NOTICE": "Notice",
    "PRESDOCU": "Presidential document",
}


def keyword_query(keywords: list[str], search: str | None = None) -> str:
    """A Federal Register full-text query matching any keyword (ANDed with ``search``).

    Running the keyword gate server-side means full text is only downloaded for
    documents that can possibly score. The query must match a *superset* of what
    the local matcher accepts, or documents are lost without any error:

    * Single words are left bare, so the search engine stems them ("drone"
      also finds "drones").
    * Phrases become an AND-group of their words rather than a quoted phrase,
      because the engine does not stem inside quotes: ``"remote pilot"`` misses
      "remote pilots", while ``(remote pilot)`` finds it. Hyphenated terms are
      split the same way.
    """
    terms = []
    for keyword in keywords:
        words = re.findall(r"[\w']+", keyword)
        if len(words) == 1:
            terms.append(words[0])
        elif words:
            terms.append("(" + " ".join(words) + ")")
    query = " | ".join(terms)
    if search:
        return f"({search}) ({query})" if query else search
    return query


def type_code(value: str | None) -> str:
    key = (value or "").strip().upper()
    return _TYPE_CODES.get(key, key or "NOTICE")


@dataclass
class Document:
    document_number: str
    title: str
    type: str
    publication_date: date
    abstract: str = ""
    action: str = ""
    agencies: list[str] = field(default_factory=list)
    agency_slugs: list[str] = field(default_factory=list)
    cfr_references: list[tuple[int, str]] = field(default_factory=list)
    citation: str = ""
    html_url: str = ""
    pdf_url: str = ""
    raw_text_url: str = ""
    effective_on: date | None = None
    comments_close_on: date | None = None

    @property
    def type_label(self) -> str:
        return TYPE_LABELS.get(self.type, self.type.title())

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Document:
        def as_date(value):
            return date.fromisoformat(value) if value else None

        refs = []
        for ref in data.get("cfr_references") or []:
            if ref.get("title") and ref.get("part"):
                refs.append((int(ref["title"]), str(ref["part"])))
        return cls(
            document_number=data["document_number"],
            title=(data.get("title") or "").strip(),
            type=type_code(data.get("type")),
            publication_date=as_date(data["publication_date"]),
            abstract=(data.get("abstract") or "").strip(),
            action=(data.get("action") or "").strip(),
            agencies=[a.get("name") or a.get("raw_name") or "" for a in data.get("agencies") or []],
            agency_slugs=[a["slug"] for a in data.get("agencies") or [] if a.get("slug")],
            cfr_references=refs,
            citation=data.get("citation") or "",
            html_url=data.get("html_url") or "",
            pdf_url=data.get("pdf_url") or "",
            raw_text_url=data.get("raw_text_url") or "",
            effective_on=as_date(data.get("effective_on")),
            comments_close_on=as_date(data.get("comments_close_on")),
        )


class FederalRegisterClient:
    def __init__(self, session: requests.Session, base_url: str = BASE_URL):
        self.session = session
        self.base_url = base_url

    def search(
        self,
        start: date,
        end: date,
        *,
        agencies: list[str] | None = None,
        document_types: list[str] | None = None,
        term: str | None = None,
        cfr: tuple[int, str] | None = None,
    ) -> list[Document]:
        """All documents published in [start, end] matching the conditions."""
        params: dict[str, Any] = {
            "conditions[publication_date][gte]": start.isoformat(),
            "conditions[publication_date][lte]": end.isoformat(),
            "fields[]": FIELDS,
            "per_page": 100,
            "order": "newest",
        }
        if agencies:
            params["conditions[agencies][]"] = list(agencies)
        if document_types:
            params["conditions[type][]"] = list(document_types)
        if term:
            params["conditions[term]"] = term
        if cfr:
            params["conditions[cfr][title]"] = cfr[0]
            params["conditions[cfr][part]"] = cfr[1]

        documents: list[Document] = []
        url: str | None = f"{self.base_url}/documents.json"
        while url:
            payload = get(self.session, url, params=params).json()
            documents.extend(Document.from_api(d) for d in payload.get("results") or [])
            # next_page_url already carries every condition plus the cursor.
            url = payload.get("next_page_url")
            params = None
        return documents

    def count(
        self,
        start: date,
        end: date,
        *,
        agencies: list[str] | None = None,
        document_types: list[str] | None = None,
        term: str | None = None,
    ) -> int:
        """How many documents match, without downloading them."""
        params: dict[str, Any] = {
            "conditions[publication_date][gte]": start.isoformat(),
            "conditions[publication_date][lte]": end.isoformat(),
            "fields[]": ["document_number"],
            "per_page": 1,
        }
        if agencies:
            params["conditions[agencies][]"] = list(agencies)
        if document_types:
            params["conditions[type][]"] = list(document_types)
        if term:
            params["conditions[term]"] = term
        return int(
            get(self.session, f"{self.base_url}/documents.json", params=params).json()["count"]
        )

    def full_text(self, document: Document) -> str:
        if not document.raw_text_url:
            return ""
        return get(self.session, document.raw_text_url).text

    def agencies(self) -> list[dict[str, Any]]:
        return get(self.session, f"{self.base_url}/agencies.json").json()
