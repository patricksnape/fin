"""Write lock and git helpers for the data repo."""

import fcntl
import subprocess
from contextlib import contextmanager
from pathlib import Path

LOCK_NAME = ".fin.lock"


class GitError(Exception):
    pass


def git(root: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), *args], check=False, capture_output=True, text=True
    )
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


@contextmanager
def write_lock(root: Path):
    """Exclusive lock covering write -> bean-check -> commit."""
    with (root / LOCK_NAME).open("w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def is_dirty(root: Path) -> bool:
    return bool(git(root, "status", "--porcelain").strip())


def commit_all(root: Path, message: str) -> bool:
    """Commit every change. Returns False when there was nothing to commit."""
    if not is_dirty(root):
        return False
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", message)
    return True


def revert_paths(root: Path, paths: list[str]) -> None:
    git(root, "checkout", "--", *paths)
