"""Regression tests for `uptonica.repl` (the Textual REPL).

Each test is here because it caught a real bug during the rewrite from
prompt_toolkit+rich to Textual — this is a regression suite, not a
speculative one. Run against the real Textual compositor via `run_test()`,
not mocks of Textual itself: `_open_ask_stream`/`_iter_sse` are the only
things ever stubbed, since those are the network boundary.
"""

from __future__ import annotations

import io
import threading
from typing import ClassVar

import pytest
from rich.console import Console

from uptonica import cli, repl


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    """Every test gets its own config dir — `_history_path()`/`config_dir()`
    read `XDG_CONFIG_HOME`, and without this a test run would read and
    write the real user's `~/.config/uptonica/repl_history.jsonl`."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("UPTONICA_TOKEN", "")
    # `_send_turn_blocking`'s backstop re-raises instead of showing the
    # generic "unexpected failure" message when this is set — a developer
    # running the suite locally with it set in their shell would otherwise
    # get real assertion failures for the wrong reason.
    monkeypatch.delenv("UPTONICA_DEBUG", raising=False)


@pytest.fixture(autouse=True)
def _restore_network_stubs():
    """`_open_ask_stream`/`_iter_sse`/`_request` get monkeypatched directly
    onto the `repl` module by individual tests (they're imported by name
    into it) — restore the real ones after each test so stubbing in one
    test can't leak into the next."""
    original_stream = repl._open_ask_stream
    original_sse = repl._iter_sse
    original_request = repl._request
    yield
    repl._open_ask_stream = original_stream
    repl._request = original_request
    repl._iter_sse = original_sse


def capture_text(app) -> str:
    """Renders the app's current screen to plain text via the real
    compositor — the only reliable way to assert what a Textual app would
    actually show, short of a real terminal."""
    width, height = app.size
    console = Console(width=width, height=height, file=io.StringIO(), record=True, force_terminal=False, no_color=True)
    console.print(app.screen._compositor.render_update(full=True, screen_stack=app._background_screens))
    return console.export_text(clear=True)


class _NoopResponse:
    """A response that opens, has no headers, and immediately ends."""

    headers: ClassVar[dict] = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _stub_noop_turn():
    repl._open_ask_stream = lambda tenant, message, conversation_uuid: _NoopResponse()
    repl._iter_sse = lambda response: iter([("stream_end", {})])


async def test_box_closes_on_resize():
    """The regression this whole rewrite exists for: the old
    rich+prompt_toolkit box was drawn by two engines that didn't share
    layout state, so it stopped closing on the right/bottom the moment the
    terminal resized."""
    app = repl.ReplApp("acme-demo", None)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await pilot.resize_terminal(50, 15)
        await pilot.pause()
        text = capture_text(app)
        assert "╭" in text and "╮" in text and "╰" in text and "╯" in text, text


async def test_ctrl_d_exits():
    """`TextArea` itself binds ctrl+d to delete-right; without
    `priority=True` on the app-level binding, Ctrl+D silently deleted a
    character instead of exiting — even though the banner and /help both
    advertise it as the way out."""
    app = repl.ReplApp(None, None)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        box = app.query_one("#input")
        box.text = "ciao"
        await pilot.press("ctrl+d")
        await pilot.pause()
        # Both assertions belong INSIDE the `run_test()` block: its own
        # teardown always stops the app, so `not app.is_running` checked
        # after the block exits is true regardless of whether Ctrl+D itself
        # did anything — that check alone would still pass even with the
        # regression (delete-right instead of exit) reintroduced.
        assert not app.is_running
        assert box.text == "ciao", "ctrl+d should exit, not delete a character"


async def test_history_recall_walks_multiple_entries():
    """Two bugs, both silent: (1) `self._history = self._history[-N:]`
    always allocates a new list, so the App and the widget held different
    objects from the second submission onward; (2) gating recall on "buffer
    is empty" meant Up only ever stepped back ONE entry, since recalling
    fills the buffer and the next Up saw a non-empty buffer."""
    _stub_noop_turn()
    app = repl.ReplApp(None, None)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        for msg in ("primo", "secondo", "terzo"):
            for ch in msg:
                await pilot.press(ch)
                await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

        box = app.query_one("#input")
        await pilot.press("up")
        await pilot.pause()
        assert box.text == "terzo"
        await pilot.press("up")
        await pilot.pause()
        assert box.text == "secondo"
        await pilot.press("up")
        await pilot.pause()
        assert box.text == "primo"
        await pilot.press("down")
        await pilot.pause()
        assert box.text == "secondo"


