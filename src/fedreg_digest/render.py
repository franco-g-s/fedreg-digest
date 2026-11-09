"""Turn a Digest into HTML (email and browser), Markdown and plain text."""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from pathlib import Path

from jinja2 import Environment, PackageLoader, select_autoescape
from markupsafe import Markup, escape

from fedreg_digest.pipeline import Digest

DIFF_LINES = 20

LEVEL_STYLE = {
    "HIGH": ("High", "#b42318", "#fef3f2"),
    "MEDIUM": ("Medium", "#b54708", "#fffaeb"),
    "LOW": ("Low", "#175cd3", "#eff8ff"),
    "MINIMAL": ("Minimal", "#475467", "#f2f4f7"),
}
CHANGE_STYLE = {
    "ADDED": ("Added", "#067647", "#ecfdf3"),
    "MODIFIED": ("Amended", "#b54708", "#fffaeb"),
    "REMOVED": ("Removed", "#b42318", "#fef3f2"),
}


def _paragraphs(text: str | None) -> Markup:
    if not text:
        return Markup("")
    parts = [p.strip() for p in text.split("\n\n") if p.strip()]
    return Markup("".join(f"<p>{escape(p)}</p>" for p in parts))


def _human_date(value: date | datetime | None) -> str:
    if not value:
        return ""
    return f"{value.day} {value:%b %Y}"


def environment() -> Environment:
    env = Environment(
        loader=PackageLoader("fedreg_digest", "templates"),
        autoescape=select_autoescape(
            enabled_extensions=("html", "html.j2"), default_for_string=False
        ),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["paragraphs"] = _paragraphs
    env.filters["human_date"] = _human_date
    env.globals.update(LEVEL_STYLE=LEVEL_STYLE, CHANGE_STYLE=CHANGE_STYLE, DIFF_LINES=DIFF_LINES)
    return env


def subject(digest: Digest) -> str:
    n = digest.count
    template = digest.profile.email.subject
    return template.format(name=digest.profile.name, count=n, plural="" if n == 1 else "s")


def render(digest: Digest) -> dict[str, str]:
    env = environment()
    context = {"d": digest, "subject": subject(digest)}
    return {
        "html": env.get_template("digest.html.j2").render(**context),
        "md": env.get_template("digest.md.j2").render(**context),
        "txt": env.get_template("digest.txt.j2").render(**context),
        "json": to_json(digest),
    }


def to_json(digest: Digest) -> str:
    def default(o):
        if isinstance(o, (date, datetime)):
            return o.isoformat()
        if isinstance(o, Path):
            return str(o)
        if is_dataclass(o):
            return asdict(o)
        return str(o)

    data = asdict(digest)
    data["profile"] = {"name": digest.profile.name, "source": str(digest.profile.source or "")}
    return json.dumps(data, default=default, indent=2)


def write(digest: Digest, output_dir: str | Path) -> dict[str, Path]:
    """Write every format to ``output_dir/<end-date>.<ext>`` plus a ``latest`` copy."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = {}
    for ext, content in render(digest).items():
        dated = out / f"{digest.end.isoformat()}.{ext}"
        dated.write_text(content, encoding="utf-8")
        (out / f"latest.{ext}").write_text(content, encoding="utf-8")
        paths[ext] = dated
    return paths
