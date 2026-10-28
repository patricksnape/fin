import hashlib
from decimal import Decimal

import pytest

from fin import demo, ledger


@pytest.fixture(scope="module")
def demo_root(tmp_path_factory):
    root = tmp_path_factory.mktemp("demo") / "repo"
    demo.create_demo(root)
    return root


def _digest(root):
    h = hashlib.sha256()
    for p in sorted((root / "ledger").rglob("*.beancount")):
        h.update(p.read_bytes())
    return h.hexdigest()


def _txns(root):
    from beancount.core import data

    led = ledger.load(root / "ledger" / "main.beancount")
    return led, [e for e in led.entries if isinstance(e, data.Transaction)]


def test_demo_ledger_is_valid(demo_root):
    led, _ = _txns(demo_root)
    assert not led.errors, ledger.format_errors(led.errors)


def test_demo_is_deterministic(demo_root, tmp_path):
    other = tmp_path / "again"
    demo.create_demo(other)
    assert _digest(demo_root) == _digest(other)


def test_refs_are_unique_and_present(demo_root):
    _, txns = _txns(demo_root)
    refs = [t.meta.get("ref") for t in txns if "_placeholder" not in t.meta["filename"]]
    assert all(refs)
    assert len(refs) == len(set(refs))


def test_layout_matches_importer_shape(demo_root):
    assert (demo_root / "ledger/imported/ubs/2025/2025-03.beancount").exists()
    assert (demo_root / "ledger/opening/opening.beancount").exists()


def _balance(demo_root, account, ccy="CHF"):
    _, txns = _txns(demo_root)
    total = Decimal(0)
    for t in txns:
        for p in t.postings:
            if p.account == account and p.units.currency == ccy:
                total += p.units.number
    return total


def test_espp_receivable_clears_to_zero(demo_root):
    assert _balance(demo_root, "Assets:Receivable:ESPP-Contributions") == 0


def test_share_comp_clearing_nets_to_zero(demo_root):
    assert _balance(demo_root, "Equity:Clearing:ShareComp") == 0


def test_ubs_never_negative(demo_root):
    _, txns = _txns(demo_root)
    bal = Decimal(0)
    for t in sorted(txns, key=lambda t: t.date):
        for p in t.postings:
            if p.account == "Assets:UBS:Checking":
                bal += p.units.number
        assert bal > 0, t.date


def test_scenarios_present(demo_root):
    _, txns = _txns(demo_root)
    narrations = {t.narration for t in txns}
    assert {"Car purchase", "Laptop", "Refund", "RSU vest", "ESPP purchase"} <= narrations
    assert sum(1 for t in txns if "review" in (t.tags or ())) == 10
