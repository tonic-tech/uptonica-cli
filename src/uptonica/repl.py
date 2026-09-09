"""
uptonica repl — the interactive session opened by a bare `uptonica` or
`uptonica ask` with no message.

Rebuilt on Textual (was: rich + prompt_toolkit — see git history for that
version). The reason for the rewrite: a box drawn by two engines that don't
share layout state can't stay closed once anything reflows it (a resize, a
tool-call line printed mid-turn). Textual owns the whole screen with one
compositor, so the input's `border: round` in the CSS below is drawn,
resized and closed by ONE engine — the same trick OpenCode gets from
Bubble Tea owning its whole screen in Go.

This module owns the interactive loop and UI; `cli.py` still owns the
network/auth machinery it reuses as-is (`_open_ask_stream`, `_iter_sse`, the
token/tenant helpers) — none of that changed, because none of it was the
bug. Imported lazily from `cmd_ask` specifically so a one-shot
`uptonica ask "..."` — the common case in a script — never pays for
importing textual at all.
"""

from __future__ import annotations

import http.client
import json
import os
import sys
import threading
import time

from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.css.query import NoMatches
from textual.message import Message
from textual.widgets import Markdown, Static, TextArea

from uptonica.cli import (
    UUID_RE,
    AskError,
    __version__,
    _iter_sse,
    _open_ask_stream,
    _request,
    _safe,
    _warn_if_deprecated,
    _write_last_conversation,
)
from uptonica.secrets import config_dir

SLASH_COMMANDS = ["/workspace", "/new", "/help", "/exit", "/quit"]


def _safe_line(s: object) -> str:
    """`_safe()` for a value that is supposed to be ONE line — a tool name, a
    tenant slug/name, a command word — never free-form diagnostic text.

    `_safe()` alone keeps `\\n`: right for a streamed reply (real line
    breaks) and for an error message (may genuinely span lines), wrong here.
    A server-controlled "label" is exactly what `_safe()`'s own docstring
    warns about forging a plausible status line with — folding
    newlines/tabs to spaces before it ever reaches a widget closes that.
    """
    return _safe(s).replace("\n", " ").replace("\r", " ").replace("\t", " ")


def escape(text: str) -> str:
    """Escapes text so it can't be interpreted as Textual markup.

    NOT `textual.markup.escape` — that one's own regex only escapes a `[`
    followed by a LOWERCASE letter (or `#`/`@`): `r"\\[[a-z#/@]..."`. The
    actual markup PARSER treats a `[WORD]`-shaped run as a tag regardless
    of case, and silently drops it if the name isn't a recognized style —
    verified: `Content.from_markup("[DEPRECATION] foo")` renders as just
    " foo". `_warn_if_deprecated`'s own banner text starts with exactly
    that shape, so the library's escape() would have quietly eaten the
    first word of the one warning this file exists to make sure the user
    sees. Escaping every literal `[` unconditionally sidesteps trusting
    that regex's case sensitivity — this is what actually matches what the
    parser does, not what the library's own escape helper assumes it does.

    Only `[` is touched — NOT backslashes. The parser only treats `\\` as
    special immediately before a `[`; doubling every backslash (as an
    earlier version of this function did) survives round-tripping through
    Textual's renderer as a LITERAL doubled backslash, corrupting anything
    that legitimately contains one (a Windows path in an error message, a
    regex pattern) instead of leaving it alone.
    """
    return text.replace("[", "\\[")


def _history_path() -> str:
    """Pre-create the config dir and the history file at 0700/0600 — same
    reasoning this had in the prompt_toolkit version: a fresh install has no
    config dir yet, and this file logs every message typed into an AI
    prompt, which routinely includes secrets ("collega Shopify, la chiave è
    ..."). JSON-lines instead of prompt_toolkit's own history format, since
    that format is this module's own choice to make now, not something to
    preserve compatibility with.
    """
    directory = config_dir()
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    path = directory / "repl_history.jsonl"
    path.touch(mode=0o600, exist_ok=True)
    os.chmod(path, 0o600)
    return str(path)


_MAX_HISTORY = 500


