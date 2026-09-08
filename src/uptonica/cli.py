"""
uptonica — the Uptonica command-line client.

One binary. What you can do is decided by the token you configure, not by
which command you type: a plain workspace token reaches your own store(s);
a token minted with broader scope (Settings -> API Tokens -> "Terminal
access") reaches whatever workspaces and tool areas you picked when you
made it, including several client workspaces if you manage more than one
(agencies: OperatorTenantResolver already unions your own store, if you
manage sub-tenants, at makes yours).

Get a token:  https://app.uptonica.com/account/api-tokens
Store it:     uptonica config set-token

Commands:
  whoami                          Who this token is, and which workspace(s) it reaches
  tools                           List tools this token can call (self-describing catalog)
  call <tool> [args...]           Invoke a tool
  config set-token                Save a token (Keychain on macOS, else a 0600 file)
  config show                     Where the token resolves from (never prints the value)

`call` argument shapes:
  --tenant SLUG_OR_ID              Which workspace, if your token reaches more than one
  --arg key=value                  Value auto-typed (true/false/int/float/string)
  --arg-string key=value            Value kept as a literal string, no auto-typing
  --json '{"key": "value"}'        Whole argument body as JSON (mutually exclusive with --arg*)
  --dry-run                        Preview a write tool without executing it
  --confirm TOKEN                  Resubmit a write tool after it returned confirmation_required

Exit codes: 0 ok | 2 usage error | 3 auth/forbidden | 4 4xx from the API | 5 5xx | 124 timeout
"""

from __future__ import annotations

import argparse
import getpass
import json
import math
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import re

from uptonica.output import banner, emit, err
from uptonica.secrets import (
    CredentialError,
    clear_token,
    file_store_path_hint,
    looks_like_sanctum_token,
    resolve,
    store_token,
)

__version__ = "0.1.0"
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


def _tenant(args: argparse.Namespace) -> str | None:
    return getattr(args, "tenant", None) or os.environ.get("UPTONICA_TENANT") or None


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


def _request(method: str, path: str, *, json_body: dict | None = None,
             tenant: str | None = None, timeout: int = 60) -> Any:
    url = f"{API}{path}"
    headers = {"Authorization": f"Bearer {_token()}", "Accept": "application/json",
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
        try:
            j = json.loads(body)
            detail = _safe(j.get("error_description") or j.get("error") or detail)
        except (ValueError, TypeError):
            pass
        err(f"ERROR: HTTP {e.code} {method} {path}\n  {detail}")
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


def cmd_whoami(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    data = _request("GET", "/whoami")
    emit(data, explicit=args.output, pretty_renderer=_render_whoami)


def cmd_tools(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    data = _request("GET", "/tools")
    if getattr(args, "area", None):
        data = dict(data)
        data["tools"] = [t for t in data.get("tools", []) if t.get("module") == args.area]
        data["count"] = len(data["tools"])
    emit(data, explicit=args.output, pretty_renderer=_render_tools)


def cmd_call(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    tenant = _tenant(args)

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

    data = _request("POST", f"/tools/{urllib.parse.quote(args.tool, safe='.')}", json_body=body, tenant=tenant)
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
    tenants = d.get("tenants", [])
    print(f"workspaces ({d.get('tenant_count', len(tenants))}):")
    for t in tenants[:20]:
        print(f"  {_safe(t.get('slug')):<40} {_safe(t.get('name', ''))}")
    if len(tenants) > 20:
        print(f"  ... and {len(tenants) - 20} more (use --output json to see all)")


def _render_tools(d: dict) -> None:
    print(f"{d.get('count', 0)} tools, areas: {', '.join(_safe(a) for a in (d.get('areas') or ['(unrestricted)']))}")
    for t in d.get("tools", []):
        mark = "write" if not t.get("read_only", True) else "read"
        print(f"  [{mark:5}] {_safe(t.get('name')):<32} {_safe(t.get('label', ''))}")


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

    p = argparse.ArgumentParser(prog="uptonica", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 parents=[output_parent])
    p.add_argument("--version", action="version", version=f"uptonica {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("whoami", help="Who this token is, and which workspace(s) it reaches",
                         parents=[output_parent])
    sp.set_defaults(func=cmd_whoami)

    sp = sub.add_parser("tools", help="List tools this token can call", parents=[output_parent])
    sp.add_argument("--area", help="Filter to one module (e.g. catalog, ads, crm)")
    sp.set_defaults(func=cmd_tools)

    sp = sub.add_parser("call", help="Invoke a tool", parents=[output_parent])
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

    cfg = sub.add_parser("config", help="Manage the stored token")
    cfg_sub = cfg.add_subparsers(dest="config_command", required=True)
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
