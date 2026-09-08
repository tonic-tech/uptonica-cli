"""Output helpers — JSON for scripts, a short human line for a terminal."""

from __future__ import annotations

import json
import sys
from typing import Any, Callable


def err(msg: str) -> None:
    print(msg, file=sys.stderr)


def banner(msg: str) -> None:
    print(msg, file=sys.stderr)


def emit(data: Any, *, explicit: str | None, pretty_renderer: Callable[[Any], None] | None) -> None:
    """`--output json` (or piped stdout) prints machine JSON; otherwise, if a
    pretty renderer exists for this payload shape, use it — else fall back to
    JSON anyway rather than guessing at a table format nobody asked for."""
    want_json = explicit == "json" or (explicit is None and not sys.stdout.isatty())
    if want_json or pretty_renderer is None:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return
    pretty_renderer(data)
