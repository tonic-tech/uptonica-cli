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
uptonica tools
uptonica call catalog.stats.summary
uptonica call crm.contact.create_task --tenant my-store --arg title="Follow up" --arg priority=2
```

A write tool that needs confirmation returns a `confirmation_token`; re-run
the same command with `--confirm <token>` within ~15 minutes to actually
apply it. `--dry-run` previews a write without executing it.

## Status

Early — v0.1.0, built against the existing Uptonica operator/tool API. See
the repo issues for what's still open.
