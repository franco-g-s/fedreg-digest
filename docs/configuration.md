# Configuration

A profile is one YAML file. `fedreg-digest init` writes one for you, and `profiles/` has three
complete examples. Unknown keys are rejected with an error, so a typo cannot silently fall back to
a default.

Secrets never go in the profile. They come from environment variables or a `.env` file in the
directory you run from (see [`.env.example`](../.env.example)).

## Required

| Key | What it is |
|---|---|
| `name` | Shown at the top of the digest and in the email subject. |
| `topic` | A few sentences on who you are and what matters to you. Claude reads it for every review and summary, so this is where filtering quality comes from. Say what is *out* of scope too. |
| `keywords` | Words and phrases that signal relevance. Matching is whole-word and case-insensitive, and simple plurals (`drone` → `drones`) are automatic. Irregular forms (`therapy` / `therapies`) need their own entries. Generic words (`sodium`, `safety`) make noise. Prefer phrases. |

## `federal_register`

| Key | Default | |
|---|---|---|
| `agencies` | `[]` | Agency slugs, e.g. `food-and-drug-administration`. Find them with `fedreg-digest agencies <search>`. At least one agency or a `search` is required. |
| `document_types` | `[RULE, PRORULE, NOTICE]` | Any of `RULE` (final rules), `PRORULE` (proposed rules), `NOTICE`, `PRESDOCU` (presidential documents). Notices are numerous; drop them for a tighter digest. |
| `search` | none | Optional full-text query run by the Federal Register itself, e.g. `"remote identification"`. Narrows the candidates before local scoring. |

## `ecfr` (optional)

| Key | Default | |
|---|---|---|
| `title` | none | CFR title number: 21 Food and Drugs, 14 Aeronautics and Space, 40 Protection of Environment, and so on. |
| `parts` | `[]` | Part numbers as strings, e.g. `["101"]`. Every amendment to these parts is reported with a diff, and any Federal Register rule that amends them is included in the digest whatever its wording. |
| `substantive_only` | `true` | eCFR flags amendments that are only editorial (renumbering, corrected references). Set `false` to include them. |

## `scoring`

How the keyword stage turns matches into a level. The defaults work for most profiles.

| Key | Default | |
|---|---|---|
| `title_weight` | `10` | Points per keyword hit in the title. |
| `abstract_weight` | `5` | Points per hit in the abstract or action line. |
| `body_weight` | `1` | Points per hit in the full text... |
| `body_cap` | `6` | ...counting at most this many, so a document that mentions a keyword only in its body is at most Low. |
| `type_bonus` | `RULE: 3, PRORULE: 2` | Extra points by document type. |
| `monitored_part_bonus` | `15` | Extra points for a document that amends one of `ecfr.parts`. Enough on its own to make it High. |
| `high` / `medium` / `low` | `15` / `10` / `5` | Level thresholds. Anything above 0 but below `low` is Minimal. Zero is dropped. |

## `ai`

| Key | Default | |
|---|---|---|
| `enabled` | `true` | Off, or with no `ANTHROPIC_API_KEY`, the digest uses keyword scoring only and has no summaries. |
| `model` | `claude-haiku-5-5` | Any Claude model ID. Haiku is fast and costs cents a week. Use a larger model for denser legal text if you prefer. |
| `effort` | `low` | How hard the model thinks: `low`, `medium`, `high`, `xhigh`, `max`. |
| `review_levels` | `[HIGH, MEDIUM, LOW]` | Levels Claude double-checks against your topic. A document judged off-topic is demoted to Minimal, not deleted. Documents that amend a monitored part skip review. |
| `summarize_levels` | `[HIGH, MEDIUM]` | Levels that get key points, a two-paragraph summary and a deadline list. |
| `analyze_cfr_changes` | `true` | Rate each CFR amendment 0-100 for your topic and explain its impact. |

## `digest`

| Key | Default | |
|---|---|---|
| `lookback_days` | `7` | Window size. Match it to how often you run. `--days`, `--since` and `--until` override it per run. |
| `include_minimal` | `true` | Show Minimal items in a condensed "Also mentioned" list. |
| `output_dir` | `digests` | Where `YYYY-MM-DD.{html,md,txt,json}` and `latest.*` are written. |

## `email`

| Key | Default | |
|---|---|---|
| `enabled` | `false` | Send the digest after writing it. `--email` / `--no-email` override per run. |
| `to` | `[]` | Recipients. `FEDREG_EMAIL_TO` in the environment wins, so a committed profile never needs an address. |
| `subject` | `"{name}: {count} regulatory update{plural}"` | Python format string with `name`, `count` and `plural`. |
| `send_when_empty` | `true` | Send even when nothing was found, so silence never has to be interpreted. |

SMTP settings come from the environment: `SMTP_HOST`, `SMTP_PORT` (587 for STARTTLS, 465 for
SSL), `SMTP_USERNAME`, `SMTP_PASSWORD` and optionally `SMTP_FROM`. `fedreg-digest doctor` tests the
login without sending anything.

## Command reference

```text
fedreg-digest init [PATH]              create a profile interactively (default fedreg.yaml)
fedreg-digest doctor [-p PROFILE]      check profile, APIs, Claude key and SMTP
fedreg-digest run [-p PROFILE]         build the digest
    --days N | --since YYYY-MM-DD [--until YYYY-MM-DD]
    --no-ai                       keyword scoring only
    --email / --no-email          override email.enabled
    --out DIR                     override digest.output_dir
    --open                        open the HTML digest when done
    --cache PATH                  AI cache location ('' disables it)
fedreg-digest agencies [SEARCH]        list Federal Register agency slugs
```

Exit codes: `0` complete, `1` the digest is incomplete or was not delivered (the reasons are
printed and shown at the top of the digest), `2` the profile is invalid.