def _load_history() -> list[str]:
    # The prompt_toolkit version left plaintext-ish message history at
    # `repl_history` (no extension) — it has no reader left now that this
    # module writes `repl_history.jsonl` instead, so it just sits there
    # holding old prompt text (routinely including secrets, per
    # `_history_path()`'s own docstring) forever. Removed on first load of
    # the new format rather than migrated: the two formats aren't
    # compatible enough to be worth reading the old one just to re-write it.
    try:
        (config_dir() / "repl_history").unlink()
    except FileNotFoundError:
        pass

    path = _history_path()
    lines: list[str] = []
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    value = json.loads(raw)
                except json.JSONDecodeError:
                    continue  # a truncated last line from a killed process — skip, don't crash the REPL over it
                if isinstance(value, str):
                    lines.append(value)
    except FileNotFoundError:
        return []
    return lines[-_MAX_HISTORY:]


def _save_history(history: list[str]) -> None:
    # Rewrites the whole (already-capped-at-`_MAX_HISTORY`) file rather
    # than appending a line — appending only ever grows the file, so it
    # would keep every secret ever typed into this REPL forever, exactly
    # the hazard `_history_path()`'s own docstring worries about. At most
    # `_MAX_HISTORY` short lines, typed at human speed — the extra I/O
    # this costs over a true append is not a real cost here.
    with open(_history_path(), "w", encoding="utf-8") as f:
        f.writelines(json.dumps(text) + "\n" for text in history)


