"""Read a secret (an API token) from where you keep it, only at the moment it is needed.

A secret is named in config, never written there: `bitwarden:<item name>` is the password of that
Bitwarden item, read with the `bw` CLI. If the vault is locked, Bitwarden's own prompt asks for
your master password in the terminal; the session it returns lives in this process only and is
never written to disk or logged. Nothing here prints a secret, and errors never contain one.
"""

import json
import os
import shutil
import subprocess

_session: dict[str, str] = {}  # the Bitwarden session key, for this process only


class SecretError(Exception):
    pass


def get(ref: str) -> str:
    kind, _, name = ref.partition(":")
    if kind == "bitwarden" and name:
        return _bitwarden(name)
    raise SecretError(f"unknown secret location {ref!r} (expected bitwarden:<item name>)")


def _bw(*args: str, interactive: bool = False) -> subprocess.CompletedProcess:
    if not shutil.which("bw"):
        raise SecretError("the Bitwarden CLI (bw) is not installed")
    env = {**os.environ, **({"BW_SESSION": _session["key"]} if "key" in _session else {})}
    return subprocess.run(
        ["bw", *args] + ([] if interactive else ["--nointeraction"]),
        env=env,
        stdout=subprocess.PIPE,
        stderr=None if interactive else subprocess.PIPE,  # an unlock prompt needs the terminal
        text=True,
        check=False,
    )


def _status() -> str:
    out = _bw("status")
    try:
        return json.loads(out.stdout).get("status", "")
    except json.JSONDecodeError:
        return ""


def _bitwarden(name: str) -> str:
    status = _status()
    if status == "unauthenticated":
        raise SecretError("Bitwarden CLI is not logged in: run `bw login` once")
    if status == "locked":
        unlocked = _bw("unlock", "--raw", interactive=True)
        if unlocked.returncode or not unlocked.stdout.strip():
            raise SecretError("Bitwarden vault was not unlocked")
        _session["key"] = unlocked.stdout.strip()
    for attempt in range(2):
        found = _bw("get", "password", name)
        if found.returncode == 0 and found.stdout.strip():
            return found.stdout.strip()
        if attempt == 0:
            _bw("sync")  # an item made in the desktop app reaches the CLI after a sync
    reason = (found.stderr or "").strip().splitlines()[-1:] or ["not found"]
    raise SecretError(f"Bitwarden item {name!r}: {reason[0]}")
