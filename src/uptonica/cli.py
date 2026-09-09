"""
uptonica — the Uptonica command-line client.

What you can do is decided by the token you configure, not by which
command you type: a plain token reaches your own workspace(s); a token
minted with broader scope reaches whatever workspaces and tool areas you
picked when you made it — including several client workspaces if you
manage more than one (agencies get this automatically, scoped to the
clients they actually manage).

Get a token:  https://app.uptonica.com/account/api-tokens
Store it:     uptonica config set-token

Run `uptonica <command> --help` for details on a specific command.
Exit codes: 0 ok | 2 usage error | 3 auth/forbidden | 4 4xx from the API | 5 5xx | 124 timeout
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import math
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterator

import re

from uptonica.output import banner, emit, err
from uptonica.secrets import (
    CredentialError,
    atomic_write_text,
    clear_token,
    config_dir,
    file_store_path_hint,
    looks_like_sanctum_token,
    resolve,
    store_token,
)

__version__ = "0.1.2"
TOKEN_ENV_VAR = "UPTONICA_TOKEN"
KEYCHAIN_ITEM = "uptonica-token"
TOOL_NAME_RE = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)*$")


def _resolve_base() -> str:
    """The API base is `https://app.uptonica.com` unless overridden — and an
    override is a real credential-routing decision, not a cosmetic one: every
    request sends the bearer token to whatever host this resolves to. So it is
    validated (https, unless explicitly allowed insecure for local dev) and
    ANNOUNCED on stderr — an env var is a low-bar attack surface on a
    customer's own machine (direnv, a Makefile, a compromised shell rc), and
    the previous cut sent the token to a silently-overridden host with no
    signal the user could have noticed."""
    raw = os.environ.get("UPTONICA_API_BASE")
    if not raw:
        return "https://app.uptonica.com"

    parsed = urllib.parse.urlparse(raw)
    if not parsed.netloc:
        err(f"ERROR: UPTONICA_API_BASE is not a valid URL: {raw!r}")
        sys.exit(2)
    if parsed.scheme != "https" and os.environ.get("UPTONICA_ALLOW_INSECURE_BASE") != "1":
        err(f"ERROR: UPTONICA_API_BASE must be https:// (got scheme {parsed.scheme or '(none)'!r}).")
        err("  Set UPTONICA_ALLOW_INSECURE_BASE=1 only for local development against a non-TLS server.")
        sys.exit(2)

    banner(f"[NOTE] UPTONICA_API_BASE override — sending your token to {parsed.scheme}://{parsed.netloc}")
    return raw.rstrip("/")


BASE = _resolve_base()
API = f"{BASE}/api/v1/operator"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect rather than following it.

    `urllib`'s default handler forwards `Authorization` to the redirect
    target — including a different host and a downgraded http:// scheme
    (verified: CPython's `redirect_request` strips only `Content-Length` and
    `Content-Type`). A single 302 from a compromised proxy hop, a stale
    redirect rule, or a captive portal would hand a live bearer token to
    whatever host issued it. The platform's own hard rule for server-side
    calls is `allow_redirects: false` for exactly this reason; a client
    holding the same kind of token needs the same rule.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # None => urlopen raises HTTPError for the 3xx instead of following it


_OPENER = urllib.request.build_opener(_NoRedirect)


def _safe(s: Any, limit: int = 4000) -> str:
    """Strip control/escape characters from a server-derived string before it
    reaches a terminal. A `Warning` header or a tool label is server-controlled
    text; an embedded ANSI/OSC sequence could rewrite prior output lines, hide
    text, or forge a plausible status line. `--output json` already escapes
    these via `json.dumps`; this covers the pretty-print path, which is the
    one a human is actually watching."""
    text = str(s)
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text)
    return text if len(text) <= limit else text[:limit] + "...[truncated]"


def _token() -> str:
    try:
        return resolve(env_var=TOKEN_ENV_VAR, keychain_item=KEYCHAIN_ITEM, validator=looks_like_sanctum_token)
    except CredentialError as e:
        err(f"ERROR: {e}")
        sys.exit(3)


DEFAULT_TENANT_PATH = config_dir() / "default_tenant"

# What a tenant slug/id may look like. Applied both when STORING it (a
# malformed value here is unusable later) and after READING it back from
# disk — the file is ours, but treating a value we wrote ourselves as trusted
# just because we wrote it is how a bug in an earlier version becomes a
# standing hazard in every later one. This also bounds what can ever reach
# `X-Uptonica-Tenant` from this path: no newlines, no header-folding
# whitespace, no surprises.
TENANT_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def _read_default_tenant() -> str | None:
    try:
        raw = DEFAULT_TENANT_PATH.read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        # A directory where a file should be, a permissions error, a
        # decoding failure — any of these must fall back to "no default" and
        # nothing else, since this is called from `whoami` and every `call`.
        # An exception here would take out both.
        return None
    if not raw or not TENANT_SLUG_RE.match(raw):
        return None
    return raw


def _write_default_tenant(value: str) -> None:
    atomic_write_text(DEFAULT_TENANT_PATH, value.strip() + "\n", mode=0o600)


def _clear_default_tenant() -> bool:
    try:
        DEFAULT_TENANT_PATH.unlink()
        return True
    except FileNotFoundError:
        return False


# `ask` continues the last thread per workspace by default, the same way a
# chat tab does — otherwise every follow-up ("sì", "e per il mese scorso?")
# would need its own --conversation flag. One file per tenant, not one file
# total: a token spanning several workspaces must not reply to yesterday's
# "sì" against a DIFFERENT client's pending write.
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _conversation_state_path(tenant: str) -> Path:
    # The tenant string here may be a --tenant flag value, which (unlike the
    # stored default) is never validated against TENANT_SLUG_RE — an operator
    # could type anything. A slug-shaped value gets a readable filename; any-
    # thing else falls back to a hash, so this can never become a path outside
    # config_dir() (no `..`, no `/`) regardless of what was typed.
    safe = tenant if TENANT_SLUG_RE.match(tenant) else hashlib.sha256(tenant.encode("utf-8")).hexdigest()
    return config_dir() / f"conversation_{safe}"


def _read_last_conversation(tenant: str) -> str | None:
    try:
        raw = _conversation_state_path(tenant).read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return None
    if not raw or not UUID_RE.match(raw):
        return None
    return raw


def _write_last_conversation(tenant: str, uuid: str) -> None:
    atomic_write_text(_conversation_state_path(tenant), uuid.strip() + "\n", mode=0o600)


def _tenant_with_source(args: argparse.Namespace) -> tuple[str | None, str | None]:
    """Same resolution as `_tenant()`, but also says WHERE the value came
    from — so a caller can warn when a workspace was chosen by state the
    person didn't type in this command (the stored default), as opposed to
    something explicit (`--tenant`, `UPTONICA_TENANT`) that needs no such
    warning."""
    explicit = getattr(args, "tenant", None)
    if explicit:
        return explicit, "flag"
    env = os.environ.get("UPTONICA_TENANT")
    if env:
        return env, "env"
    default = _read_default_tenant()
    if default:
        return default, "default"
    return None, None


def _tenant(args: argparse.Namespace) -> str | None:
    # --tenant wins (explicit, one call) over UPTONICA_TENANT (explicit, this
    # shell) over the stored default (implicit, set once with `tenant use`) —
    # each level is a deliberately narrower override of the one below it.
    return _tenant_with_source(args)[0]


class AmbiguousValue(ValueError):
    """A value that reads as boolean/null but isn't spelled as JSON true/false."""


