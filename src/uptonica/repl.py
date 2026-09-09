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

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.history import FileHistory
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown

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


def run_repl(initial_tenant: str | None, initial_conversation: str | None) -> None:
    """Blocks until the user exits (Ctrl+D, `/exit`, `/quit`, or Ctrl+C at
    the prompt — mid-turn Ctrl+C is left to the normal KeyboardInterrupt
    path in main())."""
    console = Console(highlight=False)
    # File-backed so arrow-up recalls earlier turns across sessions too, the
    # same convenience `tenant use` gives the default workspace. 0600 via
    # atomic_write_text elsewhere in this package is for secrets specifically;
    # this file holds only the user's own typed messages, not credentials.
    history_path = config_dir() / "repl_history"
    session: PromptSession[str] = PromptSession(
        history=FileHistory(str(history_path)),
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
    return f"[{tenant or 'nessun workspace'}] > "


def _print_banner(console: Console, tenant: str | None) -> None:
    console.print(f"[bold]uptonica[/bold] v{__version__} — parla con Lia. /help per i comandi, Ctrl+D per uscire.")
    if tenant:
        console.print(f"workspace: [cyan]{tenant}[/cyan]")
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

    console.print(f"[red]comando sconosciuto:[/red] {_safe(cmd)} — /help per la lista")
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
    except SystemExit:
        # _request() already printed its own ERROR/exit-coded message via
        # err() before calling sys.exit() — that path is shared with every
        # other command, and duplicating its error text here would just
        # repeat it. The REPL survives a failed lookup; it does not survive
        # sys.exit(), so it's caught rather than left to propagate.
        return None

    tenants = data.get("tenants", [])

    if not arg:
        if not tenants:
            console.print("[yellow]nessun workspace raggiungibile da questo token[/yellow]")
            return None
        console.print("[bold]workspace raggiungibili[/bold]")
        for t in tenants:
            console.print(f"  {_safe(t.get('slug', '?'))}  {_safe(t.get('name', ''))}")
        return None

    match = next(
        (t for t in tenants if t.get("slug") == arg or str(t.get("id")) == arg),
        None,
    )
    if match is None:
        console.print(f"[red]'{_safe(arg)}' non è un workspace raggiungibile da questo token.[/red] /workspace per la lista")
        return None

    slug = match.get("slug")
    if not isinstance(slug, str):
        console.print("[red]il workspace trovato non ha uno slug utilizzabile — non dovrebbe succedere lato server.[/red]")
        return None

    console.print(f"workspace: [cyan]{_safe(slug)}[/cyan] — nuovo thread")
    # A conversation UUID belongs to one tenant (see OperatorTurnController's
    # own pair check server-side); carrying the old one across a workspace
    # switch would just get refused as conversation_not_found on the next
    # turn. Starting fresh here is what that refusal would have forced anyway.
    return (slug, None)


def _send_turn(console: Console, tenant: str | None, message: str, conversation_uuid: str | None) -> str | None:
    try:
        response = _open_ask_stream(tenant, message, conversation_uuid)
    except AskError as e:
        console.print(f"[red]ERRORE:[/red] {_safe(str(e))}")
        return conversation_uuid

    new_uuid = conversation_uuid
    accumulated = ""

    with response:
        _warn_if_deprecated(response.headers)
        header = response.headers.get("X-Lia-Conversation")
        if isinstance(header, str) and UUID_RE.match(header):
            new_uuid = header

        with Live(Markdown(""), console=console, refresh_per_second=12, vertical_overflow="visible") as live:
            try:
                for event_type, data in _iter_sse(response):
                    if event_type == "text_delta":
                        delta = data.get("delta")
                        if isinstance(delta, str):
                            accumulated += _safe(delta)
                            live.update(Markdown(accumulated))
                    elif event_type == "error":
                        live.update(Markdown(accumulated))
                        console.print(f"[red]ERRORE:[/red] {_safe(str(data.get('message', 'unknown error')))}")
                    elif event_type == "stream_end":
                        break
            except (OSError, TimeoutError) as e:
                live.update(Markdown(accumulated))
                console.print(f"[red]ERRORE:[/red] connessione persa: {_safe(str(e))}")

    # Persisted even after an error above: the server already committed
    # whatever it streamed before failing, and the next line in the REPL
    # should continue that thread, not silently orphan it into a new one —
    # same reasoning as cmd_ask's identical persist-on-failure step.
    if tenant and isinstance(new_uuid, str) and UUID_RE.match(new_uuid):
        _write_last_conversation(tenant, new_uuid)

    return new_uuid
