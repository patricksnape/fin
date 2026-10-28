"""Encrypted backup and multi-machine sync of the data repo over git-remote-gcrypt.

gcrypt force-pushes whatever you give it, so two machines could silently overwrite each other.
The rules here: always fetch and merge first, only push when the remote still holds what we last
fetched, and re-read the remote after pushing to confirm it equals our head.
"""

import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from fin import config, gitops, ledger

GPG_CANDIDATES = [
    "/usr/local/MacGPG2/bin/gpg",
    "/opt/homebrew/bin/gpg",
    "/usr/local/bin/gpg",
    "/usr/bin/gpg",
]
ATTRIBUTES = "ledger/imported/** merge=finref\nledger/manual/** merge=finref\n"
MAX_TRIES = 3


class SyncError(Exception):
    pass


class ConflictError(SyncError):
    def __init__(self, files: list[str]):
        self.files = files
        super().__init__("merge conflict in: " + ", ".join(files))


def find_gpg() -> str:
    found = shutil.which("gpg") or next((p for p in GPG_CANDIDATES if Path(p).exists()), None)
    if not found:
        raise SyncError("gpg not found; install GPG Suite or GnuPG first")
    return found


def branch(root: Path) -> str:
    return gitops.git(root, "symbolic-ref", "--short", "HEAD").strip()


def remotes(root: Path) -> dict[str, str]:
    """Remotes that use gcrypt, name -> url."""
    out = {}
    for line in gitops.git(root, "remote", "-v").splitlines():
        name, url, kind = line.split()[0], line.split()[1], line.split()[2]
        if kind == "(push)" and url.startswith("gcrypt::"):
            out[name] = url
    return out


def add_remote(
    root: Path, name: str, location: str, fingerprint: str, gpg: str | None = None
) -> None:
    """Add an encrypted remote and configure the repo for safe multi-machine use."""
    gitops.git(root, "remote", "add", name, f"gcrypt::{location}")
    configure_remote(root, name, fingerprint, gpg)


def configure_remote(root: Path, name: str, fingerprint: str, gpg: str | None = None) -> None:
    """Per-repo settings for an encrypted remote (also used when cloning on a second machine)."""
    gpg = gpg or find_gpg()
    for key, value in [
        ("gpg.program", gpg),
        (f"remote.{name}.gcrypt-participants", fingerprint),
        ("gcrypt.require-explicit-force-push", "true"),
        ("merge.finref.name", "union of ledger transactions by ref"),
        ("merge.finref.driver", f"{sys.executable} -m fin.mergedriver %O %A %B"),
    ]:
        gitops.git(root, "config", key, value)
    attrs = root / ".gitattributes"
    existing = attrs.read_text() if attrs.exists() else ""
    if ATTRIBUTES not in existing:
        attrs.write_text(existing + ATTRIBUTES)
        gitops.commit_all(root, "configure sync merge driver")


def remote_head(root: Path, remote: str) -> str | None:
    """Commit the remote currently holds for our branch (decrypts the manifest), or None if empty."""
    out = gitops.git(root, "ls-remote", remote, f"refs/heads/{branch(root)}", check=False).strip()
    return out.split()[0] if out else None


def _require_clean(root: Path) -> None:
    if gitops.is_dirty(root):
        raise SyncError("uncommitted changes; run `fin commit` first")


def pull(root: Path, remote: str) -> str:
    """Bring in the remote's commits. Returns empty | up-to-date | ahead | fast-forward | rebased."""
    _require_clean(root)
    if remote_head(root, remote) is None:
        return "empty"
    gitops.git(root, "fetch", "-q", remote)
    tracking, local = f"{remote}/{branch(root)}", gitops.git(root, "rev-parse", "HEAD").strip()
    theirs = gitops.git(root, "rev-parse", tracking).strip()
    if local == theirs:
        return "up-to-date"
    if _ancestor(root, theirs, local):
        return "ahead"
    if _ancestor(root, local, theirs):
        gitops.git(root, "merge", "-q", "--ff-only", tracking)
        return "fast-forward"
    try:
        gitops.git(root, "rebase", "-q", tracking)
    except gitops.GitError:
        files = gitops.git(root, "diff", "--name-only", "--diff-filter=U").split()
        gitops.git(root, "rebase", "--abort", check=False)
        raise ConflictError(files or ["(unknown)"]) from None
    return "rebased"