_AMBIGUOUS_BOOLISH = {"yes", "no", "on", "off", "null", "none", "nil"}


def _coerce(v: str) -> Any:
    """"49.90"->float, true/false->bool, integers that round-trip exactly->int,
    else the raw string. Raises AmbiguousValue for yes/no/on/off/null-shaped
    input rather than silently forwarding a truthy string server-side."""
    low = v.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low in _AMBIGUOUS_BOOLISH:
        raise AmbiguousValue(
            f"'{v}' reads as a boolean/null but isn't valid JSON — write true/false "
            f"explicitly, or use --arg-string for a literal string."
        )
    try:
        i = int(v)
    except ValueError:
        pass
    else:
        if str(i) == v:
            return i
        return v
    try:
        f = float(v)
    except ValueError:
        return v
    return f if math.isfinite(f) else v


def _suggest_tool_names(bad_name: str, token: str) -> list[str]:
    """Best-effort 'did you mean' for a tool_not_found response.

    A failure here (network hiccup, an unexpected payload shape) must never
    mask or replace the real error the caller is already reporting — so this
    swallows everything and returns no suggestions rather than raising.

    Takes the ALREADY-RESOLVED token rather than calling `_token()` again:
    that would re-run the Keychain/file lookup a second time, and on
    failure raises via `sys.exit()` — a `SystemExit`, which is a
    `BaseException` and so is NOT caught by `except Exception` below. That
    let a resolver hiccup here escape and replace the real error (a 404)
    with an unrelated exit code, which is exactly the outcome this
    function's docstring promises never happens.
    """
    try:
        req = urllib.request.Request(
            f"{API}/tools",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json",
                     "User-Agent": f"uptonica-cli/{__version__}"},
        )
        with _OPENER.open(req, timeout=15) as r:
            catalog = json.loads(r.read())
        return _closest_names(bad_name, catalog.get("tools", []))
    except Exception:
        return []


