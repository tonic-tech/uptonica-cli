"""Tests for the 2026-09-10 polish pass: `uptonica logout`, the
`create-image`/`create-video`/`ads-report` shortcuts over `ask`, and the
CLI-native hints appended to a handful of well-known server `error` codes.
"""

from __future__ import annotations

import argparse
import io
import json
import urllib.error

import pytest

from uptonica import cli


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("UPTONICA_TOKEN", raising=False)
    monkeypatch.delenv("UPTONICA_DEBUG", raising=False)


def _args(**overrides) -> argparse.Namespace:
    base = {"message": None, "tenant": None, "conversation": None, "new": False, "output": None}
    base.update(overrides)
    return argparse.Namespace(**base)


# --- logout -------------------------------------------------------------

def test_logout_is_the_same_removal_as_config_clear_token(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(cli, "clear_token", lambda: (calls.append(1), ["macOS Keychain"])[1])

    cli.cmd_logout(_args(), parser=None)

    assert calls == [1]
    assert "Removed from: macOS Keychain" in capsys.readouterr().err


def test_logout_with_nothing_stored_says_so(monkeypatch, capsys):
    monkeypatch.setattr(cli, "clear_token", lambda: [])

    cli.cmd_logout(_args(), parser=None)

    assert "Nothing stored" in capsys.readouterr().err


# --- shortcuts over ask ---------------------------------------------------

def test_shortcut_prepends_the_base_prompt_when_a_message_is_given(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "cmd_ask", lambda args, parser: seen.setdefault("message", args.message))

    cmd = cli._make_shortcut_cmd("Create an image")
    cmd(_args(message="a black cat wearing sunglasses"), parser=None)

    assert seen["message"] == "Create an image: a black cat wearing sunglasses"


def test_shortcut_uses_the_bare_prompt_when_no_message_is_given(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "cmd_ask", lambda args, parser: seen.setdefault("message", args.message))

    cmd = cli._make_shortcut_cmd("Give me an ads performance report")
    cmd(_args(message=None), parser=None)

    assert seen["message"] == "Give me an ads performance report"
    assert not seen["message"].endswith(":")


def test_shortcut_preserves_the_other_ask_arguments(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "cmd_ask", lambda args, parser: seen.setdefault("args", args))

    cmd = cli._make_shortcut_cmd("Create a video")
    args = _args(message="a dog skateboarding", tenant="my-store", new=True)
    cmd(args, parser=None)

    assert seen["args"].tenant == "my-store"
    assert seen["args"].new is True


def test_registered_shortcuts_match_the_ones_the_repl_advertises():
    """`repl.py`'s `SLASH_COMMANDS` builds its shortcut list FROM this dict —
    this just pins the three names/prompts actually asked for, so a typo
    in one place shows up as a failing assertion here instead of only as a
    mismatched help text somebody notices by eye."""
    assert cli.SHORTCUT_PROMPTS == {
        "create-image": "Create an image",
        "create-video": "Create a video",
        "ads-report": "Give me an ads performance report",
    }


# --- error hints -----------------------------------------------------------

def test_hint_for_known_error_codes():
    assert "uptonica tenant use" in cli._hint_for_error("tenant_required")
    assert "uptonica login" in cli._hint_for_error("insufficient_scope")
    assert cli._hint_for_error("budget_exhausted")
    assert cli._hint_for_error("rate_limited")


def test_hint_for_unknown_or_missing_error_code_is_none():
    assert cli._hint_for_error("some_future_code_this_cli_has_never_heard_of") is None
    assert cli._hint_for_error(None) is None
    assert cli._hint_for_error(404) is None  # a code that isn't even a string


def _http_error(status: int, body: dict) -> urllib.error.HTTPError:
    payload = json.dumps(body).encode("utf-8")
    return urllib.error.HTTPError(
        url="https://app.uptonica.com/api/v1/operator/turns",
        code=status,
        msg="Bad Request",
        hdrs={},
        fp=io.BytesIO(payload),
    )


def test_request_appends_the_hint_for_a_tenant_required_400(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_token", lambda: "1|" + "a" * 24)

    def fake_open(req, timeout=60):
        raise _http_error(400, {"error": "tenant_required", "error_description": "Select a workspace via the X-Uptonica-Tenant header or ?tenant= (slug or id). Call /api/v1/operator/whoami for the list."})

    monkeypatch.setattr(cli._OPENER, "open", fake_open)

    with pytest.raises(SystemExit) as exc:
        cli._request("POST", "/tools/catalog.stats.summary")

    assert exc.value.code == 2  # _request's own 400/404 -> 2 bucket
    err = capsys.readouterr().err
    assert "Select a workspace via" in err  # the server's own text, unchanged
    assert "uptonica tenant use" in err  # the CLI-native hint, appended


def test_request_has_no_hint_line_for_an_error_code_with_none_registered(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_token", lambda: "1|" + "a" * 24)

    def fake_open(req, timeout=60):
        raise _http_error(404, {"error": "tool_not_found", "error_description": "Unknown tool 'x'."})

    monkeypatch.setattr(cli._OPENER, "open", fake_open)

    with pytest.raises(SystemExit):
        cli._request("POST", "/tools/x")

    err = capsys.readouterr().err
    assert "Unknown tool" in err
    assert "uptonica tenant use" not in err
    assert "uptonica login" not in err


def test_open_ask_stream_appends_the_hint_for_tenant_required(monkeypatch):
    monkeypatch.setattr(cli, "_token", lambda: "1|" + "a" * 24)

    def fake_open(req, timeout=300):
        raise _http_error(400, {"error": "tenant_required", "error_description": "Select a workspace via the X-Uptonica-Tenant header or ?tenant= (slug or id). Call /api/v1/operator/whoami for the list."})

    monkeypatch.setattr(cli._OPENER, "open", fake_open)

    with pytest.raises(cli.AskError) as exc:
        cli._open_ask_stream(None, "quanto ho venduto?", None)

    assert "Select a workspace via" in str(exc.value)
    assert "uptonica tenant use" in str(exc.value)
    assert exc.value.exit_code == 2
