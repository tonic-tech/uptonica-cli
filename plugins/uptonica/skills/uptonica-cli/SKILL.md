---
name: uptonica-cli
description: Use the `uptonica` command-line client to read or write data in a customer's Uptonica workspace from a terminal/agent context — discover the token's tool catalog, call a tool or ask Lia in natural language, handle a multi-workspace token, and confirm a write correctly. Use when a task needs to inspect or change Uptonica data (contacts, deals, quotes, catalog, ads, content), or when asked "what can uptonica do", "call an uptonica tool", "ask uptonica", "list my uptonica workspaces".
---

# Uptonica CLI

`uptonica` is one binary whose scope is decided by the API token you configure,
not by which subcommand you type. Everything you can do lives in a
self-describing tool catalog — **discover it, don't guess it.**

Install and get a token: see this repo's README.md. This doc is about using
it well once you have one.

## Before anything else

```bash
uptonica whoami            # your identity + which workspace(s) this token reaches
```

## Discover, then invoke — never guess a tool name

Tool names, their arguments, and what they return live in the catalog, not in
this doc — they change as the product ships.

```bash
uptonica tools --search contact              # find candidates by keyword
uptonica tools --area crm                    # or browse a whole module
uptonica tools --show catalog.product.get    # full description + parameter schema, before you call it
```

Calling a name that doesn't exist prints `ERROR: HTTP 404 ...` and, when the
server can tell you meant a real tool, an extra "Did you mean: ..." line
under it — the suggestion is a bonus, not guaranteed, so don't assume one
will always be there to lean on.

## Calling a tool

```bash
uptonica call <tool> [--tenant <slug|id>] --arg key=value [--arg key=value ...]
```

- `--arg key=value` auto-types the value: `true`/`false` → bool, `123`/`4.5` →
  number, else a string. A value that reads as boolean/null-ish but isn't
  valid JSON (`yes`/`no`/`on`/`off`/`null`/`none`/`nil`) is **rejected**, not
  silently forwarded — spell it `true`/`false`
  explicitly, or use `--arg-string` to force a literal string.
- For a nested/complex body, pass it all at once:
  `--json '{"items": [...]}'` (exclusive with `--arg`/`--arg-string`).
- Read the tool's own schema (`uptonica tools --show <tool>`) for the argument
  names it actually expects — don't infer them from the tool name.
- A `confirmation_token` (see Writes, below) must go through `--confirm`, not
  `--arg confirmation_token=...` or inside `--json` — both are rejected.

## Asking in natural language — the simpler alternative to `call`

```bash
uptonica ask "quanto ho venduto questo mese?" --tenant my-store
```

For a task that doesn't need programmatic output, `ask` skips tool discovery
entirely: Lia (the same engine behind chat and the mobile app) reads the
message and picks the tool(s) herself. Prefer `ask` when you want an answer,
not structured data to parse further — prefer `call` when you need JSON
back, or need one specific tool to run and nothing else.

- **Permission scope isn't confirmed to match a restricted token.** The
  request carries only the bearer token + tenant header — the client code
  doesn't show whether a read-only or area-scoped token actually constrains
  what Lia can do through `ask` the way it constrains `call`. If you're
  relying on a restricted token specifically to prevent certain actions,
  don't assume `ask` respects that restriction until it's confirmed
  server-side — prefer `call` for that case.
- **Conversation continues by default, per workspace — but only once a
  tenant actually resolves** (`--tenant`, `UPTONICA_TENANT`, or a stored
  default). With none of those, nothing persists and nothing continues:
  every call starts a fresh thread, silently. When a tenant does resolve, a
  follow-up like `uptonica ask "sì"` answers whatever Lia just asked in the
  *last* turn on that tenant — she always asks for explicit confirmation
  before a write, the same way chat does. `--new` starts a fresh thread;
  `--conversation <uuid>` picks a specific one (if you pass both, the
  explicit `--conversation` silently wins over `--new`).
- **The risk this creates**: switching to an unrelated task on the same
  tenant without `--new` means a bare confirmation reply can land on a
  stale pending write from an earlier, unrelated turn — pass `--new`
  whenever the task changes, don't rely on remembering what the last turn
  left pending.
- **No `dry_run`/`confirmation_token`/`--confirm` here** — `ask` doesn't
  take those flags at all. Confirmation for a write happens
  conversationally (Lia asks in her reply; you answer in the next `ask`
  call), not through the tool-level mechanism described under Writes,
  below, which is `call`-specific.
- **Output is plain streamed text, not JSON** — see Parsing output, below.
- Tenant resolution is the same as `call` (`--tenant` → `UPTONICA_TENANT` →
  stored default), including the same `[NOTE]` stderr line when it falls
  back to a default.
- Message cap: 10,000 characters.
- **A failed turn (server-side error, or the connection dropping
  mid-stream) exits `1`** — for `ask` specifically, that's a normal,
  anticipated failure path, not the "the CLI didn't anticipate this"
  backstop `1` means everywhere else in this doc. See Exit codes, below.