def _request(method: str, path: str, *, json_body: dict | None = None,
              tenant: str | None = None, timeout: int = 60,
              tool_name_for_suggestions: str | None = None) -> Any:
    token = _token()
    url = f"{API}{path}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json",
               "User-Agent": f"uptonica-cli/{__version__}"}
    if tenant:
        headers["X-Uptonica-Tenant"] = tenant
    data = None
    if json_body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(json_body).encode("utf-8")

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            _warn_if_deprecated(r.headers)
            payload = r.read()
            if "json" in r.headers.get("Content-Type", ""):
                return json.loads(payload)
            return payload.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        if 300 <= e.code < 400:
            # _NoRedirect refused to follow it — surfaced here as an HTTPError
            # rather than silently falling through with no exit(), which
            # would have returned None to a caller expecting a dict.
            err(f"ERROR: the API tried to redirect this request to {e.headers.get('Location', '(unknown)')}.")
            err("  Refusing to forward your token to a different host. If this is expected "
                "(e.g. a load balancer change), report it — this should not happen in normal use.")
            sys.exit(4)
        _warn_if_deprecated(e.headers)
        body = e.read().decode("utf-8", errors="replace")
        detail = _safe(body[:600])
        j: dict | None = None
        try:
            j = json.loads(body)
            detail = _safe(j.get("error_description") or j.get("error") or detail)
        except (ValueError, TypeError):
            pass
        err(f"ERROR: HTTP {e.code} {method} {path}\n  {detail}")
        if (
            e.code == 404
            and tool_name_for_suggestions
            and isinstance(j, dict)
            and j.get("error") == "tool_not_found"
        ):
            suggestions = _suggest_tool_names(tool_name_for_suggestions, token)
            if suggestions:
                err("  Did you mean: " + ", ".join(_safe(s) for s in suggestions) + "?")
        if 400 <= e.code < 500:
            sys.exit(3 if e.code in (401, 403) else (2 if e.code in (400, 404) else 4))
        sys.exit(5)
    except urllib.error.URLError as e:
        err(f"ERROR: network {url}: {e}")
        sys.exit(124)
    except TimeoutError:
        err(f"ERROR: timeout on {url}")
        sys.exit(124)


_deprecation_warned = False


def _warn_if_deprecated(headers) -> None:
    """Surface the server's RFC 8594 Sunset/Deprecation headers, once per run.

    This closes a gap the platform's own middleware comment calls out by
    name: a wildcard-shaped token gets served (not refused) during the
    deprecation window, but nothing downstream told the person holding it —
    "a CLI will not surface any of these on its own... that lives in
    another repo." This is that repo.
    """
    global _deprecation_warned
    if _deprecation_warned:
        return
    warning = headers.get("Warning")
    sunset = headers.get("Sunset")
    if warning or sunset:
        _deprecation_warned = True
        banner(f"[DEPRECATION] {_safe(warning) if warning else 'This token will stop being accepted.'}")
        if sunset:
            banner(f"  Sunset: {_safe(sunset)} — re-mint a token at https://app.uptonica.com/account/api-tokens")


def _iter_sse(response) -> Iterator[tuple[str, dict]]:
    """Parse a `text/event-stream` body into `(event_type, data)` pairs.

    Minimal on purpose: the server side ({@code SafeSSEAdapter}) only ever
    writes one `event:` line and one `data:` line per event, so there is no
    multi-line `data:` folding or `id:`/`retry:` handling to do here — a
    general SSE client would be solving a problem this specific server
    doesn't have.
    """
    event_type: str | None = None
    data_line: str | None = None
    while True:
        raw = response.readline()
        if not raw:
            return
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if line == "":
            if event_type is not None and data_line is not None:
                try:
                    yield event_type, json.loads(data_line)
                except json.JSONDecodeError:
                    pass
            event_type = None
            data_line = None
            continue
        if line.startswith("event:"):
            event_type = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_line = line[len("data:"):].strip()


