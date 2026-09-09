"""
uptonica repl — the interactive session opened by a bare `uptonica` or
`uptonica ask` with no message.

A persistent chat with Lia instead of one process per question: the boxed
input, slash commands, and streamed markdown are what prompt_toolkit and
rich are for — this module owns the interactive loop, `cli.py` owns the
network/auth machinery it reuses (`_open_ask_stream`, `_iter_sse`, the
token/tenant helpers). Imported lazily from `cmd_ask` specifically so a
one-shot `uptonica ask "..."` — the common case in a script — never pays for
importing prompt_toolkit/rich at all.
"""

from __future__ import annotations

import os
import time

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.markup import escape

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
    warns about forging a plausible status line with — `escape(_safe(...))`
    stops it from being COLORED like a real `✓`/`✗` line, but `_safe()` on
    its own still lets a tool_name or tenant name carrying `\\n✓ real.tool`
    print as a second, uncolored-but-otherwise-convincing line. Folding
    newlines/tabs to spaces here, before `escape()`, closes that — verified:
    a tool_name containing "\\n  ✓ payments.refund.execute" now renders on
    one line instead of forging a second.
    """
    return _safe(s).replace("\n", " ").replace("\r", " ").replace("\t", " ")


class _SlashCompleter(Completer):
    """Completes slash commands only — a line not starting with `/` is a
    message to Lia, and offering catalog/tool-name completion for THAT would
    need a network round-trip on every keystroke. Out of scope for v1."""

    def get_completions(self, document, complete_event):  # noqa: ARG002
        # current_line_before_cursor, not text_before_cursor: in multiline
        # mode the buffer can hold earlier lines too, and a slash command is
        # only ever meaningful as the whole of the CURRENT line.
        text = document.current_line_before_cursor
        if not text.startswith("/") or " " in text:
            return
        for cmd in SLASH_COMMANDS:
            if cmd.startswith(text):
                yield Completion(cmd, start_position=-len(text))


def _history_path() -> str:
    """Pre-create the config dir and the history file at 0700/0600, rather
    than letting `FileHistory` create them implicitly.

    Two real bugs otherwise: `FileHistory` never creates its parent
    directory, so on a fresh install where `config_dir()` doesn't exist yet
    (Keychain-only token, no `tenant use` ever run) the REPL opens fine and
    then crashes with FileNotFoundError the moment the user types a line.
    And `FileHistory`'s own file creation is a plain `open(..., "ab")` —
    0644 under a normal umask — for a file that logs every message typed
    into an AI prompt, which routinely includes secrets a user is asking
    Lia to use ("collega Shopify, la chiave è ..."). Every OTHER file this
    package writes goes through `atomic_write_text` at 0600; this matches
    that, by hand, since FileHistory doesn't take a mode argument.
    """
    directory = config_dir()
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    path = directory / "repl_history"
    path.touch(mode=0o600, exist_ok=True)
    os.chmod(path, 0o600)
    return str(path)


def _multiline_key_bindings() -> KeyBindings:
    """Enter submits; Alt+Enter (or Esc then Enter — the same keystroke on a
    terminal that can't tell the two apart, which is most of them) inserts a
    newline instead. `PromptSession(multiline=True)` alone would give Enter
    to the newline and leave nothing bound to submit, so both have to be
    rebound together: Shift+Enter is deliberately NOT used for the newline
    side, because most terminal emulators (unlike Kitty/iTerm2's opt-in
    protocol) report it identically to a bare Enter — binding to it would
    silently do nothing on those, with no way to tell the user why.
    """
    bindings = KeyBindings()

    @bindings.add(Keys.Enter)
    def _submit(event) -> None:  # noqa: ANN001
        event.current_buffer.validate_and_handle()

    @bindings.add(Keys.Escape, Keys.Enter)
    def _newline(event) -> None:  # noqa: ANN001
        event.current_buffer.insert_text("\n")

    return bindings


def run_repl(initial_tenant: str | None, initial_conversation: str | None) -> None:
    """Blocks until the user exits (Ctrl+D, `/exit`, `/quit`, or Ctrl+C at
    the prompt — mid-turn Ctrl+C is left to the normal KeyboardInterrupt
    path in main())."""
    console = Console(highlight=False)
    session: PromptSession[str] = PromptSession(
        history=FileHistory(_history_path()),
        completer=_SlashCompleter(),
        multiline=True,
        key_bindings=_multiline_key_bindings(),
    )

    tenant = initial_tenant
    conversation_uuid = initial_conversation

    _print_banner(console)

    while True:
        _box_top(console, tenant)
        try:
            text = session.prompt(_prompt_message(), prompt_continuation=_prompt_continuation)
        except EOFError:
            _box_bottom(console)
            break
        except KeyboardInterrupt:
            _box_bottom(console)
            continue  # Ctrl+C on an empty prompt clears the line, doesn't exit — matches most shells
        _box_bottom(console)

        text = text.strip()
        if not text:
            continue

        if text.startswith("/"):
            outcome = _handle_slash(text, console, tenant)
            if outcome is _EXIT:
                break
            if isinstance(outcome, tuple):
                tenant, conversation_uuid = outcome
            continue

        conversation_uuid = _send_turn(console, tenant, text, conversation_uuid)

    console.print()


_EXIT = object()  # sentinel distinct from "no state change" (None) and "new state" (tuple)


# A single restrained glyph as Lia's mark, not an ASCII illustration: a
# multi-line ASCII rendition of the U-arrow brand mark would depend on the
# terminal's own font for alignment (box-drawing/line characters render at
# different widths across terminal fonts) and risk looking broken rather
# than distinctive. One character in the brand's lime accent is the same
# restraint Claude Code's own "✻" mark uses, and degrades safely everywhere.
_LIA_MARK = "✻"
_LIME = "#c8f04f"

# Frames the input, top and bottom, on every turn — the approved mockup
# (a boxed terminal window: titlebar, rounded border, a boxed input row)
# was matched with the lightweight version of that idea rather than the
# full one: a fixed border drawn fresh around each prompt instead of a
# persistent full-screen frame with a pinned status bar, which would need
# an alt-screen layout engine (rich's own screen isn't one) to keep a
# border painted correctly across resizes and scrollback. "bright_black"
# rather than a fixed hex: it's an ANSI SGR code the terminal's own theme
# resolves, so the same border reads correctly on a light- or dark-background
# terminal instead of picking one and looking wrong on the other — and it's
# the one style name both rich (below) and prompt_toolkit's HTML() (in
# _prompt_message/_prompt_continuation) can render identically, even though
# they're two unrelated rendering engines drawing two halves of the same box.
_BORDER_STYLE = "bright_black"


def _box_top(console: Console, tenant: str | None) -> None:
    if tenant:
        label = escape(_safe_line(tenant))
        colored = f"[cyan]{label}[/cyan]"
    else:
        label = "nessun workspace"
        colored = f"[yellow]{label}[/yellow]"
    # "  ╭─ " (5 cols) + label + " " (1 col) + dashes should fill the
    # terminal width — sized here rather than left to wrap, since a wrapped
    # border line looks broken rather than merely long.
    dashes = "─" * max(console.size.width - 5 - len(label) - 1, 3)
    console.print(f"  [{_BORDER_STYLE}]╭─[/{_BORDER_STYLE}] {colored} [{_BORDER_STYLE}]{dashes}[/{_BORDER_STYLE}]")


def _box_bottom(console: Console) -> None:
    dashes = "─" * max(console.size.width - 3, 3)
    console.print(f"  [{_BORDER_STYLE}]╰{dashes}[/{_BORDER_STYLE}]")


def _prompt_message() -> HTML:
    # Rendered by prompt_toolkit itself, not rich — HTML() is prompt_toolkit's
    # own markup, a different vocabulary from rich.markup.escape() used
    # everywhere else in this file. No user- or server-controlled text
    # passes through this string, so nothing here needs escaping.
    return HTML(f'  <ansibrightblack>│</ansibrightblack> <style fg="{_LIME}">›</style> ')


def _prompt_continuation(width: int, line_number: int, is_soft_wrap: bool) -> HTML:  # noqa: ARG001
    # Same visible width as _prompt_message()'s "  │ › " (6 columns), so a
    # second line of a multi-line message lines up under the first line's
    # text instead of under the box's left border.
    return HTML("  <ansibrightblack>│</ansibrightblack>   ")


def _print_banner(console: Console) -> None:
    console.print()
    console.print(f"  [bold {_LIME}]{_LIA_MARK}[/bold {_LIME}]  [bold]Lia[/bold] — chiedimi qualsiasi cosa sul tuo workspace: vendite, catalogo, contatti, campagne.")
    console.print(f"     uptonica v{__version__} · /help per i comandi · Ctrl+D per uscire")
    console.print()


def _handle_slash(text: str, console: Console, tenant: str | None):
    parts = text.split(maxsplit=1)
    cmd = parts[0]
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd in ("/exit", "/quit"):
        return _EXIT
    if cmd == "/help":
        _print_help(console)
        return None
    if cmd == "/new":
        console.print("[green]nuovo thread — la prossima domanda parte da zero[/green]")
        return (tenant, None)
    if cmd == "/workspace":
        return _switch_workspace(console, arg)

    # cmd is user-typed, not server-controlled — but escape() anyway rather
    # than reason about which strings need it: it's a no-op on plain text
    # and the one time this list grows a server-derived value, it's already
    # covered rather than a bug waiting to be reintroduced.
    console.print(f"[red]comando sconosciuto:[/red] {escape(_safe_line(cmd))} — /help per la lista")
    return None


def _print_help(console: Console) -> None:
    console.print(
        "[bold]comandi[/bold]\n"
        "  /workspace          elenca i workspace raggiungibili da questo token\n"
        "  /workspace <slug>   passa a quel workspace (apre un thread nuovo)\n"
        "  /new                apre un thread nuovo nello stesso workspace\n"
        "  /help               questo elenco\n"
        "  /exit, /quit        esce (anche Ctrl+D)\n"
        "\n"
        "qualsiasi altra riga è una domanda per Lia.\n"
        "Invio invia; Option+Invio (Alt+Invio) va a capo senza inviare."
    )


def _switch_workspace(console: Console, arg: str):
    try:
        data = _request("GET", "/whoami")
    except SystemExit as e:
        # exit(3) is _request()'s "auth/forbidden" bucket (see the module
        # docstring's exit-code table) — not just _token()'s own "no usable
        # credential" CredentialError, but ALSO a 401/403 the server itself
        # returned for this call. Every case in that bucket means the SAME
        # token will fail the SAME way on the next turn too — a dead or
        # revoked token, or one lacking the ability to reach /whoami at all
        # — so letting the REPL "recover" into a loop of identical failures
        # is worse than exiting once with the reason err() already printed
        # upstream. A network blip or a 5xx (any OTHER exit code) is exactly
        # what the REPL exists to survive, so only the auth bucket re-raises.
        if e.code == 3:
            raise
        return None

    tenants = data.get("tenants", [])

    if not arg:
        if not tenants:
            console.print("[yellow]nessun workspace raggiungibile da questo token[/yellow]")
            return None
        console.print("[bold]workspace raggiungibili[/bold]")
        for t in tenants:
            slug = escape(_safe_line(str(t.get("slug", "?"))))
            name = escape(_safe_line(str(t.get("name", ""))))
            console.print(f"  {slug}  {name}")
        return None

    match = next(
        (t for t in tenants if t.get("slug") == arg or str(t.get("id")) == arg),
        None,
    )
    if match is None:
        console.print(f"[red]'{escape(_safe_line(arg))}' non è un workspace raggiungibile da questo token.[/red] /workspace per la lista")
        return None

    slug = match.get("slug")
    if not isinstance(slug, str):
        console.print("[red]il workspace trovato non ha uno slug utilizzabile — non dovrebbe succedere lato server.[/red]")
        return None

    console.print(f"workspace: [cyan]{escape(_safe_line(slug))}[/cyan] — nuovo thread")
    # A conversation UUID belongs to one tenant (see OperatorTurnController's
    # own pair check server-side); carrying the old one across a workspace
    # switch would just get refused as conversation_not_found on the next
    # turn. Starting fresh here is what that refusal would have forced anyway.
    return (slug, None)


# Matches the mockup's tool-status blue (distinct from the lime brand accent
# and from the green/red success/failure colors, so a tool call reads as its
# own category of line rather than competing with either).
_TOOL_COLOR = "#6fb7d9"

# Minimum interval between Markdown() re-parses of the accumulating reply.
# Markdown.__init__ parses eagerly, so calling it once per SSE delta re-walks
# the WHOLE reply so far every time — measured 11.3s of CPU for a 1000-delta,
# 70KB answer, independent of Live's own refresh_per_second (that throttles
# the terminal redraw, not how often this constructs a new Markdown object
# to hand it). This throttles the construction itself; the final state is
# always flushed once more on stream_end/error so the visible answer is
# never behind what was actually received.
_MARKDOWN_REFRESH_INTERVAL = 1 / 12


def _send_turn(console: Console, tenant: str | None, message: str, conversation_uuid: str | None) -> str | None:
    try:
        response = _open_ask_stream(tenant, message, conversation_uuid)
    except AskError as e:
        console.print(f"[red]ERRORE:[/red] {escape(_safe(str(e)))}")
        return conversation_uuid

    new_uuid = conversation_uuid
    accumulated = ""

    with response:
        _warn_if_deprecated(response.headers)
        header = response.headers.get("X-Lia-Conversation")
        if isinstance(header, str) and UUID_RE.match(header):
            new_uuid = header

        # tool_id -> tool_name, so the tool_result event (which carries no
        # name, only {tool_id, success, error} — see SafeSSEAdapter's own
        # redaction of this event's real payload) can still say WHAT
        # finished, not just that something did.
        pending_tools: dict[str, str] = {}

        with Live(Markdown(""), console=console, refresh_per_second=12, vertical_overflow="visible") as live:
            last_render = 0.0
            try:
                for event_type, data in _iter_sse(response):
                    if event_type == "text_delta":
                        delta = data.get("delta")
                        if isinstance(delta, str):
                            accumulated += _safe(delta)
                            now = time.monotonic()
                            if now - last_render >= _MARKDOWN_REFRESH_INTERVAL:
                                live.update(Markdown(accumulated, hyperlinks=False))
                                last_render = now
                    elif event_type == "tool_call":
                        tool_id = data.get("tool_id")
                        tool_name = data.get("tool_name")
                        if isinstance(tool_id, str) and isinstance(tool_name, str):
                            pending_tools[tool_id] = tool_name
                            # console.print() while a Live is active is
                            # supported (rich suspends the live region,
                            # prints above it, resumes) — this is a log
                            # line, not something this Live tracks, since
                            # each tool call needs its OWN line rather than
                            # overwriting the one before it.
                            console.print(f"  [{_TOOL_COLOR}]◐[/{_TOOL_COLOR}] {escape(_safe_line(tool_name))}...")
                    elif event_type == "tool_result":
                        tool_id = data.get("tool_id")
                        name = pending_tools.pop(tool_id, tool_id) if isinstance(tool_id, str) else "?"
                        if data.get("success", True):
                            console.print(f"  [green]✓[/green] {escape(_safe_line(str(name)))}")
                        else:
                            # error, unlike name/tool_name, is free-form
                            # diagnostic text rather than a single-word
                            # label — _safe() (newlines kept) is the right
                            # tool here, matching the ERROR: prints below,
                            # not _safe_line().
                            error = data.get("error")
                            suffix = f": {escape(_safe(str(error)))}" if error else ""
                            console.print(f"  [red]✗[/red] {escape(_safe_line(str(name)))}{suffix}")
                    elif event_type == "error":
                        live.update(Markdown(accumulated, hyperlinks=False))
                        console.print(f"[red]ERRORE:[/red] {escape(_safe(str(data.get('message', 'unknown error'))))}")
                    elif event_type == "stream_end":
                        break
            except (OSError, TimeoutError) as e:
                live.update(Markdown(accumulated, hyperlinks=False))
                console.print(f"[red]ERRORE:[/red] connessione persa: {escape(_safe(str(e)))}")
            else:
                # Final flush: the throttle above may have left the last
                # delta or two un-rendered even though they were received.
                live.update(Markdown(accumulated, hyperlinks=False))

    # Persisted even after an error above: the server already committed
    # whatever it streamed before failing, and the next line in the REPL
    # should continue that thread, not silently orphan it into a new one —
    # same reasoning as cmd_ask's identical persist-on-failure step.
    if tenant and isinstance(new_uuid, str) and UUID_RE.match(new_uuid):
        _write_last_conversation(tenant, new_uuid)

    return new_uuid
