"""Command line: ``fedreg-digest init``, ``doctor``, ``run``, ``agencies``."""

from __future__ import annotations

import os
import sys
import webbrowser
from datetime import date, timedelta
from pathlib import Path

import click
import yaml
from dotenv import load_dotenv
from rich.console import Console
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.table import Table

from fedreg_digest import __version__
from fedreg_digest.cache import Cache
from fedreg_digest.config import DEFAULT_MODEL, ConfigError, Profile, Secrets, load_profile
from fedreg_digest.ecfr import ECFRClient
from fedreg_digest.federal_register import FederalRegisterClient
from fedreg_digest.http import APIError, make_session

console = Console(highlight=False)
err = Console(stderr=True, highlight=False)

DEFAULT_PROFILE = "fedreg.yaml"
PROFILE_HELP = "Profile YAML. Defaults to $FEDREG_PROFILE, then ./fedreg.yaml."

EXIT_OK, EXIT_INCOMPLETE, EXIT_CONFIG = 0, 1, 2


def _profile_path(value: str | None) -> Path:
    return Path(value or os.environ.get("FEDREG_PROFILE") or DEFAULT_PROFILE)


def _load(path: Path) -> Profile:
    # A .env next to the profile also counts, so a cron job started from $HOME
    # still finds its keys. One in the working directory takes precedence.
    load_dotenv(path.resolve().parent / ".env")
    try:
        return load_profile(path)
    except ConfigError as exc:
        err.print(f"[red]Profile problem:[/red] {exc}")
        sys.exit(EXIT_CONFIG)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="fedreg-digest")
def main() -> None:
    """Watch the Federal Register and the eCFR for the rules you care about.

    \b
    Start here:
      fedreg-digest init      create a profile interactively
      fedreg-digest doctor    check that everything is connected
      fedreg-digest run       build this week's digest
    """
    load_dotenv(Path.cwd() / ".env")


# --------------------------------------------------------------------------- run


@main.command()
@click.option("-p", "--profile", "profile_path", help=PROFILE_HELP)
@click.option("--days", type=int, help="Look back this many days (default: digest.lookback_days).")
@click.option("--since", type=click.DateTime(["%Y-%m-%d"]), help="Start date, YYYY-MM-DD.")
@click.option(
    "--until", type=click.DateTime(["%Y-%m-%d"]), help="End date, YYYY-MM-DD (default today)."
)
@click.option("--no-ai", is_flag=True, help="Skip Claude: keyword scoring only, no summaries.")
@click.option("--email/--no-email", "send_email", default=None, help="Override email.enabled.")
@click.option("--out", "output_dir", help="Output directory (default: digest.output_dir).")
@click.option("--open", "open_browser", is_flag=True, help="Open the HTML digest when done.")
@click.option(
    "--cache",
    "cache_path",
    default=".fedreg-digest/cache.sqlite",
    show_default=True,
    help="AI result cache. Pass '' to disable.",
)
def run(profile_path, days, since, until, no_ai, send_email, output_dir, open_browser, cache_path):
    """Build a digest for the last N days and write it to disk (and email it if enabled)."""
    from fedreg_digest import mailer
    from fedreg_digest.pipeline import Pipeline, window
    from fedreg_digest.render import render, subject, write

    profile = _load(_profile_path(profile_path))
    secrets = Secrets.from_env()

    end = until.date() if until else date.today()
    if since:
        start = since.date()
    else:
        start, end = window(days or profile.digest.lookback_days, end)
    if start > end:
        err.print("[red]--since is after --until[/red]")
        sys.exit(EXIT_CONFIG)

    use_ai = profile.ai.enabled and not no_ai
    missing_key = use_ai and not secrets.anthropic_api_key
    if missing_key:
        err.print(
            "[yellow]ANTHROPIC_API_KEY is not set, so this run uses keyword scoring only.[/yellow] "
            "Add it to .env, or pass --no-ai (or set ai.enabled: false) if that is intended."
        )
        use_ai = False

    console.print(f"[bold]{profile.name}[/bold]  {start} → {end}")
    cache = Cache(cache_path or None)
    pipeline = Pipeline(
        profile,
        api_key=secrets.anthropic_api_key,
        use_ai=use_ai,
        cache=cache,
        log=lambda msg: console.print(f"[dim]·[/dim] {msg}"),
    )
    with console.status("Working..."):
        digest = pipeline.run(start, end)
    if missing_key:
        # The profile asked for AI and did not get it: the digest is incomplete.
        digest.problems.append(
            "ANTHROPIC_API_KEY is not set, so there are no AI reviews or summaries in this digest"
        )
    cache.close()

    paths = write(digest, output_dir or profile.digest.output_dir)
    _print_summary(digest)
    console.print(f"\nDigest written to [bold]{paths['html']}[/bold] (also .md, .txt, .json)")

    failed = bool(digest.problems)
    should_email = profile.email.enabled if send_email is None else send_email
    if should_email:
        to = mailer.recipients(profile.email.to, secrets)
        if digest.count == 0 and not profile.email.send_when_empty and not digest.problems:
            console.print("Nothing to report and email.send_when_empty is false; no email sent.")
        else:
            rendered = render(digest)
            try:
                mailer.send(secrets, to, subject(digest), rendered["html"], rendered["txt"])
                console.print(f"[green]Emailed[/green] {', '.join(to)}")
            except mailer.EmailError as exc:
                err.print(f"[red]Email failed:[/red] {exc}")
                failed = True

    if open_browser:
        webbrowser.open(paths["html"].resolve().as_uri())

    if digest.problems:
        err.print(
            f"\n[red]{len(digest.problems)} problem(s) - the digest says so at the top:[/red]"
        )
        for problem in digest.problems:
            err.print(f"  • {problem}")
    # Non-zero whenever the digest is incomplete or undelivered, so a scheduler
    # shows a failure instead of a green run that quietly did less.
    sys.exit(EXIT_INCOMPLETE if failed else EXIT_OK)