## Multi-workspace tokens: name the tenant

A token can reach more than one workspace. Resolution for `call`, narrowest
wins: `--tenant` flag → `UPTONICA_TENANT` env var → the stored default
(`uptonica tenant use <slug>`). Falling back to the stored default is silent
everywhere except the CLI prints a `[NOTE] using default workspace '<slug>'`
line to stderr when it applies — that's your signal a call is about to run
somewhere the command line itself doesn't say. On anything that writes,
prefer an explicit `--tenant` over trusting a default that may be stale from
an earlier session or a different task.

## Writes via `call`: most tools fire on the first call — check before you assume a safety net

This section is about `call`. If you're using `ask` instead, its writes are
confirmed conversationally — see the section above, not this one.

**A write tool executes immediately by default.** `--dry-run` and confirmation
are opt-in *per tool*, declared server-side — the CLI itself adds no gate of
its own. Most write tools in the catalog declare neither. Before calling any
write tool for the first time on real data:

```bash
uptonica tools --show <tool>    # does it list a `dry_run` and/or `confirmation_token` parameter?
```

- **If it declares `dry_run`**: `--dry-run` previews without a side effect —
  `uptonica call <tool> --arg ... --dry-run`.
- **If it declares `confirmation_token`**: the first real call (no
  `--dry-run`) returns one instead of applying; re-run the same command with
  `--confirm <token>` within ~15 minutes to apply it. `--confirm` and
  `--dry-run` don't combine. Never invent or reuse a `confirmation_token`
  from a different call — it round-trips one specific pending write, not a
  generic "yes."
- **If it declares neither**: the call is live and final the moment you run
  it. There is no preview step to fall back on — double-check the arguments
  before you run it, the same care you'd give a command with no undo.

Don't assume every write tool behaves like the ones that happen to support
dry-run/confirm — check first, every time, for a tool you haven't called
before.

## Parsing output

`whoami`, `tools`, and `call` accept `--output json` — pass it explicitly on
every one of those you run programmatically. (The CLI already emits JSON
automatically whenever stdout isn't a terminal — true for every subprocess
call — but naming the flag makes the behavior explicit instead of depending
on TTY detection nobody reading the command can see.) `tenant show`/`tenant
use`/`tenant clear`, `config show`/`config set-token`/`config clear-token`,
and `ask` do **not** take `--output` in the position right after the
subcommand: `uptonica tenant show --output json` (or `uptonica ask "x"
--output json`) fails with "unrecognized arguments". Put before the
subcommand instead (`uptonica --output json tenant show` / `uptonica
--output json ask "x"`), it parses — but is silently ignored either way.
`config show` always prints JSON regardless; `tenant show` always prints a
bare slug; `ask` always streams plain text (Lia's answer, token by token),
never JSON, regardless of TTY. If you need something to parse
programmatically, use `call`, not `ask`.

Errors go to stderr with a plain `ERROR:` prefix. `[NOTE]`, `[WARNING]`,
`[DEPRECATION]`, and `[WILL APPLY]` prefixes on stderr are all advisory, not
a failure.

Exit codes, if you're branching on them: `0` ok · `2` bad input, or tool/
tenant not found · `3` auth — no token configured, or 401/403 (token
invalid/expired, or it doesn't reach that workspace; only the latter is
fixed by re-minting one, the former needs `uptonica config set-token`) ·
`4` other 4xx or rate limit · `5` server error · `124` network/timeout ·
`130` interrupted (Ctrl-C) · `1` unexpected/unhandled failure — everywhere
except `ask`, this means something the CLI didn't anticipate, not a normal
error path.

**`ask` is the one exception to that `1`.** Once the response headers come
back, `ask` reports every failure — a server-sent error mid-answer, or the
connection dropping mid-stream — as exit `1`, not `124` or anything else.
For `ask`, `1` is a normal, expected failure code, not just a backstop; the
`2`/`3`/`4`/`5`/`124` codes above still apply, but only to failures before
the stream starts (bad input, auth, network/timeout reaching the server at
all).

## What NOT to do

- Don't hardcode a tool name or its arguments from memory or from an example
  in this doc — the catalog is the source of truth and it changes.
- Don't script a write loop against a tool you haven't checked with `tools
  --show` first — most write tools have no dry-run or confirm step at all,
  so a bad loop applies on iteration one, not on some later "real" pass.
- Don't pass a token via `--token` (lands in shell history and `ps` output) —
  use `uptonica config set-token` interactively, or `--token-stdin`.
- Don't assume a single-workspace token — check `whoami` before any
  cross-tenant-shaped task ("do this for all my workspaces").
- Don't send an `ask` confirmation reply (e.g. `"sì"`) into a new, unrelated
  task on the same tenant without `--new` first — it can confirm a stale
  pending write from an earlier turn instead of doing nothing.
