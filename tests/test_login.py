"""Tests for `uptonica login` (RFC 8628 device authorization).

`_device_auth_request` is the only network boundary here — it's stubbed
directly, the same pattern `test_repl.py` uses for `_open_ask_stream`/`_iter_sse`.
`time.sleep` is stubbed too so the polling loop doesn't actually wait.
"""

from __future__ import annotations

import argparse

import pytest

from uptonica import cli


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    """Own config dir per test — `store_token`'s file-store fallback and
    `_token()`'s resolve() must never touch the real user's Keychain/file."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("UPTONICA_TOKEN", raising=False)
    monkeypatch.delenv("UPTONICA_DEBUG", raising=False)


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    monkeypatch.setattr(cli.time, "sleep", lambda _seconds: None)


def _args(no_browser: bool = True) -> argparse.Namespace:
    return argparse.Namespace(no_browser=no_browser)


# Shape-valid per `SANCTUM_TOKEN_RE` (`<digits>|<20+ alnum>`) — `cmd_login`
# now rejects anything that doesn't look like a real Sanctum token before
# storing it, so a placeholder like the old "1|t" would fail every "happy
# path" test at the validation step meant for the *hostile*-token test.
VALID_TOKEN = "1|" + "a" * 24


def _start_response(**overrides) -> dict:
    base = {
        "device_code": "dc-123",
        "user_code": "ABCD-EFGH",
        "verification_uri": "https://app.uptonica.com/device",
        "verification_uri_complete": "https://app.uptonica.com/device?user_code=ABCD-EFGH",
        "interval": 5,
        "expires_in": 600,
    }
    base.update(overrides)
    return base


def test_login_success_stores_token_and_prints_workspace_count(monkeypatch, capsys):
    calls = []

    def fake_request(path, json_body=None):
        calls.append((path, json_body))
        if path == "/oauth/device/code":
            return _start_response()
        return {"access_token": VALID_TOKEN}

    monkeypatch.setattr(cli, "_device_auth_request", fake_request)
    monkeypatch.setattr(cli, "_request", lambda method, path: {"tenant_count": 3})

    stored = {}

    def fake_store_token(value):
        stored["value"] = value
        return "macOS Keychain"

    monkeypatch.setattr(cli, "store_token", fake_store_token)

    cli.cmd_login(_args(no_browser=True), parser=None)

    assert stored["value"] == VALID_TOKEN
    err = capsys.readouterr().err
    assert "ABCD-EFGH" in err
    assert "Saved to macOS Keychain" in err
    assert "reaches 3 workspaces" in err
    assert calls[0][0] == "/oauth/device/code"
    assert calls[1] == ("/oauth/device/token", {"device_code": "dc-123"})


def test_login_no_browser_never_opens_one(monkeypatch, capsys):
    opened = []
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: opened.append(url))
    monkeypatch.setattr(cli, "_device_auth_request", lambda path, json_body=None: (
        _start_response() if path == "/oauth/device/code" else {"access_token": VALID_TOKEN}
    ))
    monkeypatch.setattr(cli, "_request", lambda method, path: {"tenant_count": 1})
    monkeypatch.setattr(cli, "store_token", lambda value: "macOS Keychain")

    cli.cmd_login(_args(no_browser=True), parser=None)

    assert opened == []
    assert "Visit https://app.uptonica.com/device" in capsys.readouterr().err


def test_login_opens_browser_by_default(monkeypatch):
    opened = []
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: opened.append(url))
    monkeypatch.setattr(cli, "_device_auth_request", lambda path, json_body=None: (
        _start_response() if path == "/oauth/device/code" else {"access_token": VALID_TOKEN}
    ))
    monkeypatch.setattr(cli, "_request", lambda method, path: {"tenant_count": 1})
    monkeypatch.setattr(cli, "store_token", lambda value: "macOS Keychain")

    cli.cmd_login(_args(no_browser=False), parser=None)

    assert opened == ["https://app.uptonica.com/device?user_code=ABCD-EFGH"]


def test_login_polls_through_authorization_pending(monkeypatch, capsys):
    responses = iter([
        {"error": "authorization_pending"},
        {"error": "authorization_pending"},
        {"access_token": VALID_TOKEN},
    ])

    def fake_request(path, json_body=None):
        if path == "/oauth/device/code":
            return _start_response()
        return next(responses)

    monkeypatch.setattr(cli, "_device_auth_request", fake_request)
    monkeypatch.setattr(cli, "_request", lambda method, path: {"tenant_count": 1})
    monkeypatch.setattr(cli, "store_token", lambda value: "macOS Keychain")

    cli.cmd_login(_args(), parser=None)  # no exception == it kept polling past both pendings

    assert "Logged in" in capsys.readouterr().err


def test_login_slow_down_widens_the_poll_interval(monkeypatch):
    sleeps = []
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: sleeps.append(seconds))
    responses = iter([
        {"error": "slow_down"},
        {"access_token": VALID_TOKEN},
    ])
    monkeypatch.setattr(cli, "_device_auth_request", lambda path, json_body=None: (
        _start_response(interval=5) if path == "/oauth/device/code" else next(responses)
    ))
    monkeypatch.setattr(cli, "_request", lambda method, path: {"tenant_count": 1})
    monkeypatch.setattr(cli, "store_token", lambda value: "macOS Keychain")

    cli.cmd_login(_args(), parser=None)

    # First sleep at the starting interval, second widened by +5 after slow_down.
    assert sleeps == [5, 10]


def test_login_access_denied_exits_3(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_device_auth_request", lambda path, json_body=None: (
        _start_response() if path == "/oauth/device/code" else {"error": "access_denied"}
    ))

    with pytest.raises(SystemExit) as exc:
        cli.cmd_login(_args(), parser=None)

    assert exc.value.code == 3
    assert "denied" in capsys.readouterr().err


def test_login_expired_token_exits_3(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_device_auth_request", lambda path, json_body=None: (
        _start_response() if path == "/oauth/device/code" else {"error": "expired_token"}
    ))

    with pytest.raises(SystemExit) as exc:
        cli.cmd_login(_args(), parser=None)

    assert exc.value.code == 3
    assert "expired" in capsys.readouterr().err


def test_login_unexpected_poll_error_exits_5(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_device_auth_request", lambda path, json_body=None: (
        _start_response() if path == "/oauth/device/code" else {"error": "server_error"}
    ))

    with pytest.raises(SystemExit) as exc:
        cli.cmd_login(_args(), parser=None)

    assert exc.value.code == 5
    assert "unexpected" in capsys.readouterr().err


def test_login_deadline_reached_without_a_result_exits_3(monkeypatch, capsys):
    # A fake clock: the first monotonic() read (computing the deadline) is 0,
    # every read after that (the `while` condition) is already past it — so
    # the `while` body never runs, and no poll ever happens. Using a real
    # short `expires_in` instead would race against how fast the mocked
    # (no-op) sleep lets the loop spin, which is not a deadline this test
    # controls.
    reads = iter([0.0, 1000.0])
    monkeypatch.setattr(cli.time, "monotonic", lambda: next(reads, 1000.0))
    monkeypatch.setattr(cli, "_device_auth_request", lambda path, json_body=None: (
        _start_response(interval=5, expires_in=1) if path == "/oauth/device/code"
        else pytest.fail("should never poll — the deadline is already in the past")
    ))

    with pytest.raises(SystemExit) as exc:
        cli.cmd_login(_args(), parser=None)

    assert exc.value.code == 3
    assert "expired" in capsys.readouterr().err


def test_login_malformed_start_response_exits_5(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_device_auth_request", lambda path, json_body=None: {"error": "boom"})

    with pytest.raises(SystemExit) as exc:
        cli.cmd_login(_args(), parser=None)

    assert exc.value.code == 5
    assert "unexpected response starting login" in capsys.readouterr().err


def test_login_redacts_device_code_from_a_hostile_interval_error(monkeypatch, capsys):
    """A security review caught this: `device_code` is the sole bearer
    credential for the unauthenticated device-token poll, and in the REPL
    this exact error text lands in the on-screen transcript, not just
    stderr. A malformed `interval`/`expires_in` alongside an otherwise-valid
    device_code must not be what puts it there."""
    monkeypatch.setattr(cli, "_device_auth_request", lambda path, json_body=None: (
        _start_response(device_code="dc-secret-987", interval="not a number")
    ))

    with pytest.raises(SystemExit) as exc:
        cli.cmd_login(_args(), parser=None)

    assert exc.value.code == 5
    err = capsys.readouterr().err
    assert "unexpected response starting login" in err
    assert "dc-secret-987" not in err
    assert "[redacted]" in err


def test_login_rejects_a_malformed_access_token_without_storing_it(monkeypatch, capsys):
    stored = []
    monkeypatch.setattr(cli, "store_token", lambda value: stored.append(value) or "macOS Keychain")
    monkeypatch.setattr(cli, "_device_auth_request", lambda path, json_body=None: (
        _start_response() if path == "/oauth/device/code" else {"access_token": "not-a-real-token"}
    ))

    with pytest.raises(SystemExit) as exc:
        cli.cmd_login(_args(), parser=None)

    assert exc.value.code == 5
    assert stored == []
    assert "doesn't look like an Uptonica token" in capsys.readouterr().err


def test_login_clamps_a_hostile_interval_instead_of_busy_looping(monkeypatch):
    sleeps = []
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr(cli, "_device_auth_request", lambda path, json_body=None: (
        _start_response(interval=0, expires_in=10 ** 9) if path == "/oauth/device/code" else {"access_token": VALID_TOKEN}
    ))
    monkeypatch.setattr(cli, "_request", lambda method, path: {"tenant_count": 1})
    monkeypatch.setattr(cli, "store_token", lambda value: "macOS Keychain")

    cli.cmd_login(_args(), parser=None)

    assert sleeps == [1]  # interval=0 clamped up to the 1s floor, not a sleepless spin


def test_login_rejects_a_non_numeric_interval(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_device_auth_request", lambda path, json_body=None: (
        _start_response(interval="a lot") if path == "/oauth/device/code"
        else pytest.fail("should never poll — the start response itself was malformed")
    ))

    with pytest.raises(SystemExit) as exc:
        cli.cmd_login(_args(), parser=None)

    assert exc.value.code == 5
    assert "unexpected response starting login" in capsys.readouterr().err


def test_login_warns_when_env_token_would_shadow_the_new_one(monkeypatch, capsys):
    monkeypatch.setenv("UPTONICA_TOKEN", "1|some-other-token")
    monkeypatch.setattr(cli, "_device_auth_request", lambda path, json_body=None: (
        _start_response() if path == "/oauth/device/code" else {"access_token": VALID_TOKEN}
    ))
    monkeypatch.setattr(cli, "_request", lambda method, path: {"tenant_count": 1})
    monkeypatch.setattr(cli, "store_token", lambda value: "macOS Keychain")

    cli.cmd_login(_args(), parser=None)

    assert "UPTONICA_TOKEN is set" in capsys.readouterr().err


def test_login_sanitizes_control_characters_from_server_strings(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_device_auth_request", lambda path, json_body=None: (
        _start_response(user_code="AB\x1b[2JCD") if path == "/oauth/device/code"
        else {"error": "authorization_pending"}
    ))

    # First monotonic() read sets the deadline, the second (the `while`
    # check) is already past it — the loop body, and its poll, never runs.
    # Only the banner printed before the loop matters for this test.
    reads = iter([0.0])
    monkeypatch.setattr(cli.time, "monotonic", lambda: next(reads, 1000.0))

    with pytest.raises(SystemExit):
        cli.cmd_login(_args(), parser=None)

    err_out = capsys.readouterr().err
    # `_safe()` strips the raw ESC/control byte, which is what defangs the
    # escape sequence — it does not also delete the printable characters
    # that followed it ("[2J" stays, just inert without the ESC in front).
    assert "\x1b" not in err_out
    assert "AB" in err_out and "CD" in err_out


# --- _prompt_default_workspace: avoid logging in with no workspace picked ---
#
# Prompted by Tony hitting exactly this live: `uptonica login` succeeded,
# then the very first `ask` 400'd with `tenant_required` because nothing had
# ever set a default. These run `cli._prompt_default_workspace` directly —
# unit-level, not through the whole `cmd_login` flow — since that's the only
# new logic here; `cmd_login` itself already calls it (see the success-path
# tests above still passing unchanged with an empty `tenants` list).


def _tenant(slug: str, name: str = "", id_: int = 1) -> dict:
    return {"slug": slug, "name": name, "id": id_}


def test_single_reachable_workspace_is_set_automatically(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_read_default_tenant", lambda: None)
    written = {}
    monkeypatch.setattr(cli, "_write_default_tenant", lambda slug: written.setdefault("slug", slug))

    cli._prompt_default_workspace([_tenant("acme", "Acme")])

    assert written["slug"] == "acme"
    assert "Default workspace set to acme" in capsys.readouterr().err


def test_existing_valid_default_is_left_alone_silently(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_read_default_tenant", lambda: "acme")
    monkeypatch.setattr(cli, "_write_default_tenant", lambda slug: pytest.fail("should not write — the current default is still reachable"))

    cli._prompt_default_workspace([_tenant("acme"), _tenant("beta")])

    assert capsys.readouterr().err == ""


def test_stale_default_no_longer_reachable_is_treated_as_unset(monkeypatch, capsys):
    """The workspace the stored default points at is gone (scope changed,
    access revoked) — silently keeping it would be actively wrong, not
    merely unhelpful, so this re-runs the same picker as a fresh login."""
    monkeypatch.setattr(cli, "_read_default_tenant", lambda: "old-gone-tenant")
    written = {}
    monkeypatch.setattr(cli, "_write_default_tenant", lambda slug: written.setdefault("slug", slug))

    cli._prompt_default_workspace([_tenant("acme")])

    assert written["slug"] == "acme"


def test_multiple_workspaces_interactive_pick_sets_the_chosen_one(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_read_default_tenant", lambda: None)
    written = {}
    monkeypatch.setattr(cli, "_write_default_tenant", lambda slug: written.setdefault("slug", slug))
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "beta")

    cli._prompt_default_workspace([_tenant("acme"), _tenant("beta", "Beta Co")])

    assert written["slug"] == "beta"
    assert "Default workspace set to beta" in capsys.readouterr().err


def test_multiple_workspaces_interactive_skip_on_empty_input(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_read_default_tenant", lambda: None)
    monkeypatch.setattr(cli, "_write_default_tenant", lambda slug: pytest.fail("should not write on a skipped prompt"))
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "")

    cli._prompt_default_workspace([_tenant("acme"), _tenant("beta")])

    assert "Skipped" in capsys.readouterr().err


def test_multiple_workspaces_bad_slug_typed_does_not_set_anything(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_read_default_tenant", lambda: None)
    monkeypatch.setattr(cli, "_write_default_tenant", lambda slug: pytest.fail("should not write — the typed slug isn't in the list"))
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "not-a-real-slug")

    cli._prompt_default_workspace([_tenant("acme"), _tenant("beta")])

    assert "isn't one of the workspaces" in capsys.readouterr().err


def test_multiple_workspaces_non_interactive_just_prints_the_hint(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_read_default_tenant", lambda: None)
    monkeypatch.setattr(cli, "_write_default_tenant", lambda slug: pytest.fail("should not write — nothing to prompt into"))
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)

    cli._prompt_default_workspace([_tenant("acme"), _tenant("beta")])

    err_out = capsys.readouterr().err
    assert "acme" in err_out and "beta" in err_out
    assert "uptonica tenant use" in err_out


def test_multiple_workspaces_skips_the_prompt_when_stdout_is_redirected(monkeypatch, capsys):
    """A security review caught this: gating on `sys.stdin.isatty()` alone
    misses `uptonica login > log.txt` run from a real terminal — stdin IS a
    tty, but the prompt would be written into the redirected file, invisible
    to the person watching the terminal. Reads as a hang, not a login
    that's actually waiting on input."""
    monkeypatch.setattr(cli, "_read_default_tenant", lambda: None)
    monkeypatch.setattr(cli, "_write_default_tenant", lambda slug: pytest.fail("should not write — nothing to prompt into"))
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: False)
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("must not prompt with stdout redirected"))

    cli._prompt_default_workspace([_tenant("acme"), _tenant("beta")])

    assert "uptonica tenant use" in capsys.readouterr().err


def test_name_with_embedded_newline_does_not_add_extra_list_rows(monkeypatch, capsys):
    """A hostile/malformed `name` must not be able to print extra,
    unindented rows in a list a person is about to pick a slug from by
    eye — `_safe_line()` folds it to one line instead."""
    monkeypatch.setattr(cli, "_read_default_tenant", lambda: None)
    monkeypatch.setattr(cli, "_write_default_tenant", lambda slug: pytest.fail("no interactive input available in this test"))
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)

    cli._prompt_default_workspace([_tenant("acme", "Real Name\nFAKE ROW: rm -rf /"), _tenant("beta")])

    lines = capsys.readouterr().err.splitlines()
    assert not any("FAKE ROW" in line and "Real Name" not in line for line in lines)
