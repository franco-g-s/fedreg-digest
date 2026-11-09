"""One run: fetch, score, review, summarise, detect CFR changes, build the digest."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone

from fedreg_digest.ai import AIError, Analyst, ChangeAnalysis, Review, Summary
from fedreg_digest.cache import Cache
from fedreg_digest.config import Profile
from fedreg_digest.ecfr import CFRChange, ECFRClient
from fedreg_digest.federal_register import Document, FederalRegisterClient, keyword_query
from fedreg_digest.http import APIError, make_session
from fedreg_digest.relevance import Assessment, Scorer

LEVEL_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2, "MINIMAL": 3}

Log = Callable[[str], None]


@dataclass
class DocumentItem:
    document: Document
    assessment: Assessment
    summary: Summary | None = None


@dataclass
class ChangeItem:
    change: CFRChange
    analysis: ChangeAnalysis | None = None


@dataclass
class Digest:
    profile: Profile
    start: date
    end: date
    generated_at: datetime
    documents: list[DocumentItem] = field(default_factory=list)
    changes: list[ChangeItem] = field(default_factory=list)
    fetched: int = 0
    rejected: int = 0
    ai_used: bool = False
    ai_calls: int = 0
    # Problems the reader must see, because they mean the digest is incomplete.
    problems: list[str] = field(default_factory=list)
    cfr_checked: bool = False
    cfr_current_as_of: date | None = None

    @property
    def count(self) -> int:
        return len(self.documents) + len(self.changes)

    def by_level(self, level: str) -> list[DocumentItem]:
        return [i for i in self.documents if i.assessment.level == level]

    @property
    def deadlines(self) -> list[tuple[date, str, Document]]:
        """Upcoming comment and effective dates across all documents, soonest first."""
        out = []
        for item in self.documents:
            if item.assessment.level == "MINIMAL":
                continue
            d = item.document
            if d.comments_close_on and d.comments_close_on >= self.end:
                out.append((d.comments_close_on, "Comments close", d))
            if d.effective_on and d.effective_on >= self.end:
                out.append((d.effective_on, "Takes effect", d))
        return sorted(out, key=lambda x: x[0])


def window(lookback_days: int, end: date | None = None) -> tuple[date, date]:
    end = end or date.today()
    return end - timedelta(days=lookback_days - 1), end


def _quiet(_: str) -> None:
    pass


class Pipeline:
    def __init__(
        self,
        profile: Profile,
        *,
        api_key: str | None = None,
        use_ai: bool = True,
        cache: Cache | None = None,
        session=None,
        analyst: Analyst | None = None,
        log: Log = _quiet,
    ):
        self.profile = profile
        self.session = session or make_session()
        self.fr = FederalRegisterClient(self.session)
        self.ecfr = ECFRClient(self.session)
        self.scorer = Scorer(profile)
        self.cache = cache or Cache(None)
        self.log = log
        self.use_ai = use_ai and profile.ai.enabled
        self.analyst = analyst
        if self.use_ai and self.analyst is None:
            self.analyst = Analyst(profile, api_key=api_key)
        self.scope = Cache.scope(profile.ai.model, profile.topic)

    # -- AI with caching ----------------------------------------------------

    def _ai(self, digest: Digest, kind: str, key: str, call, build):
        cached = self.cache.get(kind, key, self.scope)
        if cached is not None:
            return build(**cached)
        try:
            result = call()
        except AIError as exc:
            digest.problems.append(f"AI {kind} failed for {key}: {exc}")
            return None
        digest.ai_calls += 1
        self.cache.put(kind, key, self.scope, asdict(result))
        return result

    # -- Federal Register ---------------------------------------------------

    def _fetch_documents(self, digest: Digest) -> list[Document]:
        """Candidates: documents whose text matches a keyword, plus anything amending
        a monitored part. Everything else would score zero, so it is only counted."""
        fr = self.profile.federal_register
        start, end = digest.start, digest.end
        digest.fetched = self.fr.count(
            start, end, agencies=fr.agencies, document_types=fr.document_types, term=fr.search
        )
        found: dict[str, Document] = {}
        for doc in self.fr.search(
            start,
            end,
            agencies=fr.agencies,
            document_types=fr.document_types,
            term=keyword_query(self.profile.keywords, fr.search),
        ):
            found[doc.document_number] = doc
        # Anything that amends a monitored part is fetched even if it comes from
        # an agency or uses words the profile did not anticipate.
        if self.profile.watches_cfr:
            for part in self.profile.ecfr.parts:
                for doc in self.fr.search(
                    start,
                    end,
                    document_types=["RULE", "PRORULE"],
                    cfr=(self.profile.ecfr.title, part),
                ):
                    if doc.document_number not in found:
                        found[doc.document_number] = doc
                        if fr.agencies and not set(fr.agencies) & set(doc.agency_slugs):
                            digest.fetched += 1  # from outside the counted agencies
        return list(found.values())

    def _process_documents(self, digest: Digest) -> None:
        self.log("Searching the Federal Register")
        documents = self._fetch_documents(digest)
        self.log(
            f"  {digest.fetched} documents published {digest.start} to {digest.end}, "
            f"{len(documents)} mention a keyword or a monitored part"
        )

        bodies: dict[str, str] = {}
        for doc in documents:
            try:
                bodies[doc.document_number] = self.fr.full_text(doc)
            except APIError as exc:
                bodies[doc.document_number] = ""
                digest.problems.append(
                    f"Could not read the full text of {doc.document_number}; scored on title and "
                    f"abstract only ({exc})"
                )
            assessment = self.scorer.assess(doc, bodies[doc.document_number])
            if assessment.level != "REJECTED":
                digest.documents.append(DocumentItem(doc, assessment))
        digest.rejected = max(0, digest.fetched - len(digest.documents))

        if self.use_ai:
            review_levels = set(self.profile.ai.review_levels)
            to_review = [
                i
                for i in digest.documents
                if i.assessment.level in review_levels and not i.assessment.amends_monitored_part
            ]
            if to_review:
                self.log(f"Asking Claude to review {len(to_review)} borderline document(s)")
            for item in to_review:
                review = self._ai(
                    digest,
                    "review",
                    item.document.document_number,
                    lambda item=item: self.analyst.review(item.document, item.assessment.matched),
                    Review,
                )
                a = item.assessment
                if review is None:
                    a.ai_failed = True
                    continue
                a.ai_reviewed = True
                a.ai_verdict = review.reason
                if not review.relevant:
                    a.reasons.append("Claude judged it off-topic")
                    a.level = "MINIMAL"

        if not self.profile.digest.include_minimal:
            digest.documents = [i for i in digest.documents if i.assessment.level != "MINIMAL"]

        digest.documents.sort(
            key=lambda i: (LEVEL_ORDER[i.assessment.level], -i.assessment.score, i.document.title)
        )

        if self.use_ai:
            to_summarize = [
                i
                for i in digest.documents
                if i.assessment.level in set(self.profile.ai.summarize_levels)
            ]
            if to_summarize:
                self.log(f"Summarising {len(to_summarize)} document(s)")
            for item in to_summarize:
                item.summary = self._ai(
                    digest,
                    "summary",
                    item.document.document_number,
                    lambda item=item: self.analyst.summarize(
                        item.document, bodies.get(item.document.document_number, "")
                    ),
                    Summary,
                )

    # -- eCFR -----------------------------------------------------------------

    def _related_rules(self, change: CFRChange) -> list[Document]:
        """Final rules that amended this part and took effect on the amendment date."""
        start = change.amendment_date - timedelta(days=730)
        rules = self.fr.search(
            start, change.amendment_date, document_types=["RULE"], cfr=(change.title, change.part)
        )
        exact = [r for r in rules if r.effective_on == change.amendment_date]
        return exact or sorted(rules, key=lambda r: r.publication_date, reverse=True)[:1]

    def _process_cfr(self, digest: Digest) -> None:
        ecfr = self.profile.ecfr
        self.log(f"Checking {ecfr.title} CFR part(s) {', '.join(ecfr.parts)} on eCFR")
        try:
            current = self.ecfr.current_date(ecfr.title)
        except APIError as exc:
            digest.problems.append(
                f"eCFR could not be reached, so CFR changes were not checked ({exc})"
            )
            return
        end = min(digest.end, current)
        if current < digest.end:
            self.log(f"  eCFR is current as of {current}")

        complete = True
        for part in ecfr.parts:
            try:
                changes = self.ecfr.changes(
                    ecfr.title, part, digest.start, end, ecfr.substantive_only
                )
            except APIError as exc:
                digest.problems.append(f"Could not check {ecfr.title} CFR {part} ({exc})")
                complete = False
                continue
            for change in changes:
                try:
                    change.related_documents = self._related_rules(change)
                except APIError:
                    change.related_documents = []
                digest.changes.append(ChangeItem(change))
        digest.cfr_checked = complete
        digest.cfr_current_as_of = current
        self.log(f"  {len(digest.changes)} amended section(s)")

        if self.use_ai and self.profile.ai.analyze_cfr_changes and digest.changes:
            self.log(f"Analysing {len(digest.changes)} CFR change(s)")
            for item in digest.changes:
                item.analysis = self._ai(
                    digest,
                    "cfr_change",
                    item.change.key,
                    lambda item=item: self.analyst.analyze_change(item.change),
                    ChangeAnalysis,
                )
            digest.changes.sort(key=lambda i: -(i.analysis.relevance if i.analysis else 0))

    # -- entry point ----------------------------------------------------------

    def run(self, start: date, end: date) -> Digest:
        digest = Digest(
            profile=self.profile,
            start=start,
            end=end,
            generated_at=datetime.now(timezone.utc),
            ai_used=self.use_ai,
        )
        try:
            self._process_documents(digest)
        except APIError as exc:
            digest.problems.append(
                "The Federal Register could not be searched, so this digest has no documents "
                f"({exc})"
            )
        if self.profile.watches_cfr:
            self._process_cfr(digest)
        return digest
