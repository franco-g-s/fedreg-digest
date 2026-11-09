from click.testing import CliRunner
from conftest import FakeResponse, FakeSession

from fedreg_digest import pipeline
from fedreg_digest.cli import main

PROFILE = """
name: CLI test
topic: Testing
keywords: [drone]
federal_register:
  agencies: [federal-aviation-administration]
"""


def invoke(tmp_path, monkeypatch, session, *args):
    (tmp_path / "p.yaml").write_text(PROFILE)
    monkeypatch.setattr(pipeline, "make_session", lambda: session)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    ai = [] if "--with-ai" in args else ["--no-ai"]
    args = [a for a in args if a != "--with-ai"]
    return CliRunner().invoke(main, ["run", "-p", "p.yaml", *ai, "--cache", "", *args])


def test_run_writes_all_formats_and_exits_zero(tmp_path, monkeypatch):
    session = FakeSession()
    session.add(lambda u, p: True, FakeResponse(json_data={"count": 0, "results": []}))
    result = invoke(tmp_path, monkeypatch, session, "--out", "out")
    assert result.exit_code == 0, result.output
    assert {p.suffix for p in (tmp_path / "out").iterdir()} == {".html", ".md", ".txt", ".json"}


def test_incomplete_digest_exits_one(tmp_path, monkeypatch):
    session = FakeSession()
    session.add(lambda u, p: True, FakeResponse(503, "unavailable"))
    result = invoke(tmp_path, monkeypatch, session, "--out", "out")
    assert result.exit_code == 1
    assert "could not be searched" in result.output


def test_invalid_profile_exits_two(tmp_path, monkeypatch):
    (tmp_path / "bad.yaml").write_text("name: x\n")
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["run", "-p", "bad.yaml"])
    assert result.exit_code == 2
    assert "required" in result.output


def test_ai_enabled_without_a_key_is_incomplete(tmp_path, monkeypatch):
    session = FakeSession()
    session.add(lambda u, p: True, FakeResponse(json_data={"count": 0, "results": []}))
    result = invoke(tmp_path, monkeypatch, session, "--with-ai", "--out", "out")
    assert result.exit_code == 1
    assert "ANTHROPIC_API_KEY is not set" in (tmp_path / "out" / "latest.html").read_text()