def _print_summary(digest) -> None:
    table = Table(show_header=False, box=None, padding=(0, 2))
    for level in ("HIGH", "MEDIUM", "LOW", "MINIMAL"):
        items = digest.by_level(level)
        if items:
            table.add_row(
                f"[bold]{level.title()}[/bold]", str(len(items)), items[0].document.title[:80]
            )
    if digest.profile.watches_cfr:
        first = digest.changes[0].change.citation if digest.changes else ""
        table.add_row("[bold]CFR changes[/bold]", str(len(digest.changes)), first)
    table.add_row("[dim]Not relevant[/dim]", str(digest.rejected), f"of {digest.fetched} fetched")
    console.print(table)
    if digest.ai_used:
        console.print(f"[dim]{digest.ai_calls} Claude call(s) this run[/dim]")


# ------------------------------------------------------------------------ doctor


@main.command()
@click.option("-p", "--profile", "profile_path", help=PROFILE_HELP)
@click.option("--skip-ai-call", is_flag=True, help="Check the key exists without calling the API.")
def doctor(profile_path, skip_ai_call):
    """Check the profile, both government APIs, the Claude key and SMTP."""
    ok = True

    def report(passed: bool, label: str, detail: str = "", warn: bool = False) -> None:
        nonlocal ok
        mark = "[green]✓[/green]" if passed else ("[yellow]![/yellow]" if warn else "[red]✗[/red]")
        if not passed and not warn:
            ok = False
        console.print(f" {mark} {label}" + (f"  [dim]{detail}[/dim]" if detail else ""))

    path = _profile_path(profile_path)
    try:
        load_dotenv(path.resolve().parent / ".env")
        profile = load_profile(path)
        report(True, f"Profile {path}", profile.name)
    except ConfigError as exc:
        report(False, f"Profile {path}", str(exc))
        sys.exit(EXIT_CONFIG)

    secrets = Secrets.from_env()
    session = make_session()

    fr = FederalRegisterClient(session)
    try:
        known = {a["slug"] for a in fr.agencies()}
        bad = [a for a in profile.federal_register.agencies if a not in known]
        report(
            not bad,
            "Agency slugs",
            "unknown: " + ", ".join(bad)
            if bad
            else ", ".join(profile.federal_register.agencies) or "(search only)",
        )
        end = date.today()
        docs = fr.search(
            end - timedelta(days=29),
            end,
            agencies=profile.federal_register.agencies,
            document_types=profile.federal_register.document_types,
            term=profile.federal_register.search,
        )
        report(True, "Federal Register API", f"{len(docs)} matching documents in the last 30 days")
    except APIError as exc:
        report(False, "Federal Register API", str(exc))

    if profile.watches_cfr:
        ecfr = ECFRClient(session)
        try:
            current = ecfr.current_date(profile.ecfr.title)
            report(True, "eCFR API", f"Title {profile.ecfr.title} current as of {current}")
            for part in profile.ecfr.parts:
                versions = ecfr.versions(profile.ecfr.title, part, date(1990, 1, 1), current)
                report(
                    bool(versions),
                    f"{profile.ecfr.title} CFR {part}",
                    f"{len(versions)} section versions on record"
                    if versions
                    else "no sections found - check the part number",
                )
        except APIError as exc:
            report(False, "eCFR API", str(exc))
    else:
        report(True, "eCFR", "not watching any CFR parts (optional)", warn=True)

    if profile.ai.enabled:
        if not secrets.anthropic_api_key:
            report(
                False, "ANTHROPIC_API_KEY", "not set; runs fall back to keyword scoring", warn=True
            )
        elif skip_ai_call:
            report(True, "ANTHROPIC_API_KEY", "set (not tested)")
        else:
            from fedreg_digest.ai import AIError, Analyst
            from fedreg_digest.federal_register import Document

            probe = Document(
                "0000-00000",
                "Test document for fedreg-digest doctor",
                "NOTICE",
                date.today(),
                abstract="This is a connectivity test.",
            )
            try:
                Analyst(profile, api_key=secrets.anthropic_api_key).review(probe, ["test"])
                report(True, "Claude API", profile.ai.model)
            except AIError as exc:
                report(False, "Claude API", str(exc))
    else:
        report(True, "Claude", "disabled in profile", warn=True)

    if profile.email.enabled:
        from fedreg_digest import mailer

        to = mailer.recipients(profile.email.to, secrets)
        missing = mailer.check_settings(secrets, to)
        if missing:
            report(False, "Email settings", "missing " + ", ".join(missing))
        else:
            try:
                mailer.test_login(secrets)
                report(True, "SMTP login", f"{secrets.smtp_host}, sending to {', '.join(to)}")
            except mailer.EmailError as exc:
                report(False, "SMTP login", str(exc))
    else:
        report(True, "Email", "off; digests are written to disk", warn=True)

    console.print(
        "\n[green]Ready.[/green] Try `fedreg-digest run --open`."
        if ok
        else "\n[red]Fix the items marked ✗ and run doctor again.[/red]"
    )
    sys.exit(EXIT_OK if ok else EXIT_INCOMPLETE)


