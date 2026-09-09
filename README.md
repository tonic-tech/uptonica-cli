# uptonica

[![PyPI](https://img.shields.io/pypi/v/uptonica)](https://pypi.org/project/uptonica/)
[![License](https://img.shields.io/pypi/l/uptonica)](https://github.com/tonic-tech/uptonica-cli/blob/main/LICENSE)

The Uptonica command-line client for developers, agencies, and anyone
automating their workspace — one binary, scope decided by the token you
configure, not by which command you type.

## Install

```bash
pip install uptonica   # or: uv tool install uptonica
```

## Get a token

Go to **Settings -> API Tokens** in your Uptonica workspace
(`https://app.uptonica.com/account/api-tokens`), pick "Terminal access",
choose which workspace(s) and areas it should reach, and copy it.

```bash
uptonica config set-token
```

## Use it

```bash
uptonica whoami
uptonica tenant use my-store          # set a default so you don't repeat --tenant
uptonica tools --search contact       # find a tool without scrolling the whole catalog
uptonica tools --show catalog.product.get   # see its full description + parameters
uptonica call catalog.stats.summary
uptonica call crm.contact.create_task --arg title="Follow up" --arg priority=2
uptonica ask "quanto ho venduto questo mese?"
```

If your token reaches more than one workspace, `--tenant` on any command
overrides the default for that one call.

A write tool that needs confirmation returns a `confirmation_token`; re-run
the same command with `--confirm <token>` within ~15 minutes to actually
apply it. `--dry-run` previews a write without executing it. A tool name
that doesn't exist gets a "did you mean" suggestion instead of a bare error.

## Ask in natural language

`uptonica ask "<message>"` skips the tool catalog entirely — Lia (the same
assistant behind chat and the mobile app) reads your request and picks the
tool(s) herself, with the same per-tool permissions she already has
everywhere else. A workspace's conversation continues across calls by
default, so a follow-up like `uptonica ask "sì"` answers whatever Lia just
asked (she always asks before a write, same as chat). Use `--new` to start a
fresh thread, or `--conversation <uuid>` to pick a specific one.

Run `uptonica` with no arguments (or `uptonica ask` with no message) to open
an interactive session instead — one process for the whole conversation,
with `/workspace` to list or switch workspaces, `/new` for a fresh thread,
and `/help` for the rest. `Ctrl+D` exits. `Enter` sends a message;
`Option+Enter` (`Alt+Enter`) starts a new line without sending. Every tool
Lia calls shows up live (`◐ catalog.stats.summary...` → `✓`), so a slow
answer never looks like it's just hung.

## For AI agents

If you're an agent (Claude Code, Codex, or anything else) about to drive this
CLI on someone's behalf, read [AGENTS.md](AGENTS.md) first — discovery order,
how to handle a multi-workspace token, and the dry-run/confirm flow for
writes.

**Claude Code users**: this repo is also a plugin marketplace, so you don't
need to clone anything to get that guidance loaded automatically:

```
/plugin marketplace add tonic-tech/uptonica-cli
/plugin install uptonica@uptonica-cli
```

## Status

Actively developed — v0.2.2, built directly on the same tool API that powers
Uptonica's in-app assistant. See the repo issues for what's still open.