def cmd_ask(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    tenant, tenant_source = _tenant_with_source(args)
    if tenant_source == "default":
        banner(f"[NOTE] using default workspace '{tenant}' (uptonica tenant use / --tenant to change)")

    conversation_uuid = args.conversation
    if not conversation_uuid and not args.new and tenant:
        conversation_uuid = _read_last_conversation(tenant)

    body: dict[str, Any] = {"message": args.message}
    if conversation_uuid:
        body["conversation_uuid"] = conversation_uuid

    token = _token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "text/event-stream",
        "Content-Type": "application/json",
        "User-Agent": f"uptonica-cli/{__version__}",
    }
    if tenant:
        headers["X-Uptonica-Tenant"] = tenant

    req = urllib.request.Request(
        f"{API}/turns", data=json.dumps(body).encode("utf-8"), headers=headers, method="POST",
    )

    try:
        # A generous read timeout: unlike every other endpoint this one runs
        # an LLM turn (possibly several tool calls deep) before the first
        # byte, not a bounded DB query.
        response = _OPENER.open(req, timeout=300)
    except urllib.error.HTTPError as e:
        if 300 <= e.code < 400:
            err(f"ERROR: the API tried to redirect this request to {e.headers.get('Location', '(unknown)')}.")
            err("  Refusing to forward your token to a different host.")
            sys.exit(4)
        _warn_if_deprecated(e.headers)
        raw_body = e.read().decode("utf-8", errors="replace")
        detail = _safe(raw_body[:600])
        try:
            j = json.loads(raw_body)
            detail = _safe(j.get("error_description") or j.get("error") or detail)
        except (ValueError, TypeError):
            pass
        err(f"ERROR: HTTP {e.code} POST /turns\n  {detail}")
        if 400 <= e.code < 500:
            sys.exit(3 if e.code in (401, 403) else (2 if e.code in (400, 404) else 4))
        sys.exit(5)
    except urllib.error.URLError as e:
        err(f"ERROR: network {API}/turns: {e}")
        sys.exit(124)
    except TimeoutError:
        err(f"ERROR: timeout on {API}/turns")
        sys.exit(124)

    failed = False
    with response:
        _warn_if_deprecated(response.headers)
        new_uuid = response.headers.get("X-Lia-Conversation")

        for event_type, data in _iter_sse(response):
            if event_type == "text_delta":
                delta = data.get("delta")
                if isinstance(delta, str):
                    sys.stdout.write(_safe(delta))
                    sys.stdout.flush()
            elif event_type == "error":
                failed = True
                print()  # close whatever partial line was streaming
                err(f"ERROR: {_safe(str(data.get('message', 'unknown error')))}")
            elif event_type == "stream_end":
                break

    print()  # trailing newline after the streamed answer

    # Persist the thread for the NEXT `ask` — including on a failed turn, so
    # a mid-stream error doesn't silently start a fresh (unrelated) thread on
    # the next try.
    if tenant and isinstance(new_uuid, str) and UUID_RE.match(new_uuid):
        _write_last_conversation(tenant, new_uuid)

    if failed:
        sys.exit(1)