# ---------------------------------------------------------------------- agencies


@main.command()
@click.argument("search", required=False)
def agencies(search):
    """List Federal Register agency slugs, optionally filtered by SEARCH."""
    results = _find_agencies(search or "")
    table = Table("slug", "name")
    for agency in results:
        table.add_row(agency["slug"], agency["name"])
    console.print(table)
    console.print(f"[dim]{len(results)} agencies[/dim]")


def _find_agencies(search: str) -> list[dict]:
    try:
        everything = FederalRegisterClient(make_session()).agencies()
    except APIError as exc:
        err.print(f"[red]{exc}[/red]")
        sys.exit(EXIT_INCOMPLETE)
    needle = search.lower()
    return sorted(
        (
            a
            for a in everything
            if needle in a["name"].lower()
            or needle in a["slug"]
            or needle == (a.get("short_name") or "").lower()
        ),
        key=lambda a: a["name"],
    )


# -------------------------------------------------------------------------- init


def _ask_nonempty(label: str) -> str:
    while True:
        answer = Prompt.ask(label).strip()
        if answer:
            return answer
        console.print("[yellow]This one is required.[/yellow]")


@main.command()
@click.argument("path", default=DEFAULT_PROFILE, required=False)
@click.option("--force", is_flag=True, help="Overwrite an existing profile.")
def init(path, force):
    """Create a watch profile by answering a few questions."""
    target = Path(path)
    if target.exists() and not force:
        err.print(f"{target} already exists. Pass --force to overwrite, or choose another path.")
        sys.exit(EXIT_CONFIG)

    console.print("[bold]New fedreg-digest profile[/bold]  (Enter accepts the default)\n")
    name = Prompt.ask("Name for this watch", default="My regulatory watch")
    console.print(
        "\nDescribe what you care about in a sentence or two. Claude reads this to judge "
        "relevance and to write the summaries, so be concrete about who you are and what matters."
    )
    topic = _ask_nonempty("Topic")

    console.print(
        "\n[bold]Which agencies?[/bold] Search by name (e.g. 'food and drug', 'aviation')."
    )
    chosen: list[str] = []
    while True:
        query = Prompt.ask("Search agencies (blank to finish)", default="")
        if not query:
            if chosen:
                break
            console.print("[yellow]Pick at least one agency.[/yellow]")
            continue
        matches = _find_agencies(query)[:15]
        if not matches:
            console.print("No match.")
            continue
        for i, agency in enumerate(matches, 1):
            console.print(f"  {i:>2}. {agency['name']} [dim]({agency['slug']})[/dim]")
        pick = Prompt.ask("Number(s), comma separated", default="1")
        for token in pick.split(","):
            if token.strip().isdigit() and 1 <= int(token) <= len(matches):
                slug = matches[int(token) - 1]["slug"]
                if slug not in chosen:
                    chosen.append(slug)
        console.print(f"Selected: {', '.join(chosen)}")

    console.print(
        "\n[bold]Keywords[/bold] that signal relevance, comma separated. Simple plurals are "
        "matched automatically."
    )
    keywords = [k.strip() for k in _ask_nonempty("Keywords").split(",") if k.strip()]

    ecfr: dict = {}
    if Confirm.ask("\nAlso watch specific CFR parts for amendments?", default=True):
        title = IntPrompt.ask("CFR title number (e.g. 21 = Food and Drugs, 14 = Aeronautics)")
        parts = [
            p.strip()
            for p in _ask_nonempty("Part numbers, comma separated").split(",")
            if p.strip()
        ]
        ecfr = {"title": title, "parts": parts}

    use_ai = Confirm.ask(
        "\nUse Claude for reviews and summaries? (needs ANTHROPIC_API_KEY)", default=True
    )

    profile: dict = {
        "name": name,
        "topic": topic,
        "keywords": keywords,
        "federal_register": {"agencies": chosen, "document_types": ["RULE", "PRORULE", "NOTICE"]},
    }
    if ecfr:
        profile["ecfr"] = ecfr
    profile["ai"] = {"enabled": use_ai, "model": DEFAULT_MODEL}
    profile["digest"] = {"lookback_days": 7, "output_dir": "digests"}
    profile["email"] = {"enabled": False}

    header = (
        "# fedreg-digest profile. Every option is documented in docs/configuration.md.\n"
        "# Secrets (ANTHROPIC_API_KEY, SMTP_*) belong in .env, never in this file.\n\n"
    )
    target.write_text(
        header + yaml.safe_dump(profile, sort_keys=False, allow_unicode=True, width=88)
    )
    console.print(f"\n[green]Wrote {target}.[/green]")
    if not Path(".env").exists() and use_ai:
        console.print("Next: copy .env.example to .env and add your ANTHROPIC_API_KEY.")
    console.print(
        f"Then: [bold]fedreg-digest doctor -p {target}[/bold] and "
        f"[bold]fedreg-digest run -p {target} --open[/bold]"
    )


if __name__ == "__main__":
    main()
