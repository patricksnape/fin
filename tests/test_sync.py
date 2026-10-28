import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from fin import scaffold, sync
from fin.gitops import commit_all, git, is_dirty

GPG = shutil.which("gpg") or next((p for p in sync.GPG_CANDIDATES if Path(p).exists()), None)
pytestmark = [pytest.mark.slow, pytest.mark.skipif(GPG is None, reason="gpg not installed")]


@pytest.fixture
def fpr(monkeypatch):
    """A throwaway key in an isolated GnuPG home (short path: the agent socket has a length limit)."""
    home = Path(tempfile.mkdtemp(prefix="fg", dir="/tmp"))
    home.chmod(0o700)
    monkeypatch.setenv("GNUPGHOME", str(home))
    run = lambda *a: subprocess.run(
        [GPG, "--batch", "--pinentry-mode", "loopback", "--passphrase", "", *a],
        check=True,
        capture_output=True,
        text=True,
    )
    run("--quick-generate-key", "Test <test@example.invalid>", "ed25519", "cert,sign", "never")
    fingerprint = next(
        line.split(":")[9]
        for line in run("--list-keys", "--with-colons").stdout.splitlines()
        if line.startswith("fpr")
    )
    run("--quick-add-key", fingerprint, "cv25519", "encr", "never")
    yield fingerprint
    subprocess.run(
        [str(Path(GPG).resolve().parent / "gpgconf"), "--kill", "gpg-agent"],
        check=False,
        capture_output=True,
    )
    shutil.rmtree(home, ignore_errors=True)


@pytest.fixture
def machines(fpr, tmp_path):
    remote = tmp_path / "remote"
    remote.mkdir()
    a = tmp_path / "a"
    scaffold.init_data(a)
    sync.add_remote(a, "origin", str(remote), fpr, gpg=GPG)
    assert sync.backup(a) == {"origin": "pushed"}
    b = tmp_path / "b"
    subprocess.run(
        [
            "git",
            "-c",
            f"gpg.program={GPG}",
            "clone",
            "-q",
            "-b",
            "main",
            f"gcrypt::{remote}",
            str(b),
        ],
        check=True,
        capture_output=True,
    )
    sync.configure_remote(b, "origin", fpr, gpg=GPG)
    return a, b, remote


def txn(ref, day, amount="1.00", payee="P"):
    return f'2026-01-{day:02d} * "{payee}" "n"\n  ref: "{ref}"\n  Assets:Transfers:Pending  {amount} CHF\n  Equity:Opening-Balances\n'


FILE = "ledger/manual/2026.beancount"


def write(root, text, rel=FILE, append=False):
    path = root / rel
    path.write_text((path.read_text() + "\n" if append and path.exists() else "") + text)
    commit_all(root, "update")


def head(root):
    return git(root, "rev-parse", "HEAD").strip()


def test_backup_pushes_encrypted_and_verifies(machines):
    a, _, remote = machines
    write(a, txn("m:1", 1, payee="SECRET-PAYEE-XYZ"))
    assert sync.backup(a) == {"origin": "pushed"}
    assert sync.remote_head(a, "origin") == head(a)
    assert sync.backup(a) == {"origin": "up-to-date"}
    stored = b"".join(p.read_bytes() for p in remote.rglob("*") if p.is_file())
    assert b"SECRET-PAYEE-XYZ" not in stored and b"ledger" not in stored


def test_second_machine_fast_forwards(machines):
    a, b, _ = machines
    write(a, txn("m:1", 1))
    sync.backup(a)
    assert sync.pull(b, "origin") == "fast-forward"
    assert 'ref: "m:1"' in (b / FILE).read_text()
    assert sync.status(b)["origin"] == {"ahead": 0, "behind": 0, "fetched": True}


def test_diverged_machines_union_their_transactions(machines):
    a, b, _ = machines
    write(a, txn("a:1", 1))
    write(b, txn("b:1", 2))
    sync.backup(a)
    assert sync.backup(b) == {
        "origin": "pushed"
    }  # b rebases onto a; merge driver unions the new file
    merged = (b / FILE).read_text()
    assert 'ref: "a:1"' in merged and 'ref: "b:1"' in merged
    assert sync.pull(a, "origin") == "fast-forward"
    assert head(a) == head(b) == sync.remote_head(a, "origin")


def test_diverged_edits_in_existing_file_merge_cleanly(machines):
    a, b, _ = machines
    write(a, txn("m:1", 1))
    sync.backup(a)
    sync.pull(b, "origin")
    write(a, txn("a:2", 3), append=True)
    write(b, txn("b:2", 2), append=True)
    sync.backup(a)
    sync.backup(b)
    sync.pull(a, "origin")
    for root in (a, b):
        text = (root / FILE).read_text()
        assert all(f'ref: "{r}"' in text for r in ("m:1", "a:2", "b:2"))


def test_conflicting_edit_aborts_and_leaves_tree_untouched(machines):
    a, b, _ = machines
    write(a, txn("m:1", 1, "1.00"))
    sync.backup(a)
    sync.pull(b, "origin")
    write(a, txn("m:1", 1, "8.00"))
    write(b, txn("m:1", 1, "9.00"))
    sync.backup(a)
    before = head(b)
    with pytest.raises(sync.ConflictError) as e:
        sync.backup(b)
    assert FILE in e.value.files
    assert head(b) == before and not is_dirty(b)
    assert "9.00" in (b / FILE).read_text()
    assert sync.remote_head(b, "origin") == head(a)  # b's version was not pushed


def test_uncommitted_changes_are_refused(machines):
    a, _, _ = machines
    (a / FILE).write_text(txn("m:1", 1))
    with pytest.raises(sync.SyncError, match="uncommitted"):
        sync.backup(a)


def test_ledger_errors_block_the_push(machines):
    a, _, _ = machines
    remote_before = sync.remote_head(a, "origin")
    write(
        a,
        txn("m:1", 1).replace(
            "  Equity:Opening-Balances\n", "  Equity:Opening-Balances  5.00 CHF\n"
        ),
    )
    with pytest.raises(sync.SyncError, match="ledger has errors"):
        sync.backup(a)
    assert sync.remote_head(a, "origin") == remote_before


def test_a_push_that_raced_in_is_merged_not_overwritten(machines):
    a, b, _ = machines
    write(a, txn("a:1", 1))
    write(b, txn("b:1", 2))
    fired = []

    def other_machine_pushes_meanwhile():
        if not fired:
            fired.append(1)
            sync.backup(b)

    assert sync.backup(a, _between=other_machine_pushes_meanwhile) == {"origin": "pushed"}
    sync.pull(b, "origin")
    for root in (a, b):
        text = (root / FILE).read_text()
        assert 'ref: "a:1"' in text and 'ref: "b:1"' in text
    assert head(a) == head(b) == sync.remote_head(a, "origin")


def test_doctor_reports_a_healthy_setup(machines):
    a, _, _ = machines
    checks = {name: ok for name, ok, _ in sync.doctor(a)}
    assert all(checks.values()), checks
    assert "origin: secret key present" in checks and "origin: remote readable" in checks


def test_status_counts_unpushed_commits(machines):
    a, _, _ = machines
    write(a, txn("m:1", 1))
    assert sync.status(a)["origin"]["ahead"] == 1