async def test_malicious_markup_tenant_does_not_crash():
    """`border_title` is rendered through Textual's markup parser. The old
    code escaped the tenant slug before handing it to the border; this one
    slipped through unescaped and a tenant slug of `"[/bogus]"` (a
    server-controlled value from `/whoami`) took the whole app down with a
    `MarkupError`."""
    app = repl.ReplApp("[/bogus]", None)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        text = capture_text(app)
        assert "[/bogus]" in text


async def test_worker_exception_does_not_leak_secrets_or_crash():
    """An exception escaping a Textual worker gets Textual's own
    fatal-error renderer, which prints a full traceback WITH LOCALS —
    `_open_ask_stream`'s locals include the bearer token. The whole turn
    body now runs inside one `try/except Exception` specifically to make
    sure nothing ever reaches that renderer."""

    def boom(tenant, message, conversation_uuid):
        secret_token = "upt_live_SUPERSECRET"  # noqa: F841 — deliberately local, must never be printed
        raise ValueError("kaboom")

    repl._open_ask_stream = boom

    app = repl.ReplApp("acme", None)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        box = app.query_one("#input")
        await pilot.press(*list("hello"))
        await pilot.press("enter")

        for _ in range(30):
            await pilot.pause()
            if not box.disabled:
                break

        assert box.disabled is False, "input never re-enabled after a worker exception"
        assert app.is_running
        text = capture_text(app)
        assert "SUPERSECRET" not in text
        assert "unexpected failure" in text


async def test_tab_does_not_steal_focus():
    """The default `tab_behavior="focus"` handed focus away to the scroll
    pane on a bare Tab — there's nothing else useful to focus in this
    screen, so every keystroke after that was silently dropped until the
    user tabbed back."""
    app = repl.ReplApp(None, None)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        box = app.query_one("#input")
        await pilot.press(*list("hello"))
        await pilot.press("tab")
        await pilot.pause()
        assert app.focused is box


async def test_dead_token_exit_code_relayed():
    """`_token()` (reached via `_open_ask_stream`) calls `sys.exit(3)`
    directly on a missing/invalid credential — the same contract every
    other command in this file relies on. `sys.exit()` from a non-main
    thread does not terminate the process by itself; `run_repl()` has to
    relay the code and re-raise it once Textual has torn down."""

    def dead_token(tenant, message, conversation_uuid):
        raise SystemExit(3)

    repl._open_ask_stream = dead_token

    app = repl.ReplApp(None, None)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await pilot.press(*list("hello"))
        await pilot.press("enter")
        for _ in range(30):
            await pilot.pause()
            if not app.is_running:
                break

        # Both checked INSIDE the block, and both matter: without the
        # relay, the app stays open (`is_running` true) and `exit_code`
        # stays `None` — a test that only asserted the attribute would
        # stay green even if `run_repl()`'s `self.call_from_thread(self.exit)`
        # were deleted and the app never closed at all.
        assert not app.is_running
        assert app.exit_code == 3


