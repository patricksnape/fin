import sys
import types
from datetime import date
from decimal import Decimal as D

import pytest
from typer.testing import CliRunner

from fin import audit, config, importers, ledger
from fin.cli import app
from fin.importers import base
from fin.importers.base import ImportFailed, RawTxn

ACCOUNTS = """
2020-01-01 open Assets:Acme:Cash CHF
2020-01-01 open Assets:Transfers:Pending
2020-01-01 open Expenses:Uncategorized
2020-01-01 open Expenses:Food
2020-01-01 open Equity:Opening-Balances
"""


def _row(ref, day, amount, **kw) -> RawTxn:
    return RawTxn(
        date.fromisoformat(day), "Assets:Acme:Cash", D(amount), "CHF", "ACME", "x", ref, **kw
    )


ROWS = [_row("acme:1", "2026-03-01", "-10.00"), _row("acme:2", "2026-03-02", "-20.00")]


@pytest.fixture
def acme(data, monkeypatch):
    """A made-up source whose raw files parse to `rows`, written into a fresh ledger."""
    mod = types.ModuleType("fin_test_acme")
    mod.SOURCE = "acme"
    mod.rows = list(ROWS)
    mod.source_files = lambda root: [root / "sources" / "acme" / "a.json"]
    mod.parse_files = lambda files: list(mod.rows)
    monkeypatch.setitem(sys.modules, "fin_test_acme", mod)
    monkeypatch.setitem(importers.SOURCES, "acme", "fin_test_acme")
    (data / "ledger" / "accounts.beancount").write_text(ACCOUNTS)
    base.write_new(data, "acme", mod.rows)
    return mod


def _find(findings, area, severity=None):
    return [f for f in findings if f.area == area and (severity is None or f.severity == severity)]


def _edit(root, old, new):
    path = root / "ledger" / "imported" / "acme" / "2026" / "2026-03.beancount"
    text = path.read_text()
    assert old in text
    path.write_text(text.replace(old, new))


def test_a_ledger_that_matches_its_source_has_no_findings(data, acme):
    out = audit.run(data, "acme")
    assert [f.severity for f in out if f.severity != audit.INFO] == []
    assert "2 rows" in _find(out, "diff acme")[0].text


def test_a_deleted_entry_is_missing_from_the_ledger(data, acme):
    path = data / "ledger" / "imported" / "acme" / "2026" / "2026-03.beancount"
    blocks = path.read_text().split("\n\n")
    path.write_text("\n\n".join(b for b in blocks if "acme:2" not in b))
    (f,) = _find(audit.run(data, "acme"), "diff acme", audit.ERROR)
    assert "1 in the source, not in the ledger" in f.text and "acme:2" in f.detail[0]


def test_an_edited_amount_or_date_on_the_sources_account_is_reported(data, acme):
    _edit(data, "-20.00 CHF", "-21.00 CHF")
    _edit(data, "2026-03-01 *", "2026-03-05 *")
    (f,) = _find(audit.run(data, "acme"), "diff acme", audit.ERROR)
    assert "2 differ from the source" in f.text


def test_the_other_leg_may_be_rewritten_freely(data, acme):
    _edit(data, "Expenses:Uncategorized  10.00 CHF", "Expenses:Food  10.00 CHF")
    assert not _find(audit.run(data, "acme"), "diff acme", audit.ERROR)


def test_a_fee_beside_the_amount_on_the_same_account_is_fine(data, acme):
    _edit(
        data,
        "  Expenses:Uncategorized  10.00 CHF",
        "  Assets:Acme:Cash  -1.00 CHF\n  Expenses:Food  11.00 CHF",
    )
    assert not _find(audit.run(data, "acme"), "diff acme", audit.ERROR)


def test_a_ledger_ref_the_source_no_longer_has_is_a_warning_not_an_error(data, acme):
    acme.rows = acme.rows[:1]
    (f,) = _find(audit.run(data, "acme"), "diff acme", audit.WARN)
    assert "no longer in the source" in f.text and "acme:2" in f.detail[0]
    assert not _find(audit.run(data, "acme"), "diff acme", audit.ERROR)


def test_the_same_ref_twice_in_the_ledger_is_an_error(data, acme):
    path = data / "ledger" / "imported" / "acme" / "2026" / "2026-03.beancount"
    block = path.read_text().split("\n\n")[0]
    path.write_text(path.read_text() + "\n\n" + block + "\n")
    findings = _find(audit.run(data, "acme"), "diff acme", audit.ERROR)
    assert any("duplicated in the ledger" in f.text for f in findings)