class MessageInput(TextArea):
    """The boxed input row. Enter submits; Alt+Enter inserts a newline.

    Terminals encode Alt+Enter two different ways: some report it as a
    single `alt+enter` key, most report the RAW bytes as a standalone Escape
    immediately followed by Enter (Meta-prefixing) — the same ambiguity the
    prompt_toolkit version navigated by binding that as a two-key chord.
    Textual's own driver coalesces some of these but not reliably across
    every terminal, so both paths are handled here: a real `alt+enter` key,
    and a bare Escape whose very next key (within `_ESCAPE_CHORD_WINDOW`) is
    Enter.

    History recall (Up/Down) fires when the buffer is empty, OR when it
    already holds a still-unedited recall result — `_recalling` tracks
    that second case, since without it Up would only ever step back ONE
    entry (recalling entry N fills the buffer, so the NEXT Up sees a
    non-empty buffer and falls through to plain cursor movement instead of
    continuing to N-1). Any other key that reaches `super()._on_key()`
    (a real edit) clears the flag — once the user has changed what's in
    the buffer, Up/Down go back to being cursor movement, matching normal
    multi-line editing.
    """

    class Submitted(Message):
        def __init__(self, text: str) -> None:
            self.text = text
            super().__init__()

    _ESCAPE_CHORD_WINDOW = 0.5

    def __init__(self, *args, history: list[str], **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._history = history
        self._history_pos = len(history)
        self._recalling = False
        self._last_escape_at: float | None = None

    async def _on_key(self, event: events.Key) -> None:
        key = event.key
        now = time.monotonic()

        if key == "escape":
            self._last_escape_at = now
            event.stop()
            event.prevent_default()
            return

        chorded_newline = key == "enter" and self._last_escape_at is not None and (
            now - self._last_escape_at
        ) < self._ESCAPE_CHORD_WINDOW
        self._last_escape_at = None

        if key == "enter" and not chorded_newline:
            event.stop()
            event.prevent_default()
            text = self.text
            self.clear()
            self._recalling = False
            # NOT `self._history_pos = len(self._history)` here: the App's
            # `on_message_input_submitted` hasn't appended `text` to
            # `_history` yet at this point (it runs later, off this
            # `Submitted` message) — setting it now would leave `_history_pos`
            # stale by one entry, and the very next Up would recall the
            # SECOND-to-last message instead of the last one. The App
            # resyncs it right after the append instead.
            self.post_message(self.Submitted(text))
            return

        if key == "alt+enter" or chorded_newline:
            event.stop()
            event.prevent_default()
            self.insert("\n")
            self._recalling = False
            return

        if key == "up" and (not self.text or self._recalling) and self._history:
            event.stop()
            event.prevent_default()
            self._recall(-1)
            return

        if key == "down" and self._recalling and self._history_pos < len(self._history):
            event.stop()
            event.prevent_default()
            self._recall(1)
            return

        if key == "tab" and self.text.startswith("/") and "\n" not in self.text:
            event.stop()
            event.prevent_default()
            self._complete_slash()
            return

        self._recalling = False
        await super()._on_key(event)

    def _recall(self, direction: int) -> None:
        # `_recall` is only ever entered with the buffer non-empty when
        # `_recalling` is already True — which only happens as the result
        # of a PRIOR call to this same method, and every prior call landed
        # `_history_pos` at some index < len(self._history). So
        # `_history_pos == len(self._history)` here always means the
        # buffer is empty too (there is no "recall past the end back to
        # what you were typing" case to restore) — landing back at
        # len(self._history) (via Down) always means back to an empty
        # buffer, never anything else.
        self._history_pos = max(0, min(len(self._history), self._history_pos + direction))
        value = self._history[self._history_pos] if self._history_pos < len(self._history) else ""
        self.load_text(value)
        self.move_cursor(self.document.end)
        self._recalling = True

    def _complete_slash(self) -> None:
        matches = [c for c in SLASH_COMMANDS if c.startswith(self.text)]
        if not matches:
            return
        common = matches[0]
        for m in matches[1:]:
            i = 0
            while i < len(common) and i < len(m) and common[i] == m[i]:
                i += 1
            common = common[:i]
        if len(common) > len(self.text):
            self.load_text(common)
            self.move_cursor(self.document.end)


# A single restrained glyph as Lia's mark, not an ASCII illustration: a
# multi-line ASCII rendition of the U-arrow brand mark would depend on the
# terminal's own font for alignment and risk looking broken rather than
# distinctive. One character in the brand's lime accent is the same
# restraint Claude Code's own "✻" mark uses, and degrades safely everywhere.
_LIA_MARK = "✻"
_LIME = "#c8f04f"

# Matches the mockup's tool-status blue (distinct from the lime brand accent
# and from the green/red success/failure colors, so a tool call reads as its
# own category of line rather than competing with either).
_TOOL_COLOR = "#6fb7d9"

# Minimum interval between Markdown.update() re-parses of the accumulating
# reply. Textual's Markdown widget re-parses its whole source on every
# update() call, same as rich.markdown.Markdown did in the previous version
# (measured there: 11.3s of CPU for a 1000-delta, 70KB answer) — this
# throttles the update calls themselves; the final state is always flushed
# once more on stream_end/error so the visible answer is never behind what
# was actually received.
_MARKDOWN_REFRESH_INTERVAL = 1 / 12


class ReplApp(App[None]):
    CSS = """
    Screen {
        layout: vertical;
        background: $background;
    }

    #history {
        height: 1fr;
        padding: 0 2;
    }

    #input {
        height: auto;
        max-height: 40%;
        margin: 0 1 1 1;
        padding: 0 1;
        border: round ansi_bright_black;
    }

    #input.has-tenant {
        border-title-color: ansi_cyan;
    }

    #input.no-tenant {
        border-title-color: ansi_yellow;
    }
    """

    BINDINGS = [
        # priority=True: TextArea itself binds "ctrl+d" to delete-right, and
        # a plain (non-priority) app binding loses to a focused widget's own
        # binding — without priority, Ctrl+D silently deletes a character
        # instead of exiting, even though the banner and /help both
        # advertise it as the way out.
        Binding("ctrl+d", "quit_repl", "", priority=True),
        Binding("ctrl+g", "abort_turn", "", priority=True, show=False),
    ]

    def __init__(self, tenant: str | None, conversation_uuid: str | None) -> None:
        super().__init__()
        # Both read AND written from the turn's daemon thread (`_send_turn`
        # reads `self.tenant`, writes `self.conversation_uuid`) without
        # going through `call_from_thread`. Safe only because the input is
        # disabled for the whole turn, so nothing on the main thread (/new,
        # /workspace) can race that write — if a mid-turn abort ever lets
        # the user act while a turn is still in flight, that invariant is
        # exactly what would need re-checking.
        self.tenant = tenant
        self.conversation_uuid = conversation_uuid
        self._history = _load_history()
        self._active_response = None  # set for the duration of a turn — see _send_turn_blocking/action_abort_turn
        self._aborting = False
        self.exit_code: int | None = None  # relays a sys.exit() raised on the turn thread — see _send_turn_blocking

    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="history")
        # tab_behavior="indent": the default "focus" would hand focus away
        # to the scroll pane on a bare Tab (there's nothing else useful to
        # focus in this screen), silently dropping every keystroke that
        # follows until the user tabs back — "indent" makes a bare Tab a
        # harmless no-op (a few inserted spaces) instead, while the
        # slash-completion branch in `_on_key` still intercepts Tab first
        # when the line is a `/` command.
        yield MessageInput(id="input", history=self._history, tab_behavior="indent")

    def on_mount(self) -> None:
        # `cli.py`'s own `err()`/`banner()` just `print(..., file=sys.stderr)`
        # — Textual redirects stdout/stderr for the whole run, so without
        # this those calls (the deprecation/Sunset warning, any error
        # `_request()` prints before a non-fatal exit) vanish silently
        # instead of reaching the terminal the way they did before the
        # rewrite. `on_print` below turns them back into visible lines.
        self.begin_capture_print(self)
        self._print_banner()
        self._refresh_border()
        self.query_one(MessageInput).focus()

    def on_print(self, event: events.Print) -> None:
        text = event.text.rstrip("\n")
        if text:
            # Captured stdout/stderr is plain terminal text, not markup —
            # and some of it (the Sunset/Warning deprecation headers) is
            # server-controlled, so it goes through the same escape() every
            # other dynamic string in this file does before reaching a
            # widget.
            self._line(escape(text))

    def action_quit_repl(self) -> None:
        self.exit()

    def action_abort_turn(self) -> None:
        if self._active_response is not None:
            self._aborting = True
            self._active_response.close()

    def _refresh_border(self) -> None:
        box = self.query_one("#input", MessageInput)
        if self.tenant:
            box.border_title = escape(_safe_line(self.tenant))
            box.set_classes("has-tenant")
        else:
            box.border_title = "nessun workspace"
            box.set_classes("no-tenant")

    def _line(self, renderable: str, classes: str = "") -> Static | None:
        # `call_from_thread` runs its callable on the main thread, but that
        # doesn't mean the screen is still there to receive it — a turn's
        # daemon thread can call this (via `_add_error` etc.) after the app
        # has already started exiting (Ctrl+D, /exit, quitting mid-turn).
        # `query_one` on a torn-down screen raises `NoMatches`, and an
        # exception escaping a `call_from_thread`-dispatched callback hits
        # Textual's OWN fatal-error renderer (`show_locals=True`) same as
        # an unhandled worker exception would — the exact class of problem
        # `_send_turn_blocking`'s backstop exists to prevent, just reached
        # from a different direction. There's nothing useful to show once
        # the screen is gone anyway, so this is a silent no-op, not a retry.
        try:
            history = self.query_one("#history", VerticalScroll)
        except NoMatches:
            return None
        widget = Static(renderable, classes=classes)
        history.mount(widget)
        history.scroll_end(animate=False)
        return widget

    def _print_banner(self) -> None:
        self._line("")
        self._line(
            f"[bold {_LIME}]{_LIA_MARK}[/bold {_LIME}]  [bold]Lia[/bold] — chiedimi qualsiasi cosa sul tuo "
            "workspace: vendite, catalogo, contatti, campagne."
        )
        self._line(f"   uptonica v{__version__} · /help per i comandi · Ctrl+D per uscire")
        self._line("")

    def _print_help(self) -> None:
        self._line(
            "[bold]comandi[/bold]\n"
            "  /workspace          elenca i workspace raggiungibili da questo token\n"
            "  /workspace <slug>   passa a quel workspace (apre un thread nuovo)\n"
            "  /new                apre un thread nuovo nello stesso workspace\n"
            "  /help               questo elenco\n"
            "  /exit, /quit        esce (anche Ctrl+D)\n"
            "\n"
            "qualsiasi altra riga è una domanda per Lia.\n"
            "Invio invia; Option+Invio (Alt+Invio) va a capo senza inviare.\n"
            "Ctrl+G interrompe una risposta in corso."
        )

    def on_message_input_submitted(self, event: MessageInput.Submitted) -> None:
        box = self.query_one(MessageInput)
        text = event.text.strip()

        if text:
            # In-place truncation, not `self._history = self._history[-N:]`
            # — a slice always allocates a NEW list even when nothing is
            # trimmed, which would leave `MessageInput` (holding the list it
            # was constructed with) pointing at a stale copy from the second
            # submission onward, silently breaking history recall.
            self._history.append(text)
            del self._history[:-_MAX_HISTORY]
            _save_history(self._history)

        # Resync unconditionally, even when nothing was appended (an empty
        # submit): the widget's own `_on_key` can't do this itself, since
        # the append above hasn't happened yet at the point `_on_key` posts
        # `Submitted` — setting it there would always be one entry stale.
        # Resetting it here also means a cancelled recall (Up, Up, then
        # Enter on the now-empty line) starts the NEXT Up fresh from the
        # end, instead of continuing from wherever browsing left off.
        box._history_pos = len(self._history)

        if not text:
            return

        if text.startswith("/"):
            self._handle_slash(text)
            return

        self._start_turn(text)

    def _handle_slash(self, text: str) -> None:
        parts = text.split(maxsplit=1)
        cmd = parts[0]
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd in ("/exit", "/quit"):
            self.exit()
            return
        if cmd == "/help":
            self._print_help()
            return
        if cmd == "/new":
            self._line("[green]nuovo thread — la prossima domanda parte da zero[/green]")
            self.conversation_uuid = None
            return
        if cmd == "/workspace":
            self._switch_workspace(arg)
            return

        self._line(f"[red]comando sconosciuto:[/red] {escape(_safe_line(cmd))} — /help per la lista")

    def _switch_workspace(self, arg: str) -> None:
        try:
            data = _request("GET", "/whoami")
        except SystemExit as e:
            # exit(3) is _request()'s "auth/forbidden" bucket — the SAME
            # token will fail the SAME way on the next turn too, so let a
            # dead/revoked token exit the REPL once with the reason err()
            # already printed, rather than loop on identical failures. Any
            # OTHER exit code (network blip, 5xx) is exactly what the REPL
            # exists to survive.
            if e.code == 3:
                raise
            return

        tenants = data.get("tenants", [])

        if not arg:
            if not tenants:
                self._line("[yellow]nessun workspace raggiungibile da questo token[/yellow]")
                return
            self._line("[bold]workspace raggiungibili[/bold]")
            for t in tenants:
                slug = escape(_safe_line(str(t.get("slug", "?"))))
                name = escape(_safe_line(str(t.get("name", ""))))
                self._line(f"  {slug}  {name}")
            return

        match = next(
            (t for t in tenants if t.get("slug") == arg or str(t.get("id")) == arg),
            None,
        )
        if match is None:
            self._line(f"[red]'{escape(_safe_line(arg))}' non è un workspace raggiungibile da questo token.[/red] /workspace per la lista")
            return

        slug = match.get("slug")
        if not isinstance(slug, str):
            self._line("[red]il workspace trovato non ha uno slug utilizzabile — non dovrebbe succedere lato server.[/red]")
            return

        self._line(f"workspace: [cyan]{escape(_safe_line(slug))}[/cyan] — nuovo thread")
        # A conversation UUID belongs to one tenant (OperatorTurnController's
        # own pair check server-side) — carrying the old one across a
        # workspace switch would just get refused as conversation_not_found
        # on the next turn.
        self.tenant = slug
        self.conversation_uuid = None
        self._refresh_border()

    def _start_turn(self, message: str) -> None:
        self._line(f"[bold]›[/bold] {escape(_safe_line(message))}")
        box = self.query_one(MessageInput)
        box.disabled = True
        self._aborting = False
        # A raw daemon thread, not `run_worker(thread=True)`: Textual's
        # worker pool runs on a non-daemon executor that `asyncio.run()`
        # joins on shutdown with no timeout — quitting mid-turn used to
        # hang the process for as long as the blocking SSE read stayed
        # open (up to `_open_ask_stream`'s 300s timeout). A daemon thread
        # is simply not joined, so the process exits immediately regardless
        # of what the thread is doing. The input being disabled for the
        # whole turn is what stands in for `run_worker`'s `exclusive=True`
        # here — a disabled widget doesn't receive key events, so a second
        # turn can't start while one is in flight.
        threading.Thread(target=self._send_turn_blocking, args=(message,), daemon=True).start()

    def _end_turn(self) -> None:
        self._active_response = None
        # `finally` in `_send_turn_blocking` calls this unconditionally, so
        # it can run after the app has already started exiting — same
        # "screen might be gone" case `_line()` guards against, and for
        # the same reason: an uncaught `NoMatches` here would escape a
        # `call_from_thread`-dispatched callback straight into Textual's
        # own fatal-error-with-locals renderer.
        try:
            box = self.query_one(MessageInput)
        except NoMatches:
            return
        box.disabled = False
        box.focus()

    def _add_tool_call(self, tool_name: str) -> None:
        self._line(f"  [{_TOOL_COLOR}]◐[/{_TOOL_COLOR}] {escape(_safe_line(tool_name))}...")

    def _add_tool_result(self, name: str, success: bool, error: object) -> None:
        if success:
            self._line(f"  [green]✓[/green] {escape(_safe_line(str(name)))}")
        else:
            # error, unlike name/tool_name, is free-form diagnostic text
            # rather than a single-word label — _safe() (newlines kept) is
            # the right tool here, matching the error lines below.
            suffix = f": {escape(_safe(str(error)))}" if error else ""
            self._line(f"  [red]✗[/red] {escape(_safe_line(str(name)))}{suffix}")

    def _add_error(self, message: str) -> None:
        self._line(f"[red]ERRORE:[/red] {escape(_safe(message))}")

    def _start_answer_widget(self) -> Markdown:
        history = self.query_one("#history", VerticalScroll)
        # open_links=False: Markdown defaults to opening a clicked link in
        # the system browser, and the text being rendered here is the
        # model's own reply — tenant-derived content, the same trust level
        # the pygments CVE note in pyproject.toml calls out by name. A
        # click-to-navigate primitive on that content is worse than the
        # ReDoS that note worries about.
        widget = Markdown("", open_links=False)
        history.mount(widget)
        history.scroll_end(animate=False)
        return widget

    def _update_answer(self, widget: Markdown, text: str) -> None:
        widget.update(text)
        self.query_one("#history", VerticalScroll).scroll_end(animate=False)

    def _send_turn_blocking(self, message: str) -> None:
        """Runs on its own daemon thread (see `_start_turn`) —
        `_open_ask_stream`/`_iter_sse` are blocking `urllib` calls, unchanged
        from `cli.py`. Every touch of the UI from here goes through
        `call_from_thread`, which runs the given callable on the main thread
        and blocks this one until it returns — Textual's documented pattern
        for exactly this ("call a sync function that does I/O, keep the UI
        thread-safe").

        The whole body is one `try/except Exception` — deliberately, not
        just tidiness: `cli.py::main()` has a backstop for an unexpected
        exception (search for "Never let a raw traceback reach the
        terminal") that never shows a raw traceback by default because one
        could carry the bearer token in a local variable (`_open_ask_stream`
        has exactly such a local). That backstop is in the MAIN thread's
        call stack; nothing on this thread ever reaches it, so this
        function has to be its own backstop — an uncaught exception here
        would otherwise hit Python's default `threading.excepthook` and
        print directly, bypassing `main()`'s guard entirely.
        """
        try:
            self._send_turn(message)
        except SystemExit as e:
            # `_token()` (reached via `_open_ask_stream`) calls `sys.exit(3)`
            # directly on a missing/invalid credential — the same contract
            # every other command in this file relies on. `sys.exit()` from
            # a non-main thread does NOT terminate the process (Python's
            # threading machinery silently swallows a `SystemExit` that
            # reaches the top of a spawned thread); relay the code to the
            # main thread and let `run_repl()` re-raise it once Textual has
            # cleanly torn down, or a dead token would just make every turn
            # silently fail forever instead of exiting like `cmd_ask`'s
            # one-shot path does for the exact same error.
            self.exit_code = e.code if isinstance(e.code, int) else 1
            self.call_from_thread(self.exit)
        except Exception as e:  # see docstring: this IS the backstop, on this thread
            if os.environ.get("UPTONICA_DEBUG") == "1":
                raise
            self.call_from_thread(self._add_error, f"{e.__class__.__name__}: unexpected failure.")
        finally:
            self.call_from_thread(self._end_turn)

    def _send_turn(self, message: str) -> None:
        try:
            response = _open_ask_stream(self.tenant, message, self.conversation_uuid)
        except AskError as e:
            self.call_from_thread(self._add_error, str(e))
            return

        self._active_response = response
        new_uuid = self.conversation_uuid
        accumulated = ""
        answer_widget = self.call_from_thread(self._start_answer_widget)
        last_render = 0.0
        # tool_id -> tool_name, so the tool_result event (which carries no
        # name, only {tool_id, success, error}) can still say WHAT finished.
        pending_tools: dict[str, str] = {}

        with response:
            self.call_from_thread(_warn_if_deprecated, response.headers)
            header = response.headers.get("X-Lia-Conversation")
            if isinstance(header, str) and UUID_RE.match(header):
                new_uuid = header

            try:
                for event_type, data in _iter_sse(response):
                    if event_type == "text_delta":
                        delta = data.get("delta")
                        if isinstance(delta, str):
                            accumulated += _safe(delta)
                            now = time.monotonic()
                            if now - last_render >= _MARKDOWN_REFRESH_INTERVAL:
                                self.call_from_thread(self._update_answer, answer_widget, accumulated)
                                last_render = now
                    elif event_type == "tool_call":
                        tool_id = data.get("tool_id")
                        tool_name = data.get("tool_name")
                        if isinstance(tool_id, str) and isinstance(tool_name, str):
                            pending_tools[tool_id] = tool_name
                            self.call_from_thread(self._add_tool_call, tool_name)
                    elif event_type == "tool_result":
                        tool_id = data.get("tool_id")
                        name = pending_tools.pop(tool_id, tool_id) if isinstance(tool_id, str) else "?"
                        self.call_from_thread(self._add_tool_result, str(name), bool(data.get("success", True)), data.get("error"))
                    elif event_type == "error":
                        self.call_from_thread(self._update_answer, answer_widget, accumulated)
                        self.call_from_thread(self._add_error, str(data.get("message", "unknown error")))
                    elif event_type == "stream_end":
                        break
            except Exception as e:
                # `action_abort_turn` closes `response` from the main
                # thread while this loop is blocked in `readline()` — what
                # that raises HERE depends on exactly where the blocked
                # read was: verified against a real `http.client` response,
                # `close()` sets `fp = None` and the blocked reader raises
                # `AttributeError` (`'NoneType' object has no attribute
                # 'peek'`), not an `OSError`. `self._aborting` is what
                # distinguishes "the user asked for this" from a genuine
                # bug — checked FIRST and unconditionally, rather than
                # trying to keep the exception-type allowlist in sync with
                # whatever `http.client`'s internals happen to raise for a
                # closed-out-from-under-it socket. `(OSError, TimeoutError,
                # http.client.HTTPException)` — including
                # `http.client.IncompleteRead`, which is NOT an `OSError` —
                # still covers an ordinary dropped connection so THAT
                # doesn't fall through to the generic "unexpected failure"
                # backstop either.
                if not (self._aborting or isinstance(e, (OSError, TimeoutError, http.client.HTTPException))):
                    raise
                self.call_from_thread(self._update_answer, answer_widget, accumulated)
                if self._aborting:
                    self.call_from_thread(self._add_error, "risposta interrotta.")
                else:
                    self.call_from_thread(self._add_error, f"connessione persa: {e}")
            else:
                # Final flush: the throttle above may have left the last
                # delta or two un-rendered even though they were received.
                self.call_from_thread(self._update_answer, answer_widget, accumulated)

        # Persisted even after an error above: the server already committed
        # whatever it streamed before failing, and the next turn should
        # continue that thread, not silently orphan it into a new one. Also
        # wrapped by `_send_turn_blocking`'s backstop, since a read-only
        # filesystem or full disk here shouldn't be able to crash the app.
        if self.tenant and isinstance(new_uuid, str) and UUID_RE.match(new_uuid):
            _write_last_conversation(self.tenant, new_uuid)
        self.conversation_uuid = new_uuid


def run_repl(initial_tenant: str | None, initial_conversation: str | None) -> None:
    app = ReplApp(initial_tenant, initial_conversation)
    app.run()
    if app.exit_code is not None:
        sys.exit(app.exit_code)
