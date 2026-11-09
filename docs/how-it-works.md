# How it works

```
             Federal Register API                          eCFR API
                      │                                        │
     agencies + types │  rules amending your parts             │ section versions in the window
                      ▼                                        ▼
            ┌───────────────────┐                   ┌──────────────────────┐
            │ 1 keyword gate    │                   │ text the day before  │
            │ 2 weighted score  │                   │ vs the day it took   │
            │ 3 Claude review   │                   │ effect → diff        │
            │ 4 Claude summary  │                   │ Claude impact rating │
            └─────────┬─────────┘                   └──────────┬───────────┘
                      │            linked to the rule          │
                      └────────────► that made it ◄────────────┘
                                         │
                                         ▼
                         digest: HTML · Markdown · text · JSON  →  email (optional)
```

## Federal Register

The [Federal Register API](https://www.federalregister.gov/developers/documentation/api/v1) is
queried twice:

- **By agency, type and keyword**: documents from your agencies whose full text matches any
  keyword, found by the Federal Register's own search. Everything else would score zero, so it
  is only counted, never downloaded. The query is built to find a superset of what the local
  matcher accepts: phrases are sent as an AND-group (`(remote pilot)`) rather than a quoted
  phrase, because the search engine does not stem inside quotes, and `"remote pilot"` would miss
  "remote pilots".
- **By CFR part**: every rule or proposed rule that amends one of your parts, from any agency. A
  technical amendment to your part can be titled something your keywords would never catch.

For each document the full text is fetched and scored:

| Where a keyword appears | Points |
|---|---|
| Title | 10 per hit |
| Abstract or action line | 5 per hit |
| Body text | 1 per hit, at most 6 |
| Final / proposed rule | +3 / +2 |
| Amends a monitored part | +15 |

The score maps to High (15 or more), Medium (10 or more), Low (5 or more) or Minimal. All of these
are configurable. Keyword scores are easy to fool, though: a drug called "dexamethasone sodium
phosphate" scores High for a profile that cares about sodium on food labels. So Claude reviews
every flagged document against your `topic`. One it judges off-topic drops to Minimal, with the
reason shown. Documents that amend a monitored part are relevant by definition and skip review.

High and Medium documents get a summary: key points, two short paragraphs on what the document
does and what it changes for you, and every comment and effective date. Claude sees up to 120,000
characters of the full text. Beyond that the prompt says it is reading an excerpt.

## eCFR

The [eCFR versioner API](https://www.ecfr.gov/developers/documentation/api/v1) keeps every past
version of every section. That makes change detection a lookup rather than a monitoring job:

1. List the versions of each monitored part whose **amendment date** falls in the window.
   Filtering on the amendment date matters, because eCFR re-issues unchanged sections whenever a
   title is republished, and those carry a new *issue* date for text that has not changed in years.
2. Fetch each section as it read the day before the amendment and on the amendment date. Diff the
   regulation text. Amendment links and source notes, which change without the rule changing,
   are left out.
3. Link the change to the Federal Register rule that made it: a final rule amending that part
   whose effective date equals the amendment date.

Nothing is stored between runs. A digest for any past window can be rebuilt exactly, and a
stateless CI job needs no database.

## Reliability

A digest that silently did less is worse than no digest. So:

- If either API fails, a review or summary call fails, or a document's full text cannot be read,
  the problem is printed, shown at the top of the digest, and the command exits `1`.
- A document whose review failed is kept and marked unreviewed. Failing open never means approving.
- Every Claude call uses structured output (a JSON schema). A reply either parses into the expected
  fields or counts as a failure. Free text is never guessed at.
- eCFR's versions endpoint silently caps long responses. If it returns fewer rows than it says
  exist, the run fails instead of reporting "no changes" from a partial list.
- With `ai.enabled: true` and no `ANTHROPIC_API_KEY`, the run still produces a keyword-only
  digest but counts as incomplete. Pass `--no-ai` when that is what you want.
- Repeated query parameters are sent as lists, and the API's `fields[]` replaces its defaults, so
  every field used is requested explicitly. Getting either wrong loses documents without an
  error, which is why both have tests.

## Cost

The government APIs are free. Claude usage on the default model, Claude Haiku 5.5 ($0.10 per
million input tokens and $0.50 per million output for prompts up to 100K tokens, November 2026):

| Call | Typical size | Approximate cost |
|---|---|---|
| Relevance review | 1-2K tokens | well under $0.001 |
| Summary of a long rule | 20-40K tokens | ~$0.005 |
| CFR change analysis | 2-20K tokens | ~$0.002 |

A weekly digest for one agency usually costs a few cents. Results are cached in
`.fedreg-digest/cache.sqlite`, keyed by model and topic, so re-running the same window is free.
