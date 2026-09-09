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
from prompt_toolkit.history import FileHistory
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


class _SlashCompleter(Completer):
    """Completes slash commands only — a line not starting with `/` is a
    message to Lia, and offering catalog/tool-name completion for THAT would
    need a network round-trip on every keystroke. Out of scope for v1."""

    def get_completions(self, document, complete_event):  # noqa: ARG002
        text = document.text_before_cursor
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


def run_repl(initial_tenant: str | None, initial_conversation: str | None) -> None:
    """Blocks until the user exits (Ctrl+D, `/exit`, `/quit`, or Ctrl+C at
    the prompt — mid-turn Ctrl+C is left to the normal KeyboardInterrupt
    path in main())."""
    console = Console(highlight=False)
    session: PromptSession[str] = PromptSession(
        history=FileHistory(_history_path()),
        completer=_SlashCompleter(),
    )

    tenant = initial_tenant
    conversation_uuid = initial_conversation

    _print_banner(console, tenant)

    while True:
        try:
            text = session.prompt(_prompt_label(tenant))
        except EOFError:
            break
        except KeyboardInterrupt:
            continue  # Ctrl+C on an empty prompt clears the line, doesn't exit — matches most shells

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


def _prompt_label(tenant: str | None) -> str:
    # Passed to prompt_toolkit's session.prompt() as a plain string, which it
    # renders literally (not through rich's markup parser, and not through
    # any ANSI interpretation unless wrapped in HTML()/ANSI()) — so unlike
    # every console.print() call below, this one needs no escaping.
    return f"[{tenant or 'nessun workspace'}] > "


def _print_banner(console: Console, tenant: str | None) -> None:
    console.print(f"[bold]uptonica[/bold] v{__version__} — parla con Lia. /help per i comandi, Ctrl+D per uscire.")
    if tenant:
        console.print(f"workspace: [cyan]{escape(_safe(tenant))}[/cyan]")
    else:
        console.print("[yellow]nessun workspace selezionato[/yellow] — usa /workspace per sceglierne uno")


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
    console.print(f"[red]comando sconosciuto:[/red] {escape(_safe(cmd))} — /help per la lista")
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
        "qualsiasi altra riga è una domanda per Lia."
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
            slug = escape(_safe(str(t.get("slug", "?"))))
            name = escape(_safe(str(t.get("name", ""))))
            console.print(f"  {slug}  {name}")
        return None

    match = next(
        (t for t in tenants if t.get("slug") == arg or str(t.get("id")) == arg),
        None,
    )
    if match is None:
        console.print(f"[red]'{escape(_safe(arg))}' non è un workspace raggiungibile da questo token.[/red] /workspace per la lista")
        return None

    slug = match.get("slug")
    if not isinstance(slug, str):
        console.print("[red]il workspace trovato non ha uno slug utilizzabile — non dovrebbe succedere lato server.[/red]")
        return None

    console.print(f"workspace: [cyan]{escape(_safe(slug))}[/cyan] — nuovo thread")
    # A conversation UUID belongs to one tenant (see OperatorTurnController's
    # own pair check server-side); carrying the old one across a workspace
    # switch would just get refused as conversation_not_found on the next
    # turn. Starting fresh here is what that refusal would have forced anyway.
    return (slug, None)


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
