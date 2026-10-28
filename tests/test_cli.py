from typer.testing import CliRunner

from fin.cli import app

runner = CliRunner()

TXN = """2026-01-01 open Assets:Bank CHF
2026-01-01 open Expenses:Food CHF

2026-01-05 * "Coop" "groceries"
  Assets:Bank   -42.50 CHF
  Expenses:Food  42.50 CHF
"""


def test_fresh_ledger_checks_clean(data):
    r = runner.invoke(app, ["check"])
    assert r.exit_code == 0, r.output
    assert "OK" in r.output


def test_query_returns_rows(data):
    (data / "ledger" / "manual" / "2026.beancount").write_text(TXN)
    r = runner.invoke(
        app, ["query", "SELECT account, sum(position) GROUP BY account ORDER BY account"]
    )
    assert r.exit_code == 0, r.output
    assert "Expenses:Food" in r.output and "42.50 CHF" in r.output


UNBALANCED = TXN.replace("  Expenses:Food  42.50 CHF", "  Expenses:Food  40.00 CHF")


def test_check_fails_on_unbalanced(data):
    (data / "ledger" / "manual" / "2026.beancount").write_text(UNBALANCED)
    assert runner.invoke(app, ["check"]).exit_code == 1


def test_commit_blocked_by_errors_and_ok_when_clean(data):
    f = data / "ledger" / "manual" / "2026.beancount"
    f.write_text(UNBALANCED)
    assert runner.invoke(app, ["commit"]).exit_code == 1
    f.write_text(TXN)
    r = runner.invoke(app, ["commit", "-m", "add txn"])
    assert r.exit_code == 0 and "Committed" in r.output
    assert "clean" in runner.invoke(app, ["status"]).output