def test_a_parser_that_refuses_is_an_error_and_other_sources_still_run(data, acme):
    def refuse(files):
        raise ImportFailed("a newer layout needs a new parser")

    acme.parse_files = refuse
    out = audit.run(data)
    assert "refuses its own files" in _find(out, "diff acme", audit.ERROR)[0].text
    assert _find(out, "diff swisscard")  # the rest of the sources were audited


def test_stale_statement_balances_are_a_warning(data, acme):
    acme.derived = lambda root: "2026-03-03 balance Assets:Acme:Cash  -30.00 CHF\n"
    folder = data / "ledger" / "imported" / "acme" / "derived"
    assert _find(audit.run(data, "acme"), "derived acme", audit.ERROR)  # nothing written yet
    folder.mkdir(parents=True)
    (folder / "balances.beancount").write_text("2026-03-03 balance Assets:Acme:Cash  -25.00 CHF\n")
    assert _find(audit.run(data, "acme"), "derived acme", audit.WARN)
    (folder / "balances.beancount").write_text(acme.derived(data))
    assert _find(audit.run(data, "acme"), "derived acme", audit.INFO)


def test_an_account_without_assertions_is_flagged_unless_the_rules_name_it(data, acme):
    led = ledger.load(config.main_ledger(data))
    (f,) = audit.check_coverage(led, set())
    assert f.severity == audit.WARN and "Assets:Acme:Cash" in f.detail[0]
    (f,) = audit.check_coverage(led, {"Assets:Acme:Cash"})
    assert f.severity == audit.INFO
    (data / "ledger" / "manual" / "b.beancount").write_text(
        "2026-03-03 balance Assets:Acme:Cash  -30.00 CHF\n"
    )
    (f,) = audit.check_coverage(ledger.load(config.main_ledger(data)), set())
    assert "current assertion" in f.text


def test_the_clearing_account_reports_open_legs_and_not_linked_pairs(data, acme):
    path = data / "ledger" / "manual" / "c.beancount"
    path.write_text(
        '2026-03-04 * "x" "out"\n  ref: "m:1"\n  Assets:Acme:Cash  -5.00 CHF\n  Assets:Transfers:Pending  5.00 CHF\n\n'
        '2026-03-04 * "x" "in"\n  ref: "m:2"\n  transfer: "m:1"\n  Assets:Acme:Cash  5.00 CHF\n  Assets:Transfers:Pending  -5.00 CHF\n'
    )
    (f,) = audit.check_clearing(ledger.load(config.main_ledger(data)))
    assert f.severity == audit.INFO
    path.write_text(path.read_text().split("\n\n")[0] + "\n")
    (f,) = audit.check_clearing(ledger.load(config.main_ledger(data)))
    assert f.severity == audit.WARN and "CHF: net 5.00" in f.text and "m:1" in f.detail[0]


def test_estimates_tags_gap_entries_and_named_exceptions_are_totalled(data, acme):
    (data / "ledger" / "manual" / "e.beancount").write_text(
        '2026-04-01 * "x" "guess" #etrade-unconfirmed\n  basis: "estimate: market price"\n'
        "  Assets:Acme:Cash  100.00 CHF\n  Expenses:Food  -100.00 CHF\n\n"
        '2026-05-01 * "x" "gap"\n  Assets:Acme:Cash  50.00 CHF\n  Equity:Opening-Balances  -50.00 CHF\n\n'
        '2026-06-01 * "x" "no detail"\n  Assets:Acme:Cash  -7.00 CHF\n  Expenses:Food  7.00 CHF\n'
    )
    led = ledger.load(config.main_ledger(data))
    out = audit.check_estimates(led, [{"hole": "Expenses:Food", "note": "source gone"}])
    text = " | ".join(f.text for f in out)
    assert "estimated value" in text and "#etrade-unconfirmed" in text
    assert "gap entry" in text and "Expenses:Food (source gone)" in text


def test_the_command_exits_1_on_an_error_and_writes_no_narration_to_the_inbox(data, acme):
    run = CliRunner().invoke(app, ["audit", "--source", "acme"])
    assert run.exit_code == 0, run.output
    _edit(data, "-20.00 CHF", "-21.00 CHF")
    run = CliRunner().invoke(app, ["audit", "--source", "acme"])
    assert run.exit_code == 1 and "ERROR diff acme" in run.output
    report = (data / "inbox" / "audit.md").read_text()
    assert "acme:2" in report and "ACME" not in report