async def test_dead_token_on_workspace_shows_message_before_exiting():
    """Reported live on Termius, 2026-09-09: `/workspace` on a dead/revoked
    token closed the REPL with NO visible message at all, while the exact
    same dead token on an ordinary question showed its 401 error fine
    (that path doesn't exit — see `test_dead_token_exit_code_relayed`).

    Root cause: `_switch_workspace` used to `raise` the `SystemExit(3)`
    straight through — `_request()`'s own `err()` call had already printed
    the reason, captured into the transcript by `on_print()`, but the app
    was already tearing down its alt-screen by the time that queued
    message got dispatched, so it never reached the real terminal.
    `App.exit(message=...)` is the fix: queued now, printed to the REAL
    terminal only once the screen is actually gone.

    A SECOND bug was found and fixed alongside it: `err()`'s `Print`
    message (below, mirroring what the real `_request()` does before it
    raises) can be POSTED before `self.exit()` runs but DISPATCHED after —
    at which point `#history` is detached, and `_line()`'s `.mount()` call
    raised `MountError`, not `NoMatches`. Calling `err()` here — not just
    raising `SystemExit(3)` bare — is what actually exercises that path;
    verified by reverting `_line()`'s `except (NoMatches, MountError)`
    back to `except NoMatches` alone: this test fails with Textual's own
    fatal-error renderer instead of a clean exit.
    """

    def dead_token_request(
        method: str, path: str, *, json_body: dict | None = None,
        tenant: str | None = None, timeout: int = 60,
        tool_name_for_suggestions: str | None = None,
    ):
        # Mirrors `cli.py`'s own `_request()`: it prints the reason via
        # `err()` BEFORE raising — that print is what's still in flight
        # (queued, not yet dispatched) when `self.exit()` runs.
        from uptonica.cli import err
        err("ERROR: HTTP 401 GET /whoami\n  Operator token invalid or revoked.")
        raise SystemExit(3)

    repl._request = dead_token_request

    app = repl.ReplApp(None, None)
    exit_renderables_at_exit_call: list | None = None
    real_exit = app.exit

    def spy_exit(*a, **kw):
        nonlocal exit_renderables_at_exit_call
        real_exit(*a, **kw)
        # Captured immediately after the call — Textual's own teardown
        # prints and CLEARS `_exit_renderables`, so checking it after the
        # `run_test()` block has exited would just see it already empty.
        exit_renderables_at_exit_call = list(app._exit_renderables)

    app.exit = spy_exit

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await pilot.press(*list("/workspace"))
        await pilot.press("enter")
        for _ in range(30):
            await pilot.pause()
            if not app.is_running:
                break

    assert not app.is_running
    assert app.exit_code == 3
    assert exit_renderables_at_exit_call, "exit() was called with no message queued — silent exit, the exact bug reported"
    assert any("api-tokens" in str(r) for r in exit_renderables_at_exit_call), (
        f"queued message doesn't look like the dead-token message: {exit_renderables_at_exit_call!r}"
    )


async def test_abort_turn():
    """Before this, there was no way to interrupt a running turn at all —
    Ctrl+C is bound to copy inside `TextArea`, and quitting mid-turn used
    to hang the process for up to `_open_ask_stream`'s 300s read timeout
    (a non-daemon worker thread that `asyncio.run()` joins on shutdown)."""

    class _SlowResponse:
        headers: ClassVar[dict] = {}

        def __init__(self):
            self.closed = threading.Event()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def readline(self, limit):
            self.closed.wait(timeout=5)
            if self.closed.is_set():
                # Matches what a REAL `http.client.HTTPResponse` does when
                # `close()` is called on it from another thread while a
                # `readline()` is blocked: `close()` sets `self.fp = None`,
                # and the blocked reader's next access raises
                # `AttributeError` ("'NoneType' object has no attribute
                # 'peek'") — NOT `OSError`. A fake that raised `OSError`
                # here would pass even if the app only handled `OSError`
                # and not the exception abort actually produces.
                raise AttributeError("'NoneType' object has no attribute 'peek'")
            return b""

        def close(self):
            self.closed.set()

    resp = _SlowResponse()
    repl._open_ask_stream = lambda tenant, message, conversation_uuid: resp

    app = repl.ReplApp("acme", None)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        box = app.query_one("#input")
        await pilot.press(*list("domanda lenta"))
        await pilot.press("enter")
        await pilot.pause()
        assert box.disabled is True

        await pilot.press("ctrl+g")
        for _ in range(30):
            await pilot.pause()
            if not box.disabled:
                break

        assert box.disabled is False
        assert "interrotta" in capture_text(app)


async def test_user_message_echoed():
    """Nothing mounted the typed question into the transcript — a session
    of five questions showed five answers with no record of what was
    asked, and it wasn't recoverable from scrollback either (alt-screen)."""
    _stub_noop_turn()
    app = repl.ReplApp(None, None)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await pilot.press(*list("quanti clienti ho"))
        await pilot.press("enter")
        await pilot.pause()
        assert "quanti clienti ho" in capture_text(app)


