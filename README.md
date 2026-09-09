# uptonica

The Uptonica command-line client. One binary — what you can do is decided by
the token you configure, not by which command you type.

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
```

If your token reaches more than one workspace, `--tenant` on any command
overrides the default for that one call.

A write tool that needs confirmation returns a `confirmation_token`; re-run
the same command with `--confirm <token>` within ~15 minutes to actually
apply it. `--dry-run` previews a write without executing it. A tool name
that doesn't exist gets a "did you mean" suggestion instead of a bare error.

## For AI agents

If you're an agent (Claude Code, Codex, or anything else) about to drive this
CLI on someone's behalf, read [AGENTS.md](AGENTS.md) first — discovery order,
how to handle a multi-workspace token, and the dry-run/confirm flow for
writes.

## Status

Early — v0.1.0, built against the existing Uptonica operator/tool API. See
the repo issues for what's still open.
