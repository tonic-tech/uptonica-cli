"""
uptonica — the Uptonica command-line client.

What you can do is decided by the token you configure, not by which
command you type: a plain token reaches your own workspace(s); a token
minted with broader scope reaches whatever workspaces and tool areas you
picked when you made it — including several client workspaces if you
manage more than one (agencies get this automatically, scoped to the
clients they actually manage).

Log in:       uptonica login
Log out:      uptonica logout
(or mint a token yourself at https://app.uptonica.com/account/api-tokens,
 then `uptonica config set-token`)

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
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
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

__version__ = "0.2.9"
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


def _safe_line(s: Any) -> str:
    """`_safe()` for a value that is supposed to be ONE line — a workspace
    name printed next to its slug, never free-form diagnostic text.
    `_safe()` alone keeps `\\n`/`\\t`, which is right for a multi-line error
    body but wrong for a single label: a hostile `name` containing a
    newline would print extra, unindented rows in a list a person is about
    to pick a slug from by eye (`_prompt_default_workspace`). Mirrors
    `repl.py`'s own `_safe_line` — same reasoning, same fix, this file just
    never needed it until that list became a decision surface."""
    return _safe(s).replace("\n", " ").replace("\r", " ").replace("\t", " ")


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
#
# `\Z`, not `$` — `$` matches just before a trailing "\n" too, so
# "my-store\n" would pass this as slug-shaped, take the readable-filename
# branch in _conversation_state_path(), and only fail later with an
# uncaught ValueError from http.client when it reaches a header.
TENANT_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}\Z")


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


# Maps a handful of well-known machine-readable `error` codes the operator
# API returns (see e.g. `OperatorTurnController`/`OperatorToolController` on
# the server) to a hint phrased in terms of what THIS CLI can do about it,
# appended after the server's own `error_description`. Without this, a
# person reading the error sees the server's own instructions verbatim —
# "Call /api/v1/operator/whoami for the list", a raw API path, not
# `uptonica whoami` — which is accurate but not something a CLI user should
# have to translate themselves. Keyed by `error`, not matched against the
# description text, so a wording change on the server side can't silently
# stop the hint from firing.
_ERROR_HINTS: dict[str, str] = {
    "tenant_required": "Run `uptonica whoami` to see your workspaces, then `uptonica tenant use <slug>` "
                        "to set a default (or pass `--tenant <slug>` just this once; inside the REPL, `/workspace <slug>`).",
    "insufficient_scope": "`ask`/the REPL need an unrestricted, write-capable token — mint one without an "
                           "`area:` scope via `uptonica login`, or use `uptonica call <tool>` with the scoped token you already have.",
    "conversation_not_found": "That thread is gone server-side — the next `ask`/REPL message in this workspace "
                               "starts a fresh one automatically; pass `--new` (or `/new` in the REPL) to do that explicitly.",
    "budget_exhausted": "Nothing to do from here — this resets monthly, or an admin can raise the limit "
                         "under Settings -> Billing on the workspace.",
    "rate_limited": "Wait a few seconds and try again.",
}


def _hint_for_error(error_code: object) -> str | None:
    if not isinstance(error_code, str):
        return None
    return _ERROR_HINTS.get(error_code)


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
            err(f"ERROR: the API tried to redirect this request to {_safe(e.headers.get('Location', '(unknown)'))}.")
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
        hint = _hint_for_error(j.get("error") if isinstance(j, dict) else None)
        if hint:
            err(f"  {hint}")
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


def _device_auth_request(path: str, json_body: dict | None = None) -> dict:
    """POST to an unauthenticated `/oauth/device/*` endpoint — `login` runs
    before a token exists, so there is no bearer to attach. An RFC 8628 error
    response (`authorization_pending`, `slow_down`, `access_denied`,
    `expired_token`) comes back as an ordinary 4xx JSON body and is returned
    to the caller rather than turned into an exit() the way `_request` does
    for the authenticated API — the polling loop in `cmd_login` needs to
    branch on `error`, not stop on the first non-2xx response."""
    url = f"{BASE}{path}"
    headers = {"Accept": "application/json", "User-Agent": f"uptonica-cli/{__version__}"}
    data = None
    if json_body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(json_body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with _OPENER.open(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if 300 <= e.code < 400:
            err(f"ERROR: the login server tried to redirect this request to {_safe(e.headers.get('Location', '(unknown)'))}.")
            err("  Refusing to follow it — this should not happen in normal use.")
            sys.exit(4)
        body = e.read().decode("utf-8", errors="replace")
        try:
            return json.loads(body)
        except (ValueError, TypeError):
            err(f"ERROR: HTTP {e.code} {path}\n  {_safe(body[:600])}")
            sys.exit(5 if e.code >= 500 else 4)
    except urllib.error.URLError as e:
        err(f"ERROR: network {url}: {e}")
        sys.exit(124)
    except TimeoutError:
        err(f"ERROR: timeout on {url}")
        sys.exit(124)


class DeviceLoginError(Exception):
    """A terminal failure of the device-authorization flow.

    Raised by `_device_login_flow`, never a bare `sys.exit()` — that
    function is shared between the one-shot `uptonica login` (where exiting
    is correct, same as `AskError`'s reasoning) and the REPL's `/login`
    (which must report the failure and keep the session alive, not tear
    down the process). `cli_exit_code` is what the top-level command exits
    with; the REPL just prints the message and re-enables input."""

    def __init__(self, message: str, cli_exit_code: int) -> None:
        super().__init__(message)
        self.cli_exit_code = cli_exit_code


def _device_login_flow(no_browser: bool, *, emit) -> str:
    """Drives the RFC 8628 device-authorization flow to completion and
    returns the minted token. `emit(line)` is called for every user-facing
    status line as it happens (the code to confirm, where it opened, etc.)
    — the top-level command routes it through `banner()`, the REPL through
    its own transcript — so this function has no idea which UI is driving
    it and never touches stdout/stderr or `sys.exit()` directly."""
    start = _device_auth_request("/oauth/device/code", json_body={})
    device_code = start.get("device_code")
    # `_safe()` on every field the server hands back before it reaches the
    # terminal (banner/print) or a browser open — same reasoning as `_safe`'s
    # own docstring: this response is unauthenticated (there is no token yet
    # to prove it came from a session we trust), so it gets the same
    # control-character stripping as any other server-derived string, not an
    # exemption for being part of the login path itself.
    user_code = _safe(start.get("user_code") or "")
    verification_uri = _safe(start.get("verification_uri") or "")
    verification_uri_complete = _safe(start.get("verification_uri_complete") or start.get("verification_uri") or "")
    # Redacted before it ever reaches a DeviceLoginError message: `device_code`
    # is the sole bearer credential for the unauthenticated `/oauth/device/token`
    # poll (same reasoning as DeviceApproval's own docblock server-side), and
    # in the REPL a DeviceLoginError's text lands in the on-screen transcript,
    # not just stderr — a malformed `interval`/`expires_in` alongside an
    # otherwise-valid device_code must not be the thing that puts it there.
    _debug_payload = _safe(json.dumps({**start, "device_code": "[redacted]"} if device_code else start))
    if not device_code or not user_code or not verification_uri:
        raise DeviceLoginError(f"unexpected response starting login: {_debug_payload}", 5)
    try:
        # Clamped, not just parsed: this is the server telling the CLI how to
        # pace itself, and a malformed or hostile value (0, negative, a
        # multi-year expiry) must not turn into a sleepless hammering loop or
        # a client that appears to hang forever. `is None` rather than `or`
        # for the default — 0 is a real (if hostile) value the clamp below
        # must still see, not something to treat the same as absent.
        interval_raw = start.get("interval")
        expires_raw = start.get("expires_in")
        interval = max(1, min(int(5 if interval_raw is None else interval_raw), 60))
        expires_in = max(60, min(int(600 if expires_raw is None else expires_raw), 1800))
    except (TypeError, ValueError):
        raise DeviceLoginError(f"unexpected response starting login: {_debug_payload}", 5) from None

    emit(f"Confirm this code in your browser: {user_code}")
    if no_browser:
        emit(f"Visit {verification_uri} and enter the code above.")
    else:
        emit(f"Opening {verification_uri_complete} ...")
        try:
            webbrowser.open(verification_uri_complete)
        except Exception:
            pass
        emit(f"If it didn't open, visit {verification_uri} and enter the code above.")

    deadline = time.monotonic() + expires_in
    while time.monotonic() < deadline:
        time.sleep(interval)
        result = _device_auth_request("/oauth/device/token", json_body={"device_code": device_code})
        token = result.get("access_token")
        if token:
            if not looks_like_sanctum_token(token):
                raise DeviceLoginError("the server returned something that doesn't look like an Uptonica token — not saving it.", 5)
            return token
        error = result.get("error")
        if error == "slow_down":
            interval = min(interval + 5, 60)
        elif error == "authorization_pending":
            pass
        elif error == "access_denied":
            raise DeviceLoginError("login was denied in the browser.", 3)
        elif error == "expired_token":
            raise DeviceLoginError("this login code expired — run `uptonica login` again.", 3)
        else:
            raise DeviceLoginError(f"unexpected response while waiting for login: {_safe(json.dumps(result))}", 5)

    raise DeviceLoginError("this login code expired — run `uptonica login` again.", 3)


def cmd_login(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    try:
        token = _device_login_flow(args.no_browser, emit=banner)
    except DeviceLoginError as e:
        err(f"ERROR: {e}")
        sys.exit(e.cli_exit_code)

    where = store_token(token)
    banner(f"Saved to {where}.")
    if os.environ.get(TOKEN_ENV_VAR):
        banner(f"[WARNING] {TOKEN_ENV_VAR} is set in this shell and takes priority over what was just "
               f"saved — unset it if you want the new token to actually be used.")
    banner("Verifying...")
    data = _request("GET", "/whoami")
    tenants = data.get("tenants", [])
    n = data.get("tenant_count", len(tenants))
    banner(f"Logged in — this token reaches {n} workspace{'s' if n != 1 else ''}.")
    _prompt_default_workspace(tenants)


def _prompt_default_workspace(tenants: list) -> None:
    """Called once, right after `login` confirms the token works — the
    whole point is to leave the person able to run `ask`/`call` right away,
    not needing to discover `tenant use` for themselves the first time a
    bare `ask` 400s with `tenant_required` (the incident this exists to
    prevent: a person's first question after logging in hit exactly that
    error). Free of an extra round-trip: the `/whoami` `login` already made
    to confirm the token works is the same list this reads.

    Left alone (no prompt) when the CURRENT default is still among the
    reachable workspaces — a fresh login with the same access shouldn't nag
    about something already correctly set. It's only unset or now-stale
    (the workspace it pointed at is no longer reachable — a scope change,
    a revoked grant) that gets this to run again, same as no default at all.
    """
    valid = [t for t in tenants if isinstance(t.get("slug"), str) and TENANT_SLUG_RE.match(t["slug"])]
    current = _read_default_tenant()
    if current and any(t["slug"] == current for t in valid):
        return
    if not valid:
        return
    if len(valid) == 1:
        # Nothing to choose between — setting it automatically is strictly
        # friendlier than making the person type `tenant use` for the one
        # slug they'd see if they ran `whoami` next anyway.
        slug = valid[0]["slug"]
        _write_default_tenant(slug)
        banner(f"Default workspace set to {slug} ({_safe_line(valid[0].get('name', ''))}).")
        return

    banner("This token reaches more than one workspace:")
    for t in valid[:20]:
        banner(f"  {_safe_line(t['slug']):<30} {_safe_line(t.get('name', ''))}")
    if len(valid) > 20:
        banner(f"  ... and {len(valid) - 20} more (uptonica whoami --output json to see all)")

    # Both streams, not just stdin: stdout redirected while stdin is still a
    # real terminal (`uptonica login > log.txt`, run interactively) would
    # otherwise send this prompt into the file — invisible, reads as a hang
    # rather than a login that's actually waiting on you.
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        # Piped/scripted invocation — nothing to prompt into, and guessing
        # which one to default to would be worse than asking explicitly.
        banner("Pick one with: uptonica tenant use <slug>")
        return
    try:
        choice = input("Set a default workspace now (slug, or Enter to skip): ").strip()
    except (EOFError, KeyboardInterrupt):
        print()  # the interrupted prompt line otherwise stays half-written
        banner("Skipped — run `uptonica tenant use <slug>` any time.")
        return
    if not choice:
        banner("Skipped — run `uptonica tenant use <slug>` any time.")
        return
    match = next((t for t in valid if t["slug"] == choice or str(t.get("id")) == choice), None)
    if match is None:
        err(f"ERROR: '{_safe(choice)}' isn't one of the workspaces above — not set. Run `uptonica tenant use <slug>` when you're ready.")
        return
    _write_default_tenant(match["slug"])
    banner(f"Default workspace set to {match['slug']} ({_safe_line(match.get('name', ''))}).")


def cmd_logout(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    """Top-level alias for `config clear-token` — same removal, but under
    the name someone reaching for the counterpart of `uptonica login` will
    actually type. Kept as a thin wrapper (not a duplicate) so the two never
    drift: see `cmd_config_clear_token` for the actual behavior."""
    cmd_config_clear_token(args, parser)


def _iter_sse(response) -> Iterator[tuple[str, dict]]:
    """Parse a `text/event-stream` body into `(event_type, data)` pairs.

    Minimal on purpose: the server side ({@code SafeSSEAdapter}) only ever
    writes one `event:` line and one `data:` line per event, so there is no
    multi-line `data:` folding or `id:`/`retry:` handling to do here — a
    general SSE client would be solving a problem this specific server
    doesn't have.
    """
    # Bounded, not unlimited: a single endless line (malformed proxy, byte-fed
    # stream with no "\n") would otherwise grow this process's memory forever
    # regardless of the request timeout, since bytes are still arriving. A
    # line hitting the cap is malformed SSE either way, so it is dropped as a
    # protocol error rather than silently split into two fake events.
    max_line = 1 << 20  # 1 MiB — generous for a single delta/data line

    event_type: str | None = None
    data_line: str | None = None
    while True:
        raw = response.readline(max_line)
        if not raw:
            return
        if len(raw) >= max_line and not raw.endswith(b"\n"):
            event_type = None
            data_line = None
            continue
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if line == "":
            if event_type is not None and data_line is not None:
                try:
                    parsed = json.loads(data_line)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, dict):
                    yield event_type, parsed
            event_type = None
            data_line = None
            continue
        if line.startswith("event:"):
            event_type = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_line = line[len("data:"):].strip()


MAX_ASK_MESSAGE_LEN = 10000  # mirrors the server's own `message` max:10000


class AskError(Exception):
    """A recoverable failure opening or reading an ask turn.

    Deliberately NOT a `sys.exit()` at the point of failure, unlike every
    other command in this file: `_open_ask_stream()` is shared between the
    one-shot `ask` command (where exiting immediately is correct — there is
    nothing left to run) and the interactive REPL (`uptonica.repl`), where a
    failed turn must be reported and the session kept alive for the next one.
    `exit_code` carries what `cmd_ask` would have called `sys.exit()` with,
    so it doesn't have to re-derive it from the message.
    """

    def __init__(self, message: str, exit_code: int) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def _open_ask_stream(tenant: str | None, message: str, conversation_uuid: str | None):
    """POST a turn to `/turns` and return the opened streaming response.

    Raises `AskError` on any failure (never `sys.exit`s) — see `AskError` for
    why. Callers still need to consume the body with `_iter_sse()` and close
    it; this only covers "did the request even open".
    """
    body: dict[str, Any] = {"message": message}
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
        return _OPENER.open(req, timeout=300)
    except urllib.error.HTTPError as e:
        if 300 <= e.code < 400:
            raise AskError(
                f"the API tried to redirect this request to {_safe(e.headers.get('Location', '(unknown)'))}.\n"
                "  Refusing to forward your token to a different host.",
                exit_code=4,
            ) from e
        _warn_if_deprecated(e.headers)
        raw_body = e.read().decode("utf-8", errors="replace")
        detail = _safe(raw_body[:600])
        error_code = None
        try:
            j = json.loads(raw_body)
            error_code = j.get("error")
            detail = _safe(j.get("error_description") or error_code or detail)
        except (ValueError, TypeError):
            pass
        hint = _hint_for_error(error_code)
        if hint:
            detail = f"{detail}\n  {hint}"
        exit_code = 5 if e.code >= 500 else (3 if e.code in (401, 403) else (2 if e.code in (400, 404) else 4))
        raise AskError(f"HTTP {e.code} POST /turns\n  {detail}", exit_code=exit_code) from e
    except urllib.error.URLError as e:
        raise AskError(f"network {API}/turns: {e}", exit_code=124) from e
    except TimeoutError as e:
        raise AskError(f"timeout on {API}/turns", exit_code=124) from e


def cmd_ask(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    # Validated here, before the REPL branch below, not after it: --conversation
    # is accepted by BOTH entry points (a one-shot ask AND `uptonica ask` with
    # no message, to resume a specific thread in the REPL), so this must not
    # live only in the one-shot path below it used to be the only one.
    if args.conversation is not None and not UUID_RE.match(args.conversation):
        err(f"ERROR: --conversation '{_safe(args.conversation)}' is not a UUID.")
        sys.exit(2)

    if args.message is None:
        from uptonica.repl import run_repl  # deferred: textual is only needed for the REPL path

        tenant, _source = _tenant_with_source(args)
        conversation_uuid = args.conversation
        if not conversation_uuid and not args.new and tenant:
            conversation_uuid = _read_last_conversation(tenant)
        run_repl(tenant, conversation_uuid)
        return

    if len(args.message) > MAX_ASK_MESSAGE_LEN:
        err(f"ERROR: message is {len(args.message)} characters, over the {MAX_ASK_MESSAGE_LEN}-character limit.")
        sys.exit(2)

    tenant, tenant_source = _tenant_with_source(args)
    if tenant_source == "default":
        banner(f"[NOTE] using default workspace '{tenant}' (uptonica tenant use / --tenant to change)")

    conversation_uuid = args.conversation
    if not conversation_uuid and not args.new and tenant:
        conversation_uuid = _read_last_conversation(tenant)

    try:
        response = _open_ask_stream(tenant, args.message, conversation_uuid)
    except AskError as e:
        err(f"ERROR: {e}")
        sys.exit(e.exit_code)

    failed = False
    with response:
        _warn_if_deprecated(response.headers)
        new_uuid = response.headers.get("X-Lia-Conversation")

        try:
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
        except (OSError, TimeoutError) as e:
            # A dropped connection mid-stream (reset, incomplete read, a
            # second read timeout after the first byte) — not an HTTPError,
            # so it never touches the try/except above this block. Falls
            # through to the SAME persist-the-thread step below: the server
            # already committed whatever it streamed before dying, and the
            # next `ask` should continue that thread, not silently orphan it
            # by starting a fresh one.
            failed = True
            print()
            err(f"ERROR: connection lost mid-stream: {e}")

    print()  # trailing newline after the streamed answer

    # Persist the thread for the NEXT `ask` — including on a failed turn, so
    # a mid-stream error doesn't silently start a fresh (unrelated) thread on
    # the next try.
    if tenant and isinstance(new_uuid, str) and UUID_RE.match(new_uuid):
        _write_last_conversation(tenant, new_uuid)

    if failed:
        sys.exit(1)


# Shortcuts over `ask` — frequent requests spelled as a dedicated command
# instead of composing the natural-language prompt yourself every time.
# Deliberately not new tool calls: this pre-fills `ask`'s message and runs
# the exact same path (same engine, same per-tool permissions, same
# streaming), so a shortcut works for anything Lia's existing tools already
# cover, and never gets out of sync with what she can actually do. Shared
# with the REPL's matching `/create-image`/`/create-video`/`/ads-report`
# slash commands (see `repl.py`) so the wording is written once.
SHORTCUT_PROMPTS: dict[str, str] = {
    "create-image": "Create an image",
    "create-video": "Create a video",
    "ads-report": "Give me an ads performance report",
}


def _make_shortcut_cmd(base_prompt: str):
    def _cmd(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
        args.message = f"{base_prompt}: {args.message}" if args.message else base_prompt
        cmd_ask(args, parser)

    return _cmd


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
            "  uptonica login\n"
            "  uptonica whoami\n"
            "  uptonica tenant use my-store\n"
            "  uptonica call catalog.stats.summary\n"
            "  uptonica tools --search contact\n"
            "  uptonica ask \"quanto ho venduto questo mese?\"\n"
            "  uptonica create-image \"a black cat wearing sunglasses\"\n"
            "  uptonica ads-report\n"
            "  uptonica logout\n"
        ),
        parents=[output_parent],
    )
    p.add_argument("--version", action="version", version=f"uptonica {__version__}")
    # metavar hides argparse's default "{whoami,tools,call,config}" — that's
    # already spelled out, one per line with its own help text, right below.
    # NOT required: a bare `uptonica` (no subcommand at all) opens the same
    # interactive session as `uptonica ask` with no message — see main()'s
    # dispatch when args.command is None. Every other invocation shape keeps
    # requiring one of the subcommands below.
    sub = p.add_subparsers(dest="command", required=False, metavar="<command>")

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
            "  uptonica ask                             # no message: opens an interactive session\n"
        ),
    )
    sp.add_argument("message", nargs="?", default=None, help="What to ask Lia. Omit to open an interactive session instead")
    sp.add_argument("--tenant", help="Workspace slug or id, if your token reaches more than one")
    sp.add_argument("--conversation", metavar="UUID", help="Continue a specific thread instead of the last one")
    sp.add_argument("--new", action="store_true", help="Start a new thread instead of continuing the last one")
    sp.set_defaults(func=cmd_ask)

    sp = sub.add_parser(
        "login", help="Log in via your browser and store the token (no copy-paste)",
        epilog=(
            "examples:\n"
            "  uptonica login\n"
            "  uptonica login --no-browser   # print the URL instead of opening it\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sp.add_argument("--no-browser", action="store_true", dest="no_browser",
                     help="Don't try to open a browser automatically — just print the URL and code")
    sp.set_defaults(func=cmd_login)

    sp = sub.add_parser("logout", help="Remove the stored token from this machine (counterpart of `login`)")
    sp.set_defaults(func=cmd_logout)

    for name, base_prompt in SHORTCUT_PROMPTS.items():
        sp = sub.add_parser(
            name, help=f"Shortcut: asks Lia to “{base_prompt.lower()}”",
            parents=[output_parent],
            description=f"Shortcut over `ask` — pre-fills the message with “{base_prompt}” "
                        "(plus whatever you add) and runs it exactly like `uptonica ask` would, "
                        "same engine, same permissions, same streaming output.",
            formatter_class=argparse.RawDescriptionHelpFormatter,
            epilog=(
                "examples:\n"
                f"  uptonica {name}\n"
                f"  uptonica {name} \"a black cat wearing sunglasses\" --tenant my-store\n"
            ),
        )
        sp.add_argument("message", nargs="?", default=None, help="Extra detail to append to the shortcut's prompt")
        sp.add_argument("--tenant", help="Workspace slug or id, if your token reaches more than one")
        sp.add_argument("--conversation", metavar="UUID", help="Continue a specific thread instead of the last one")
        sp.add_argument("--new", action="store_true", help="Start a new thread instead of continuing the last one")
        sp.set_defaults(func=_make_shortcut_cmd(base_prompt))

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
    if args.command is None:
        # Bare `uptonica` — same entry point as `uptonica ask` with no
        # message, reusing cmd_ask's own dispatch to the REPL rather than
        # duplicating it. Namespace built by hand: argparse never populated
        # these because the `ask` subparser (which owns them) never ran.
        args = argparse.Namespace(message=None, tenant=None, conversation=None, new=False, output=args.output)
        args.func = cmd_ask
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