async def test_answer_links_do_not_open_a_browser():
    """`Markdown` defaults to `open_links=True` — a click-to-navigate
    primitive on the model's own reply (tenant-derived content) is worse
    than the pygments ReDoS `pyproject.toml`'s CVE note worries about."""

    class _Resp:
        headers: ClassVar[dict] = {}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    repl._open_ask_stream = lambda tenant, message, conversation_uuid: _Resp()
    repl._iter_sse = lambda response: iter(
        [("text_delta", {"delta": "clicca [qui](http://evil.example/steal)"}), ("stream_end", {})]
    )

    opened = []
    app = repl.ReplApp(None, None)
    app.open_url = lambda url, *a, **kw: opened.append(url)  # type: ignore[method-assign]

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        box = app.query_one("#input")
        await pilot.press(*list("ciao"))
        await pilot.press("enter")
        for _ in range(30):
            await pilot.pause()
            if not box.disabled:
                break

        markdown = app.query_one(repl.Markdown)
        # `_open_links` is the flag `Markdown.__init__` stores from the
        # `open_links=` constructor arg — there's no public property, so
        # this is the actual thing `on_markdown_link_clicked` checks
        # (`textual/widgets/_markdown.py`), not a proxy for it.
        assert markdown._open_links is False

        # Posting the message directly rather than simulating a pixel-exact
        # click on the rendered link span — what matters here is Markdown's
        # OWN guard (`if self._open_links: self.app.open_url(...)`), and
        # posting `LinkClicked` exercises exactly that without depending on
        # where the link happens to land in the current layout.
        markdown.post_message(repl.Markdown.LinkClicked(markdown, "http://evil.example/steal"))
        await pilot.pause()

    assert opened == [], f"a link click should not open a browser, but opened: {opened}"


async def test_dropped_connection_mid_stream_shows_ordinary_error():
    """`http.client.IncompleteRead` (a dropped connection mid-stream) is
    NOT an `OSError` — without it in the except tuple, an ordinary network
    hiccup reached the generic "unexpected failure" backstop instead of
    this more specific, correct message."""
    import http.client

    class _Resp:
        headers: ClassVar[dict] = {}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def boom_sse(response):
        yield "text_delta", {"delta": "parziale..."}
        raise http.client.IncompleteRead(b"", 10)

    repl._open_ask_stream = lambda tenant, message, conversation_uuid: _Resp()
    repl._iter_sse = boom_sse

    app = repl.ReplApp(None, None)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        box = app.query_one("#input")
        await pilot.press(*list("ciao"))
        await pilot.press("enter")
        for _ in range(30):
            await pilot.pause()
            if not box.disabled:
                break

        text = capture_text(app)
        assert "connessione persa" in text
        assert "unexpected failure" not in text


async def test_deprecation_banner_reaches_transcript():
    """`cli.py`'s `err()`/`banner()` just `print(..., file=sys.stderr)` —
    Textual redirects stdout/stderr for the whole run, so without
    `begin_capture_print`/`on_print` the deprecation/Sunset warning (and
    any error `_request()` prints) vanished silently instead of reaching
    the terminal the way it did before the rewrite. Also regression-tests
    the escape() gap: `textual.markup.escape()`'s regex only escapes a `[`
    followed by a LOWERCASE letter, so `[DEPRECATION]` (uppercase) was
    silently eaten by the real parser even after "escaping" it."""
    resp_headers = {"Warning": "199 - deprecated token", "Sunset": "2026-12-31"}

    class _Resp:
        headers = resp_headers

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    repl._open_ask_stream = lambda tenant, message, conversation_uuid: _Resp()
    repl._iter_sse = lambda response: iter([("stream_end", {})])
    cli._deprecation_warned = False  # module-level once-per-process guard

    app = repl.ReplApp(None, None)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        box = app.query_one("#input")
        await pilot.press(*list("ciao"))
        await pilot.press("enter")

        # The turn runs on a daemon thread — a single `pause()` only waits
        # for the CURRENT frame, not for that thread to actually reach
        # `_warn_if_deprecated`. Poll for the turn to finish, the same
        # pattern every other test that starts a real turn already uses;
        # verified flaky without it under an artificially slow stream.
        for _ in range(30):
            await pilot.pause()
            if not box.disabled:
                break

        text = capture_text(app)
        assert "DEPRECATION" in text
        assert "2026-12-31" in text