def cmd_whoami(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    data = _request("GET", "/whoami")
    emit(data, explicit=args.output, pretty_renderer=_render_whoami)


def cmd_tools(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    data = _request("GET", "/tools")
    tools_all = data.get("tools", [])

    if getattr(args, "show", None):
        target = args.show
        tool = next(
            (t for t in tools_all
             if t.get("name") == target or str(t.get("name", "")).replace(".", "_") == target),
            None,
        )
        if tool is None:
            err(f"ERROR: '{_safe(target)}' is not in this token's tool catalog.")
            suggestions = _closest_names(target, tools_all)
            if suggestions:
                err("  Did you mean: " + ", ".join(_safe(s) for s in suggestions) + "?")
            sys.exit(2)
        emit(tool, explicit=args.output, pretty_renderer=_render_tool_detail)
        return

    tools = tools_all
    if getattr(args, "area", None):
        tools = [t for t in tools if t.get("module") == args.area]
    if getattr(args, "search", None):
        term = args.search.lower()
        tools = [
            t for t in tools
            if term in str(t.get("name", "")).lower()
            or term in str(t.get("catalog_name", "")).lower()
            or term in str(t.get("label", "")).lower()
            or term in str(t.get("description", "")).lower()
        ]

    data = dict(data, tools=tools, count=len(tools))
    emit(data, explicit=args.output, pretty_renderer=_render_tools)


def _closest_names(target: str, tools: list[dict]) -> list[str]:
    import difflib
    names = [t["name"] for t in tools if isinstance(t.get("name"), str)]
    return difflib.get_close_matches(target, names, n=3, cutoff=0.4)


def cmd_tenant_use(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    # Validated against whoami rather than stored blind — a typo'd slug saved
    # silently would make every later `call` fail with a server-side 403/404
    # instead of a clear error right here, where the person can still see
    # what they typed.
    data = _request("GET", "/whoami")
    target = args.tenant
    match = next(
        (t for t in data.get("tenants", []) if t.get("slug") == target or str(t.get("id")) == target),
        None,
    )
    if match is None:
        err(f"ERROR: '{_safe(target)}' is not a workspace this token reaches.")
        err("  Run `uptonica whoami` to see what it can reach.")
        sys.exit(2)
    slug = match.get("slug")
    # The match came from the SERVER's response, not from `target` — a
    # workspace with no slug, or one that doesn't look like one, must not
    # become the value later sent verbatim as the `X-Uptonica-Tenant` header
    # on every subsequent call. This is defense in depth, not a real-world
    # expectation: today every workspace has a slug.
    if not isinstance(slug, str) or not TENANT_SLUG_RE.match(slug):
        err(f"ERROR: the matched workspace has no usable slug ({slug!r}) — this looks like a server-side issue, not something to work around here.")
        sys.exit(4)
    _write_default_tenant(slug)
    banner(f"Default workspace set to {slug} ({_safe(match.get('name', ''))}).")


def cmd_tenant_show(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    current = _read_default_tenant()
    if current is None:
        banner("No default workspace set. Pass --tenant explicitly, or run: uptonica tenant use <slug>")
    else:
        print(current)  # plain stdout: scriptable, no decoration


def cmd_tenant_clear(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    if _clear_default_tenant():
        banner("Default workspace cleared.")
    else:
        banner("No default workspace was set.")


def cmd_call(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    tenant, tenant_source = _tenant_with_source(args)
    if tenant_source == "default":
        # The one source that isn't visible in the command as typed. A token
        # that unions several client workspaces can have a stale default
        # from a previous session; on a write, that's the wrong customer's
        # data with no signal — this is the signal, cheap and always-on.
        banner(f"[NOTE] using default workspace '{tenant}' (uptonica tenant use / --tenant to change)")

    if not TOOL_NAME_RE.match(args.tool):
        # Belt-and-braces: the server is the real boundary (it matches the
        # route then rejects an unknown tool name), but a name shaped nothing
        # like a tool name is almost certainly a typo, not an attempt worth
        # a round trip to find out.
        err(f"ERROR: '{args.tool}' doesn't look like a tool name (expected lowercase.dotted.segments).")
        sys.exit(2)

    if args.confirm is not None and not args.confirm.strip():
        err("ERROR: --confirm requires a non-empty token.")
        sys.exit(2)

    if args.json_body is not None and (args.arg or args.arg_string):
        err("ERROR: --json cannot be combined with --arg/--arg-string.")
        sys.exit(2)

    if args.json_body is not None:
        try:
            body: dict[str, Any] = json.loads(args.json_body)
        except json.JSONDecodeError as e:
            err(f"ERROR: --json is not valid JSON: {e}")
            sys.exit(2)
        if "confirmation_token" in body:
            # Same guard as the --arg path below, for the same reason: one
            # place sets this key (--confirm), so a round trip through JSON
            # can't quietly disagree with it.
            err("ERROR: pass the confirmation token via --confirm, not as a confirmation_token key in --json.")
            sys.exit(2)
    else:
        body = {}
        try:
            for kv in args.arg or []:
                if "=" not in kv:
                    err(f"ERROR: --arg expects key=value, got '{kv}' (no '=').")
                    sys.exit(2)
                k, _, v = kv.partition("=")
                if k == "confirmation_token":
                    # Same key --confirm sets — use --confirm instead so the
                    # round-trip byte-for-byte guarantee holds in one place.
                    err("ERROR: pass the confirmation token via --confirm, not --arg confirmation_token=...")
                    sys.exit(2)
                body[k] = _coerce(v)
            for kv in args.arg_string or []:
                if "=" not in kv:
                    err(f"ERROR: --arg-string expects key=value, got '{kv}' (no '=').")
                    sys.exit(2)
                k, _, v = kv.partition("=")
                body[k] = v
        except AmbiguousValue as e:
            err(f"ERROR: {e}")
            sys.exit(2)

    if args.dry_run:
        body["dry_run"] = True

    if args.confirm is not None and body.get("dry_run") is True:
        err("ERROR: --confirm and a dry run don't combine — a confirmed call must actually run.")
        sys.exit(2)

    if args.confirm is not None:
        banner(f"[WILL APPLY] {args.tool} on {tenant or '(default)'} — resubmitting with confirmation")
        body["confirmation_token"] = args.confirm.strip()

    data = _request(
        "POST", f"/tools/{urllib.parse.quote(args.tool, safe='.')}",
        json_body=body, tenant=tenant, tool_name_for_suggestions=args.tool,
    )
    emit(data, explicit=args.output, pretty_renderer=_render_call)


def cmd_config_set_token(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    if args.token:
        banner("[WARNING] --token puts your token in shell history and in `ps` output while this runs.")
        banner("  Prefer --token-stdin, or the interactive prompt, and rotate this token afterwards:")
        banner("  https://app.uptonica.com/account/api-tokens")
        token = args.token
    elif args.token_stdin:
        token = sys.stdin.readline()
    else:
        token = getpass.getpass("Paste your Uptonica token (input hidden): ")
    token = token.strip()
    if not looks_like_sanctum_token(token):
        err("ERROR: that doesn't look like an Uptonica token (expected '<id>|<random string>').")
        err("  Get one: https://app.uptonica.com/account/api-tokens")
        sys.exit(2)
    where = store_token(token)
    banner(f"Saved to {where}.")
    banner("Verifying...")
    data = _request("GET", "/whoami")
    n = data.get("tenant_count", 0)
    banner(f"OK — this token reaches {n} workspace{'s' if n != 1 else ''}.")


def cmd_config_clear_token(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    removed = clear_token()
    if not removed:
        banner("Nothing stored (no token in Keychain or the file store).")
        return
    banner(f"Removed from: {', '.join(removed)}.")
    banner("Note: this only removes it from THIS machine — the token is still valid until you "
           "revoke it at https://app.uptonica.com/account/api-tokens")


def cmd_config_show(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    print(json.dumps({
        "env_var": TOKEN_ENV_VAR,
        "env_var_set": bool(os.environ.get(TOKEN_ENV_VAR)),
        "keychain_item": KEYCHAIN_ITEM if sys.platform == "darwin" else None,
        "file_store": file_store_path_hint(),
        "api_base": BASE,
    }, indent=2))


def _render_whoami(d: dict) -> None:
    u = d.get("user", {})
    print(f"{_safe(u.get('email', '?'))}" + ("  (admin)" if u.get("is_admin") else ""))
    print(f"write access: {'yes' if d.get('can_write') else 'no'}")
    default_tenant = _read_default_tenant()
    if default_tenant:
        print(f"default workspace: {_safe(default_tenant)}  (change with: uptonica tenant use <slug>)")
    tenants = d.get("tenants", [])
    print(f"workspaces ({d.get('tenant_count', len(tenants))}):")
    for t in tenants[:20]:
        print(f"  {_safe(t.get('slug')):<40} {_safe(t.get('name', ''))}")
    if len(tenants) > 20:
        print(f"  ... and {len(tenants) - 20} more (use --output json to see all)")
    if not default_tenant and len(tenants) > 1:
        print("\nTip: `uptonica tenant use <slug>` sets a default so `call` doesn't need --tenant every time.")


def _render_tools(d: dict) -> None:
    print(f"{d.get('count', 0)} tools, areas: {', '.join(_safe(a) for a in (d.get('areas') or ['(unrestricted)']))}")
    for t in d.get("tools", []):
        mark = "write" if not t.get("read_only", True) else "read"
        # catalog_name is the human-readable name (e.g. "Catalog · Product · Get").
        # `label` is a first-person chat status phrase ("Sto leggendo..."), never
        # meant to be read outside the chat "thinking" chip — fall back to it only
        # against an older server that hasn't shipped catalog_name yet.
        display_name = t.get("catalog_name") or t.get("label") or ""
        print(f"  [{mark:5}] {_safe(t.get('name')):<32} {_safe(display_name)}")


def _render_tool_detail(t: dict) -> None:
    print(_safe(t.get("name", "?")))
    if t.get("catalog_name"):
        print(f"  {_safe(t['catalog_name'])}")
    elif t.get("label"):
        print(f"  {_safe(t['label'])}")
    kind = "read-only" if t.get("read_only", True) else "write"
    print(f"  module: {_safe(t.get('module', '?'))}   {kind}")
    if t.get("description"):
        print(f"\n{_safe(t['description'])}")

    params = t.get("parameters") or {}
    required = set(t.get("required") or [])
    if not params:
        print("\nparameters: none")
        return
    print("\nparameters:")
    for name, spec in params.items():
        spec = spec if isinstance(spec, dict) else {}
        flag = " (required)" if name in required else ""
        ptype = spec.get("type", "any")
        desc = spec.get("description")
        line = f"  {_safe(name)}: {_safe(ptype)}{flag}"
        if desc:
            line += f" — {_safe(desc)}"
        print(line)


def _render_call(d: dict) -> None:
    payload = d.get("result") if isinstance(d.get("result"), dict) else {}
    envelope_flagged = bool(d.get("confirmation_required") or d.get("error") == "confirmation_required")
    result_flagged = bool(payload.get("confirmation_required"))
    token = d.get("confirmation_token") or payload.get("confirmation_token")

    if not d.get("ok", True):
        print(f"ERROR: {_safe(d.get('error'))}: {_safe(d.get('error_description', ''))}")
        return

    if envelope_flagged or result_flagged:
        if token:
            import shlex
            hint = f"  Needs confirmation — not executed. Re-run with:  --confirm {shlex.quote(_safe(token))}  (expires in ~15 minutes)"
        else:
            hint = "  Needs confirmation — not executed, but no confirmation_token was found in the response."
        print(hint)
        return

    print(json.dumps(d.get("result", d), indent=2, ensure_ascii=False))


def _build_parser() -> argparse.ArgumentParser:
    # A shared parent so `--output` works BEFORE or AFTER the subcommand
    # (`uptonica --output json whoami` and `uptonica whoami --output json`
    # both parse) — argparse does not let a top-level-only option follow a
    # subcommand, and a CLI user reaches for both orders interchangeably.
    output_parent = argparse.ArgumentParser(add_help=False)
    output_parent.add_argument("--output", choices=["json"], default=None,
                                help="Force JSON output (default: JSON when piped, a short summary in a terminal)")

    p = argparse.ArgumentParser(
        prog="uptonica", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  uptonica whoami\n"
            "  uptonica tenant use my-store\n"
            "  uptonica call catalog.stats.summary\n"
            "  uptonica tools --search contact\n"
            "  uptonica ask \"quanto ho venduto questo mese?\"\n"
        ),
        parents=[output_parent],
    )
    p.add_argument("--version", action="version", version=f"uptonica {__version__}")
    # metavar hides argparse's default "{whoami,tools,call,config}" — that's
    # already spelled out, one per line with its own help text, right below.
    sub = p.add_subparsers(dest="command", required=True, metavar="<command>")

    sp = sub.add_parser("whoami", help="Who this token is, and which workspace(s) it reaches",
                         parents=[output_parent])
    sp.set_defaults(func=cmd_whoami)

    sp = sub.add_parser(
        "tools", help="List, search, or describe the tools this token can call",
        parents=[output_parent],
        epilog=(
            "examples:\n"
            "  uptonica tools --area crm\n"
            "  uptonica tools --search contact\n"
            "  uptonica tools --show catalog.product.get\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sp.add_argument("--area", help="Filter to one module (e.g. catalog, ads, crm)")
    sp.add_argument("--search", help="Filter to tools whose name, catalog name, label, or description matches (substring, case-insensitive)")
    sp.add_argument("--show", metavar="TOOL", help="Print full detail (description, parameters) for one tool")
    sp.set_defaults(func=cmd_tools)

    sp = sub.add_parser("tenant", help="Manage the default workspace for `call`")
    tenant_sub = sp.add_subparsers(dest="tenant_command", required=True, metavar="<command>")
    tu = tenant_sub.add_parser("use", help="Set the default workspace (validated against whoami)")
    tu.add_argument("tenant", help="Workspace slug or id")
    tu.set_defaults(func=cmd_tenant_use)
    tsh = tenant_sub.add_parser("show", help="Print the current default workspace, if any")
    tsh.set_defaults(func=cmd_tenant_show)
    tc = tenant_sub.add_parser("clear", help="Unset the default workspace")
    tc.set_defaults(func=cmd_tenant_clear)

    sp = sub.add_parser(
        "call", help="Invoke a tool", parents=[output_parent],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Invoke a tool. Run `uptonica tools` to see what your token can call,\n"
                     "and what arguments each one takes.",
        epilog=(
            "argument shapes:\n"
            "  --arg key=value         auto-typed: true/false, 123, 4.5, else a string\n"
            "  --arg-string key=value  kept as a literal string, no auto-typing\n"
            "  --json '{\"k\": \"v\"}'     whole argument body as JSON (exclusive with --arg*)\n"
            "\n"
            "confirming a write:\n"
            "  a write tool that needs confirmation returns a confirmation_token; re-run\n"
            "  the same command with --confirm <token> within ~15 minutes to apply it.\n"
            "  --dry-run previews a write without executing it.\n"
            "\n"
            "examples:\n"
            "  uptonica call catalog.product.get --tenant my-store --arg id=42\n"
            "  uptonica call crm.deal.create --arg title=\"Follow up\" --arg value=990.0\n"
            "  uptonica call crm.deal.create --arg title=\"Follow up\" --confirm ct_abc123\n"
        ),
    )
    sp.add_argument("tool", help="Dotted tool name, e.g. catalog.stats.summary")
    sp.add_argument("--tenant", help="Workspace slug or id, if your token reaches more than one")
    sp.add_argument("--arg", action="append", default=[], metavar="key=value", help="Auto-typed argument")
    sp.add_argument("--arg-string", action="append", default=[], dest="arg_string",
                     metavar="key=value", help="Argument kept as a literal string")
    sp.add_argument("--json", dest="json_body", metavar="JSON",
                     help="Whole argument body as JSON (exclusive with --arg/--arg-string)")
    sp.add_argument("--dry-run", action="store_true", help="Preview a write tool without executing it")
    sp.add_argument("--confirm", metavar="TOKEN",
                     help="Resubmit after a confirmation_required response")
    sp.set_defaults(func=cmd_call)

    sp = sub.add_parser(
        "ask", help="Ask Lia in natural language — she picks the tool(s)",
        description="Ask Lia in natural language instead of naming a tool yourself. Same engine,\n"
                     "same per-tool permissions as chat and the mobile app — there is no separate\n"
                     "tool allowlist to configure here.\n"
                     "\n"
                     "By default a workspace's conversation continues across calls, the same way\n"
                     "a chat tab does: if Lia asks 'vuoi che proceda?', just run `uptonica ask "
                     "\"sì\"` — Lia asks for explicit confirmation before any write, same as chat.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  uptonica ask \"quanto ho venduto questo mese?\" --tenant my-store\n"
            "  uptonica ask \"aggiorna il prezzo del prodotto X a 19.90\"\n"
            "  uptonica ask \"sì\"                       # confirms the pending write above\n"
            "  uptonica ask \"...\" --new                 # start a fresh thread\n"
        ),
    )
    sp.add_argument("message", help="What to ask Lia, in plain language")
    sp.add_argument("--tenant", help="Workspace slug or id, if your token reaches more than one")
    sp.add_argument("--conversation", metavar="UUID", help="Continue a specific thread instead of the last one")
    sp.add_argument("--new", action="store_true", help="Start a new thread instead of continuing the last one")
    sp.set_defaults(func=cmd_ask)

    cfg = sub.add_parser("config", help="Manage the stored token")
    cfg_sub = cfg.add_subparsers(dest="config_command", required=True, metavar="<command>")
    st = cfg_sub.add_parser("set-token", help="Save a token")
    st_group = st.add_mutually_exclusive_group()
    st_group.add_argument("--token", help="Provide the token non-interactively — shows up in shell "
                           "history and `ps`; prefer --token-stdin")
    st_group.add_argument("--token-stdin", action="store_true",
                           help="Read the token from stdin, e.g.: echo \"$TOK\" | uptonica config set-token --token-stdin")
    st.set_defaults(func=cmd_config_set_token)
    sh = cfg_sub.add_parser("show", help="Where the token resolves from (never prints the value)")
    sh.set_defaults(func=cmd_config_show)
    ct = cfg_sub.add_parser("clear-token", help="Remove the stored token from this machine "
                             "(does NOT revoke it — do that on the account page)")
    ct.set_defaults(func=cmd_config_clear_token)

    return p


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args, parser)
    except SystemExit:
        raise  # our own sys.exit(N) calls — pass the code through untouched
    except KeyboardInterrupt:
        err("Interrupted.")
        sys.exit(130)
    except Exception as e:  # noqa: BLE001 — this IS the last-resort boundary
        if os.environ.get("UPTONICA_DEBUG") == "1":
            raise
        # Never let a raw traceback reach the terminal by default. secrets.py
        # scrubs the ONE known way an exception here could carry the token
        # (CalledProcessError's string form includes argv) at the source, but
        # this is the backstop for anything else that surfaces unexpectedly —
        # a traceback is also just noise for a customer running this, not
        # only a possible leak. Full trace on request via UPTONICA_DEBUG=1.
        err(f"ERROR: {e.__class__.__name__}: unexpected failure. Set UPTONICA_DEBUG=1 to see the full trace.")
        sys.exit(1)


if __name__ == "__main__":
    main()
