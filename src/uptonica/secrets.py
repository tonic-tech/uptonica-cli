"""Token storage — env var, then macOS Keychain, then a plain file.

Adapted from an internal predecessor CLI's token resolver, trimmed to what a
customer machine actually has: no third-party secret managers, just the
env var, the OS keychain where one exists, and a plain file as the
cross-platform fallback (Linux/Windows, or macOS without Keychain access).
"""

from __future__ import annotations

import getpass
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

SANCTUM_TOKEN_RE = re.compile(r"^\d+\|[A-Za-z0-9]{20,}$")


class CredentialError(RuntimeError):
    """No usable token found — or one was found and it doesn't validate."""


def looks_like_sanctum_token(tok: str) -> bool:
    """Sanity-check shape (`<id>|<plaintext>`) — catches a pasted URL or a
    truncated copy-paste before it becomes a confusing 401 from the server."""
    return bool(tok) and bool(SANCTUM_TOKEN_RE.match(tok.strip()))


def _account() -> str:
    # Not os.environ["USER"]: under sudo/doas, or a scrubbed environment, that
    # can be empty or point at a different account than the one Keychain
    # actually indexes under. getpass.getuser() asks the OS, not the shell.
    return getpass.getuser()


def config_dir() -> Path:
    """Where uptonica keeps non-secret local state (e.g. the default-workspace
    bookmark in cli.py) alongside the token file below. XDG on Linux, ~/.config
    on macOS too — this directory is ours, not a platform convention to match."""
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "uptonica"


def _file_store_path() -> Path:
    return config_dir() / "token"


def file_store_path_hint() -> str:
    """The file-store path, for display only — never the token itself."""
    return str(_file_store_path())


def _try_file_store() -> str | None:
    path = _file_store_path()
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except FileNotFoundError:
        return None


def file_store_set(value: str) -> Path:
    """Write the token so it is NEVER readable by anyone but the owner, not
    even for the instant between write and chmod.

    `os.O_CREAT | os.O_EXCL`-then-write-then-chmod still has a window; instead
    the mode is passed to `os.open` itself (masked by umask, so verified after)
    and the write goes to a temp file that is `os.replace`'d into place —
    atomic on POSIX, so a crash mid-write leaves either nothing or a complete,
    correctly-permissioned file, never a partial one at the real path.
    `O_NOFOLLOW` refuses to write through a pre-existing symlink.
    """
    path = _file_store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)  # mkdir's mode is a no-op when the dir already existed

    tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(value.strip() + "\n")
        os.chmod(tmp, 0o600)  # belt-and-braces against an unexpected umask
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return path


def file_store_delete() -> bool:
    path = _file_store_path()
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


def _try_keychain(item: str) -> str | None:
    if sys.platform != "darwin" or not shutil.which("security"):
        return None
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-a", _account(), "-s", item, "-w"],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def keychain_set(item: str, value: str) -> None:
    """Raises RuntimeError with the failure reason — NEVER the underlying
    CalledProcessError, whose stringification includes the full argv, which
    includes the plaintext token (verified: `str(CalledProcessError)` renders
    the whole command)."""
    if sys.platform != "darwin" or not shutil.which("security"):
        raise RuntimeError("Keychain is macOS-only.")
    try:
        subprocess.run(
            ["security", "add-generic-password", "-U", "-a", _account(), "-s", item, "-w", value],
            check=True, capture_output=True, text=True, timeout=5,
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"security add-generic-password failed (exit {e.returncode}).") from None
    except (subprocess.SubprocessError, OSError) as e:
        raise RuntimeError(f"could not reach the Keychain: {e.__class__.__name__}.") from None


def keychain_delete(item: str) -> bool:
    if sys.platform != "darwin" or not shutil.which("security"):
        return False
    out = subprocess.run(
        ["security", "delete-generic-password", "-a", _account(), "-s", item],
        capture_output=True, text=True, timeout=5,
    )
    return out.returncode == 0


def resolve(*, env_var: str, keychain_item: str, validator=None) -> str:
    """env var -> Keychain (macOS) -> file store, in that order.

    A source that IS present but fails validation raises immediately rather
    than falling through to the next one. Falling through used to mean a
    truncated `UPTONICA_TOKEN` (a mangled CI secret, a bad paste) silently ran
    the command against whatever was in the file store instead — on a token
    that can union several client workspaces, that is the wrong identity
    executing a write nobody chose, with no error to notice. Setting the env
    var is a deliberate instruction; failing to honor it should never be
    silent.
    """
    val = os.environ.get(env_var)
    if val:
        if validator is not None and not validator(val):
            raise CredentialError(
                f"{env_var} is set but is not a valid Uptonica token "
                f"(expected '<id>|<random>'). Refusing to fall back to a stored "
                f"token — unset {env_var} if you meant to use the stored one."
            )
        return val.strip()

    val = _try_keychain(keychain_item)
    if val:
        if validator is not None and not validator(val):
            raise CredentialError(
                "The token in macOS Keychain is not valid. Run: uptonica config set-token"
            )
        return val.strip()

    val = _try_file_store()
    if val:
        if validator is not None and not validator(val):
            raise CredentialError(
                f"The token in {_file_store_path()} is not valid. Run: uptonica config set-token"
            )
        return val.strip()

    raise CredentialError(
        f"No Uptonica token found (checked {env_var}, macOS Keychain, {_file_store_path()}).\n"
        f"  Get one: https://app.uptonica.com/account/api-tokens\n"
        f"  Then run: uptonica config set-token"
    )


def store_token(value: str) -> str:
    """Save via Keychain on macOS, else the file store. Returns where it landed."""
    value = value.strip()
    if sys.platform == "darwin" and shutil.which("security"):
        try:
            keychain_set("uptonica-token", value)
            return "macOS Keychain"
        except RuntimeError:
            pass  # fall through to the file store
    path = file_store_set(value)
    return str(path)


def clear_token() -> list[str]:
    """Remove the stored token from every place it might live. Returns the
    list of places it was actually found and removed (never the value)."""
    removed = []
    if keychain_delete("uptonica-token"):
        removed.append("macOS Keychain")
    if file_store_delete():
        removed.append(file_store_path_hint())
    return removed
