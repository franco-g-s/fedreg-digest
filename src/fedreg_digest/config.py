"""Watch profiles: what to monitor, how to score it, and where to send the digest.

A profile is one YAML file. Everything domain-specific lives here, so the same
code can watch food labeling, drone rules or drinking-water standards without a
single change. Secrets (API keys, SMTP passwords) never go in the profile; they
come from the environment or a ``.env`` file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

DOCUMENT_TYPES = ("RULE", "PRORULE", "NOTICE", "PRESDOCU")
LEVELS = ("HIGH", "MEDIUM", "LOW", "MINIMAL")
DEFAULT_MODEL = "claude-haiku-5-5"


class ConfigError(ValueError):
    """A profile is missing something or contains a value that cannot work."""


@dataclass
class FederalRegisterConfig:
    agencies: list[str] = field(default_factory=list)
    document_types: list[str] = field(default_factory=lambda: ["RULE", "PRORULE", "NOTICE"])
    # Optional full-text search run server-side by the Federal Register, which
    # narrows the candidate set before any local scoring.
    search: str | None = None


@dataclass
class ECFRConfig:
    title: int | None = None
    parts: list[str] = field(default_factory=list)
    # Only report amendments eCFR marks as substantive. Non-substantive ones
    # are mostly editorial (renumbering, corrected cross-references).
    substantive_only: bool = True


@dataclass
class ScoringConfig:
    title_weight: int = 10
    abstract_weight: int = 5
    body_weight: int = 1
    # A long document that repeats a keyword 200 times is not 200x more
    # relevant. Body hits beyond this cap add nothing, so a document that only
    # mentions a keyword in its body tops out at Low even with the rule bonus
    # (6 + 3 < medium), e.g. the boilerplate "drinking water exposure" section
    # in every EPA pesticide tolerance.
    body_cap: int = 6
    type_bonus: dict[str, int] = field(
        default_factory=lambda: {"RULE": 3, "PRORULE": 2, "NOTICE": 0, "PRESDOCU": 0}
    )
    # Bonus for a document that amends one of the monitored CFR parts.
    monitored_part_bonus: int = 15
    high: int = 15
    medium: int = 10
    low: int = 5


@dataclass
class AIConfig:
    enabled: bool = True
    model: str = DEFAULT_MODEL
    # Levels Claude double-checks. HIGH is included because keyword scores are
    # easily fooled ("sodium" in a drug name); at Haiku prices a review costs a
    # fraction of a cent. Documents amending a monitored CFR part skip review.
    review_levels: list[str] = field(default_factory=lambda: ["HIGH", "MEDIUM", "LOW"])
    summarize_levels: list[str] = field(default_factory=lambda: ["HIGH", "MEDIUM"])
    analyze_cfr_changes: bool = True
    effort: str = "low"


@dataclass
class DigestConfig:
    lookback_days: int = 7
    include_minimal: bool = True
    output_dir: str = "digests"


@dataclass
class EmailConfig:
    enabled: bool = False
    to: list[str] = field(default_factory=list)
    subject: str = "{name}: {count} regulatory update{plural}"
    send_when_empty: bool = True


@dataclass
class Profile:
    name: str
    topic: str
    keywords: list[str]
    federal_register: FederalRegisterConfig = field(default_factory=FederalRegisterConfig)
    ecfr: ECFRConfig = field(default_factory=ECFRConfig)
    scoring: ScoringConfig = field(default_factory=ScoringConfig)
    ai: AIConfig = field(default_factory=AIConfig)
    digest: DigestConfig = field(default_factory=DigestConfig)
    email: EmailConfig = field(default_factory=EmailConfig)
    source: Path | None = None

    @property
    def watches_cfr(self) -> bool:
        return self.ecfr.title is not None and bool(self.ecfr.parts)


def _build(cls, data: dict[str, Any] | None, where: str):
    data = dict(data or {})
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ConfigError(f"Unknown key(s) in {where}: {', '.join(sorted(unknown))}")
    return cls(**data)


def load_profile(path: str | os.PathLike) -> Profile:
    path = Path(path)
    if not path.exists():
        raise ConfigError(
            f"No profile at {path}. Create one with `fedreg-digest init`, "
            "or copy one from profiles/."
        )
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a YAML mapping")

    sections = {
        "federal_register": FederalRegisterConfig,
        "ecfr": ECFRConfig,
        "scoring": ScoringConfig,
        "ai": AIConfig,
        "digest": DigestConfig,
        "email": EmailConfig,
    }
    built = {key: _build(cls, raw.pop(key, None), key) for key, cls in sections.items()}
    for required in ("name", "topic", "keywords"):
        if not raw.get(required):
            raise ConfigError(f"{path}: `{required}` is required")
    unknown = set(raw) - {"name", "topic", "keywords"}
    if unknown:
        raise ConfigError(f"{path}: unknown key(s): {', '.join(sorted(unknown))}")

    profile = Profile(
        name=str(raw["name"]),
        topic=str(raw["topic"]).strip(),
        keywords=[str(k) for k in raw["keywords"]],
        source=path,
        **built,
    )
    validate(profile)
    return profile


def validate(profile: Profile) -> None:
    fr = profile.federal_register
    fr.document_types = [t.upper() for t in fr.document_types]
    bad = [t for t in fr.document_types if t not in DOCUMENT_TYPES]
    if bad:
        raise ConfigError(
            f"federal_register.document_types: {bad} not one of {', '.join(DOCUMENT_TYPES)}"
        )
    if not fr.agencies and not fr.search:
        raise ConfigError(
            "federal_register needs at least one agency slug or a search term; otherwise every "
            "document from every agency would be fetched. Find slugs with `fedreg-digest agencies`."
        )
    profile.ecfr.parts = [str(p) for p in profile.ecfr.parts]
    if profile.ecfr.parts and profile.ecfr.title is None:
        raise ConfigError("ecfr.parts is set but ecfr.title is not")
    s = profile.scoring
    if not s.high > s.medium > s.low > 0:
        raise ConfigError("scoring thresholds must satisfy high > medium > low > 0")
    for key in ("review_levels", "summarize_levels"):
        levels = [lv.upper() for lv in getattr(profile.ai, key)]
        if any(lv not in LEVELS for lv in levels):
            raise ConfigError(f"ai.{key} must only contain {', '.join(LEVELS)}")
        setattr(profile.ai, key, levels)
    if profile.ai.effort not in ("low", "medium", "high", "xhigh", "max"):
        raise ConfigError("ai.effort must be one of low, medium, high, xhigh, max")
    if profile.digest.lookback_days < 1:
        raise ConfigError("digest.lookback_days must be at least 1")


@dataclass
class Secrets:
    """Settings that must not live in a committed profile."""

    anthropic_api_key: str | None
    smtp_host: str | None
    smtp_port: int
    smtp_username: str | None
    smtp_password: str | None
    smtp_from: str | None
    email_to: list[str]

    @classmethod
    def from_env(cls) -> Secrets:
        to = os.environ.get("FEDREG_EMAIL_TO", "")
        return cls(
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY") or None,
            smtp_host=os.environ.get("SMTP_HOST") or None,
            smtp_port=int(os.environ.get("SMTP_PORT") or 587),
            smtp_username=os.environ.get("SMTP_USERNAME") or None,
            smtp_password=os.environ.get("SMTP_PASSWORD") or None,
            smtp_from=os.environ.get("SMTP_FROM") or os.environ.get("SMTP_USERNAME") or None,
            email_to=[a.strip() for a in to.split(",") if a.strip()],
        )
