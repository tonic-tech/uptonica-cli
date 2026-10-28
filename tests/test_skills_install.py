"""`uptonica skills install`: the caller's own link, handed to Claude Code."""

from __future__ import annotations

import argparse

import pytest

from uptonica import cli

LINK = {
    "marketplace_url": "https://app.uptonica.com/claude/skills/7/marketplace.json?rev=1&signature=abc",
    "marketplace": "uptonica-skills",
    "plugin": "uptonica-content",
    "version": "0.3.0",
    "install": "uptonica-content@uptonica-skills",
}


def _args(**overrides) -> argparse.Namespace:
    base = {"new": False, "print_link": False}
    base.update(overrides)
    return argparse.Namespace(**base)


@pytest.fixture(autouse=True)
def _api(monkeypatch):
    monkeypatch.setattr(cli, "API", "https://app.uptonica.com/api/v1/operator")


@pytest.fixture
def server(monkeypatch):
    calls = []

    def fake_request(method, path, **kwargs):
        calls.append((method, path, kwargs.get("json_body")))
        return LINK

    monkeypatch.setattr(cli, "_request", fake_request)
    return calls


def test_it_replaces_the_marketplace_then_installs(server, monkeypatch):
    ran = []
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/local/bin/claude")
    monkeypatch.setattr(cli.subprocess, "run",
                        lambda argv, **kw: (ran.append(argv), type("R", (), {"returncode": 0})())[1])

    cli.cmd_skills_install(_args(), parser=None)

    assert server == [("POST", "/claude-skills/link", {"new": False})]
    assert ran == [
        ["claude", "plugin", "marketplace", "remove", "uptonica-skills"],
        ["claude", "plugin", "marketplace", "add", LINK["marketplace_url"]],
        ["claude", "plugin", "install", "uptonica-content@uptonica-skills"],
    ]


def test_new_asks_the_server_for_a_new_link(server, monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)

    cli.cmd_skills_install(_args(new=True), parser=None)

    assert server == [("POST", "/claude-skills/link", {"new": True})]


def test_without_claude_it_prints_the_commands(server, monkeypatch, capsys):
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)

    cli.cmd_skills_install(_args(), parser=None)

    out = capsys.readouterr()
    assert f"claude plugin marketplace add '{LINK['marketplace_url']}'" in out.out
    assert "claude plugin install uptonica-content@uptonica-skills" in out.out
    assert "not on PATH" in out.err


def test_a_failed_add_stops_before_installing(server, monkeypatch):
    ran = []
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/local/bin/claude")

    def run(argv, **kw):
        ran.append(argv)
        return type("R", (), {"returncode": 1 if argv[3:4] == ["add"] else 0})()

    monkeypatch.setattr(cli.subprocess, "run", run)

    with pytest.raises(SystemExit):
        cli.cmd_skills_install(_args(), parser=None)

    assert ["claude", "plugin", "install", "uptonica-content@uptonica-skills"] not in ran


@pytest.mark.parametrize("bad", [
    {**LINK, "marketplace_url": "https://evil.example/m.json"},
    {**LINK, "marketplace_url": "http://app.uptonica.com/m.json"},
    {**LINK, "install": "--malicious"},
    "not json",
])
def test_an_unusable_answer_stops_with_a_clean_error(monkeypatch, bad):
    monkeypatch.setattr(cli, "_request", lambda *a, **k: bad)
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)

    with pytest.raises(SystemExit):
        cli.cmd_skills_install(_args(), parser=None)
