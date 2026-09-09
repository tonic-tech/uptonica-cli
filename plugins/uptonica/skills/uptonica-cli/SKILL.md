---
name: uptonica-cli
description: Use the `uptonica` command-line client to read or write data in a customer's Uptonica workspace from a terminal/agent context — discover the token's tool catalog, call a tool, handle a multi-workspace token, and confirm a write correctly. Use when a task needs to inspect or change Uptonica data (contacts, deals, quotes, catalog, ads, content), or when asked "what can uptonica do", "call an uptonica tool", "list my uptonica workspaces".
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

## Multi-workspace tokens: name the tenant

A token can reach more than one workspace. Resolution for `call`, narrowest
wins: `--tenant` flag → `UPTONICA_TENANT` env var → the stored default
(`uptonica tenant use <slug>`). Falling back to the stored default is silent
everywhere except the CLI prints a `[NOTE] using default workspace '<slug>'`
line to stderr when it applies — that's your signal a call is about to run
somewhere the command line itself doesn't say. On anything that writes,
prefer an explicit `--tenant` over trusting a default that may be stale from
an earlier session or a different task.

## Writes: most tools fire on the first call — check before you assume a safety net

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
use`/`tenant clear` and `config show`/`config set-token`/`config
clear-token` do **not** take `--output` at all: `uptonica tenant show
--output json` fails with "unrecognized arguments", and `uptonica --output
json tenant show` parses but is silently ignored. `config show` always
prints JSON regardless; `tenant show` always prints a bare slug.

Errors go to stderr with a plain `ERROR:` prefix. `[NOTE]`, `[WARNING]`,
`[DEPRECATION]`, and `[WILL APPLY]` prefixes on stderr are all advisory, not
a failure.

Exit codes, if you're branching on them: `0` ok · `2` bad input, or tool/
tenant not found · `3` auth — no token configured, or 401/403 (token
invalid/expired, or it doesn't reach that workspace; only the latter is
fixed by re-minting one, the former needs `uptonica config set-token`) ·
`4` other 4xx or rate limit · `5` server error · `124` network/timeout ·
`130` interrupted (Ctrl-C) · `1` unexpected/unhandled failure — this last one
means something the CLI didn't anticipate, not a normal error path.

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