def _ancestor(root: Path, a: str, b: str) -> bool:
    return (
        subprocess.run(
            ["git", "-C", str(root), "merge-base", "--is-ancestor", a, b], check=False
        ).returncode
        == 0
    )


def backup(
    root: Path, remote_names: list[str] | None = None, _between: Callable[[], None] | None = None
) -> dict[str, str]:
    """Pull, check the ledger, push, then confirm the remote equals our head. Returns remote -> result."""
    configured = remotes(root)
    if not configured:
        raise SyncError("no encrypted remote; add one with `fin remote add`")
    results = {}
    with gitops.write_lock(root):
        for name in remote_names or list(configured):
            results[name] = _backup_one(root, name, _between)
    return results


def _backup_one(root: Path, name: str, between: Callable[[], None] | None) -> str:
    for _ in range(MAX_TRIES):
        pull(root, name)
        led = ledger.load(config.main_ledger(root))
        if led.errors:
            raise SyncError(
                "ledger has errors; not pushing:\n"
                + "\n".join(ledger.format_errors(led.errors)[:5])
            )
        if between:
            between()
        head = gitops.git(root, "rev-parse", "HEAD").strip()
        seen = remote_head(root, name)
        tracking = gitops.git(root, "rev-parse", f"{name}/{branch(root)}", check=False).strip()
        if seen == head:
            return "up-to-date"
        if seen is not None and seen != tracking:
            continue  # someone pushed since we fetched: merge theirs first, do not overwrite
        gitops.git(root, "push", "-q", "--force", name, branch(root))
        if remote_head(root, name) == head:
            return "pushed"
    raise SyncError(f"remote {name} kept changing; gave up after {MAX_TRIES} tries")


def status(root: Path) -> dict[str, dict]:
    """Per remote: commits we have that it lacks (ahead) and the reverse (behind), as of the last fetch."""
    out = {}
    for name in remotes(root):
        tracking = f"{name}/{branch(root)}"
        have = (
            subprocess.run(
                ["git", "-C", str(root), "rev-parse", "--verify", "-q", tracking],
                capture_output=True,
                check=False,
            ).returncode
            == 0
        )
        if not have:
            total = int(gitops.git(root, "rev-list", "--count", "HEAD").strip())
            out[name] = {"ahead": total, "behind": 0, "fetched": False}
            continue
        ahead = int(gitops.git(root, "rev-list", "--count", f"{tracking}..HEAD").strip())
        behind = int(gitops.git(root, "rev-list", "--count", f"HEAD..{tracking}").strip())
        out[name] = {"ahead": ahead, "behind": behind, "fetched": True}
    return out


def doctor(root: Path) -> list[tuple[str, bool, str]]:
    """Health checks: (name, ok, detail)."""
    checks: list[tuple[str, bool, str]] = []
    try:
        gpg = find_gpg()
        checks.append(("gpg", True, gpg))
    except SyncError as e:
        return [("gpg", False, str(e))]
    configured = remotes(root)
    checks.append(
        (
            "encrypted remote",
            bool(configured),
            ", ".join(configured) or "none configured (fin remote add)",
        )
    )
    for name in configured:
        key = gitops.git(
            root, "config", "--get", f"remote.{name}.gcrypt-participants", check=False
        ).strip()
        secret = (
            subprocess.run(
                [gpg, "--list-secret-keys", key], capture_output=True, check=False
            ).returncode
            == 0
            if key
            else False
        )
        checks.append((f"{name}: secret key present", secret, key or "no participants configured"))
        try:
            head = remote_head(root, name)
            checks.append(
                (
                    f"{name}: remote readable",
                    True,
                    head[:10] if head else "empty (nothing pushed yet)",
                )
            )
        except gitops.GitError as e:
            checks.append((f"{name}: remote readable", False, str(e)[:120]))
    size_mb = (
        sum(f.stat().st_size for f in root.rglob("*") if f.is_file() and ".git" not in f.parts)
        / 1e6
    )
    checks.append(("repo size", size_mb < 1000, f"{size_mb:.1f} MB (warn above 1000)"))
    checks.append(
        ("working tree clean", not gitops.is_dirty(root), "commit pending changes before backup")
    )
    return checks
